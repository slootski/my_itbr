"""Tests for data.dividends — the dividend-projection calendar builder."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from data import dividends


def _positions(*symbols: str) -> pd.DataFrame:
    return pd.DataFrame({"symbol": list(symbols)})


def test_helpers_ym_and_month_math():
    assert dividends._ym(dt.date(2026, 6, 9)) == "2026-06"
    assert dividends._add_months("2026-11", 2) == "2027-01"
    assert dividends._idx("2026-02") - dividends._idx("2026-01") == 1
    assert dividends._mode([3, 3, 1]) == 3
    assert dividends._mode([1, 3]) == 1  # tie -> smallest


def test_paid_is_green_with_real_net():
    # Quarterly payer: gross 100, withholding -15 -> real net 85.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(2026, 3, 15), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None},
        {"type": "WHTAX", "symbol": "ABC", "amount": -15.0,
         "settle_date": dt.date(2026, 3, 15), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=False)
    assert cal.status.at["ABC", "2026-03"] == "paid"
    assert cal.values.at["ABC", "2026-03"] == "100.00"

    cal_net = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                       today=dt.date(2026, 4, 1), net=True)
    assert cal_net.values.at["ABC", "2026-03"] == "85.00"  # real withholding, not 25%


def test_net_folds_whtax_posted_in_later_month():
    # WHTAX shares the dividend's ex_date but is posted/settled a month later.
    # Net must subtract it in the dividend's own month (2026-03), and no negative
    # phantom green cell may appear in the WHTAX month (2026-04).
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(2026, 3, 15), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None},
        {"type": "WHTAX", "symbol": "ABC", "amount": -15.0,
         "settle_date": dt.date(2026, 4, 10), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 5, 1), net=True)
    assert cal.values.at["ABC", "2026-03"] == "85.00"  # tax folded into div month
    # No phantom WHTAX-only cell (the column itself exists: contiguous range
    # through the current month, but it must stay empty).
    assert cal.values.at["ABC", "2026-04"] == ""
    assert cal.status.at["ABC", "2026-04"] == ""


def test_orphan_whtax_falls_back_to_own_month():
    # WHTAX with no matching dividend ex_date still lands in its own month.
    cash_tx = pd.DataFrame([
        {"type": "WHTAX", "symbol": "ABC", "amount": -5.0,
         "settle_date": dt.date(2026, 2, 15), "ex_date": dt.date(2026, 2, 1),
         "datetime": None, "report_date": None},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 3, 1), net=True)
    assert cal.values.at["ABC", "2026-02"] == "-5.00"


def test_declared_accrual_is_yellow_and_blocks_projection():
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(d.year, d.month, 15), "ex_date": dt.date(d.year, d.month, 1),
         "datetime": None, "report_date": None}
        for d in (dt.date(2025, 9, 1), dt.date(2025, 12, 1), dt.date(2026, 3, 1))
    ])
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2026, 6, 1), "pay_date": dt.date(2026, 6, 15),
         "gross_amount": 100.0, "net_amount": 80.0, "quantity": 10, "tax": 20.0},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, accruals,
                                   today=dt.date(2026, 5, 1), net=False)
    assert cal.status.at["ABC", "2026-06"] == "declared"
    assert cal.values.at["ABC", "2026-06"] == "100.00"
    # No red projection anywhere: accrual already covers the next payment.
    assert "pay" not in cal.status.values
    assert "declare" not in cal.status.values


def test_weekly_payer_sums_overlapping_declared_accruals():
    # Weekly-pay ticker (e.g. COPY/MSTY) can have two accruals open at once
    # whose payments both land in the same calendar month. Both must be
    # summed into the yellow cell, not the second silently overwriting the
    # first, and the tooltip must list both declarations numbered.
    accruals = pd.DataFrame([
        {"symbol": "MSTY", "ex_date": dt.date(2026, 6, 2), "pay_date": dt.date(2026, 6, 9),
         "gross_amount": 30.0, "net_amount": 24.0, "quantity": 10, "tax": 6.0},
        {"symbol": "MSTY", "ex_date": dt.date(2026, 6, 9), "pay_date": dt.date(2026, 6, 16),
         "gross_amount": 20.0, "net_amount": 16.0, "quantity": 10, "tax": 4.0},
    ])
    cal = dividends.build_calendar(_positions("MSTY"), pd.DataFrame(), accruals,
                                   today=dt.date(2026, 6, 1), net=False)
    assert cal.status.at["MSTY", "2026-06"] == "declared"
    assert cal.values.at["MSTY", "2026-06"] == "50.00"
    assert cal.tooltips.at["MSTY", "2026-06"] == (
        "declaration 1: 02-06-2026\npay 1: 09-06-2026\n"
        "declaration 2: 09-06-2026\npay 2: 16-06-2026"
    )


def test_weekly_payer_does_not_project_a_false_next_month():
    # CONY/MSTY pay weekly. Month-level cadence (_project) sees a payment in
    # every recent month, infers a 1-month cadence, and — since the current
    # month already has a paid entry — skips straight to next month, showing
    # a red "Declare" in October when the real next payment (7-day cadence
    # from the last real pay date) is actually still due THIS week, within
    # September. September's cell is real received cash (paid, ground truth)
    # and must not be overwritten by a guess; the fix is that no false
    # October projection appears either.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "CONY", "amount": 40.0,
         "settle_date": d, "ex_date": d, "datetime": None, "report_date": None}
        for d in (dt.date(2026, 8, 18), dt.date(2026, 8, 25), dt.date(2026, 9, 1))
    ])
    cal = dividends.build_calendar(_positions("CONY"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 9, 7), net=False)
    assert cal.status.at["CONY", "2026-09"] == "paid"  # real cash, untouched
    assert "declare" not in cal.status.values
    assert "pay" not in cal.status.values
    assert "2026-10" not in cal.values.columns


def _quarterly_cash_tx(symbol: str = "ABC", amount: float = 50.0) -> pd.DataFrame:
    # Quarterly payer, ex one month before pay; last paid 2026-03.
    return pd.DataFrame([
        {"type": "DIVIDEND", "symbol": symbol, "amount": amount,
         "settle_date": dt.date(pay.year, pay.month, 15),
         "ex_date": dt.date(ex.year, ex.month, 20),
         "datetime": None, "report_date": None}
        for ex, pay in (
            (dt.date(2025, 8, 1), dt.date(2025, 9, 1)),
            (dt.date(2025, 11, 1), dt.date(2025, 12, 1)),
            (dt.date(2026, 2, 1), dt.date(2026, 3, 1)),
        )
    ])


def test_projection_emits_declare_and_pay_cells():
    # BUG-01: declaration month differs from payment month -> BOTH red cells.
    # Next projected payment 2026-06, declaration offset 1 month -> 2026-05.
    cal = dividends.build_calendar(_positions("ABC"), _quarterly_cash_tx(), pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=False)
    assert cal.status.at["ABC", "2026-05"] == "declare"
    assert cal.values.at["ABC", "2026-05"] == "Declare"
    assert cal.status.at["ABC", "2026-06"] == "pay"
    # BUG-02: the word "Pay", never an amount — identical in both tax modes.
    assert cal.values.at["ABC", "2026-06"] == "Pay"
    # Forward horizon: nothing beyond the projected payment month.
    assert "2026-06" == cal.values.columns[-1]
    cal_net = dividends.build_calendar(_positions("ABC"), _quarterly_cash_tx(), pd.DataFrame(),
                                       today=dt.date(2026, 4, 1), net=True)
    assert cal_net.values.at["ABC", "2026-06"] == "Pay"
    # Projected cells estimate the day from history: ex-dates land on the 20th,
    # payments on the 15th (see _quarterly_cash_tx), marked as estimates.
    assert cal.tooltips.at["ABC", "2026-05"] == "projected declaration: ~20-05-2026 (estimated)"
    assert cal.tooltips.at["ABC", "2026-06"] == "projected payment: ~15-06-2026 (estimated)"


def test_projection_tooltip_falls_back_to_month_without_ex_dates():
    """No ex-dates in history -> declaration stays month-level, never a fake day.

    Payments still carry a day: a paid dividend always has a payment date.
    """
    cash_tx = _quarterly_cash_tx()
    cash_tx["ex_date"] = None
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=False)
    tip = cal.tooltips.at["ABC", "2026-06"]
    assert "projected declaration: Jun 2026" in tip
    assert "projected payment: ~15-06-2026 (estimated)" in tip


def test_projected_day_clamped_to_short_month():
    """A 31st-of-the-month payer projected into February lands on the 28th/29th."""
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "EOM", "amount": 10.0,
         "settle_date": pay, "ex_date": pay, "datetime": None, "report_date": None}
        for pay in (dt.date(2025, 10, 31), dt.date(2025, 12, 31))
    ])
    cal = dividends.build_calendar(_positions("EOM"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 1, 15), net=False)
    assert "28-02-2026" in cal.tooltips.at["EOM", "2026-02"]


def test_same_month_declare():
    # Ex and pay in the same month historically (offset 0) -> projected "Declare".
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "XYZ", "amount": 10.0,
         "settle_date": dt.date(d.year, d.month, 20), "ex_date": dt.date(d.year, d.month, 5),
         "datetime": None, "report_date": None}
        for d in (dt.date(2026, 1, 1), dt.date(2026, 2, 1), dt.date(2026, 3, 1))
    ])
    cal = dividends.build_calendar(_positions("XYZ"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 3, 15), net=False)
    assert cal.status.at["XYZ", "2026-04"] == "declare"
    assert cal.values.at["XYZ", "2026-04"] == "Declare"


def test_same_month_declare_lands_in_current_month_when_unpaid():
    # e1 P3 regression (QA repro verbatim, e1/20-qa-backend.md §Issues): a
    # monthly same-month payer (paid Feb/Mar/Apr) whose next payment is due in
    # the CURRENT month (May) and has not arrived yet must show its single red
    # "Declare" IN the current month, not one cycle later (mock MSTY/CONY).
    tx = pd.DataFrame([{"type": "DIVIDEND", "symbol": "MM", "amount": 90.0,
        "settle_date": dt.date(2026, m, 20), "ex_date": dt.date(2026, m, 5),
        "datetime": None, "report_date": None} for m in (2, 3, 4)])
    cal = dividends.build_calendar(pd.DataFrame({"symbol": ["MM"]}), tx, pd.DataFrame(),
                                   today=dt.date(2026, 5, 10))
    assert cal.status.at["MM", "2026-05"] == "declare"
    assert cal.values.at["MM", "2026-05"] == "Declare"
    # Single-cell horizon: nothing projected beyond the current month.
    assert cal.values.columns[-1] == "2026-05"
    assert "declare" not in cal.status.drop(columns=["2026-05"]).values


def test_same_month_payer_already_paid_this_month_projects_next_cycle():
    # Interplay guard: once the current month's payment HAS landed (paid Feb..May,
    # today mid-May), green is ground truth and the projection moves to June.
    tx = pd.DataFrame([{"type": "DIVIDEND", "symbol": "MM", "amount": 90.0,
        "settle_date": dt.date(2026, m, 20), "ex_date": dt.date(2026, m, 5),
        "datetime": None, "report_date": None} for m in (2, 3, 4, 5)])
    cal = dividends.build_calendar(pd.DataFrame({"symbol": ["MM"]}), tx, pd.DataFrame(),
                                   today=dt.date(2026, 5, 25))
    assert cal.status.at["MM", "2026-05"] == "paid"
    assert cal.status.at["MM", "2026-06"] == "declare"


def test_current_month_accrual_still_suppresses_projection():
    # Interplay guard: an open accrual covering the current month suppresses the
    # red projection entirely — yellow "declared" only, no "declare"/"pay".
    tx = pd.DataFrame([{"type": "DIVIDEND", "symbol": "MM", "amount": 90.0,
        "settle_date": dt.date(2026, m, 20), "ex_date": dt.date(2026, m, 5),
        "datetime": None, "report_date": None} for m in (2, 3, 4)])
    accruals = pd.DataFrame([
        {"symbol": "MM", "ex_date": dt.date(2026, 5, 5), "pay_date": dt.date(2026, 5, 20),
         "gross_amount": 90.0, "net_amount": 72.0, "quantity": 10, "tax": 18.0},
    ])
    cal = dividends.build_calendar(pd.DataFrame({"symbol": ["MM"]}), tx, accruals,
                                   today=dt.date(2026, 5, 10))
    assert cal.status.at["MM", "2026-05"] == "declared"
    assert "declare" not in cal.status.values
    assert "pay" not in cal.status.values


def test_declared_in_past_month_dropped():
    # BUG-06: a stale accrual whose pay_date already passed must not paint history.
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2026, 2, 20), "pay_date": dt.date(2026, 3, 10),
         "gross_amount": 100.0, "net_amount": 80.0, "quantity": 10, "tax": 20.0},
    ])
    cal = dividends.build_calendar(_positions("ABC"), pd.DataFrame(), accruals,
                                   today=dt.date(2026, 6, 1))
    assert cal.empty  # only event was the stale accrual -> nothing to show


def test_declared_current_month_paid_wins():
    # BUG-06: cash already received in the current month beats a lagging accrual.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(2026, 6, 5), "ex_date": dt.date(2026, 5, 20),
         "datetime": None, "report_date": None},
    ])
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 5),
         "gross_amount": 100.0, "net_amount": 80.0, "quantity": 10, "tax": 20.0},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, accruals,
                                   today=dt.date(2026, 6, 10))
    assert cal.status.at["ABC", "2026-06"] == "paid"


def test_stale_accrual_does_not_block_projection():
    # BUG-10: an accrual fully in the past must not suppress the red projection.
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2025, 11, 20), "pay_date": dt.date(2025, 12, 15),
         "gross_amount": 50.0, "net_amount": 40.0, "quantity": 10, "tax": 10.0},
    ])
    cal = dividends.build_calendar(_positions("ABC"), _quarterly_cash_tx(), accruals,
                                   today=dt.date(2026, 4, 1))
    assert cal.status.at["ABC", "2026-05"] == "declare"
    assert cal.status.at["ABC", "2026-06"] == "pay"


def test_declared_tooltip_has_both_dates():
    # BUG-05: yellow cell hover shows declaration (ex-date proxy) and pay dates.
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2026, 5, 12), "pay_date": dt.date(2026, 6, 6),
         "gross_amount": 100.0, "net_amount": 80.0, "quantity": 10, "tax": 20.0},
    ])
    cal = dividends.build_calendar(_positions("ABC"), pd.DataFrame(), accruals,
                                   today=dt.date(2026, 6, 1))
    assert cal.tooltips.at["ABC", "2026-06"] == "declaration: 12-05-2026\npay: 06-06-2026"
    assert cal.status.at["ABC", "2026-06"] == "declared"


def test_totals_sum_paid_and_declared_only():
    # BUG-03: Total = per-month sum of displayed amounts; red words contribute 0.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": s, "amount": a,
         "settle_date": dt.date(2026, 3, 15), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None}
        for s, a in (("AAA", 100.0), ("BBB", 50.0))
    ])
    cal = dividends.build_calendar(_positions("AAA", "BBB"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 4, 1))
    assert cal.totals["2026-03"] == "150.00"
    assert cal.totals["2026-04"] == ""  # current month, no amounts


def test_lookback_clips_history_and_scopes_paid_sum():
    # BUG-04: 3-month window ending at (and including) the current month.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(d.year, d.month, 15), "ex_date": dt.date(d.year, d.month, 5),
         "datetime": None, "report_date": None}
        for d in (dt.date(2026, m, 1) for m in range(1, 7))  # paid Jan..Jun 2026
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 6, 20), lookback_months=3)
    assert cal.values.columns[0] == "2026-04"  # Jan..Mar clipped
    assert cal.paid_sum == 30.0                # Apr+May+Jun, current month included
    cal_full = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                        today=dt.date(2026, 6, 20))
    assert cal_full.values.columns[0] == "2026-01"  # no lookback -> full history
    assert cal_full.paid_sum == 60.0
    # Per-ticker Total column follows the same window as paid_sum.
    assert cal.ticker_totals["ABC"] == "30.00"
    assert cal_full.ticker_totals["ABC"] == "60.00"


def test_paid_sum_excludes_declared():
    # BUG-04: headline is strictly green — declared amounts don't count.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(2026, 5, 15), "ex_date": dt.date(2026, 5, 1),
         "datetime": None, "report_date": None},
    ])
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 5),
         "gross_amount": 40.0, "net_amount": 30.0, "quantity": 10, "tax": 10.0},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, accruals,
                                   today=dt.date(2026, 6, 1), lookback_months=6)
    assert cal.paid_sum == 100.0
    assert cal.ticker_totals["ABC"] == "100.00"  # declared 30/40 not added
    assert cal.totals["2026-06"] == ""           # declared month: nothing paid yet
    assert cal.declared_sum == 40.0              # shown separately, gross basis


def test_current_month_column_always_present():
    # BUG-07: the "today" anchor column exists even with past-only data.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 5.0,
         "settle_date": dt.date(2026, 1, 15), "ex_date": dt.date(2026, 1, 1),
         "datetime": None, "report_date": None},
    ])
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 6, 15))
    assert "2026-06" in cal.values.columns
    assert list(cal.values.columns) == [f"2026-0{m}" for m in range(1, 7)]


def test_month_label():
    assert dividends.month_label("2026-06") == "Jun 2026"


def test_no_data_returns_empty():
    cal = dividends.build_calendar(_positions("ABC"), pd.DataFrame(), pd.DataFrame())
    assert cal.empty


def test_sold_ticker_keeps_its_paid_row():
    # A ticker sold after it paid keeps its row, so the paid total matches every
    # other screen. It is flagged ``sold`` and never projected.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "SOLD", "amount": 5.0,
         "settle_date": dt.date(2026, 1, 15), "ex_date": dt.date(2026, 1, 1),
         "datetime": None, "report_date": None},
    ])
    cal = dividends.build_calendar(_positions("HELD"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 2, 1))
    assert list(cal.values.index) == ["HELD", "SOLD"]  # sold rows last
    assert cal.sold == {"SOLD"}
    assert cal.no_data == {"HELD"}
    assert cal.held_count == 1
    assert cal.paid_sum == 5.0


def test_sold_ticker_is_never_projected():
    cash_tx = _quarterly_cash_tx()
    cal = dividends.build_calendar(_positions("OTHER"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 4, 1))
    assert "ABC" in cal.sold
    assert not {"declare", "pay"} & set(cal.status.loc["ABC"])


def test_sold_ticker_with_only_a_stale_accrual_has_no_row():
    accruals = pd.DataFrame([
        {"symbol": "GONE", "ex_date": dt.date(2025, 6, 1), "pay_date": dt.date(2025, 6, 15),
         "gross_amount": 10.0, "net_amount": 8.0, "quantity": 10, "tax": 2.0},
    ])
    cal = dividends.build_calendar(_positions("HELD"), pd.DataFrame(), accruals,
                                   today=dt.date(2026, 2, 1))
    assert cal.empty or "GONE" not in cal.values.index


def test_held_without_data_gets_placeholder_row():
    # Held tickers with no dividend events still appear as (empty) rows,
    # flagged in ``no_data``; a sold ticker that paid keeps its own row.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "PAYER", "amount": 5.0,
         "settle_date": dt.date(2026, 1, 15), "ex_date": dt.date(2026, 1, 1),
         "datetime": None, "report_date": None},
        {"type": "DIVIDEND", "symbol": "SOLD", "amount": 7.0,
         "settle_date": dt.date(2026, 1, 15), "ex_date": dt.date(2026, 1, 1),
         "datetime": None, "report_date": None},
    ])
    cal = dividends.build_calendar(_positions("PAYER", "NEWBUY"), cash_tx,
                                   pd.DataFrame(), today=dt.date(2026, 2, 1))
    assert list(cal.values.index) == ["NEWBUY", "PAYER", "SOLD"]
    assert cal.no_data == {"NEWBUY"}
    assert cal.sold == {"SOLD"}
    assert (cal.values.loc["NEWBUY"] == "").all()
    assert (cal.status.loc["NEWBUY"] == "").all()
    assert cal.paid_sum == 12.0  # PAYER 5 + SOLD 7; NEWBUY contributes nothing


# ---- coverage() — statement-coverage detection (e3 B3) ---------------------

def test_coverage_none_and_empty_are_safe():
    assert dividends.coverage(None) == {"rows": 0, "min_date": None, "max_date": None}
    assert dividends.coverage(pd.DataFrame()) == {
        "rows": 0, "min_date": None, "max_date": None,
    }


def test_coverage_short_window():
    # Live-bug shape: a LastBusinessDay statement — one dividend + its WHTAX,
    # both settled the same single day.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "XOM", "amount": 0.91,
         "settle_date": dt.date(2026, 6, 10), "ex_date": dt.date(2026, 5, 15),
         "datetime": None, "report_date": None},
        {"type": "WHTAX", "symbol": "XOM", "amount": -0.14,
         "settle_date": dt.date(2026, 6, 10), "ex_date": dt.date(2026, 5, 15),
         "datetime": None, "report_date": None},
    ])
    cov = dividends.coverage(cash_tx)
    assert cov == {
        "rows": 2,
        "min_date": dt.date(2026, 6, 10),
        "max_date": dt.date(2026, 6, 10),
    }
    assert (cov["max_date"] - cov["min_date"]).days == 0  # single-day coverage


def test_coverage_full_year_window():
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(2025, 6, 15), "ex_date": None,
         "datetime": None, "report_date": None},
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(2025, 12, 15), "ex_date": None,
         "datetime": None, "report_date": None},
        {"type": "DIVIDEND", "symbol": "XYZ", "amount": 4.0,
         "settle_date": dt.date(2026, 6, 9), "ex_date": None,
         "datetime": None, "report_date": None},
    ])
    cov = dividends.coverage(cash_tx)
    assert cov["rows"] == 3
    assert cov["min_date"] == dt.date(2025, 6, 15)
    assert cov["max_date"] == dt.date(2026, 6, 9)
    assert (cov["max_date"] - cov["min_date"]).days >= 359


def test_coverage_date_fallback_and_undated_rows():
    # settle missing -> datetime; both missing -> report_date; all missing ->
    # row counts toward rows but not the date range.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "A", "amount": 1.0,
         "settle_date": None, "ex_date": None,
         "datetime": dt.datetime(2026, 1, 5, 12, 0), "report_date": None},
        {"type": "DIVIDEND", "symbol": "B", "amount": 1.0,
         "settle_date": None, "ex_date": None,
         "datetime": None, "report_date": dt.date(2026, 3, 7)},
        {"type": "DIVIDEND", "symbol": "C", "amount": 1.0,
         "settle_date": None, "ex_date": None,
         "datetime": None, "report_date": None},
    ])
    cov = dividends.coverage(cash_tx)
    assert cov["rows"] == 3
    assert cov["min_date"] == dt.date(2026, 1, 5)
    assert cov["max_date"] == dt.date(2026, 3, 7)


def test_coverage_missing_columns_do_not_raise():
    cov = dividends.coverage(pd.DataFrame([{"type": "DIVIDEND", "symbol": "A"}]))
    assert cov == {"rows": 1, "min_date": None, "max_date": None}


# ---- external (yfinance) backfill ------------------------------------------

def _empty_flex():
    return pd.DataFrame(), pd.DataFrame()


def test_external_concrete_dates_split_months():
    # COPX case: ex-div 29 Jun, pay 6 Jul, no Flex data at all.
    cash_tx, accruals = _empty_flex()
    ext = {"COPX": {"ex_date": dt.date(2026, 6, 29), "pay_date": dt.date(2026, 7, 6),
                    "history_months": ["2025-06", "2025-12"]}}
    cal = dividends.build_calendar(_positions("COPX"), cash_tx, accruals,
                                   today=dt.date(2026, 6, 12), external=ext)
    assert cal.status.at["COPX", "2026-06"] == "declare"
    assert cal.status.at["COPX", "2026-07"] == "pay"
    assert "yfinance" in cal.tooltips.at["COPX", "2026-06"]
    assert "29-06-2026" in cal.tooltips.at["COPX", "2026-06"]
    assert "06-07-2026" in cal.tooltips.at["COPX", "2026-07"]
    assert "COPX" not in cal.no_data
    # Red cells carry no amount and never feed totals/paid_sum.
    assert cal.values.at["COPX", "2026-06"] == "Declare"
    assert cal.paid_sum == 0.0


def test_external_declared_amount_renders_yellow_with_cash():
    # COPX with a real Polygon declared amount (0.258/sh) and 100 shares held:
    # a yellow `declared` cell carrying the cash figure in the pay month.
    cash_tx, accruals = _empty_flex()
    positions = pd.DataFrame({"symbol": ["COPX"], "quantity": [100.0]})
    ext = {"COPX": {"ex_date": dt.date(2026, 6, 29), "pay_date": dt.date(2026, 7, 7),
                    "decl_date": dt.date(2026, 1, 20), "amount": 0.258,
                    "currency": "USD", "history_months": ["2025-06", "2025-12"]}}
    cal = dividends.build_calendar(positions, cash_tx, accruals,
                                   today=dt.date(2026, 6, 12), external=ext)
    # Bucketed by pay month (07 Jul), yellow, with a real number (0.258 x 100).
    assert cal.status.at["COPX", "2026-07"] == "declared"
    assert cal.values.at["COPX", "2026-07"] == "25.80"
    tip = cal.tooltips.at["COPX", "2026-07"]
    assert "polygon" in tip and "20-01-2026" in tip
    # Declared amounts feed the month total.
    assert cal.totals["2026-07"] == ""  # declared, unpaid -> not summed
    assert "COPX" not in cal.no_data


def test_polygon_declared_overrides_projection_before_tax():
    # The general fix: a paid-history ticker whose next dividend would be a RED
    # projection instead shows the real Polygon declared amount (gross mode).
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [100.0]})
    ext = {"ABC": {"ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 1),
                   "decl_date": dt.date(2026, 4, 15), "amount": 0.5, "currency": "USD"}}
    cal = dividends.build_calendar(positions, _quarterly_cash_tx(), pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=False, external=ext)
    assert cal.status.at["ABC", "2026-06"] == "declared"
    assert cal.values.at["ABC", "2026-06"] == "50.00"   # 0.5 x 100
    # The real declaration suppresses BOTH projected red cells.
    assert "declare" not in cal.status.values
    assert "pay" not in cal.status.values
    assert cal.totals["2026-06"] == ""  # declared, unpaid -> not summed


def test_polygon_declared_estimates_net_after_tax():
    # Net mode: Polygon gross scaled by the symbol's historical net/gross
    # withholding ratio, rendered as a marked estimate. 25% WHTAX on each paid
    # dividend -> ratio 0.75 -> 0.5/sh x 100 sh x 0.75 = 37.50.
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [100.0]})
    cash_tx = _quarterly_cash_tx()
    whtax = pd.DataFrame([
        {"type": "WHTAX", "symbol": "ABC", "amount": -12.5,
         "settle_date": row["settle_date"], "ex_date": row["ex_date"],
         "datetime": None, "report_date": None}
        for _, row in cash_tx.iterrows()
    ])
    cash_tx = pd.concat([cash_tx, whtax], ignore_index=True)
    ext = {"ABC": {"ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 1),
                   "decl_date": dt.date(2026, 4, 15), "amount": 0.5, "currency": "USD"}}
    cal = dividends.build_calendar(positions, cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=True, external=ext)
    assert cal.status.at["ABC", "2026-06"] == "declared"
    assert cal.values.at["ABC", "2026-06"] == "≈37.50"     # estimate, marked
    tip = cal.tooltips.at["ABC", "2026-06"]
    assert "15-04-2026" in tip
    assert "estimated" in tip and "25%" in tip
    assert cal.totals["2026-06"] == ""                     # unpaid estimate never summed
    assert "pay" not in cal.status.values                  # projection still gone


def test_polygon_declared_net_default_rate_no_history():
    # Net mode without any paid history: no ratio to derive, so the default
    # 25% rate is applied, marked as an estimate, with an explanatory tooltip.
    cash_tx, accruals = _empty_flex()
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [100.0]})
    ext = {"ABC": {"ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 1),
                   "decl_date": dt.date(2026, 4, 15), "amount": 0.5, "currency": "USD"}}
    cal = dividends.build_calendar(positions, cash_tx, accruals,
                                   today=dt.date(2026, 4, 1), net=True, external=ext)
    assert cal.status.at["ABC", "2026-06"] == "declared"
    assert cal.values.at["ABC", "2026-06"] == "≈37.50"     # 50 gross x 0.75
    assert "default 25% tax" in cal.tooltips.at["ABC", "2026-06"]


def test_polygon_declared_net_follows_zero_withholding_history():
    # Paid history with no WHTAX (ratio 1.0): the estimate follows the real
    # history, so net equals gross. The default rate is only for no history.
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [100.0]})
    ext = {"ABC": {"ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 1),
                   "decl_date": dt.date(2026, 4, 15), "amount": 0.5, "currency": "USD"}}
    cal = dividends.build_calendar(positions, _quarterly_cash_tx(), pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=True, external=ext)
    assert cal.values.at["ABC", "2026-06"] == "≈50.00"


def test_zero_withholding_paid_is_real_cash_after_tax():
    # HAFN shape: IBKR posts a dividend with no WHTAX row. After tax is the cash
    # that landed, so it equals gross. No rate is assumed.
    cash_tx = pd.DataFrame([
        {"type": "PAYMENTINLIEU", "symbol": "HAFN", "amount": 3.88,
         "settle_date": dt.date(2026, 3, 13), "ex_date": dt.date(2026, 3, 6),
         "datetime": None, "report_date": None},
        {"type": "DIVIDEND", "symbol": "HAFN", "amount": 6.33,
         "settle_date": dt.date(2026, 6, 16), "ex_date": dt.date(2026, 6, 4),
         "datetime": None, "report_date": None},
    ])
    gross = dividends.build_calendar(_positions("HAFN"), cash_tx, pd.DataFrame(),
                                     today=dt.date(2026, 9, 14), net=False)
    net = dividends.build_calendar(_positions("HAFN"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 9, 14), net=True)
    assert gross.values.at["HAFN", "2026-03"] == "3.88"
    assert gross.values.at["HAFN", "2026-06"] == "6.33"
    assert net.values.at["HAFN", "2026-03"] == "3.88"
    assert net.values.at["HAFN", "2026-06"] == "6.33"
    assert net.paid_sum == pytest.approx(3.88 + 6.33)


def test_zero_withholding_accrual_is_ibkr_net_after_tax():
    # HAFN live accrual: grossAmount=11.01 netAmount=11.01 tax=0.
    accruals = pd.DataFrame([
        {"symbol": "HAFN", "ex_date": dt.date(2026, 9, 8), "pay_date": dt.date(2026, 9, 18),
         "gross_amount": 11.01, "net_amount": 11.01, "quantity": 22, "tax": 0.0},
    ])
    gross = dividends.build_calendar(_positions("HAFN"), pd.DataFrame(), accruals,
                                     today=dt.date(2026, 9, 14), net=False)
    net = dividends.build_calendar(_positions("HAFN"), pd.DataFrame(), accruals,
                                   today=dt.date(2026, 9, 14), net=True)
    assert gross.values.at["HAFN", "2026-09"] == "11.01"
    assert net.values.at["HAFN", "2026-09"] == "11.01"
    assert net.totals["2026-09"] == ""  # declared, unpaid -> not summed


def test_net_ratio_by_symbol():
    paid = {
        "ABC": {"2026-01": {"gross": 100.0, "net": 75.0},
                "2026-04": {"gross": 100.0, "net": 75.0}},
        "ZERO": {"2026-01": {"gross": 0.0, "net": 0.0}},          # zero gross
        "NEG": {"2026-01": {"gross": 100.0, "net": -5.0}},        # net <= 0
        "OVER": {"2026-01": {"gross": 100.0, "net": 120.0}},      # net > gross
    }
    ratios = dividends._net_ratio_by_symbol(paid)
    assert ratios == {"ABC": 0.75}


def test_flex_accrual_beats_polygon_same_month():
    # Flex is ground truth: a real accrual is never overridden by Polygon.
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [100.0]})
    accruals = pd.DataFrame([
        {"symbol": "ABC", "ex_date": dt.date(2026, 6, 1), "pay_date": dt.date(2026, 6, 15),
         "gross_amount": 100.0, "net_amount": 80.0, "quantity": 10, "tax": 20.0},
    ])
    ext = {"ABC": {"ex_date": dt.date(2026, 6, 1), "pay_date": dt.date(2026, 6, 15),
                   "decl_date": dt.date(2026, 1, 20), "amount": 0.5, "currency": "USD"}}
    cal = dividends.build_calendar(positions, pd.DataFrame(), accruals,
                                   today=dt.date(2026, 5, 1), net=False, external=ext)
    assert cal.status.at["ABC", "2026-06"] == "declared"
    assert cal.values.at["ABC", "2026-06"] == "100.00"     # Flex gross, not 50.00


def test_polygon_skipped_for_zero_or_short_shares():
    # No honest cash figure without a long position -> no fabricated amount;
    # falls back to the yfinance dates (red), never a yellow number.
    positions = pd.DataFrame({"symbol": ["COPX"], "quantity": [0.0]})
    cash_tx, accruals = _empty_flex()
    ext = {"COPX": {"ex_date": dt.date(2026, 6, 29), "pay_date": dt.date(2026, 7, 7),
                    "decl_date": dt.date(2026, 1, 20), "amount": 0.258, "currency": "USD"}}
    cal = dividends.build_calendar(positions, cash_tx, accruals,
                                   today=dt.date(2026, 6, 12), net=False, external=ext)
    assert "declared" not in cal.status.values             # no Polygon amount
    assert cal.status.at["COPX", "2026-06"] == "declare"   # yfinance red fallback


def test_external_without_amount_leaves_projection_intact():
    # No Polygon amount (e.g. no API key) -> red projection unchanged.
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [100.0]})
    ext = {"ABC": {"ex_date": dt.date(2026, 5, 20), "pay_date": dt.date(2026, 6, 1),
                   "history_months": []}}
    cal = dividends.build_calendar(positions, _quarterly_cash_tx(), pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=False, external=ext)
    assert cal.status.at["ABC", "2026-05"] == "declare"
    assert cal.status.at["ABC", "2026-06"] == "pay"
    assert "declared" not in cal.status.values


def test_external_same_month_dates_single_declare_cell():
    cash_tx, accruals = _empty_flex()
    ext = {"AAA": {"ex_date": dt.date(2026, 6, 10), "pay_date": dt.date(2026, 6, 25),
                   "history_months": []}}
    cal = dividends.build_calendar(_positions("AAA"), cash_tx, accruals,
                                   today=dt.date(2026, 6, 1), external=ext)
    assert cal.status.at["AAA", "2026-06"] == "declare"
    tip = cal.tooltips.at["AAA", "2026-06"]
    assert "10-06-2026" in tip and "25-06-2026" in tip


def test_external_stale_dates_fall_back_to_history_projection():
    # Published dates in the past must not render; semi-annual history
    # (Jun/Dec ex months) projects the next ex month instead.
    cash_tx, accruals = _empty_flex()
    ext = {"AAA": {"ex_date": dt.date(2025, 12, 29), "pay_date": dt.date(2026, 1, 6),
                   "history_months": ["2025-06", "2025-12"]}}
    cal = dividends.build_calendar(_positions("AAA"), cash_tx, accruals,
                                   today=dt.date(2026, 6, 12), external=ext)
    assert cal.status.at["AAA", "2026-06"] == "declare"
    assert "yfinance" in cal.tooltips.at["AAA", "2026-06"]
    assert "2026-01" not in cal.status.columns or (cal.status.loc["AAA"] == "declare").sum() == 1


def test_external_history_needs_two_months():
    cash_tx, accruals = _empty_flex()
    ext = {"AAA": {"ex_date": None, "pay_date": None, "history_months": ["2025-12"]}}
    cal = dividends.build_calendar(_positions("AAA"), cash_tx, accruals,
                                   today=dt.date(2026, 6, 12), external=ext)
    assert cal.empty  # single past month -> no cadence -> nothing to render


def test_external_never_overrides_flex_data():
    # Ticker has real paid history -> external info must be ignored entirely.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(2026, 3, 15), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None},
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(2026, 4, 15), "ex_date": dt.date(2026, 4, 1),
         "datetime": None, "report_date": None},
    ])
    ext = {"ABC": {"ex_date": dt.date(2026, 8, 1), "pay_date": dt.date(2026, 9, 1),
                   "history_months": ["2026-03", "2026-04"]}}
    cal = dividends.build_calendar(_positions("ABC"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 4, 20), external=ext)
    # Flex cadence (monthly) projects 2026-05; the external Aug/Sep dates must
    # not appear anywhere.
    assert cal.status.at["ABC", "2026-05"] == "declare"
    assert "2026-08" not in cal.status.columns
    assert "yfinance" not in "".join(cal.tooltips.loc["ABC"])


def test_external_mixed_with_flex_symbols():
    # ABC has Flex data; COPX only external. Both render; only COPX is backfilled.
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(2026, 5, 15), "ex_date": dt.date(2026, 5, 1),
         "datetime": None, "report_date": None},
    ])
    ext = {"COPX": {"ex_date": dt.date(2026, 6, 29), "pay_date": dt.date(2026, 7, 6),
                    "history_months": []}}
    cal = dividends.build_calendar(_positions("ABC", "COPX"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 6, 12), external=ext)
    assert cal.status.at["ABC", "2026-05"] == "paid"
    assert cal.status.at["COPX", "2026-06"] == "declare"
    assert cal.status.at["COPX", "2026-07"] == "pay"
    assert cal.no_data == set()


def test_external_empty_info_keeps_no_data_row():
    cash_tx = pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 10.0,
         "settle_date": dt.date(2026, 5, 15), "ex_date": dt.date(2026, 5, 1),
         "datetime": None, "report_date": None},
    ])
    ext = {"ZZZ": {"ex_date": None, "pay_date": None, "history_months": []}}
    cal = dividends.build_calendar(_positions("ABC", "ZZZ"), cash_tx, pd.DataFrame(),
                                   today=dt.date(2026, 6, 12), external=ext)
    assert "ZZZ" in cal.no_data


# ---- trade markers ------------------------------------------------------------

def _trades(rows):
    return pd.DataFrame([
        {"symbol": s, "trade_date": d, "datetime": None, "side": side, "quantity": q,
         "price": p, "proceeds": -q * p if side == "BUY" else q * p, "commission": -1.0,
         "currency": "USD"}
        for s, d, side, q, p in rows
    ])


def test_trade_marks_one_per_buy_day_in_its_month():
    trades = _trades([
        ("ABC", dt.date(2020, 1, 1), "BUY", 1.0, 10.0),
        ("ABC", dt.date(2020, 2, 2), "BUY", 1.0, 20.0),
        ("ABC", dt.date(2020, 2, 20), "SELL", -1.0, 25.0),
        ("XYZ", dt.date(2020, 1, 5), "BUY", 1.0, 5.0),      # not a calendar row
    ])
    marks = dividends.trade_marks(trades=trades, symbols=["ABC"],
                                  columns=["2020-01", "2020-02"], shares={"ABC": 1.0})
    assert set(marks) == {"ABC"}
    jan, feb = marks["ABC"]["2020-01"], marks["ABC"]["2020-02"]
    assert [(b["side"], b["date"], b["price"], b["amount"]) for b in jan] == [
        ("BUY", dt.date(2020, 1, 1), 10.0, 10.0)]
    assert [(b["side"], b["date"], b["price"]) for b in feb] == [
        ("BUY", dt.date(2020, 2, 2), 20.0), ("SELL", dt.date(2020, 2, 20), 25.0)]
    assert 0 < jan[0]["pos"] < feb[0]["pos"] < 1  # day 1 sits left of day 2 of 29


def test_trade_marks_merge_same_day_fills_with_average_price():
    trades = _trades([
        ("ABC", dt.date(2020, 1, 10), "BUY", 2.0, 10.0),
        ("ABC", dt.date(2020, 1, 10), "BUY", 2.0, 12.0),
    ])
    (buy,) = dividends.trade_marks(trades=trades, symbols=["ABC"], columns=["2020-01"],
                                  shares={"ABC": 4.0})["ABC"]["2020-01"]
    assert (buy["quantity"], buy["price"], buy["amount"], buy["commission"], buy["fills"]) == (
        4.0, 11.0, 44.0, 2.0, 2)


def test_trade_marks_skip_months_outside_the_window():
    trades = _trades([("ABC", dt.date(2019, 12, 31), "BUY", 1.0, 10.0)])
    assert dividends.trade_marks(trades=trades, symbols=["ABC"], columns=["2020-01"],
                               shares={"ABC": 1.0}) == {}



def test_trade_marks_keep_same_day_buy_and_sell_apart():
    trades = _trades([
        ("ABC", dt.date(2020, 1, 10), "BUY", 2.0, 10.0),
        ("ABC", dt.date(2020, 1, 10), "SELL", -1.0, 12.0),
    ])
    marks = dividends.trade_marks(trades=trades, symbols=["ABC"], columns=["2020-01"],
                                  shares={"ABC": 1.0})["ABC"]["2020-01"]
    assert sorted((m["side"], m["quantity"], m["amount"]) for m in marks) == [
        ("BUY", 2.0, 20.0), ("SELL", 1.0, 12.0)]


def test_trade_marks_sum_realized_pnl_of_sell_fills():
    trades = _trades([
        ("ABC", dt.date(2020, 1, 10), "SELL", -1.0, 12.0),
        ("ABC", dt.date(2020, 1, 10), "SELL", -1.0, 14.0),
    ]).assign(realized_pnl=[2.0, 4.0])
    (sell,) = dividends.trade_marks(trades=trades, symbols=["ABC"], columns=["2020-01"],
                                    shares={"ABC": 0.0})["ABC"]["2020-01"]
    assert (sell["side"], sell["quantity"], sell["price"], sell["realized_pnl"], sell["fills"]) == (
        "SELL", 2.0, 13.0, 6.0, 2)
    assert sell["holding"] == 0.0


def test_trade_marks_without_realized_pnl_column_leave_it_none():
    trades = _trades([("ABC", dt.date(2020, 1, 10), "SELL", -1.0, 12.0)])
    (sell,) = dividends.trade_marks(trades=trades, symbols=["ABC"], columns=["2020-01"],
                                    shares={"ABC": 0.0})["ABC"]["2020-01"]
    assert sell["realized_pnl"] is None

def test_extend_back_adds_empty_months_and_keeps_totals():
    cal = dividends.build_calendar(_positions("ABC"), _quarterly_cash_tx(), pd.DataFrame(),
                                   today=dt.date(2026, 4, 1), net=False, lookback_months=None)
    first = list(cal.values.columns)[0]
    earlier = dividends._add_months(first, -2)
    wide = dividends.extend_back(cal=cal, first_ym=earlier)
    assert list(wide.values.columns)[:3] == [earlier, dividends._add_months(first, -1), first]
    assert (wide.values[earlier] == "").all() and wide.totals[earlier] == ""
    assert wide.paid_sum == cal.paid_sum
    assert dividends.extend_back(cal=cal, first_ym=first) is cal


def test_first_buy_month_ignores_sells_and_other_symbols():
    trades = _trades([
        ("ABC", dt.date(2020, 3, 1), "BUY", 1.0, 10.0),
        ("ABC", dt.date(2020, 1, 1), "SELL", 1.0, 10.0),
        ("XYZ", dt.date(2019, 1, 1), "BUY", 1.0, 10.0),
    ])
    assert dividends.first_buy_month(trades=trades, symbols=["ABC"]) == "2020-03"


def test_trade_marks_holding_after_each_buy_counts_back_from_today():
    # Held 1 share today: bought 1 (Jan), bought 1 (Feb 2), sold 1 (Feb 20).
    trades = _trades([
        ("ABC", dt.date(2020, 1, 1), "BUY", 1.0, 10.0),
        ("ABC", dt.date(2020, 2, 2), "BUY", 1.0, 20.0),
        ("ABC", dt.date(2020, 2, 20), "SELL", -1.0, 25.0),
    ])
    marks = dividends.trade_marks(trades=trades, symbols=["ABC"],
                                columns=["2020-01", "2020-02"], shares={"ABC": 1.0})
    assert marks["ABC"]["2020-01"][0]["holding"] == 1.0
    assert marks["ABC"]["2020-02"][0]["holding"] == 2.0


def test_trade_marks_sold_ticker_counts_back_from_zero():
    trades = _trades([
        ("GONE", dt.date(2020, 1, 1), "BUY", 3.0, 10.0),
        ("GONE", dt.date(2020, 3, 1), "SELL", -3.0, 12.0),
    ])
    marks = dividends.trade_marks(trades=trades, symbols=["GONE"], columns=["2020-01"], shares={})
    assert marks["GONE"]["2020-01"][0]["holding"] == 3.0


@pytest.mark.parametrize(("value", "text"), [
    (17.0, "17"), (2.5, "2.5"), (2.056, "2.06"), (0.444, "0.44"), (1234.5, "1,234.5"), (0.001, "0"),
])
def test_format_qty_two_decimals_at_most(value, text):
    assert dividends.format_qty(value=value) == text


def test_calendar_carries_current_shares():
    positions = pd.DataFrame({"symbol": ["ABC"], "quantity": [12.3456]})
    cal = dividends.build_calendar(positions, _quarterly_cash_tx(), pd.DataFrame(),
                                   today=dt.date(2026, 4, 1))
    assert cal.shares == {"ABC": 12.3456}


# ---- sold tickers outside the shown months -----------------------------------

def _sold_calendar() -> dividends.Calendar:
    index, columns = ["HELD", "GONE", "PAID", "TRADED"], ["2026-05", "2026-06"]
    blank = pd.DataFrame("", index=index, columns=columns)
    values = blank.copy()
    values.loc["PAID", "2026-06"] = "1.50"
    return dividends.Calendar(
        values=values, status=blank.copy(), tooltips=blank.copy(), current_ym="2026-06",
        ticker_totals=pd.Series({"HELD": "", "GONE": "", "PAID": "1.50", "TRADED": ""}),
        declared_sum=0.0, sold={"GONE", "PAID", "TRADED"}, held_count=1, shares={"HELD": 3.0},
        totals=pd.Series({"2026-05": "", "2026-06": "1.50"}), paid_sum=1.5,
        no_data={"HELD"}, projected=set(),
    )


def test_sold_ticker_with_nothing_in_the_shown_months_is_dropped():
    """Sold before the first month shown: it was not in the portfolio then."""
    marks = {"TRADED": {"2026-05": [{"side": "SELL"}]}}
    out = dividends.drop_absent_sold(cal=_sold_calendar(), marks=marks)
    # A held ticker stays even with an empty row; a sold one needs a dividend
    # or a trade inside the months shown.
    assert list(out.values.index) == ["HELD", "PAID", "TRADED"]
    assert list(out.status.index) == list(out.tooltips.index) == ["HELD", "PAID", "TRADED"]
    assert out.sold == {"PAID", "TRADED"}
    assert "GONE" not in out.ticker_totals
    # No money moved: the dropped row had no cell.
    assert out.paid_sum == 1.5 and out.totals["2026-06"] == "1.50" and out.held_count == 1


def test_drop_absent_sold_returns_the_same_calendar_when_nothing_to_drop():
    cal = _sold_calendar()
    marks = {"GONE": {"2026-05": [{"side": "BUY"}]}, "TRADED": {"2026-05": [{"side": "SELL"}]}}
    assert dividends.drop_absent_sold(cal=cal, marks=marks) is cal
