"""Tests for data.prices — yfinance column normalization, no network."""
from __future__ import annotations

import pandas as pd

from data import prices


class _FakeTicker:
    def __init__(self, frame):
        self._frame = frame

    def history(self, **kwargs):
        return self._frame


def _daily_frame():
    idx = pd.to_datetime(["2024-01-01", "2024-01-02"])
    idx.name = "Date"
    return pd.DataFrame({
        "Open": [1.0, 1.5], "High": [2.0, 2.5], "Low": [0.5, 1.0],
        "Close": [1.5, 2.0], "Volume": [100, 200],
    }, index=idx)


def test_fetch_ohlc_normalizes_columns(monkeypatch):
    monkeypatch.setattr(prices.yf, "Ticker", lambda s: _FakeTicker(_daily_frame()))
    df = prices.fetch_ohlc("AAPL")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.iloc[0]["close"] == 1.5


def test_fetch_ohlc_empty_symbol():
    assert prices.fetch_ohlc("").empty


def test_fetch_ohlc_download_error_returns_empty(monkeypatch):
    def _boom(symbol):
        raise RuntimeError("boom")

    monkeypatch.setattr(prices.yf, "Ticker", _boom)
    assert prices.fetch_ohlc("AAPL").empty


def test_fetch_ohlc_empty_history_returns_empty(monkeypatch):
    monkeypatch.setattr(prices.yf, "Ticker", lambda s: _FakeTicker(pd.DataFrame()))
    assert prices.fetch_ohlc("AAPL").empty
