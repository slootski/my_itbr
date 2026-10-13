"""Historical OHLC price data via yfinance.

Flex carries no price data, so the Prices tab pulls candlestick history here.
Returns a tidy DataFrame with a ``date`` column plus open/high/low/close/volume.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFPricesMissingError

from data import watchlist


def fetch_ohlc(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Fetch OHLC history for one symbol.

    Returns an empty DataFrame (never raises) when the symbol is unknown or the
    download fails, so the UI can show a friendly "no data" message.
    """
    if not symbol:
        return pd.DataFrame()

    try:
        raw = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    except Exception:
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.reset_index()
    # The datetime column is "Date" for daily, "Datetime" for intraday.
    date_col = "Date" if "Date" in df.columns else ("Datetime" if "Datetime" in df.columns else df.columns[0])
    df = df.rename(columns={
        date_col: "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    return df[keep]


def fetch_expense_ratio(*, symbol: str) -> float | None:
    """Yearly net expense ratio of a fund, in percent (0.4 means 0.40% a year).

    Returns None (never raises) for a stock, an unknown symbol or a failed
    lookup, so the UI can show "no data" instead of an error.
    """
    if not symbol:
        return None
    try:
        raw = yf.Ticker(symbol).info["netExpenseRatio"]
        ratio = float(raw)
    except Exception:
        return None
    return None if pd.isna(ratio) or ratio < 0 else ratio


def _positive(value) -> float | None:
    """A usable price: a finite number above zero, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and not pd.isna(number) and number != float("inf") else None


def fetch_quote(*, symbol: str) -> dict | None:
    """Latest (delayed intraday) price of one symbol, with its previous regular close.

    Returns ``{"price", "previous_close", "currency"}``, or None (never raises)
    when the lookup fails or gives no usable price. ``previous_close`` is None
    when it is missing or not positive.
    """
    if not symbol:
        return None
    try:
        info = yf.Ticker(symbol).fast_info
        price = _positive(info["last_price"])
        if price is None:
            return None
        try:
            previous = _positive(info["regular_market_previous_close"])
            currency = info["currency"]
        except Exception:
            previous, currency = None, None
    except Exception:
        return None
    return {"price": price, "previous_close": previous,
            "currency": currency if isinstance(currency, str) else None}


def fetch_quotes(*, symbols: tuple[str, ...], workers: int) -> dict[str, dict | None]:
    """``fetch_quote`` for every symbol, in parallel. Keys are in input order."""
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = list(pool.map(lambda symbol: fetch_quote(symbol=symbol), symbols))
    return dict(zip(symbols, found))


def resolve_symbol(*, text: str) -> dict:
    """Check a typed ticker against yfinance.

    Returns ``{"status": "ok" | "unknown" | "unreachable", "symbol": str | None}``.
    The ticker is tried as typed and then with "." as "-", so "BRK.B" resolves to
    "BRK-B" while "7203.T" stays as typed. "unknown" means yfinance answered and
    has no such ticker; "unreachable" means it could not be asked.
    """
    symbol = watchlist.clean_symbol(text=text)
    candidates = [symbol] if "." not in symbol else [symbol, symbol.replace(".", "-")]
    for candidate in candidates:
        try:
            frame = yf.Ticker(candidate).history(period="5d", raise_errors=True)
        except YFPricesMissingError:
            continue
        except Exception:
            return {"status": "unreachable", "symbol": None}
        if frame is not None and not frame.empty:
            return {"status": "ok", "symbol": candidate}
    return {"status": "unknown", "symbol": None}
