"""Tests for the append-only Flex archive (data/history.py).

The point of the archive is that data surviving *past* the rolling Flex window
is still there, so these tests lean on the case that matters: pull A contains
rows that pull B no longer does, and the read must return the union.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from data import history, snapshots


def _trades(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=history.TRADE_COLS)


def _cash_tx(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=history.CASH_TX_COLS)


def _trade(symbol="AAPL", day=1, qty=10.0, price=100.0, trade_id=None):
    date = dt.date(2025, 6, day)
    return {
        "symbol": symbol, "asset_class": "STK", "trade_date": date,
        "datetime": dt.datetime(2025, 6, day, 10, 0, 0), "side": "BUY",
        "quantity": qty, "price": price, "proceeds": -qty * price,
        "commission": -1.0, "realized_pnl": 0.0, "currency": "USD",
        "trade_id": trade_id, "transaction_id": None,
    }


def _tx(symbol="AAPL", day=20, amount=24.0, tx_id=None):
    return {
        "type": "DIVIDEND", "symbol": symbol, "amount": amount,
        "datetime": dt.datetime(2025, 9, day, 12, 0, 0),
        "report_date": dt.date(2025, 9, day), "settle_date": dt.date(2025, 9, day),
        "ex_date": dt.date(2025, 8, 15), "transaction_id": tx_id, "action_id": None,
    }


@pytest.fixture
def db(tmp_path):
    return tmp_path / "snapshots.sqlite"


# ---- the core promise: data outlives the rolling window --------------------

def test_rows_survive_after_leaving_the_flex_window(db):
    """An old trade stays readable once the statement no longer reports it."""
    history.record_trades(db, _trades([_trade(day=1), _trade(day=2)]))
    # Next pull: the window has rolled and day 1 is gone, day 3 has appeared.
    history.record_trades(db, _trades([_trade(day=2), _trade(day=3)]))

    out = history.read_trades(db)
    assert len(out) == 3
    assert [d.day for d in out["trade_date"]] == [1, 2, 3]


def test_cash_tx_survive_after_leaving_the_flex_window(db):
    history.record_cash_tx(db, _cash_tx([_tx(day=20), _tx(day=21, amount=25.0)]))
    history.record_cash_tx(db, _cash_tx([_tx(day=21, amount=25.0)]))

    out = history.read_cash_tx(db)
    assert len(out) == 2
    assert sorted(out["amount"]) == [24.0, 25.0]


# ---- idempotency and identity ---------------------------------------------

def test_repeated_pulls_do_not_duplicate(db):
    rows = _trades([_trade(day=1), _trade(day=2)])
    for _ in range(3):
        history.record_trades(db, rows)
    assert len(history.read_trades(db)) == 2


def test_ibkr_id_is_preferred_over_the_content_hash(db):
    """With a tradeID, a restated row updates in place instead of duplicating."""
    history.record_trades(db, _trades([_trade(trade_id="T-1", price=100.0)]))
    # IBKR restates the same trade with a corrected price — same id.
    history.record_trades(db, _trades([_trade(trade_id="T-1", price=101.0)]))

    out = history.read_trades(db)
    assert len(out) == 1
    assert out.iloc[0]["price"] == 101.0


def test_distinct_trades_are_kept_apart_without_ids(db):
    history.record_trades(db, _trades([
        _trade(symbol="AAPL", day=1), _trade(symbol="MSFT", day=1),
    ]))
    assert len(history.read_trades(db)) == 2


# ---- dtype fidelity: an archived frame must behave like a parsed one -------

def test_read_restores_date_and_datetime_types(db):
    history.record_trades(db, _trades([_trade(day=1)]))
    row = history.read_trades(db).iloc[0]

    assert isinstance(row["trade_date"], dt.date)
    assert not isinstance(row["trade_date"], dt.datetime)
    assert row["trade_date"] == dt.date(2025, 6, 1)
    # pandas promotes an all-datetime column to datetime64, exactly as the parser
    # path does, so Timestamp here is correct, not a regression.
    assert pd.Timestamp(row["datetime"]) == pd.Timestamp("2025-06-01 10:00:00")
    assert isinstance(row["quantity"], float)


def test_read_restores_cash_tx_types(db):
    history.record_cash_tx(db, _cash_tx([_tx()]))
    row = history.read_cash_tx(db).iloc[0]

    for col in ("report_date", "settle_date", "ex_date"):
        assert isinstance(row[col], dt.date)
    assert row["ex_date"] == dt.date(2025, 8, 15)


def test_fractional_quantity_survives_the_round_trip(db):
    """The whole reason the Position metric was wrong — do not round here."""
    history.record_trades(db, _trades([_trade(qty=0.2596)]))
    assert history.read_trades(db).iloc[0]["quantity"] == pytest.approx(0.2596)


# ---- empty / missing inputs ------------------------------------------------

def test_empty_frames_are_noops(db):
    assert history.record_trades(db, _trades([])) == 0
    assert history.record_cash_tx(db, _cash_tx([])) == 0
    assert history.record_trades(db, None) == 0


def test_read_on_missing_db_returns_empty_frame_with_columns(tmp_path):
    missing = tmp_path / "nope.sqlite"
    out = history.read_trades(missing)
    assert out.empty
    assert list(out.columns) == history.TRADE_COLS


def test_none_dates_round_trip_as_none(db):
    row = _trade()
    row["trade_date"] = None
    history.record_trades(db, _trades([row]))
    assert history.read_trades(db).iloc[0]["trade_date"] is None


# ---- NAV backfill ----------------------------------------------------------

def _equity(days: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"report_date": dt.date(2025, 6, d), "cash": 100.0 + d,
          "stock": 900.0, "net_liq": 1000.0 + d} for d in days],
        columns=["report_date", "cash", "stock", "net_liq"],
    )


def test_backfill_fills_days_the_dashboard_never_saw(db):
    history.backfill_nav(db, _equity([1, 2, 3]), "USD")
    out = snapshots.read_history(db)
    assert len(out) == 3
    assert list(out["net_liq"]) == [1001.0, 1002.0, 1003.0]


def test_backfill_never_overwrites_a_live_snapshot(db):
    """A row from a real pull is the better record of that day; keep it."""
    snapshots.record_snapshot(
        db, net_liq=12345.0, cash=1.0, currency="USD", snapshot_date="2025-06-02",
    )
    history.backfill_nav(db, _equity([1, 2, 3]), "USD")

    out = snapshots.read_history(db).set_index(
        snapshots.read_history(db)["snapshot_date"].dt.date
    )
    assert out.loc[dt.date(2025, 6, 2), "net_liq"] == 12345.0
    assert out.loc[dt.date(2025, 6, 1), "net_liq"] == 1001.0


def test_backfill_is_idempotent(db):
    history.backfill_nav(db, _equity([1, 2]), "USD")
    history.backfill_nav(db, _equity([1, 2]), "USD")
    assert len(snapshots.read_history(db)) == 2


def test_backfill_on_empty_equity_is_a_noop(db):
    assert history.backfill_nav(db, pd.DataFrame(), "USD") == 0
    assert history.backfill_nav(db, None, "USD") == 0


# ---- coverage --------------------------------------------------------------

def test_coverage_reports_extent(db):
    history.record_trades(db, _trades([_trade(day=1), _trade(day=5)]))
    history.record_cash_tx(db, _cash_tx([_tx()]))

    cover = history.coverage(db)
    assert cover["trades"] == 2
    assert cover["first_trade"] == "2025-06-01"
    assert cover["cash_tx"] == 1


def test_coverage_on_missing_db_is_safe(tmp_path):
    assert history.coverage(tmp_path / "nope.sqlite")["trades"] == 0


def test_migration_adds_currency_columns_to_an_existing_store(tmp_path):
    """A store created before the currency columns gains them on open."""
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE cash_tx_archive ("
            " tx_key TEXT PRIMARY KEY, type TEXT, symbol TEXT, amount REAL,"
            " datetime TEXT, report_date TEXT, settle_date TEXT, ex_date TEXT,"
            " transaction_id TEXT, action_id TEXT, first_seen TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cash_tx_archive VALUES"
            " ('ib:t1','DEPOSITWITHDRAW',NULL,4000.0,NULL,NULL,'2026-01-05',NULL,"
            " 't1',NULL,'2026-01-05')"
        )

    history.init_db(db)
    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(cash_tx_archive)")}
    assert {"currency", "fx_rate", "amount_base"} <= cols


def test_refresh_backfills_currency_onto_pre_migration_rows(tmp_path):
    """The row key ignores the new columns, so a re-pull updates in place."""
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE cash_tx_archive ("
            " tx_key TEXT PRIMARY KEY, type TEXT, symbol TEXT, amount REAL,"
            " datetime TEXT, report_date TEXT, settle_date TEXT, ex_date TEXT,"
            " transaction_id TEXT, action_id TEXT, first_seen TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cash_tx_archive VALUES"
            " ('ib:t1','DEPOSITWITHDRAW',NULL,4000.0,NULL,NULL,'2026-01-05',NULL,"
            " 't1',NULL,'2026-01-05')"
        )

    fresh = pd.DataFrame([{
        "type": "DEPOSITWITHDRAW", "symbol": None, "amount": 4000.0,
        "currency": "ILS", "fx_rate": 0.27, "amount_base": 1080.0,
        "datetime": None, "report_date": None,
        "settle_date": dt.date(2026, 1, 5), "ex_date": None,
        "transaction_id": "t1", "action_id": None,
    }])
    history.record_cash_tx(db, fresh)

    out = history.read_cash_tx(db)
    assert len(out) == 1  # updated, not duplicated
    assert out["amount_base"].iloc[0] == pytest.approx(1080.0)
    assert out["currency"].iloc[0] == "ILS"
    with sqlite3.connect(db) as conn:
        seen = conn.execute("SELECT first_seen FROM cash_tx_archive").fetchone()[0]
    assert seen == "2026-01-05"  # original date preserved through the update
