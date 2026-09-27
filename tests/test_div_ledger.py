"""Tests for data.div_ledger — the single definition of dividend income."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from data import capital, div_ledger, dividends, price_stats
from ibkr import parser


def _row(kind, symbol, amount, settle, ex, base):
    return {"type": kind, "symbol": symbol, "amount": amount, "amount_base": base,
            "settle_date": settle, "ex_date": ex, "datetime": None, "report_date": None}


@pytest.fixture
def cash_tx():
    return pd.DataFrame([
        # ABC: gross 100, tax -15 posted a month later against the same ex-date.
        _row("DIVIDEND", "ABC", 100.0, dt.date(2026, 3, 15), dt.date(2026, 3, 1), 100.0),
        _row("WHTAX", "ABC", -15.0, dt.date(2026, 4, 10), dt.date(2026, 3, 1), -15.0),
        # ZERO: IBKR withheld nothing — after tax equals gross.
        _row("PAYMENTINLIEU", "ZERO", 20.0, dt.date(2026, 4, 5), dt.date(2026, 3, 25), 20.0),
        # EUR: paid in EUR, summed in base (1.1 rate).
        _row("DIVIDEND", "EUR", 10.0, dt.date(2026, 5, 2), dt.date(2026, 4, 20), 11.0),
        # Orphan tax refund with no matching dividend keeps its own event.
        _row("WHTAX", "ABC", 2.0, dt.date(2026, 6, 1), dt.date(2025, 12, 1), 2.0),
        # Not dividend income.
        _row("BROKERINTRCVD", None, 3.0, dt.date(2026, 3, 3), None, 3.0),
        _row("DEPOSITWITHDRAW", None, 500.0, dt.date(2026, 1, 1), None, 500.0),
    ])


def test_events_join_tax_to_its_payment(cash_tx):
    ledger, unpriced = div_ledger.events(cash_tx=cash_tx)
    assert unpriced == 0
    abc = ledger[(ledger["symbol"] == "ABC") & (ledger["payments"] > 0)].iloc[0]
    assert abc["pay_date"] == dt.date(2026, 3, 15)
    assert (abc["gross"], abc["tax"], abc["net"]) == (100.0, -15.0, 85.0)


def test_events_after_tax_is_real_cash_when_nothing_withheld(cash_tx):
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    zero = ledger[ledger["symbol"] == "ZERO"].iloc[0]
    assert zero["net"] == zero["gross"] == 20.0


def test_events_use_base_currency(cash_tx):
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    assert ledger.loc[ledger["symbol"] == "EUR", "gross"].iloc[0] == 11.0


def test_orphan_tax_is_kept(cash_tx):
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    orphan = ledger[(ledger["symbol"] == "ABC") & (ledger["payments"] == 0)]
    assert list(orphan["net"]) == [2.0]


def test_row_without_base_amount_is_unpriced_not_guessed(cash_tx):
    cash_tx.loc[cash_tx["symbol"] == "EUR", "amount_base"] = None
    ledger, unpriced = div_ledger.events(cash_tx=cash_tx)
    assert unpriced == 1
    assert "EUR" not in set(ledger["symbol"])


def test_totals(cash_tx):
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    assert div_ledger.total(ledger=ledger, net=False, start=None, end=None) == 131.0
    assert div_ledger.total(ledger=ledger, net=True, start=None, end=None) == 118.0
    assert div_ledger.total(ledger=ledger, net=True, start=dt.date(2026, 4, 1),
                            end=dt.date(2026, 5, 31)) == 31.0


def test_every_screen_reports_the_same_total(cash_tx):
    """The point of the ledger: calendar, Net Worth and per-symbol stats agree."""
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    expected_net = div_ledger.total(ledger=ledger, net=True, start=None, end=None)
    expected_gross = div_ledger.total(ledger=ledger, net=False, start=None, end=None)

    positions = pd.DataFrame({"symbol": ["ABC", "ZERO"], "quantity": [10.0, 5.0]})
    today = dt.date(2026, 9, 1)
    cal_net = dividends.build_calendar(positions, cash_tx, pd.DataFrame(), today=today,
                                       net=True, lookback_months=None)
    cal_gross = dividends.build_calendar(positions, cash_tx, pd.DataFrame(), today=today,
                                         net=False, lookback_months=None)
    net_worth = capital.dividend_income(cash_tx)["cumulative"].iloc[-1]
    per_symbol = sum(
        price_stats.dividends_received(cash_tx, sym)["net"] for sym in ("ABC", "ZERO", "EUR")
    )

    assert cal_net.paid_sum == pytest.approx(expected_net)
    assert cal_gross.paid_sum == pytest.approx(expected_gross)
    assert net_worth == pytest.approx(expected_net)
    assert per_symbol == pytest.approx(expected_net)
    assert cal_net.sold == {"EUR"}  # sold ticker still counted in the calendar


def test_synthetic_statement_totals_agree(stmt):
    cash_tx = parser.cash_transactions_df(stmt)
    positions = parser.positions_df(stmt)
    cal = dividends.build_calendar(positions, cash_tx, pd.DataFrame(),
                                   today=dt.date(2025, 1, 31), net=True, lookback_months=None)
    net_worth = capital.dividend_income(cash_tx)["cumulative"].iloc[-1]
    stats = price_stats.dividends_received(cash_tx, "AAPL")["net"]
    assert cal.paid_sum == pytest.approx(36.75)
    assert net_worth == pytest.approx(36.75)
    assert stats == pytest.approx(36.75)
