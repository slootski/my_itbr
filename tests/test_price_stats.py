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
    s = price_stats.summarize(ohlc, "AAPL", live_price=None, live_previous_close=None)
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
    s = price_stats.summarize(ohlc, "AAPL", year_ohlc=year, live_price=None, live_previous_close=None)
    assert s["high_52w"] == pytest.approx(999.0)
    assert s["period_high"] == pytest.approx(30.0)   # selected period is unaffected


def test_summarize_position_and_live_pnl(ohlc, positions):
    s = price_stats.summarize(ohlc, "AAPL", positions=positions, live_price=None, live_previous_close=None)
    assert s["avg_cost"] == pytest.approx(20.0)
    assert s["quantity"] == pytest.approx(100.0)
    # Live P&L uses the yfinance close (29), not the stale Flex mark (25).
    assert s["unrealized_live"] == pytest.approx(900.0)
    assert s["market_value_live"] == pytest.approx(2900.0)
    assert s["vs_cost_pct"] == pytest.approx(45.0)


def test_summarize_yield_and_total_return(ohlc, positions, cash_tx):
    s = price_stats.summarize(ohlc, "AAPL", positions=positions, cash_tx=cash_tx, live_price=None, live_previous_close=None)
    # TTM 100 over 100 shares = 1.00/share on a 29.00 price.
    assert s["yield_pct"] == pytest.approx(1.0 / 29.0 * 100.0)
    assert s["yield_on_cost_pct"] == pytest.approx(1.0 / 20.0 * 100.0)
    # 900 price gain + 165 net dividends.
    assert s["total_return"] == pytest.approx(1065.0)
    assert s["total_return_pct"] == pytest.approx(1065.0 / 2000.0 * 100.0)


def test_summarize_empty_ohlc_is_all_none(positions):
    s = price_stats.summarize(pd.DataFrame(), "AAPL", positions=positions, live_price=None, live_previous_close=None)
    assert s["current"] is None and s["period_mean"] is None
    # Position data still comes through, falling back to the Flex figures.
    assert s["avg_cost"] == pytest.approx(20.0)
    assert s["unrealized_live"] == pytest.approx(500.0)


def test_summarize_symbol_not_held(ohlc, positions):
    s = price_stats.summarize(ohlc, "TSLA", positions=positions, live_price=None, live_previous_close=None)
    assert s["avg_cost"] is None and s["quantity"] is None
    assert s["current"] == pytest.approx(29.0)


def test_moving_average_shorter_than_window(ohlc):
    assert price_stats.moving_average(ohlc, 50) is None
    ma = price_stats.moving_average(ohlc, 5)
    assert ma is not None and ma.iloc[-1] == pytest.approx(27.0)


@pytest.mark.parametrize(
    ("ratio", "value", "expected"),
    [
        (0.4, 270.67, pytest.approx(1.08268)),
        (0.4, -270.67, pytest.approx(1.08268)),   # a short still pays the fee on its size
        (0.0, 270.67, 0.0),
        (None, 270.67, None),
        (0.4, None, None),
    ],
)
def test_expense_cost_per_year(ratio, value, expected):
    assert price_stats.expense_cost_per_year(expense_ratio_pct=ratio, market_value=value) == expected


def _closes(days: list[str], close: float) -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime(days), "close": [close] * len(days)})


def _trades(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["QTUM"] * len(rows),
        "trade_date": [dt.date.fromisoformat(day) for day, _, _ in rows],
        "side": [side for _, side, _ in rows],
        "quantity": [quantity for _, _, quantity in rows],
    })


_TEN_DAYS = [f"2026-03-{day:02d}" for day in range(1, 11)]
# 3.65% a year on 10 shares at 100.00 is exactly 0.10 a day.
_RATIO = 3.65


def _paid(*, closes, trades, quantity, ratio, today):
    return price_stats.expense_paid(
        closes=closes, trades=trades, symbol="QTUM", quantity=quantity,
        expense_ratio_pct=ratio, today=dt.date.fromisoformat(today),
    )


def test_expense_paid_counts_every_day_held():
    out = _paid(closes=_closes(_TEN_DAYS, 100.0), trades=_trades([("2026-03-01", "BUY", 10.0)]),
                quantity=10.0, ratio=_RATIO, today="2026-03-10")
    assert out == {"amount": pytest.approx(1.0), "since": dt.date(2026, 3, 1)}


def test_expense_paid_follows_the_share_count_through_buys_and_sells():
    trades = _trades([("2026-03-01", "BUY", 10.0), ("2026-03-06", "BUY", 10.0),
                      ("2026-03-09", "SELL", -5.0)])   # IBKR signs sells negative
    out = _paid(closes=_closes(_TEN_DAYS, 100.0), trades=trades,
                quantity=15.0, ratio=_RATIO, today="2026-03-10")
    # 5 days x 10 shares, 3 days x 20, 2 days x 15 = 140 share-days at 0.01 each.
    assert out["amount"] == pytest.approx(1.4)


def test_expense_paid_reads_an_unsigned_sell_as_a_sell():
    trades = _trades([("2026-03-01", "BUY", 10.0), ("2026-03-06", "SELL", 4.0)])
    out = _paid(closes=_closes(_TEN_DAYS, 100.0), trades=trades,
                quantity=6.0, ratio=_RATIO, today="2026-03-10")
    assert out["amount"] == pytest.approx(0.5 + 5 * 0.06)


def test_expense_paid_accrues_over_days_without_a_close():
    closes = _closes(["2026-03-06", "2026-03-09"], 100.0)   # Friday, Monday
    out = _paid(closes=closes, trades=_trades([("2026-03-06", "BUY", 10.0)]),
                quantity=10.0, ratio=_RATIO, today="2026-03-10")
    assert out["amount"] == pytest.approx(0.5)              # Fri..Tue, 5 days


def test_expense_paid_starts_at_the_first_close_when_prices_start_late():
    out = _paid(closes=_closes(_TEN_DAYS[5:], 100.0), trades=_trades([("2026-03-01", "BUY", 10.0)]),
                quantity=10.0, ratio=_RATIO, today="2026-03-10")
    assert out == {"amount": pytest.approx(0.5), "since": dt.date(2026, 3, 6)}


def test_expense_paid_keeps_shares_older_than_the_known_trades():
    # 10 shares today, only a 4-share buy on record: 6 were held all along.
    out = _paid(closes=_closes(_TEN_DAYS, 100.0), trades=_trades([("2026-03-06", "BUY", 4.0)]),
                quantity=10.0, ratio=_RATIO, today="2026-03-10")
    assert out == {"amount": pytest.approx(0.5), "since": dt.date(2026, 3, 6)}


def test_expense_paid_handles_timezone_aware_closes():
    closes = _closes(_TEN_DAYS, 100.0)
    closes["date"] = closes["date"].dt.tz_localize("America/New_York")
    out = _paid(closes=closes, trades=_trades([("2026-03-01", "BUY", 10.0)]),
                quantity=10.0, ratio=_RATIO, today="2026-03-10")
    assert out["amount"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("closes", "trades", "quantity", "ratio"),
    [
        (_closes(_TEN_DAYS, 100.0), _trades([("2026-03-01", "BUY", 10.0)]), 10.0, None),   # a stock
        (_closes(_TEN_DAYS, 100.0), _trades([("2026-03-01", "BUY", 10.0)]), None, _RATIO),
        (_closes(_TEN_DAYS, 100.0), _trades([]), 10.0, _RATIO),
        (_closes(_TEN_DAYS, 100.0), None, 10.0, _RATIO),
        (pd.DataFrame(), _trades([("2026-03-01", "BUY", 10.0)]), 10.0, _RATIO),
        (None, _trades([("2026-03-01", "BUY", 10.0)]), 10.0, _RATIO),
        (_closes(_TEN_DAYS, 100.0), _trades([("2026-04-01", "BUY", 10.0)]), 10.0, _RATIO),  # after today
    ],
)
def test_expense_paid_is_none_without_its_inputs(closes, trades, quantity, ratio):
    out = _paid(closes=closes, trades=trades, quantity=quantity, ratio=ratio, today="2026-03-10")
    assert out == {"amount": None, "since": None}


def test_first_trade_date():
    trades = _trades([("2026-03-06", "BUY", 4.0), ("2026-03-01", "BUY", 1.0)])
    assert price_stats.first_trade_date(trades=trades, symbol="QTUM") == dt.date(2026, 3, 1)
    assert price_stats.first_trade_date(trades=trades, symbol="AAPL") is None


def test_summarize_with_a_live_price_follows_the_quote(ohlc, positions, cash_tx):
    closing = price_stats.summarize(ohlc, "AAPL", positions=positions, cash_tx=cash_tx,
                                    live_price=None, live_previous_close=None)
    live = price_stats.summarize(ohlc, "AAPL", positions=positions, cash_tx=cash_tx,
                                 live_price=40.0, live_previous_close=32.0)
    assert closing["current"] == 29.0                       # the last close of the fixture
    assert live["current"] == 40.0 and live["prev_close"] == 32.0
    assert live["day_change"] == pytest.approx(8.0) and live["day_change_pct"] == pytest.approx(25.0)
    assert live["unrealized_live"] == pytest.approx((40.0 - 20.0) * 100)
    assert live["market_value_live"] == pytest.approx(4000.0)
    assert live["vs_cost_pct"] == pytest.approx(100.0)
    assert live["off_period_high_pct"] == pytest.approx((40.0 - 30.0) / 30.0 * 100)
    assert live["yield_pct"] == pytest.approx(closing["yield_pct"] * 29.0 / 40.0)
    assert live["total_return"] == pytest.approx(closing["total_return"] + (40.0 - 29.0) * 100)
    # What the candles say does not move with the quote.
    for key in ("period_mean", "period_high", "period_low", "period_return_pct", "high_52w"):
        assert live[key] == closing[key], key


def test_summarize_with_a_live_price_and_no_previous_close(ohlc, positions):
    live = price_stats.summarize(ohlc, "AAPL", positions=positions,
                                 live_price=40.0, live_previous_close=None)
    assert live["current"] == 40.0 and live["day_change"] is None and live["day_change_pct"] is None
