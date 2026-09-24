"""External (yfinance) dividend data for tickers IBKR Flex knows nothing about.

Flex only reports dividends that were paid into the account or officially
declared (open accruals). A held ticker bought before its first payout — or
whose next distribution is not yet declared (e.g. COPX semi-annual) — renders
as an empty "no dividend data yet" row even when the next dividend is public
knowledge. This module fills that gap from yfinance:

  - ``Ticker.calendar``  -> concrete upcoming ex-dividend / payment dates
                            (stocks mostly; often empty for ETFs).
  - ``Ticker.dividends`` -> historical per-share distributions keyed by
                            ex-date, used to infer a payout cadence when no
                            concrete dates exist.

Amounts are intentionally NOT returned: the calendar's red projected cells
never show a number (unknown until declared), and yfinance per-share history
times shares held would be an estimate presented as fact.
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import yfinance as yf

logger = logging.getLogger("myitbr.div_external")

# yfinance .calendar key spellings seen across versions.
_EX_KEYS = ("Ex-Dividend Date", "exDividendDate")
_PAY_KEYS = ("Dividend Date", "dividendDate")


def _as_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else ts.date()


def _calendar_date(calendar, keys: tuple[str, ...]) -> dt.date | None:
    """Pull one date out of Ticker.calendar (dict in modern yfinance,
    DataFrame in older versions); first value when a list/Series."""
    if calendar is None:
        return None
    for key in keys:
        try:
            value = calendar.get(key) if hasattr(calendar, "get") else calendar[key]
        except (KeyError, TypeError, IndexError):
            continue
        if isinstance(value, (list, tuple, pd.Series)):
            value = value[0] if len(value) else None
        d = _as_date(value)
        if d is not None:
            return d
    return None


def fetch_upcoming(symbol: str) -> dict:
    """Upcoming-dividend info for one symbol, best effort.

    Returns ``{"ex_date": date|None, "pay_date": date|None,
    "history_months": [YYYY-MM, ...]}`` — concrete dates from the yfinance
    calendar when published, plus the chronological ex-date months of past
    distributions for cadence inference. Empty dict values (never raises) when
    the symbol is unknown or the lookup fails, so the UI degrades to the plain
    "no data" row.
    """
    out: dict = {"ex_date": None, "pay_date": None, "history_months": []}
    if not symbol:
        return out

    try:
        ticker = yf.Ticker(symbol)
    except Exception:
        logger.warning("fetch_upcoming(%s): Ticker construction failed", symbol, exc_info=True)
        return out

    try:
        calendar = ticker.calendar
        out["ex_date"] = _calendar_date(calendar, _EX_KEYS)
        out["pay_date"] = _calendar_date(calendar, _PAY_KEYS)
    except Exception:
        logger.info("fetch_upcoming(%s): calendar lookup failed", symbol, exc_info=True)

    try:
        history = ticker.dividends
        if history is not None and len(history):
            months = {f"{ts.year:04d}-{ts.month:02d}" for ts in pd.to_datetime(history.index)}
            out["history_months"] = sorted(months)
    except Exception:
        logger.info("fetch_upcoming(%s): dividend history lookup failed", symbol, exc_info=True)

    return out
