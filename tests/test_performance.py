"""Tests for data.performance — daily result per close, with deposits taken out."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from data import performance

D = dt.date
TODAY = D(2026, 3, 10)


def _closes(rows: list[tuple[dt.date, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"date": date, "nav": nav} for date, nav in rows], columns=["date", "nav"])


@pytest.fixture
def closes() -> pd.DataFrame:
    # Thu 26 Feb unfunded; Fri 27 funded; Thu 5 Mar has no close (a gap).
    return _closes([
        (D(2026, 2, 26), 0.0),
        (D(2026, 2, 27), 1000.0),
        (D(2026, 3, 2), 1010.0),
        (D(2026, 3, 3), 1222.0),
        (D(2026, 3, 4), 1222.0),
        (D(2026, 3, 6), 1200.0),
        (D(2026, 3, 9), 1300.0),
    ])


@pytest.fixture
def flows() -> dict[dt.date, float]:
    # The last deposit is reported on a Saturday, a day without a close.
    return {D(2026, 2, 27): 1000.0, D(2026, 3, 3): 200.0, D(2026, 3, 7): 100.0}


@pytest.fixture
def daily(closes, flows) -> pd.DataFrame:
    return performance.daily(closes=closes, flows=flows)


def _row(daily: pd.DataFrame, date: dt.date) -> dict:
    return daily[daily["date"] == date].iloc[0].to_dict()


# ---- flows ------------------------------------------------------------------

def test_flows_are_dated_by_report_date_not_settle_date():
    """NAV moves on the report date; dating by settle date would show a fake gain
    one day and a fake loss the other."""
    cash_tx = pd.DataFrame([
        {"type": "DEPOSITWITHDRAW", "amount_base": 114.59,
         "settle_date": D(2026, 10, 1), "report_date": D(2026, 10, 2)},
    ])
    assert performance.flows_by_day(cash_tx=cash_tx) == {D(2026, 10, 2): 114.59}


def test_flows_sum_per_day_in_base_currency_and_ignore_income():
    cash_tx = pd.DataFrame([
        {"type": "DEPOSITWITHDRAW", "amount": 4000.0, "amount_base": 1080.0, "report_date": D(2026, 3, 3)},
        {"type": "DEPOSITWITHDRAW", "amount": -50.0, "amount_base": -50.0, "report_date": D(2026, 3, 3)},
        {"type": "DIVIDEND", "amount": 25.0, "amount_base": 25.0, "report_date": D(2026, 3, 3)},
        {"type": "BROKERINTRCVD", "amount": 1.0, "amount_base": 1.0, "report_date": D(2026, 3, 3)},
    ])
    assert performance.flows_by_day(cash_tx=cash_tx) == {D(2026, 3, 3): pytest.approx(1030.0)}


def test_flows_skip_rows_without_base_amount():
    cash_tx = pd.DataFrame([
        {"type": "DEPOSITWITHDRAW", "amount": 4000.0, "amount_base": float("nan"),
         "report_date": D(2026, 3, 3)},
    ])
    assert performance.flows_by_day(cash_tx=cash_tx) == {}


@pytest.mark.parametrize("cash_tx", [None, pd.DataFrame()])
def test_flows_empty_input(cash_tx):
    assert performance.flows_by_day(cash_tx=cash_tx) == {}


# ---- closes -----------------------------------------------------------------

def test_closes_ignore_live_rows_and_prefer_the_statement():
    equity = pd.DataFrame([{"report_date": D(2026, 3, 3), "net_liq": 1222.0}])
    stored = pd.DataFrame([
        {"report_date": D(2026, 3, 2), "net_liq": 1010.0},
        {"report_date": D(2026, 3, 3), "net_liq": 9999.0},  # statement wins
    ])
    nav_history = pd.DataFrame([
        {"snapshot_date": pd.Timestamp("2026-02-27"), "ts": "2026-02-27T00:00:00", "net_liq": 1000.0},
        # A live pull: the latest figure filed under the pull date, not a close.
        {"snapshot_date": pd.Timestamp("2026-03-07"), "ts": "2026-03-07T10:59:58", "net_liq": 1200.0},
    ])
    out = performance.closes(equity=equity, stored=stored, nav_history=nav_history)
    assert list(out["date"]) == [D(2026, 2, 27), D(2026, 3, 2), D(2026, 3, 3)]
    assert list(out["nav"]) == [1000.0, 1010.0, 1222.0]


def test_closes_all_sources_empty():
    out = performance.closes(equity=None, stored=pd.DataFrame(), nav_history=None)
    assert out.empty and list(out.columns) == ["date", "nav"]


# ---- daily ------------------------------------------------------------------

def test_series_starts_at_first_funded_close(daily):
    assert daily.iloc[0]["date"] == D(2026, 2, 27)


def test_funding_day_is_not_a_gain(daily):
    row = _row(daily, D(2026, 2, 27))
    assert row["flow"] == 1000.0
    assert row["pnl"] == pytest.approx(0.0)
    assert row["ret"] == pytest.approx(0.0)


def test_plain_day(daily):
    row = _row(daily, D(2026, 3, 2))
    assert row["prev_date"] == D(2026, 2, 27)
    assert row["pnl"] == pytest.approx(10.0)
    assert row["ret"] == pytest.approx(0.01)


def test_deposit_is_taken_out_and_sits_in_the_denominator(daily):
    row = _row(daily, D(2026, 3, 3))
    assert row["pnl"] == pytest.approx(12.0)             # 1222 - 1010 - 200
    assert row["ret"] == pytest.approx(1222 / 1210 - 1)  # IBKR: E / (E_prev + F) - 1


def test_gap_measures_from_the_previous_close(daily):
    row = _row(daily, D(2026, 3, 6))
    assert row["prev_date"] == D(2026, 3, 4)
    assert row["pnl"] == pytest.approx(-22.0)


def test_flow_on_a_day_without_a_close_counts_on_the_next_close(daily):
    row = _row(daily, D(2026, 3, 9))
    assert row["flow"] == 100.0
    assert row["pnl"] == pytest.approx(0.0)


def test_pnl_adds_up_to_nav_change_minus_flows(daily, flows):
    assert daily["pnl"].sum() == pytest.approx(1300.0 - 0.0 - sum(flows.values()))


def test_no_capital_gives_no_percent():
    daily = performance.daily(
        closes=_closes([(D(2026, 3, 2), 100.0), (D(2026, 3, 3), 50.0)]),
        flows={D(2026, 3, 3): -100.0},
    )
    row = _row(daily, D(2026, 3, 3))
    assert row["pnl"] == pytest.approx(50.0)
    assert pd.isna(row["ret"])


def test_first_close_already_funded_has_no_result():
    daily = performance.daily(closes=_closes([(D(2026, 3, 2), 100.0)]), flows={})
    assert len(daily) == 1 and pd.isna(daily.iloc[0]["pnl"])


@pytest.mark.parametrize("closes", [
    pd.DataFrame(columns=["date", "nav"]),
    _closes([(D(2026, 3, 2), 0.0), (D(2026, 3, 3), 0.0)]),  # never funded
])
def test_daily_empty(closes):
    out = performance.daily(closes=closes, flows={})
    assert out.empty and list(out.columns) == performance.DAILY_COLS


# ---- summary ----------------------------------------------------------------

def test_summary_links_returns_instead_of_summing(daily):
    out = performance.summary(daily=daily, start=D(2026, 3, 1), end=D(2026, 3, 31))
    expected = 1.01 * (1222 / 1210) * 1.0 * (1200 / 1222) * 1.0 - 1
    assert out["ret"] == pytest.approx(expected)
    assert out["pnl"] == pytest.approx(10 + 12 + 0 - 22 + 0)
    assert (out["days"], out["up_days"], out["down_days"]) == (5, 2, 1)
    assert out["best"]["date"] == D(2026, 3, 2)
    assert out["worst"]["date"] == D(2026, 3, 6)


def test_summary_of_a_range_without_closes(daily):
    out = performance.summary(daily=daily, start=D(2025, 1, 1), end=D(2025, 1, 31))
    assert out == {"days": 0, "pnl": None, "ret": None, "best": None, "worst": None,
                   "up_days": 0, "down_days": 0}


def test_months_newest_first(daily):
    assert performance.months(daily=daily) == [(2026, 3), (2026, 2)]


# ---- tint -------------------------------------------------------------------

@pytest.mark.parametrize("ret, step", [
    (None, 0), (0.0, 1), (0.00249, 1), (0.0025, 2), (-0.0074, 2),
    (0.0075, 3), (-0.0149, 3), (0.015, 4), (-0.2, 4),
])
def test_tint_step_bounds(ret, step):
    assert performance.tint_step(ret=ret) == step


@pytest.mark.parametrize("pnl, expected", [
    (0.004, performance.FLAT), (-0.004, performance.FLAT), (None, performance.FLAT),
    (0.01, performance.GAIN), (-0.01, performance.LOSS),
])
def test_state(pnl, expected):
    assert performance.state(pnl=pnl) == expected


# ---- calendar grid ----------------------------------------------------------

def _states(weeks: list[dict]) -> list[list[str]]:
    return [[cell["state"] for cell in week["days"]] for week in weeks]


def test_month_weeks_states(daily):
    weeks = performance.month_weeks(daily=daily, details={}, year=2026, month=3, today=TODAY)
    states = _states(weeks)
    # Week of 2 Mar: gain, gain, flat, the gap, loss.
    assert states[0] == ["gain", "gain", "flat", "nodata", "loss"]
    # Week of 9 Mar: flat close, today pending, then the future.
    assert states[1] == ["flat", "pending", "empty", "empty", "empty"]
    assert all(state == "empty" for week in states[2:] for state in week)
    # The last week (30, 31 Mar) keeps April's days blank.
    assert len(weeks) == 5 and [c["date"].month for c in weeks[4]["days"]] == [3, 3, 4, 4, 4]


def test_month_weeks_days_before_the_first_close_are_empty(daily):
    weeks = performance.month_weeks(daily=daily, details={}, year=2026, month=2, today=TODAY)
    states = _states(weeks)
    assert states[-1] == ["empty", "empty", "empty", "empty", "flat"]
    assert all(state == "empty" for week in states[:-1] for state in week)


def test_month_weeks_cell_fields(daily):
    details = {D(2026, 3, 3): {"dividends": 4.5, "fees": 0.0, "bought": 2, "sold": 0}}
    weeks = performance.month_weeks(daily=daily, details=details, year=2026, month=3, today=TODAY)
    deposit_day, gap_day = weeks[0]["days"][1], weeks[0]["days"][4]
    assert (deposit_day["flow"], deposit_day["dividends"], deposit_day["bought"]) == (200.0, 4.5, 2)
    assert deposit_day["step"] == 3 and not deposit_day["gap"]
    assert gap_day["gap"] and gap_day["prev_date"] == D(2026, 3, 4)
    # Monday against Friday is the normal case, not a gap.
    assert not weeks[0]["days"][0]["gap"]


def test_week_summary_covers_only_this_months_days(daily):
    weeks = performance.month_weeks(daily=daily, details={}, year=2026, month=3, today=TODAY)
    assert weeks[0]["summary"]["pnl"] == pytest.approx(0.0)   # 10 + 12 + 0 - 22
    assert weeks[0]["summary"]["days"] == 4
    assert weeks[3]["summary"]["days"] == 0


def test_period_months_spans_a_year_boundary(daily):
    fees = {D(2026, 1, 15): 1.0, D(2026, 3, 3): 2.0, D(2026, 3, 31): 0.5, D(2026, 4, 1): 9.0}
    out = performance.period_months(daily=daily, details={}, fees=fees, today=TODAY,
                                    months=[(2025, 12), (2026, 1), (2026, 2), (2026, 3)])
    assert [(m["year"], m["month"]) for m in out] == [(2025, 12), (2026, 1), (2026, 2), (2026, 3)]
    assert [m["summary"]["days"] for m in out] == [0, 0, 1, 5]
    assert [m["fees"] for m in out] == [0, 1.0, 0, 2.5]


def test_day_details_counts_dividends_and_fills():
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "AAPL", "amount": 24.0, "amount_base": 24.0,
         "settle_date": D(2026, 3, 3), "ex_date": D(2026, 2, 20)},
        {"type": "WHTAX", "symbol": "AAPL", "amount": -6.0, "amount_base": -6.0,
         "settle_date": D(2026, 3, 3), "ex_date": D(2026, 2, 20)},
    ])
    trades = pd.DataFrame([
        {"trade_date": D(2026, 3, 3), "side": "BUY", "commission": -1.0},
        {"trade_date": D(2026, 3, 3), "side": "BUY", "commission": -0.35},
        {"trade_date": D(2026, 3, 4), "side": "SELL", "commission": -2.5},
    ])
    best = {"symbol": "AAPL", "pct": 0.02, "pnl": 5.0}
    out = performance.day_details(cash_tx=cash_tx, trades=trades,
                                  movers={D(2026, 3, 3): {"best": best, "worst": None}})
    assert out[D(2026, 3, 3)] == {"dividends": pytest.approx(18.0), "fees": pytest.approx(1.35),
                                  "bought": 2, "sold": 0, "best": best, "worst": None}
    assert out[D(2026, 3, 4)] == {"dividends": 0.0, "fees": 2.5, "bought": 0, "sold": 1,
                                  "best": None, "worst": None}


def test_day_details_empty():
    assert performance.day_details(cash_tx=None, trades=None, movers={}) == {}


# ---- fees -------------------------------------------------------------------

def test_fees_are_commissions_plus_fee_transactions_as_a_positive_cost():
    trades = pd.DataFrame([
        {"trade_date": D(2026, 3, 3), "side": "BUY", "commission": -1.0},
        {"trade_date": D(2026, 3, 3), "side": "SELL", "commission": -2.5},
        {"trade_date": D(2026, 3, 4), "side": "BUY", "commission": 0.0},     # free fill: no entry
        {"trade_date": D(2026, 3, 5), "side": "BUY", "commission": None},
    ])
    cash_tx = pd.DataFrame([
        {"type": "FEES", "amount_base": -10.0, "report_date": D(2026, 3, 6)},
        {"type": "COMMADJ", "amount_base": 0.4, "report_date": D(2026, 3, 3)},   # a refund
        # Tax and income are not broker fees.
        {"type": "WHTAX", "amount_base": -6.0, "report_date": D(2026, 3, 6)},
        {"type": "DIVIDEND", "amount_base": 24.0, "report_date": D(2026, 3, 6)},
    ])
    out = performance.fees_by_day(trades=trades, cash_tx=cash_tx)
    assert out == {D(2026, 3, 3): pytest.approx(3.1), D(2026, 3, 6): pytest.approx(10.0)}


def test_fees_empty_input():
    assert performance.fees_by_day(trades=None, cash_tx=pd.DataFrame()) == {}


def test_fees_total_counts_days_without_a_close():
    """A commission on a day with no close still belongs to the period total."""
    fees = {D(2026, 2, 28): 1.0, D(2026, 3, 1): 2.0, D(2026, 3, 31): 3.0, D(2026, 4, 1): 4.0}
    assert performance.fees_total(fees=fees, start=D(2026, 3, 1), end=D(2026, 3, 31)) == 5.0
    assert performance.fees_total(fees={}, start=D(2026, 3, 1), end=D(2026, 3, 31)) == 0


def test_cell_carries_the_days_fees(daily):
    details = {D(2026, 3, 3): {"dividends": 0.0, "fees": 1.35, "bought": 2, "sold": 0}}
    weeks = performance.month_weeks(daily=daily, details=details, year=2026, month=3, today=TODAY)
    assert weeks[0]["days"][1]["fees"] == 1.35 and weeks[0]["days"][0]["fees"] == 0.0


# ---- per-ticker days -----------------------------------------------------------

@pytest.fixture
def symbol_days() -> pd.DataFrame:
    statement = pd.DataFrame([
        {"date": D(2026, 3, 2), "symbol": "AAPL", "description": "APPLE", "price": 100.0, "mtm_pnl": 0.0},
        {"date": D(2026, 3, 3), "symbol": "AAPL", "description": "APPLE", "price": 103.0, "mtm_pnl": 30.0},
        {"date": D(2026, 3, 3), "symbol": "MSFT", "description": "MICROSOFT", "price": 400.0, "mtm_pnl": 7.0},
        {"date": D(2026, 3, 4), "symbol": "AAPL", "description": "APPLE", "price": 101.97, "mtm_pnl": -10.3},
        {"date": D(2026, 3, 4), "symbol": "MSFT", "description": "MICROSOFT", "price": 408.0, "mtm_pnl": 8.0},
        {"date": D(2026, 3, 4), "symbol": "KO", "description": "COCA-COLA", "price": 60.0, "mtm_pnl": -90.0},
    ])
    stored = pd.DataFrame([
        {"date": D(2026, 2, 27), "symbol": "KO", "description": "COCA-COLA", "price": 61.0, "mtm_pnl": 1.0},
        # The statement's row for the same day and symbol wins.
        {"date": D(2026, 3, 3), "symbol": "AAPL", "description": "APPLE", "price": 999.0, "mtm_pnl": 999.0},
    ])
    return performance.symbol_days(statement=statement, stored=stored)


def test_symbol_days_price_change_is_against_the_symbols_previous_row(symbol_days):
    rows = {(r["date"], r["symbol"]): r for r in symbol_days.to_dict("records")}
    assert rows[(D(2026, 3, 3), "AAPL")]["pct"] == pytest.approx(0.03)
    assert rows[(D(2026, 3, 3), "AAPL")]["pnl"] == 30.0            # statement over stored
    assert pd.isna(rows[(D(2026, 3, 3), "MSFT")]["pct"])           # first day on record
    # KO skipped two days: measured from its last known price, 27 Feb.
    assert rows[(D(2026, 3, 4), "KO")]["pct"] == pytest.approx(60 / 61 - 1)


def test_symbol_days_empty():
    out = performance.symbol_days(statement=None, stored=pd.DataFrame())
    assert out.empty and list(out.columns) == performance.MOVER_COLS


def test_movers_rank_by_price_change_not_by_money(symbol_days):
    """Best and worst *performing* ticker: KO lost the most money, but AAPL fell
    further in percent."""
    out = performance.movers(symbol_days=symbol_days)
    day = out[D(2026, 3, 4)]
    assert (day["best"]["symbol"], day["best"]["pct"]) == ("MSFT", pytest.approx(0.02))
    assert day["worst"]["symbol"] == "KO" and day["worst"]["pnl"] == -90.0
    assert day["worst"]["pct"] == pytest.approx(60 / 61 - 1)       # -1.64% vs AAPL -1.00%


def test_movers_single_rated_ticker_has_no_worst(symbol_days):
    out = performance.movers(symbol_days=symbol_days)
    assert out[D(2026, 3, 3)]["best"]["symbol"] == "AAPL" and out[D(2026, 3, 3)]["worst"] is None
    assert D(2026, 3, 2) not in out                                # nothing rated that day


def test_day_symbols_biggest_money_gain_first(symbol_days):
    rows = performance.day_symbols(symbol_days=symbol_days, date=D(2026, 3, 4))
    assert [r["symbol"] for r in rows] == ["MSFT", "AAPL", "KO"]
    assert performance.day_symbols(symbol_days=symbol_days, date=D(2030, 1, 1)) == []


def test_cell_carries_best_and_worst(daily, symbol_days):
    details = performance.day_details(cash_tx=None, trades=None,
                                      movers=performance.movers(symbol_days=symbol_days))
    weeks = performance.month_weeks(daily=daily, details=details, year=2026, month=3, today=TODAY)
    wednesday, monday = weeks[0]["days"][2], weeks[0]["days"][0]
    assert wednesday["best"]["symbol"] == "MSFT" and wednesday["worst"]["symbol"] == "KO"
    assert monday["best"] is None and monday["worst"] is None
