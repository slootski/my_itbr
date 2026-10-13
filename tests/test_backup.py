"""Tests for the rotating SQLite backups (data/backup.py).

The store holds history IBKR can no longer re-serve, so the tests lean on the
failure modes that would actually lose data: pruning ahead of a good copy,
accepting a corrupt copy, or letting an error escape and take the app down.
"""
from __future__ import annotations

import gc
import sqlite3
import warnings

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
    assert list(backup_dir.iterdir()) == []      # no staging file, no pre-sync copy either


def test_stale_part_file_does_not_block_a_backup(db, backup_dir):
    backup_dir.mkdir(parents=True)
    (backup_dir / "snapshots-2026-08-11.sqlite.part").write_text("half-written junk")

    out = backup.run(db, backup_dir, today="2026-08-11")
    assert out is not None and out.exists()
    assert list(backup_dir.glob("*.part")) == []   # the stale staging file was swept


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


# ---- refresh() and the pre-sync copy ---------------------------------------

DAY = "2026-08-11"


def _names(backup_dir):
    return sorted(p.name for p in backup_dir.iterdir()) if backup_dir.is_dir() else []


def _nav_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM nav_snapshots").fetchone()[0]
    finally:
        conn.close()


def _add_day(db, day):
    snapshots.record_snapshot(db, net_liq=1.0, cash=1.0, currency="USD", snapshot_date=day)


def test_run_also_writes_the_pre_sync_copy_once(db, backup_dir):
    backup.run(db, backup_dir, today=DAY)
    assert _names(backup_dir) == [f"snapshots-{DAY}-presync.sqlite", f"snapshots-{DAY}.sqlite"]
    _add_day(db, "2026-08-12")
    assert backup.run(db, backup_dir, today=DAY) is None      # same day: nothing rewritten
    assert _nav_rows(backup_dir / f"snapshots-{DAY}-presync.sqlite") == 1
    assert _days(backup_dir) == [f"snapshots-{DAY}.sqlite"]   # days are counted, not files


def test_refresh_replaces_todays_file_and_leaves_the_pre_sync_copy(db, backup_dir):
    backup.run(db, backup_dir, today=DAY)
    _add_day(db, "2026-08-12")
    out = backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY)
    assert out == backup_dir / f"snapshots-{DAY}.sqlite"
    assert _nav_rows(out) == 2                                # the new state
    assert _nav_rows(backup_dir / f"snapshots-{DAY}-presync.sqlite") == 1
    assert list(backup_dir.glob("*.part")) == []


def test_refresh_first_then_run_still_keeps_a_pre_sync_copy(db, backup_dir):
    """A watchlist edit before the day's IBKR write must not cost the pre-sync state."""
    backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY)
    assert _names(backup_dir) == [f"snapshots-{DAY}.sqlite"]
    assert backup.run(db, backup_dir, today=DAY) is None      # the main file already exists
    assert f"snapshots-{DAY}-presync.sqlite" in _names(backup_dir)


def test_refresh_guards(tmp_path, db, backup_dir, monkeypatch):
    assert backup.refresh(db_path=tmp_path / "absent.sqlite", backup_dir=backup_dir,
                          keep=14, today=DAY) is None
    assert not backup_dir.exists()                            # nothing created for a missing DB
    good = backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY)
    monkeypatch.setattr(backup, "_verify", lambda path: False)
    assert backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY) is None
    assert good.exists() and list(backup_dir.glob("*.part")) == []   # the old copy survives

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(backup, "_write_copy", boom)
    assert backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY) is None   # never raises
    assert backup.run(db, backup_dir, today="2026-08-12") is None


def test_a_failed_pre_sync_copy_does_not_stop_the_main_copy(db, backup_dir, monkeypatch):
    real = backup._write_copy
    monkeypatch.setattr(backup, "_write_copy", lambda *, db_path, target: (
        False if "presync" in target.name else real(db_path=db_path, target=target)))
    assert backup.run(db, backup_dir, today=DAY) is not None
    assert _names(backup_dir) == [f"snapshots-{DAY}.sqlite"]


def test_prune_takes_a_days_pre_sync_copy_and_orphans(db, backup_dir):
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        backup.run(db, backup_dir, today=day)
    (backup_dir / "snapshots-2026-07-01-presync.sqlite").write_bytes(b"orphan")   # no main file
    backup.run(db, backup_dir, keep=2, today="2026-08-04")
    assert _names(backup_dir) == [
        "snapshots-2026-08-03-presync.sqlite", "snapshots-2026-08-03.sqlite",
        "snapshots-2026-08-04-presync.sqlite", "snapshots-2026-08-04.sqlite"]
    assert backup.status(backup_dir)["count"] == 2


def test_only_this_modules_staging_files_are_swept(db, backup_dir):
    backup_dir.mkdir(parents=True)
    (backup_dir / "movie.mkv.part").write_text("someone else's download")
    (backup_dir / "snapshots-2026-08-10.sqlite.abcd1234.part").write_text("crashed run")
    backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY)
    assert _names(backup_dir) == ["movie.mkv.part", f"snapshots-{DAY}.sqlite"]


def test_backup_directory_with_special_characters(db, tmp_path):
    odd = tmp_path / "back#ups?%41"
    out = backup.refresh(db_path=db, backup_dir=odd, keep=14, today=DAY)
    assert out is not None and backup._verify(out)


def test_backups_leave_no_connection_open(db, backup_dir):
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        backup.run(db, backup_dir, today=DAY)
        backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY)
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_restoring_an_old_backup_brings_back_the_old_watchlist(db, backup_dir):
    import datetime as dt
    import shutil

    from data import watchlist

    now = dt.datetime(2026, 8, 11, 10, 0, tzinfo=dt.timezone.utc)
    watchlist.init_db(db_path=db)
    watchlist.create_list(db_path=db, name="Old list", now=now)
    watchlist.write_refresh(db_path=db, interval_minutes=4, until=None)
    old = backup.refresh(db_path=db, backup_dir=backup_dir, keep=14, today=DAY)
    watchlist.create_list(db_path=db, name="New list", now=now)
    watchlist.write_refresh(db_path=db, interval_minutes=None, until=None)
    shutil.copy(old, db)
    assert list(watchlist.read_lists(db_path=db)["lists"]["name"]) == ["Old list"]
    assert watchlist.read_refresh(db_path=db) == {"interval_minutes": 4, "until": None}
