"""Tests for data.snapshots — SQLite NAV store."""
from __future__ import annotations

from data import snapshots


def test_record_and_read_roundtrip(db_path):
    snapshots.record_snapshot(db_path, net_liq=1000.0, cash=200.0, currency="USD",
                              snapshot_date="2024-01-01")
    df = snapshots.read_history(db_path)
    assert len(df) == 1
    assert float(df.iloc[0]["net_liq"]) == 1000.0
    assert df.iloc[0]["currency"] == "USD"


def test_same_day_dedupes_latest_wins(db_path):
    snapshots.record_snapshot(db_path, net_liq=1000.0, cash=200.0, currency="USD",
                              snapshot_date="2024-01-01")
    snapshots.record_snapshot(db_path, net_liq=1100.0, cash=210.0, currency="USD",
                              snapshot_date="2024-01-01")
    df = snapshots.read_history(db_path)
    assert len(df) == 1
    assert float(df.iloc[0]["net_liq"]) == 1100.0


def test_multiple_days_ordered(db_path):
    snapshots.record_snapshot(db_path, net_liq=1100.0, cash=0.0, currency="USD",
                              snapshot_date="2024-01-02")
    snapshots.record_snapshot(db_path, net_liq=1000.0, cash=0.0, currency="USD",
                              snapshot_date="2024-01-01")
    df = snapshots.read_history(db_path)
    assert len(df) == 2
    assert list(df["net_liq"]) == [1000.0, 1100.0]


def test_read_missing_returns_empty_typed_frame(tmp_path):
    df = snapshots.read_history(tmp_path / "nope.sqlite")
    assert df.empty
    assert list(df.columns) == ["snapshot_date", "ts", "net_liq", "cash", "currency"]
