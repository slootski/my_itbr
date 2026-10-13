"""Flex Web Service client.

The Flex Web Service is a two-step async protocol:

1. ``SendRequest`` with token + query id  -> reference code.
2. ``GetStatement`` with token + reference code -> statement XML (polling while
   IBKR generates it).

We run both steps ourselves with ``ibflex``'s parsing helpers, and wrap them
to surface clear errors and a friendly message for the common failure modes
(bad token, expired token, query not found).

IBKR serves the Flex Web Service from two hosts: ``gdcdyn`` (the default
GetStatement host, and what IBKR usually hands back in ``Url``) and ``ndcdyn``
(the newer host). Each HTTP call fails over to the other host on a transport
error, so one host's DNS or network outage does not fail the download. An outer
retry then covers the case where both hosts are unreachable.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from ibflex import client as flex_client
from ibflex.client import ResponseCodeError

logger = logging.getLogger("myitbr.flex")

# Outer retry for transport failures. Each attempt restarts the 2-step cycle
# (SendRequest + GetStatement polling), so keep the count low: IBKR throttles a
# token that issues too many SendRequests (Flex error 1018).
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0

# Transport-level failures worth retrying. ConnectionError covers DNS resolution
# failures (NameResolutionError), refused connections and resets; Timeout is the
# case ibflex gives up on after exhausting its own inner retries.
RETRYABLE = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

# Hosts that serve the same Flex Web Service paths. A request that fails on one
# with a RETRYABLE error is re-sent to the others, in this order.
FLEX_HOSTS = ("gdcdyn.interactivebrokers.com", "ndcdyn.interactivebrokers.com")

# Per-HTTP-request timeout; statements can be large, so leave room to stream.
REQUEST_TIMEOUT_SECONDS = 30


# IBKR Flex response codes that mean the credential itself is dead: the only
# fix is a new token from Account Management. Kept here so the UI can branch on
# them without parsing message text.
TOKEN_EXPIRED = "1012"
TOKEN_INVALID = "1015"
TOKEN_ERROR_CODES = (TOKEN_EXPIRED, TOKEN_INVALID)

# The longest window a Flex statement can cover. Anything older ages out of IBKR
# for good, which is why data/history.py archives it locally.
MAX_WINDOW_DAYS = 365

# GetStatement polls per request/poll cycle.
MAX_POLLS = 10


class FlexError(Exception):
    """Raised when a Flex download fails for any reason.

    ``code`` is the IBKR Flex response code (e.g. "1012") when the server
    answered with an error status, else ``None``.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _scrub(text: object, token: str) -> str:
    """Redact the Flex token from text bound for the logs.

    ibflex puts the token in the request URL, so network exception text can
    carry it. The token must never reach any log sink at any level.
    """
    try:
        s = str(text)
        return s.replace(token, "***TOKEN***") if token else s
    except Exception:  # noqa: BLE001 — logging helper must never raise
        return "<unprintable error>"


def _host_candidates(url: str) -> list[str]:
    """Return ``url`` first, then the same path on every other Flex host."""
    parts = urlsplit(url)
    return [url] + [
        urlunsplit(parts._replace(netloc=host))
        for host in FLEX_HOSTS if host != parts.netloc
    ]


def _get_with_failover(*, url: str, params: dict[str, str], token: str) -> requests.Response:
    """GET ``url``; on a transport error, re-send the request to the other Flex hosts.

    Raises the last transport error when every host fails, so the outer retry in
    ``download_statement`` can take over.
    """
    candidates = _host_candidates(url)
    for index, candidate in enumerate(candidates):
        try:
            return requests.get(
                candidate, params=params, headers={"user-agent": "Java"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except RETRYABLE as exc:
            if index == len(candidates) - 1:
                raise
            logger.warning(
                "Flex host %s unreachable (%s); trying %s",
                urlsplit(candidate).netloc, _scrub(exc, token),
                urlsplit(candidates[index + 1]).netloc,
            )
    raise AssertionError("unreachable: candidates is never empty")


def _fetch(*, token: str, query_id: str, date_range: tuple[dt.date, dt.date] | None) -> bytes:
    """One request/poll cycle: the query's own period, or ``date_range`` when given.

    With ``date_range`` the request carries ``fd``/``td``. IBKR answers 1003
    ("Statement is not available") when ``td`` has no data yet (a weekend or
    today), so callers must end the range on a date IBKR has already served.
    """
    params = {"v": "3", "t": token, "q": query_id}
    if date_range is not None:
        from_date, to_date = date_range
        params["fd"] = from_date.strftime("%Y%m%d")
        params["td"] = to_date.strftime("%Y%m%d")
    response = _get_with_failover(url=flex_client.REQUEST_URL, params=params, token=token)
    access = flex_client.parse_stmt_response(response)
    if isinstance(access, flex_client.StatementError):
        raise ResponseCodeError(access.ErrorCode, access.ErrorMessage)

    delay: bool | int = 0
    for _ in range(MAX_POLLS):
        time.sleep(delay)
        statement = _get_with_failover(
            url=access.Url or flex_client.STMT_URL,
            params={"v": "3", "t": token, "q": access.ReferenceCode},
            token=token,
        )
        delay = flex_client.check_statement_response(statement)
        if delay is True:
            return statement.content
    raise flex_client.StatementGenerationTimeout(
        f"Statement not ready after {MAX_POLLS} polls"
    )


def download_statement(
    token: str, query_id: str, date_range: tuple[dt.date, dt.date] | None
) -> bytes:
    """Run the full Flex request/poll cycle and return raw statement XML bytes.

    ``date_range`` is ``None`` for the period saved in the Flex Query, or an
    inclusive ``(from, to)`` pair that overrides it (at most MAX_WINDOW_DAYS).

    Raises:
        FlexError: with a human-readable message on any failure.
    """
    if not token or not query_id:
        raise FlexError("Flex token and query id are required (set them in .env).")

    # Log the query id only — NEVER the token. We log the overall outcome and
    # duration; host failovers log their own warning.
    logger.info(
        "download_statement start (query_id=%s, range=%s)",
        query_id, "query period" if date_range is None
        else f"{date_range[0].isoformat()}..{date_range[1].isoformat()}",
    )
    started = time.monotonic()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            payload = _fetch(token=token, query_id=query_id, date_range=date_range)
            break
        except ResponseCodeError as exc:
            # IBKR returned an error status (e.g. 1003 invalid token, 1018 invalid
            # query, token expired). ResponseCodeError carries code + message.
            # The server answered, so retrying would only burn requests.
            code = getattr(exc, "code", "?")
            message = getattr(exc, "message", str(exc))
            logger.error(
                "download_statement failed after %.1fs: IBKR Flex error %s: %s",
                time.monotonic() - started, code, _scrub(message, token),
            )
            raise FlexError(f"IBKR Flex error {code}: {message}", code=str(code)) from exc
        except RETRYABLE as exc:
            if attempt == MAX_ATTEMPTS:
                logger.error(
                    "download_statement failed after %.1fs and %d attempts: %s",
                    time.monotonic() - started, attempt, _scrub(exc, token),
                )
                raise FlexError(
                    f"Failed to download Flex statement after {attempt} attempts: {exc}"
                ) from exc
            delay = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "download_statement attempt %d/%d failed (%s); retrying in %.0fs",
                attempt, MAX_ATTEMPTS, _scrub(exc, token), delay,
            )
            time.sleep(delay)
        except Exception as exc:  # malformed responses, generation timeout, bugs
            logger.error(
                "download_statement failed after %.1fs: %s",
                time.monotonic() - started, _scrub(exc, token),
            )
            raise FlexError(f"Failed to download Flex statement: {exc}") from exc

    logger.info(
        "download_statement ok: %d bytes in %.1fs (query_id=%s)",
        len(payload), time.monotonic() - started, query_id,
    )
    return payload
