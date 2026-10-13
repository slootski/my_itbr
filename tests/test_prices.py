"""Tests for data.prices — yfinance column normalization, no network."""
from __future__ import annotations

import threading

import pandas as pd
import pytest
from yfinance.exceptions import YFPricesMissingError

from data import prices

# conftest stubs prices.fetch_expense_ratio for every test; keep the real one.
_fetch_expense_ratio = prices.fetch_expense_ratio
# The same for the other yfinance entry points conftest stubs.
_fetch_ohlc = prices.fetch_ohlc
_fetch_quote = prices.fetch_quote
_fetch_quotes = prices.fetch_quotes
_resolve_symbol = prices.resolve_symbol


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
    df = _fetch_ohlc("AAPL")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.iloc[0]["close"] == 1.5


def test_fetch_ohlc_empty_symbol():
    assert _fetch_ohlc("").empty


def test_fetch_ohlc_download_error_returns_empty(monkeypatch):
    def _boom(symbol):
        raise RuntimeError("boom")

    monkeypatch.setattr(prices.yf, "Ticker", _boom)
    assert _fetch_ohlc("AAPL").empty


def test_fetch_ohlc_empty_history_returns_empty(monkeypatch):
    monkeypatch.setattr(prices.yf, "Ticker", lambda s: _FakeTicker(pd.DataFrame()))
    assert _fetch_ohlc("AAPL").empty


class _FakeInfoTicker:
    def __init__(self, info):
        self.info = info


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"quoteType": "ETF", "netExpenseRatio": 0.4}, 0.4),
        ({"quoteType": "ETF", "netExpenseRatio": 0}, 0.0),          # a free fund is not "no data"
        ({"quoteType": "EQUITY"}, None),                             # a stock has no ratio
        ({"netExpenseRatio": None}, None),
        ({"netExpenseRatio": float("nan")}, None),
        ({"netExpenseRatio": "n/a"}, None),
        ({"netExpenseRatio": -1.0}, None),
    ],
)
def test_fetch_expense_ratio(monkeypatch, info, expected):
    monkeypatch.setattr(prices.yf, "Ticker", lambda s: _FakeInfoTicker(info))
    assert _fetch_expense_ratio(symbol="QTUM") == expected


def test_fetch_expense_ratio_empty_symbol():
    assert _fetch_expense_ratio(symbol="") is None


def test_fetch_expense_ratio_lookup_error_returns_none(monkeypatch):
    def _boom(symbol):
        raise RuntimeError("boom")

    monkeypatch.setattr(prices.yf, "Ticker", _boom)
    assert _fetch_expense_ratio(symbol="QTUM") is None


# ---- delayed intraday quotes -------------------------------------------------

class _FakeQuoteTicker:
    def __init__(self, fast_info):
        self._fast_info = fast_info

    @property
    def fast_info(self):
        if isinstance(self._fast_info, Exception):
            raise self._fast_info
        return self._fast_info


class _RaisingInfo(dict):
    """Like yfinance for an unknown ticker: the lookup fails on key access."""
    def __getitem__(self, key):
        raise KeyError("currentTradingPeriod")


_GOOD = {"last_price": 152.4, "regular_market_previous_close": 150.9, "currency": "USD"}


@pytest.mark.parametrize(("fast_info", "expected"), [
    (_GOOD, {"price": 152.4, "previous_close": 150.9, "currency": "USD"}),
    ({**_GOOD, "regular_market_previous_close": None},
     {"price": 152.4, "previous_close": None, "currency": "USD"}),
    ({**_GOOD, "regular_market_previous_close": 0.0},
     {"price": 152.4, "previous_close": None, "currency": "USD"}),
    ({"last_price": 152.4}, {"price": 152.4, "previous_close": None, "currency": None}),
    ({**_GOOD, "last_price": float("nan")}, None),
    ({**_GOOD, "last_price": 0.0}, None),
    ({**_GOOD, "last_price": -3.0}, None),
    ({**_GOOD, "last_price": None}, None),
    (_RaisingInfo(), None),
    (RuntimeError("offline"), None),
])
def test_fetch_quote(monkeypatch, fast_info, expected):
    monkeypatch.setattr(prices.yf, "Ticker", lambda s: _FakeQuoteTicker(fast_info))
    assert _fetch_quote(symbol="QTUM") == expected


def test_fetch_quote_empty_symbol():
    assert _fetch_quote(symbol="") is None


def test_fetch_quotes_keeps_input_order_and_survives_one_failure(monkeypatch):
    symbols = ("AAA", "BBB", "CCC")
    done = {symbol: threading.Event() for symbol in symbols}

    def fake_quote(*, symbol):
        # Finish in reverse order: each waits for the symbol after it.
        after = symbols[symbols.index(symbol) + 1:][:1]
        if after:
            assert done[after[0]].wait(timeout=5)
        done[symbol].set()
        return None if symbol == "BBB" else {"price": 1.0, "previous_close": None, "currency": "USD"}

    monkeypatch.setattr(prices, "fetch_quote", fake_quote)
    found = _fetch_quotes(symbols=symbols, workers=len(symbols))
    assert list(found) == list(symbols)
    assert found["BBB"] is None and found["AAA"]["price"] == 1.0
    assert _fetch_quotes(symbols=(), workers=6) == {}


class _FakeHistoryTicker:
    calls: list[str] = []

    def __init__(self, symbol, outcomes):
        self._symbol, self._outcomes = symbol, outcomes

    def history(self, **kwargs):
        _FakeHistoryTicker.calls.append(self._symbol)
        outcome = self._outcomes[self._symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resolve(monkeypatch, text, outcomes):
    _FakeHistoryTicker.calls = []
    monkeypatch.setattr(prices.yf, "Ticker", lambda s: _FakeHistoryTicker(s, outcomes))
    return _resolve_symbol(text=text), list(_FakeHistoryTicker.calls)


def test_resolve_symbol(monkeypatch):
    missing = YFPricesMissingError("X", "no data")
    assert _resolve(monkeypatch, " qtum ", {"QTUM": _daily_frame()}) == (
        {"status": "ok", "symbol": "QTUM"}, ["QTUM"])
    assert _resolve(monkeypatch, "QTUMM", {"QTUMM": missing}) == (
        {"status": "unknown", "symbol": None}, ["QTUMM"])
    assert _resolve(monkeypatch, "QTUMM", {"QTUMM": pd.DataFrame()})[0]["status"] == "unknown"
    # Class shares: yfinance spells BRK.B as BRK-B, found on the second try.
    assert _resolve(monkeypatch, "brk.b", {"BRK.B": missing, "BRK-B": _daily_frame()}) == (
        {"status": "ok", "symbol": "BRK-B"}, ["BRK.B", "BRK-B"])
    # A non-US ticker with a dot is known as typed and is not rewritten.
    assert _resolve(monkeypatch, "7203.T", {"7203.T": _daily_frame()}) == (
        {"status": "ok", "symbol": "7203.T"}, ["7203.T"])
    # Offline is not "unknown", and the second spelling is not tried.
    assert _resolve(monkeypatch, "brk.b", {"BRK.B": ConnectionError("no network")}) == (
        {"status": "unreachable", "symbol": None}, ["BRK.B"])


@pytest.mark.parametrize("text", ["", "QT$UM", "A" * 21])
def test_resolve_symbol_refuses_a_bad_ticker_without_asking_yfinance(monkeypatch, text):
    from data import watchlist

    def boom(symbol):
        raise AssertionError("yfinance must not be asked")

    monkeypatch.setattr(prices.yf, "Ticker", boom)
    with pytest.raises(watchlist.WatchlistError):
        _resolve_symbol(text=text)
