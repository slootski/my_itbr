"""Real declared-dividend data from Polygon.io for tickers Flex knows nothing about.

Where yfinance (``data.div_external``) only yields ex/pay dates — never an amount —
Polygon's reference dividends endpoint returns the actual declared distribution:
ex-dividend / pay / declaration dates plus the per-share ``cash_amount``. That lets
the calendar render a real ``declared`` (yellow) cell with a number instead of a
bare red "Declare" for held tickers that never paid into the account yet (COPX-style).

Only DECLARED dividends are available here — Polygon lists announced distributions,
not pre-declaration forecasts. Requires ``POLYGON_API_KEY``; with no key the caller
falls back to the yfinance dates-only backfill.

The free tier rate-limits to ~5 requests/min, and there is one lookup per held
ticker with no Flex accrual, so HTTP 429 is expected. ``_fetch_raw`` retries 429s
(honouring ``Retry-After``) and, crucially, reports ``"unavailable"`` distinctly
from a genuine "no upcoming dividend" so the caller (``fetch_declared_many``) never
caches a rate-limit miss as a real empty. Results are cached for the day in
``data.div_cache`` so the cost is paid roughly once per day.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd
import requests

from data import div_cache

logger = logging.getLogger("myitbr.div_provider")

_URL = "https://api.polygon.io/v3/reference/dividends"
_TIMEOUT = 10
_MAX_RETRIES = 2          # extra attempts after the first, on HTTP 429
_RETRY_DEFAULT = 15       # seconds to wait when 429 carries no Retry-After
_RETRY_CAP = 20           # never sleep longer than this per retry


def _as_date(value) -> dt.date | None:
    """Coerce a Polygon date string (``YYYY-MM-DD``) to a date; None on failure."""
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else ts.date()


def _pick(results: list, today: dt.date) -> dict:
    """Build the declared record for the soonest upcoming dividend with an amount."""
    best: dict | None = None
    best_ex: dt.date | None = None
    for rec in results:
        ex = _as_date(rec.get("ex_dividend_date"))
        amount = rec.get("cash_amount")
        if ex is None or ex < today or amount is None:
            continue
        if best_ex is None or ex < best_ex:
            best, best_ex = rec, ex
    if best is None:
        return {}
    return {
        "ex_date": best_ex,
        "pay_date": _as_date(best.get("pay_date")),
        "decl_date": _as_date(best.get("declaration_date")),
        "amount": float(best["cash_amount"]),
        "currency": best.get("currency") or "",
    }


def _fetch_raw(symbol: str, api_key: str, today: dt.date) -> tuple[str, dict]:
    """Look one symbol up, distinguishing "fetched" from "couldn't fetch".

    Returns ``("ok", record)`` where ``record`` is the declared dividend or ``{}``
    when none is upcoming, or ``("unavailable", {})`` when the API could not be
    reached (429 after retries, network/JSON error). The distinction matters: an
    ``"unavailable"`` result must NOT be cached as a definitive empty.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                _URL,
                params={"ticker": symbol, "limit": 10, "apiKey": api_key},
                timeout=_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — network errors are "unavailable", not empty
            logger.info("fetch(%s): request failed", symbol, exc_info=True)
            return "unavailable", {}

        if resp.status_code == 429:
            if attempt >= _MAX_RETRIES:
                logger.info("fetch(%s): rate-limited, giving up for now", symbol)
                return "unavailable", {}
            wait = _RETRY_DEFAULT
            try:
                wait = int(resp.headers.get("Retry-After", _RETRY_DEFAULT))
            except (TypeError, ValueError):
                pass
            time.sleep(min(max(wait, 1), _RETRY_CAP))
            continue

        try:
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except Exception:  # noqa: BLE001 — other HTTP/JSON errors → unavailable
            logger.info("fetch(%s): bad response", symbol, exc_info=True)
            return "unavailable", {}
        return "ok", _pick(results, today)

    return "unavailable", {}


def fetch_declared(symbol: str, api_key: str, today: dt.date | None = None) -> dict:
    """Next upcoming DECLARED distribution for one symbol, best effort.

    Returns ``{"ex_date", "pay_date", "decl_date", "amount", "currency"}`` for the
    record with the soonest ex-dividend date at/after ``today`` that carries a
    per-share ``cash_amount``. Returns ``{}`` when there is no key, no upcoming
    declared dividend, or the lookup fails (never raises). Stateless — for the
    cached, rate-limit-resilient batch path use :func:`fetch_declared_many`.
    """
    if not symbol or not api_key:
        return {}
    _, data = _fetch_raw(symbol, api_key, today or dt.date.today())
    return data


def fetch_declared_many(
    symbols, api_key: str, db_path, today: dt.date | None = None
) -> dict[str, dict]:
    """Declared dividends for many symbols, served from a daily on-disk cache.

    For each symbol: a cache entry stamped today is reused as-is; otherwise a live
    lookup runs. An ``"ok"`` result (record or genuine empty) is written to the
    cache and used; an ``"unavailable"`` result falls back to any stale cache entry
    if present, else the symbol is omitted and resolves on a later refresh (partial
    progress persists). Cached/absent symbols cost no API call, so the per-day
    rate-limit hit is paid once. Returns ``{symbol: record}`` (empties omitted).
    """
    if not api_key:
        return {}
    today = today or dt.date.today()
    out: dict[str, dict] = {}
    for symbol in symbols:
        cached = div_cache.get(db_path, symbol)
        if cached is not None and cached[1] == today.isoformat():
            record = cached[0]
            if record:
                out[symbol] = record
            continue

        status, data = _fetch_raw(symbol, api_key, today)
        if status == "ok":
            div_cache.put(db_path, symbol, data, today)
            if data:
                out[symbol] = data
        elif cached is not None and cached[0]:
            out[symbol] = cached[0]  # stale but better than nothing
    return out
