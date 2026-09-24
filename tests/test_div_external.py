"""Tests for data.div_external — yfinance upcoming-dividend lookup, no network."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from data import div_external


class _FakeTicker:
    def __init__(self, calendar=None, dividends=None, boom: str | None = None):
        self._calendar = calendar
        self._dividends = dividends
        self._boom = boom  # "calendar" | "dividends" -> raise on that property

    @property
    def calendar(self):
        if self._boom == "calendar":
            raise RuntimeError("no calendar")
        return self._calendar

    @property
    def dividends(self):
        if self._boom == "dividends":
            raise RuntimeError("no dividends")
        return self._dividends


def _patch(monkeypatch, **kwargs):
    monkeypatch.setattr(div_external.yf, "Ticker", lambda symbol: _FakeTicker(**kwargs))


def _history(*dates: str) -> pd.Series:
    idx = pd.to_datetime(list(dates))
    return pd.Series([0.5] * len(dates), index=idx)


def test_fetch_upcoming_calendar_and_history(monkeypatch):
    _patch(
        monkeypatch,
        calendar={
            "Ex-Dividend Date": dt.date(2026, 6, 29),
            "Dividend Date": dt.date(2026, 7, 6),
        },
        dividends=_history("2025-06-30", "2025-12-30"),
    )
    out = div_external.fetch_upcoming("COPX")
    assert out["ex_date"] == dt.date(2026, 6, 29)
    assert out["pay_date"] == dt.date(2026, 7, 6)
    assert out["history_months"] == ["2025-06", "2025-12"]


def test_fetch_upcoming_calendar_list_values(monkeypatch):
    # Older yfinance returns DataFrame-ish calendars; values may be sequences.
    _patch(monkeypatch, calendar={"Ex-Dividend Date": [dt.date(2026, 6, 29)]})
    out = div_external.fetch_upcoming("COPX")
    assert out["ex_date"] == dt.date(2026, 6, 29)
    assert out["pay_date"] is None
    assert out["history_months"] == []


def test_fetch_upcoming_history_dedupes_months(monkeypatch):
    _patch(monkeypatch, dividends=_history("2025-06-01", "2025-06-15", "2025-12-30"))
    out = div_external.fetch_upcoming("COPX")
    assert out["history_months"] == ["2025-06", "2025-12"]


def test_fetch_upcoming_empty_symbol():
    out = div_external.fetch_upcoming("")
    assert out == {"ex_date": None, "pay_date": None, "history_months": []}


def test_fetch_upcoming_ticker_failure_returns_empty(monkeypatch):
    def _boom(symbol):
        raise RuntimeError("network down")

    monkeypatch.setattr(div_external.yf, "Ticker", _boom)
    out = div_external.fetch_upcoming("COPX")
    assert out == {"ex_date": None, "pay_date": None, "history_months": []}


def test_fetch_upcoming_partial_failures_keep_other_source(monkeypatch):
    # Calendar lookup blows up; history must still come through (and vice versa).
    _patch(monkeypatch, boom="calendar", dividends=_history("2025-06-30", "2025-12-30"))
    out = div_external.fetch_upcoming("COPX")
    assert out["ex_date"] is None
    assert out["history_months"] == ["2025-06", "2025-12"]

    _patch(monkeypatch, boom="dividends", calendar={"Ex-Dividend Date": dt.date(2026, 6, 29)})
    out = div_external.fetch_upcoming("COPX")
    assert out["ex_date"] == dt.date(2026, 6, 29)
    assert out["history_months"] == []


def test_fetch_upcoming_none_and_empty_sources(monkeypatch):
    _patch(monkeypatch, calendar=None, dividends=pd.Series(dtype=float))
    out = div_external.fetch_upcoming("COPX")
    assert out == {"ex_date": None, "pay_date": None, "history_months": []}
