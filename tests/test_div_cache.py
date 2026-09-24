"""Tests for data.div_cache — daily on-disk declared-dividend cache."""
from __future__ import annotations

import datetime as dt

from data import div_cache

_TODAY = dt.date(2026, 6, 12)


def _record():
    return {"ex_date": dt.date(2026, 6, 29), "pay_date": dt.date(2026, 7, 7),
            "decl_date": dt.date(2026, 1, 20), "amount": 0.258, "currency": "USD"}


def test_put_get_round_trips_record_with_dates(tmp_path):
    db = tmp_path / "c.sqlite"
    div_cache.put(db, "COPX", _record(), _TODAY)
    payload, fetched = div_cache.get(db, "COPX")
    assert fetched == _TODAY.isoformat()
    assert payload["amount"] == 0.258
    # ISO strings on disk decode back to real dates.
    assert payload["ex_date"] == dt.date(2026, 6, 29)
    assert payload["pay_date"] == dt.date(2026, 7, 7)
    assert payload["decl_date"] == dt.date(2026, 1, 20)


def test_get_missing_symbol_is_none(tmp_path):
    db = tmp_path / "c.sqlite"
    assert div_cache.get(db, "NOPE") is None


def test_empty_marker_round_trips(tmp_path):
    db = tmp_path / "c.sqlite"
    div_cache.put(db, "PLTR", {}, _TODAY)
    payload, fetched = div_cache.get(db, "PLTR")
    assert payload == {}
    assert fetched == _TODAY.isoformat()


def test_put_upserts_same_symbol(tmp_path):
    db = tmp_path / "c.sqlite"
    div_cache.put(db, "COPX", {}, dt.date(2026, 6, 1))
    div_cache.put(db, "COPX", _record(), _TODAY)
    payload, fetched = div_cache.get(db, "COPX")
    assert fetched == _TODAY.isoformat()       # newer day wins
    assert payload["amount"] == 0.258


def test_unwritable_db_degrades_to_miss(tmp_path):
    # A path under a file (not a dir) can't be opened -> best-effort no-op.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")
    db = not_a_dir / "c.sqlite"
    div_cache.put(db, "COPX", _record(), _TODAY)   # no raise
    assert div_cache.get(db, "COPX") is None       # no raise
