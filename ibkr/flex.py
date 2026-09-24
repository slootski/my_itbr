"""Flex Web Service client.

The Flex Web Service is a two-step async protocol:

1. ``SendRequest`` with token + query id  -> reference code.
2. ``GetStatement`` with token + reference code -> statement XML (polling while
   IBKR generates it).

The ``ibflex`` library already implements both steps with polling/backoff, so we
delegate to ``ibflex.client.download`` and only wrap it to surface clear errors
and a friendly message for the common failure modes (bad token, expired token,
query not found).

ibflex only retries ``requests.exceptions.Timeout`` internally, so a transient
DNS failure or dropped connection mid-poll aborts the whole download. We add an
outer retry for those transport-level errors.
"""
from __future__ import annotations

import logging
import time

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


# IBKR Flex response codes that mean the credential itself is dead: the only
# fix is a new token from Account Management. Kept here so the UI can branch on
# them without parsing message text.
TOKEN_EXPIRED = "1012"
TOKEN_INVALID = "1015"
TOKEN_ERROR_CODES = (TOKEN_EXPIRED, TOKEN_INVALID)


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


def download_statement(token: str, query_id: str) -> bytes:
    """Run the full Flex request/poll cycle and return raw statement XML bytes.

    Raises:
        FlexError: with a human-readable message on any failure.
    """
    if not token or not query_id:
        raise FlexError("Flex token and query id are required (set them in .env).")

    # Log the query id only — NEVER the token. Polling/backoff (SendRequest ->
    # GetStatement retries) happens inside ibflex.client.download and is not
    # separately observable here; we log the overall outcome and duration.
    logger.info("download_statement start (query_id=%s)", query_id)
    started = time.monotonic()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            payload = flex_client.download(token, query_id)
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
