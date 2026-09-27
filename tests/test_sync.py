"""Tests for data.sync — insert-only sync of a statement into the store."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from data import history, snapshots, sync
from ibkr import parser

TODAY = "2026-09-27"
NOW = "2026-09-27T10:00:00"
NAV = {"net_liq": 10500.0, "cash": 600.0, "currency": "USD"}
WINDOW = sync.make_window(requested=None, returned_from=dt.date(2024, 1, 1),
                          returned_to=dt.date(2024, 12, 31))


@pytest.fixture
def frames(stmt):
    return {
        "trades": parser.trades_df(stmt),
        "cash_tx": parser.cash_transactions_df(stmt),
        "equity": parser.equity_summary_df(stmt),
    }


def _plan(db_path, frames, nav):
    return sync.plan(db_path=db_path, trades=frames["trades"], cash_tx=frames["cash_tx"],
                     equity=frames["equity"], nav=nav, window=WINDOW, today=TODAY, now=NOW)


def _sync(db_path, frames, nav):
    report = _plan(db_path, frames, nav)
    report["written"] = sync.apply(db_path=db_path, report=report, today=TODAY)
    return report


def _count(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_empty_db_gets_everything(db_path, frames):
    report = _sync(db_path, frames, NAV)
    tables = report["tables"]
    assert len(tables["trades"]["missing"]) == 1
    assert len(tables["cash_tx"]["missing"]) == 6
    # Two statement days plus today's live snapshot.
    assert len(tables["nav_snapshots"]["missing"]) == 3
    assert report["written"] == 10
    assert _count(db_path, "trades_archive") == 1
    assert _count(db_path, "cash_tx_archive") == 6
    assert _count(db_path, "nav_snapshots") == 3


def test_second_sync_writes_nothing(db_path, frames):
    _sync(db_path, frames, NAV)
    report = _sync(db_path, frames, NAV)
    assert sync.pending(report=report) == 0
    assert sync.differences(report=report) == 0
    assert report["written"] == 0


def test_db_matching_old_upsert_path_is_in_sync(db_path, frames):
    """Rows archived by history.record_* must be recognised — same row keys."""
    history.record_trades(db_path, frames["trades"], today=TODAY)
    history.record_cash_tx(db_path, frames["cash_tx"], today=TODAY)
    history.backfill_nav(db_path, frames["equity"], "USD")
    report = _plan(db_path, frames, NAV)
    assert report["tables"]["trades"]["missing"] == []
    assert report["tables"]["cash_tx"]["missing"] == []
    # Only today's live snapshot is new.
    assert [m["key"] for m in report["tables"]["nav_snapshots"]["missing"]] == [TODAY]


def test_only_missing_rows_are_added(db_path, frames):
    history.record_cash_tx(db_path, frames["cash_tx"].iloc[:4], today="2024-01-01")
    report = _sync(db_path, frames, NAV)
    assert len(report["tables"]["cash_tx"]["missing"]) == 2
    assert _count(db_path, "cash_tx_archive") == 6
    with sqlite3.connect(db_path) as conn:
        seen = {r[0] for r in conn.execute("SELECT first_seen FROM cash_tx_archive")}
    assert seen == {"2024-01-01", TODAY}  # old rows untouched


def test_missing_nav_day_added(db_path, frames):
    history.backfill_nav(db_path, frames["equity"].iloc[:1], "USD")
    report = _sync(db_path, frames, NAV)
    keys = [m["key"] for m in report["tables"]["nav_snapshots"]["missing"]]
    assert keys == ["2024-12-31", TODAY]


def test_null_field_is_filled_not_overwritten(db_path, frames):
    _sync(db_path, frames, NAV)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cash_tx_archive SET currency = NULL WHERE symbol IS NULL "
                     "AND amount = 4000")
    report = _sync(db_path, frames, NAV)
    fills = report["tables"]["cash_tx"]["fills"]
    assert [(f["column"], f["value"]) for f in fills] == [("currency", "ILS")]
    assert report["written"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cash_tx_archive WHERE currency IS NULL"
                            ).fetchone()[0] == 0


def test_differing_value_is_reported_not_written(db_path, frames):
    _sync(db_path, frames, NAV)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cash_tx_archive SET amount = 99 WHERE amount = 24")
        conn.execute("UPDATE nav_snapshots SET net_liq = 1 WHERE snapshot_date = '2024-12-30'")
    report = _sync(db_path, frames, NAV)
    diffs = {(d["table"], d["column"], d["db"], d["ibkr"])
             for t in report["tables"].values() for d in t["diffs"]}
    assert diffs == {("cash_tx_archive", "amount", 99.0, 24.0),
                     ("nav_snapshots", "net_liq", 1.0, 10000.0)}
    assert report["written"] == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cash_tx_archive WHERE amount = 99"
                            ).fetchone()[0] == 1


def test_live_nav_row_is_not_a_difference(db_path, frames):
    """A live-pull row records the pull-time figure; IBKR's end-of-day value differs."""
    snapshots.record_snapshot(db_path, net_liq=1.0, cash=1.0, currency="USD",
                              snapshot_date="2024-12-30", ts="2024-12-30T15:30:00")
    report = _plan(db_path, frames, NAV)
    assert report["tables"]["nav_snapshots"]["diffs"] == []


def test_todays_live_row_is_not_overwritten(db_path, frames):
    _sync(db_path, frames, NAV)
    _sync(db_path, frames, {**NAV, "net_liq": 1.0})
    with sqlite3.connect(db_path) as conn:
        value = conn.execute("SELECT net_liq FROM nav_snapshots WHERE snapshot_date = ?",
                             (TODAY,)).fetchone()[0]
    assert value == 10500.0


def test_duplicate_keys_in_statement_count_once(db_path, frames):
    doubled = dict(frames, trades=pd.concat([frames["trades"], frames["trades"]]))
    report = _sync(db_path, doubled, NAV)
    assert len(report["tables"]["trades"]["missing"]) == 1
    assert _count(db_path, "trades_archive") == 1


def test_write_failure_rolls_back(db_path, frames):
    report = _plan(db_path, frames, NAV)
    # nav_snapshots.ts is NOT NULL; NAV rows are written last, after the archives.
    report["tables"]["nav_snapshots"]["missing"][0]["row"]["ts"] = None
    with pytest.raises(sqlite3.Error):
        sync.apply(db_path=db_path, report=report, today=TODAY)
    assert _count(db_path, "trades_archive") == 0
    assert _count(db_path, "cash_tx_archive") == 0


def test_make_window_counts_days():
    window = sync.make_window(requested=(dt.date(2025, 9, 26), dt.date(2026, 9, 25)),
                              returned_from=dt.date(2025, 9, 26), returned_to=dt.date(2026, 9, 25))
    assert window["days"] == 365
    assert window["requested_from"] == "2025-09-26"
