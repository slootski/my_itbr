"""Historical OHLC price data via yfinance.

Flex carries no price data, so the Prices tab pulls candlestick history here.
Returns a tidy DataFrame with a ``date`` column plus open/high/low/close/volume.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf


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
