"""Tests for data.capital — contributed capital vs earnings, and range clipping."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from data import capital


def _tx(rows):
    return pd.DataFrame(rows)


@pytest.fixture
def deposits():
    return _tx([
        # 400 ILS at 0.27 -> 108 base, then 350 base. Raw amounts sum to 750,
        # base amounts to 458 — the whole point of the module.
        {"type": "DEPOSITWITHDRAW", "amount": 400.0, "amount_base": 108.0,
         "settle_date": dt.date(2026, 1, 5)},
        {"type": "DEPOSITWITHDRAW", "amount": 350.0, "amount_base": 350.0,
         "settle_date": dt.date(2026, 3, 1)},
        {"type": "DIVIDEND", "amount": 9.0, "amount_base": 9.0,
         "settle_date": dt.date(2026, 3, 5)},
    ])


def test_contributions_sums_base_currency_not_raw_amount(deposits):
    out = capital.contributions(deposits)
    assert list(out["amount"]) == pytest.approx([108.0, 350.0])
    assert list(out["cumulative"]) == pytest.approx([108.0, 458.0])


def test_contributions_ignores_non_deposit_types(deposits):
    assert len(capital.contributions(deposits)) == 2  # the DIVIDEND row is out


def test_contributions_nets_withdrawals(deposits):
    rows = pd.concat([deposits, _tx([
        {"type": "DEPOSITWITHDRAW", "amount": -100.0, "amount_base": -100.0,
         "settle_date": dt.date(2026, 4, 1)},
    ])], ignore_index=True)
    assert capital.contributions(rows)["cumulative"].iloc[-1] == pytest.approx(358.0)


def test_contributions_drops_rows_without_a_base_amount(caplog):
    # Pre-migration rows carry only the transaction-currency amount. Using it
    # would mix currencies, so they are excluded and reported.
    rows = _tx([
        {"type": "DEPOSITWITHDRAW", "amount": 400.0, "amount_base": None,
         "settle_date": dt.date(2026, 1, 5)},
    ])
    assert capital.contributions(rows).empty
    assert capital.unpriced_deposits(rows) == 1


def test_unpriced_deposits_counts_all_when_column_absent():
    rows = _tx([{"type": "DEPOSITWITHDRAW", "amount": 400.0,
                 "settle_date": dt.date(2026, 1, 5)}])
    assert capital.unpriced_deposits(rows) == 1


def test_contributions_empty_inputs():
    assert capital.contributions(None).empty
    assert capital.contributions(pd.DataFrame()).empty
    assert capital.unpriced_deposits(None) == 0


@pytest.fixture
def nav():
    return pd.DataFrame({
        "snapshot_date": ["2026-01-01", "2026-01-10", "2026-03-02", "2026-06-01"],
        "net_liq": [0.0, 120.0, 470.0, 600.0],
        "currency": ["USD"] * 4,
    })


def test_composition_carries_contributions_forward(nav, deposits):
    comp = capital.composition(nav, deposits)
    # 2026-01-01 predates every deposit; the rest inherit the running total.
    assert list(comp["contributed"]) == pytest.approx([0.0, 108.0, 458.0, 458.0])
    assert list(comp["earnings"]) == pytest.approx([0.0, 12.0, 12.0, 142.0])


def test_composition_allows_negative_earnings(nav, deposits):
    down = nav.copy()
    down["net_liq"] = [0.0, 50.0, 300.0, 300.0]
    comp = capital.composition(down, deposits)
    # Worth less than what was paid in is a real state, not something to clip.
    assert comp["earnings"].iloc[-1] == pytest.approx(-158.0)


def test_composition_empty_without_usable_deposits(nav):
    assert capital.composition(nav, pd.DataFrame()).empty
    assert capital.composition(pd.DataFrame(), None).empty


def test_range_start_ytd_and_1y():
    today = dt.date(2026, 8, 22)
    assert capital.range_start("YTD", today) == dt.date(2026, 1, 1)
    assert capital.range_start("1Y", today) == dt.date(2025, 8, 22)
    assert capital.range_start("ALL", today) is None


def test_clip_keeps_only_rows_inside_the_window(nav, deposits):
    comp = capital.composition(nav, deposits)
    today = dt.date(2026, 8, 22)
    assert len(capital.clip(comp, "ALL", today)) == 4
    assert list(capital.clip(comp, "YTD", today)["date"]) == list(comp["date"])
    older = capital.clip(comp, "1Y", dt.date(2027, 5, 1))
    assert list(older["date"]) == [dt.date(2026, 6, 1)]


def test_clip_preserves_cumulative_contributions_across_the_boundary(nav, deposits):
    # A window must not restart the contribution total at zero — money paid in
    # before the window is still money paid in.
    comp = capital.composition(nav, deposits)
    window = capital.clip(comp, "1Y", dt.date(2027, 5, 1))
    assert window["contributed"].iloc[0] == pytest.approx(458.0)


@pytest.fixture
def income_tx(deposits):
    return pd.concat([deposits, _tx([
        {"type": "PAYMENTINLIEU", "amount": 4.0, "amount_base": 4.0,
         "settle_date": dt.date(2026, 4, 10)},
        {"type": "WHTAX", "amount": -3.0, "amount_base": -3.0,
         "settle_date": dt.date(2026, 3, 5)},
        {"type": "BROKERINTRCVD", "amount": 2.0, "amount_base": 2.0,
         "settle_date": dt.date(2026, 3, 6)},
    ])], ignore_index=True)


def test_dividend_income_is_net_of_withholding(income_tx):
    out = capital.dividend_income(income_tx)
    # 9 dividend - 3 tax on 03-05, then 4 payment-in-lieu. Interest is not income
    # of this kind and stays out.
    assert list(out["cumulative"]) == pytest.approx([6.0, 10.0])


def test_dividend_income_excludes_interest(income_tx):
    assert 2.0 not in list(capital.dividend_income(income_tx)["amount"])


def test_composition_carries_dividends_as_a_slice_of_earnings(nav, income_tx):
    comp = capital.composition(nav, income_tx)
    # Dividends never exceed earnings here and are not added to net_liq: the
    # cash is already counted inside it.
    # Income dated 03-05 and 04-10, so only the 06-01 NAV row sees it — an
    # as-of join must not credit a NAV date with cash that arrived later.
    assert list(comp["dividends"]) == pytest.approx([0.0, 0.0, 0.0, 10.0])
    assert list(comp["net_liq"]) == pytest.approx([0.0, 120.0, 470.0, 600.0])
    assert (comp["earnings"] == comp["net_liq"] - comp["contributed"]).all()


def test_composition_dividends_zero_without_income_rows(nav):
    only_deposits = _tx([
        {"type": "DEPOSITWITHDRAW", "amount": 350.0, "amount_base": 350.0,
         "settle_date": dt.date(2026, 1, 5)},
    ])
    assert list(capital.composition(nav, only_deposits)["dividends"]) == [0.0] * 4
