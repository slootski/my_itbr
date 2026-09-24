"""Tests for the rotating SQLite backups (data/backup.py).

The store holds history IBKR can no longer re-serve, so the tests lean on the
failure modes that would actually lose data: pruning ahead of a good copy,
accepting a corrupt copy, or letting an error escape and take the app down.
"""
from __future__ import annotations

import sqlite3

import pytest

from data import backup, history, snapshots


@pytest.fixture
def db(tmp_path):
    """A real store with a row in it, so backups have content to preserve."""
    path = tmp_path / "snapshots.sqlite"
    snapshots.record_snapshot(
        path, net_liq=1000.0, cash=100.0, currency="USD", snapshot_date="2026-08-11",
    )
    history.init_db(path)
    return path


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"


def _days(backup_dir):
    return [p.name for p in backup.existing(backup_dir)]


# ---- the basic contract ----------------------------------------------------

def test_writes_a_dated_backup(db, backup_dir):
    out = backup.run(db, backup_dir, today="2026-08-11")
    assert out is not None
    assert out.name == "snapshots-2026-08-11.sqlite"
    assert out.exists()


def test_backup_content_is_readable_and_complete(db, backup_dir):
    out = backup.run(db, backup_dir, today="2026-08-11")
    restored = snapshots.read_history(out)
    assert len(restored) == 1
    assert restored.iloc[0]["net_liq"] == 1000.0


def test_second_run_same_day_is_a_noop(db, backup_dir):
    first = backup.run(db, backup_dir, today="2026-08-11")
    assert backup.run(db, backup_dir, today="2026-08-11") is None
    assert _days(backup_dir) == [first.name]


def test_missing_source_db_is_a_noop(tmp_path, backup_dir):
    assert backup.run(tmp_path / "nope.sqlite", backup_dir) is None
    assert not backup_dir.exists()


# ---- rotation --------------------------------------------------------------

def test_prunes_to_keep_newest(db, backup_dir):
    for day in range(1, 8):
        backup.run(db, backup_dir, keep=3, today=f"2026-08-0{day}")
    assert _days(backup_dir) == [
        "snapshots-2026-08-05.sqlite",
        "snapshots-2026-08-06.sqlite",
        "snapshots-2026-08-07.sqlite",
    ]


def test_prune_ignores_unrelated_files(db, backup_dir):
    backup_dir.mkdir(parents=True)
    keeper = backup_dir / "notes.txt"
    keeper.write_text("mine")
    for day in range(1, 5):
        backup.run(db, backup_dir, keep=2, today=f"2026-08-0{day}")

    assert keeper.exists()
    assert len(_days(backup_dir)) == 2


def test_a_failed_backup_never_prunes_good_ones(db, backup_dir, monkeypatch):
    """Prune must run only after a verified new copy exists."""
    backup.run(db, backup_dir, keep=1, today="2026-08-01")
    monkeypatch.setattr(backup, "_verify", lambda path: False)

    assert backup.run(db, backup_dir, keep=1, today="2026-08-02") is None
    assert _days(backup_dir) == ["snapshots-2026-08-01.sqlite"]


def test_unverifiable_copy_is_not_left_behind(db, backup_dir, monkeypatch):
    monkeypatch.setattr(backup, "_verify", lambda path: False)
    backup.run(db, backup_dir, today="2026-08-11")

    assert _days(backup_dir) == []
    assert list(backup_dir.glob("*.part")) == []


def test_stale_part_file_does_not_block_a_backup(db, backup_dir):
    backup_dir.mkdir(parents=True)
    (backup_dir / "snapshots-2026-08-11.sqlite.part").write_text("half-written junk")

    out = backup.run(db, backup_dir, today="2026-08-11")
    assert out is not None and out.exists()


# ---- verification ----------------------------------------------------------

def test_verify_rejects_a_corrupt_file(tmp_path):
    junk = tmp_path / "corrupt.sqlite"
    junk.write_bytes(b"this is not a database")
    assert backup._verify(junk) is False


def test_verify_accepts_a_real_database(db):
    assert backup._verify(db) is True


# ---- errors never escape ---------------------------------------------------

def test_sqlite_error_is_swallowed(db, backup_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup.sqlite3, "connect", boom)
    assert backup.run(db, backup_dir, today="2026-08-11") is None


def test_os_error_is_swallowed(db, backup_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(backup.Path, "mkdir", boom)
    assert backup.run(db, backup_dir, today="2026-08-11") is None


# ---- status ----------------------------------------------------------------

def test_status_reports_latest(db, backup_dir):
    backup.run(db, backup_dir, today="2026-08-01")
    backup.run(db, backup_dir, today="2026-08-09")

    state = backup.status(backup_dir)
    assert state["count"] == 2
    assert state["latest_date"] == "2026-08-09"


def test_status_on_missing_dir_is_safe(tmp_path):
    assert backup.status(tmp_path / "nope") == {
        "count": 0, "latest": None, "latest_date": None,
    }
