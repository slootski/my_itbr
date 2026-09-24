"""Tests for data.price_stats — per-symbol price/position/dividend statistics."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from data import price_stats


@pytest.fixture
def ohlc():
    """20 daily bars rising 10.0 -> 29.0, so ranges and returns are hand-checkable."""
    dates = pd.to_datetime([dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(20)])
    close = [10.0 + i for i in range(20)]
    return pd.DataFrame({
        "date": dates,
        "open": [c - 0.5 for c in close],
        "high": [c + 1.0 for c in close],
        "low": [c - 1.0 for c in close],
        "close": close,
        "volume": [1000 + 10 * i for i in range(20)],
    })


@pytest.fixture
def positions():
    return pd.DataFrame({
        "symbol": ["AAPL", "MSFT"],
        "quantity": [100.0, 50.0],
        "cost_price": [20.0, 300.0],
        "cost_basis": [2000.0, 15000.0],
        "mark_price": [25.0, 310.0],
        "market_value": [2500.0, 15500.0],
        "unrealized_pnl": [500.0, 500.0],
        "currency": ["USD", "USD"],
    })


@pytest.fixture
def cash_tx():
    recent = dt.date.today() - dt.timedelta(days=30)
    older = dt.date.today() - dt.timedelta(days=400)
    return pd.DataFrame({
        "type": ["DIVIDEND", "WHTAX", "DIVIDEND", "DIVIDEND"],
        "symbol": ["AAPL", "AAPL", "AAPL", "MSFT"],
        "amount": [100.0, -15.0, 80.0, 40.0],
        "settle_date": [recent, recent, older, recent],
        "datetime": [None, None, None, None],
        "report_date": [None, None, None, None],
        "ex_date": [recent, recent, older, recent],
    })


# ---- dividends_received ---------------------------------------------------

def test_dividends_received_sums_gross_and_net(cash_tx):
    div = price_stats.dividends_received(cash_tx, "AAPL")
    assert div["gross"] == pytest.approx(180.0)      # 100 + 80
    assert div["net"] == pytest.approx(165.0)        # minus 15 withholding
    assert div["count"] == 2
    assert div["ttm_gross"] == pytest.approx(100.0)  # only the recent payment


def test_dividends_received_last_payment(cash_tx):
    div = price_stats.dividends_received(cash_tx, "AAPL")
    assert div["last_amount"] == pytest.approx(100.0)
    assert div["last_date"] == dt.date.today() - dt.timedelta(days=30)


def test_dividends_received_unknown_symbol(cash_tx):
    div = price_stats.dividends_received(cash_tx, "TSLA")
    assert div["gross"] is None and div["count"] == 0


def test_dividends_received_empty_frame():
    assert price_stats.dividends_received(pd.DataFrame(), "AAPL")["count"] == 0


# ---- price statistics -----------------------------------------------------

def test_summarize_price_fields(ohlc):
    s = price_stats.summarize(ohlc, "AAPL")
    assert s["current"] == pytest.approx(29.0)
    assert s["prev_close"] == pytest.approx(28.0)
    assert s["day_change"] == pytest.approx(1.0)
    assert s["period_high"] == pytest.approx(30.0)   # last close + 1
    assert s["period_low"] == pytest.approx(9.0)     # first close - 1
    assert s["period_mean"] == pytest.approx(19.5)
    assert s["period_return_pct"] == pytest.approx(190.0)
    assert s["atr"] is not None and s["volatility_pct"] is not None


def test_summarize_52w_uses_year_frame(ohlc):
    year = ohlc.copy()
    year.loc[0, "high"] = 999.0
    s = price_stats.summarize(ohlc, "AAPL", year_ohlc=year)
    assert s["high_52w"] == pytest.approx(999.0)
    assert s["period_high"] == pytest.approx(30.0)   # selected period is unaffected


def test_summarize_position_and_live_pnl(ohlc, positions):
    s = price_stats.summarize(ohlc, "AAPL", positions=positions)
    assert s["avg_cost"] == pytest.approx(20.0)
    assert s["quantity"] == pytest.approx(100.0)
    # Live P&L uses the yfinance close (29), not the stale Flex mark (25).
    assert s["unrealized_live"] == pytest.approx(900.0)
    assert s["market_value_live"] == pytest.approx(2900.0)
    assert s["vs_cost_pct"] == pytest.approx(45.0)


def test_summarize_yield_and_total_return(ohlc, positions, cash_tx):
    s = price_stats.summarize(ohlc, "AAPL", positions=positions, cash_tx=cash_tx)
    # TTM 100 over 100 shares = 1.00/share on a 29.00 price.
    assert s["yield_pct"] == pytest.approx(1.0 / 29.0 * 100.0)
    assert s["yield_on_cost_pct"] == pytest.approx(1.0 / 20.0 * 100.0)
    # 900 price gain + 165 net dividends.
    assert s["total_return"] == pytest.approx(1065.0)
    assert s["total_return_pct"] == pytest.approx(1065.0 / 2000.0 * 100.0)


def test_summarize_empty_ohlc_is_all_none(positions):
    s = price_stats.summarize(pd.DataFrame(), "AAPL", positions=positions)
    assert s["current"] is None and s["period_mean"] is None
    # Position data still comes through, falling back to the Flex figures.
    assert s["avg_cost"] == pytest.approx(20.0)
    assert s["unrealized_live"] == pytest.approx(500.0)


def test_summarize_symbol_not_held(ohlc, positions):
    s = price_stats.summarize(ohlc, "TSLA", positions=positions)
    assert s["avg_cost"] is None and s["quantity"] is None
    assert s["current"] == pytest.approx(29.0)


def test_moving_average_shorter_than_window(ohlc):
    assert price_stats.moving_average(ohlc, 50) is None
    ma = price_stats.moving_average(ohlc, 5)
    assert ma is not None and ma.iloc[-1] == pytest.approx(27.0)
