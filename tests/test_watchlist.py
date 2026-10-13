"""Tests for data.watchlist: the store, the table/alert helpers and the schedule.

No network and no clock: every time is passed in, and every DB is in tmp_path.
"""
from __future__ import annotations

import datetime as dt
import gc
import sqlite3
import warnings
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from data import history, snapshots, watchlist

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "watch.sqlite"
    watchlist.init_db(db_path=path)
    return path


def _list(db, name: str) -> int:
    return watchlist.create_list(db_path=db, name=name, now=NOW)


def _add(db, list_id: int, symbol: str, buy: float | None, sell: float | None) -> None:
    watchlist.add_item(db_path=db, list_id=list_id, symbol=symbol,
                       buy_target=buy, sell_target=sell, now=NOW)


def _items(db) -> pd.DataFrame:
    return watchlist.read_items(db_path=db)["items"]


def _lists(db) -> pd.DataFrame:
    return watchlist.read_lists(db_path=db)["lists"]


def _sql(path, query: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


def _quote(price: float | None, previous: float | None) -> dict | None:
    if price is None:
        return None
    return {"price": price, "previous_close": previous, "currency": "USD"}


def _raises(text: str):
    return pytest.raises(watchlist.WatchlistError, match=f"^{text}$".replace("(", r"\(").replace(")", r"\)")
                         .replace(".", r"\.").replace("?", r"\?"))


# ---- lists -----------------------------------------------------------------

def test_create_rename_delete_list(db):
    quantum = _list(db, "  Quantum ")
    assert list(_lists(db)["name"]) == ["Quantum"]            # trimmed
    watchlist.rename_list(db_path=db, list_id=quantum, name="Qubits")
    assert list(_lists(db)["name"]) == ["Qubits"]
    assert watchlist.delete_list(db_path=db, list_id=quantum) == 0
    assert _lists(db).empty


@pytest.mark.parametrize("second", ["quantum", "QUANTUM", " Quantum  "])
def test_duplicate_list_name_is_refused_in_any_case(db, second):
    _list(db, "Quantum")
    with _raises('A list named "Quantum" already exists.' if second.strip() == "Quantum"
                 else f'A list named "{second.strip()}" already exists.'):
        _list(db, second)


def test_duplicate_list_name_is_refused_across_accent_forms(db):
    _list(db, "Café")                                   # NFC
    with pytest.raises(watchlist.WatchlistError, match="already exists"):
        _list(db, "Café")                              # NFD of the same name
    with pytest.raises(watchlist.WatchlistError, match="already exists"):
        _list(db, "CAFÉ")


@pytest.mark.parametrize(("name", "text"), [
    ("", "Type a name."), ("   ", "Type a name."), (" \t", "Type a name."),
    ("x" * 41, "Use 40 characters or fewer."),
])
def test_bad_list_names_are_refused(db, name, text):
    with _raises(text):
        _list(db, name)
    assert _lists(db).empty


def test_rename_rules(db):
    quantum, _ = _list(db, "Quantum"), _list(db, "Metals")
    with pytest.raises(watchlist.WatchlistError, match='A list named "metals" already exists'):
        watchlist.rename_list(db_path=db, list_id=quantum, name="metals")
    watchlist.rename_list(db_path=db, list_id=quantum, name="QUANTUM")   # own name, other case
    assert sorted(_lists(db)["name"]) == ["Metals", "QUANTUM"]


@pytest.mark.parametrize("name", ['He said "buy"', "50% off; DROP TABLE x", "🚀 rockets", "O'Neil"])
def test_odd_list_names_round_trip(db, name):
    _list(db, name)
    assert list(_lists(db)["name"]) == [name]


def test_delete_list_removes_only_its_own_stocks(db):
    quantum, metals = _list(db, "Quantum"), _list(db, "Metals")
    _add(db, quantum, "QTUM", 150.0, None)
    _add(db, quantum, "IONQ", None, None)
    _add(db, metals, "COPX", None, 60.0)
    assert list(_lists(db)["count"]) == [1, 2]              # ordered by name: Metals, Quantum
    assert watchlist.delete_list(db_path=db, list_id=quantum) == 2
    assert list(_items(db)["symbol"]) == ["COPX"]


def test_writes_on_a_missing_list_say_so(db):
    with _raises(watchlist.MSG_LIST_GONE):
        watchlist.rename_list(db_path=db, list_id=99, name="X")
    with _raises(watchlist.MSG_LIST_GONE):
        watchlist.delete_list(db_path=db, list_id=99)
    with _raises(watchlist.MSG_LIST_GONE):
        _add(db, 99, "QTUM", None, None)


# ---- items -----------------------------------------------------------------

def test_add_update_move_remove(db):
    quantum, metals = _list(db, "Quantum"), _list(db, "Metals")
    _add(db, quantum, " qtum ", 150, np.float64(175.0))      # int and numpy inputs
    row = _items(db).iloc[0]
    assert (row["symbol"], row["list_name"], row["buy_target"], row["sell_target"]) == (
        "QTUM", "Quantum", 150.0, 175.0)
    assert _sql(db, "SELECT typeof(buy_target), typeof(sell_target) FROM watchlist_items") == [
        ("real", "real")]
    watchlist.update_item(db_path=db, symbol="QTUM", list_id=metals, buy_target=None, sell_target=180.0)
    row = _items(db).iloc[0]
    assert row["list_name"] == "Metals" and pd.isna(row["buy_target"]) and row["sell_target"] == 180.0
    watchlist.update_item(db_path=db, symbol="QTUM", list_id=metals, buy_target=1.0, sell_target=2.0)
    watchlist.update_item(db_path=db, symbol="QTUM", list_id=metals, buy_target=3.0, sell_target=4.0)
    assert _items(db).iloc[0]["buy_target"] == 3.0           # two saves: the last wins
    watchlist.remove_item(db_path=db, symbol="QTUM")
    assert _items(db).empty


def test_duplicate_stock_is_refused_with_the_list_it_is_in(db):
    quantum, etfs = _list(db, "Quantum"), _list(db, "ETFs")
    _add(db, etfs, "QTUM", None, None)
    with _raises('QTUM is already in "ETFs".'):
        _add(db, etfs, "qtum", None, None)
    with _raises('QTUM is already in "ETFs". Open that list and use '
                 "Edit / move / remove to move it here."):
        _add(db, quantum, "QTUM", None, None)


def test_another_spelling_of_a_watched_stock_is_refused(db):
    quantum = _list(db, "Quantum")
    _add(db, quantum, "BRK-B", None, None)
    with _raises('BRK-B is already in "Quantum".'):
        _add(db, quantum, "brk.b", None, None)
    _add(db, quantum, "7203.T", None, None)
    assert watchlist.duplicate_text(items=_items(db), symbol="7203-T", list_id=quantum) == (
        '7203.T is already in "Quantum".')
    assert watchlist.duplicate_text(items=_items(db), symbol="IONQ", list_id=quantum) is None


def test_writes_on_a_missing_stock_or_target_list_say_so(db):
    quantum = _list(db, "Quantum")
    with _raises("QTUM is no longer on the watchlist."):
        watchlist.update_item(db_path=db, symbol="QTUM", list_id=quantum, buy_target=None, sell_target=None)
    with _raises("QTUM is no longer on the watchlist."):
        watchlist.remove_item(db_path=db, symbol="QTUM")
    _add(db, quantum, "QTUM", None, None)
    with _raises(watchlist.MSG_LIST_GONE):
        watchlist.update_item(db_path=db, symbol="QTUM", list_id=99, buy_target=None, sell_target=None)


@pytest.mark.parametrize(("buy", "sell", "text"), [
    (0.0, None, "A target must be a number above 0."),
    (-1.0, None, "A target must be a number above 0."),
    (float("nan"), None, "A target must be a number above 0."),
    (None, float("inf"), "A target must be a number above 0."),
    (10.0, 10.0, "The buy target must be below the sell target."),
    (11.0, 10.0, "The buy target must be below the sell target."),
])
def test_bad_targets_are_refused_and_nothing_is_saved(db, buy, sell, text):
    quantum = _list(db, "Quantum")
    with _raises(text):
        _add(db, quantum, "QTUM", buy, sell)
    assert _items(db).empty
    _add(db, quantum, "QTUM", 5.0, 6.0)
    with _raises(text):
        watchlist.update_item(db_path=db, symbol="QTUM", list_id=quantum, buy_target=buy, sell_target=sell)
    assert _items(db).iloc[0]["buy_target"] == 5.0


@pytest.mark.parametrize("sql", [
    "INSERT INTO watchlist_items VALUES ('qtum', 'qtum', 1, NULL, NULL, 'x')",       # lower case
    "INSERT INTO watchlist_items VALUES ('QTUM', 'X', 1, NULL, NULL, 'x')",          # wrong key
    "INSERT INTO watchlist_items VALUES ('QTUM', 'QTUM', 1, 'abc', NULL, 'x')",      # text target
    "INSERT INTO watchlist_items VALUES ('QTUM', 'QTUM', 1, 9e999, NULL, 'x')",      # infinity
    "INSERT INTO watchlist_items VALUES ('QTUM', 'QTUM', 1, 5, 4, 'x')",             # buy above sell
    "INSERT INTO watchlist_items VALUES ('QTUM', 'QTUM', 99, NULL, NULL, 'x')",      # no such list
    "INSERT INTO watchlist_refresh VALUES (1, 1.5, NULL)",
    "INSERT INTO watchlist_refresh VALUES (2, 1, NULL)",
    "INSERT INTO watchlist_refresh VALUES (1, NULL, '2026-01-01T00:00:00+00:00')",
    "INSERT INTO watchlists (name, name_key, created) VALUES (' padded', 'p', 'x')",
])
def test_the_schema_refuses_bad_rows_written_with_raw_sql(db, sql):
    _list(db, "Quantum")
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    finally:
        conn.close()


def test_an_item_whose_list_is_gone_can_be_moved_or_removed(db):
    quantum, metals = _list(db, "Quantum"), _list(db, "Metals")
    _add(db, quantum, "QTUM", 1.0, None)
    _add(db, quantum, "IONQ", None, None)
    conn = sqlite3.connect(db)           # foreign keys off: the list goes, its items stay
    conn.execute("DELETE FROM watchlists WHERE id = ?", (quantum,))
    conn.commit()
    conn.close()
    items = _items(db)
    assert list(items["symbol"]) == ["IONQ", "QTUM"] and items["list_name"].isna().all()
    with _raises(watchlist.MSG_LIST_GONE):                 # still pointing at the dead list
        watchlist.update_item(db_path=db, symbol="QTUM", list_id=quantum, buy_target=2.0, sell_target=None)
    watchlist.update_item(db_path=db, symbol="QTUM", list_id=metals, buy_target=1.0, sell_target=None)
    watchlist.remove_item(db_path=db, symbol="IONQ")
    assert list(_items(db)["list_name"]) == ["Metals"]


# ---- connection, files, failures -------------------------------------------

def test_foreign_keys_are_on_and_a_raw_delete_cascades(db):
    quantum = _list(db, "Quantum")
    _add(db, quantum, "QTUM", None, None)
    conn = watchlist._write_conn(db_path=db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("DELETE FROM watchlists WHERE id = ?", (quantum,))
        conn.commit()
    finally:
        conn.close()
    assert _items(db).empty


def test_no_connection_is_left_open(db):
    gc.collect()                                             # leftovers of earlier tests
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        quantum = _list(db, "Quantum")
        _add(db, quantum, "QTUM", 1.0, 2.0)
        watchlist.update_item(db_path=db, symbol="QTUM", list_id=quantum, buy_target=None, sell_target=None)
        _items(db), _lists(db), watchlist.read_refresh(db_path=db)
        watchlist.write_refresh(db_path=db, interval_minutes=1, until=None)
        watchlist.remove_item(db_path=db, symbol="QTUM")
        watchlist.delete_list(db_path=db, list_id=quantum)
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_init_db_adds_tables_to_an_existing_store_and_changes_no_row(tmp_path):
    path = tmp_path / "store.sqlite"
    snapshots.init_db(path)
    history.init_db(path)
    snapshots.record_snapshot(path, net_liq=1.0, cash=1.0, currency="USD",
                              snapshot_date="2026-10-01", ts="2026-10-01T00:00:00")

    def counts() -> dict:
        names = [r[0] for r in _sql(
            path, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: _sql(path, f"SELECT COUNT(*) FROM {n}")[0][0] for n in names}

    before = counts()
    watchlist.init_db(db_path=path)
    watchlist.init_db(db_path=path)                          # twice changes nothing
    after = counts()
    assert {"watchlists", "watchlist_items", "watchlist_refresh"} <= set(after)
    assert {name: after[name] for name in before} == before


def test_init_db_creates_a_missing_directory(tmp_path):
    path = tmp_path / "new" / "deeper" / "store.sqlite"
    watchlist.init_db(db_path=path)
    assert path.exists()


def test_reads_never_create_anything(tmp_path):
    path = tmp_path / "absent" / "store.sqlite"
    lists = watchlist.read_lists(db_path=path)
    assert lists["lists"].empty and lists["error"] is False
    assert watchlist.read_items(db_path=path)["items"].empty
    assert watchlist.read_refresh(db_path=path) == {"interval_minutes": None, "until": None}
    assert not path.parent.exists()


def test_reads_on_a_store_without_the_tables_are_empty_not_errors(tmp_path):
    path = tmp_path / "store.sqlite"
    snapshots.init_db(path)
    assert watchlist.read_lists(db_path=path)["error"] is False
    assert watchlist.read_items(db_path=path) ["error"] is False
    assert watchlist.read_items(db_path=path)["items"].empty


@pytest.mark.parametrize("folder", ["has?mark", "has#hash", "pct%41x", "with space"])
def test_reads_open_the_exact_file_when_the_path_has_special_characters(tmp_path, folder):
    home = tmp_path / folder
    path = home / "s n?a#p%25.sqlite"
    watchlist.init_db(db_path=path)
    _list(path, "Quantum")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    assert list(_lists(path)["name"]) == ["Quantum"]
    assert sorted(p.name for p in tmp_path.rglob("*")) == before   # no other file appeared


def test_reads_work_on_a_read_only_file_and_writes_say_busy(db):
    quantum = _list(db, "Quantum")
    db.chmod(0o444)
    try:
        assert list(_lists(db)["name"]) == ["Quantum"]
        with _raises(watchlist.MSG_BUSY):
            _add(db, quantum, "QTUM", None, None)
    finally:
        db.chmod(0o644)


def test_a_failed_write_leaves_the_tables_as_they_were(db, monkeypatch):
    quantum = _list(db, "Quantum")
    _add(db, quantum, "QTUM", None, None)
    real = watchlist._write_conn

    class Breaking(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM watchlists"):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args)

    def conn(*, db_path):
        opened = sqlite3.connect(db_path, timeout=0.1, factory=Breaking)
        opened.execute("PRAGMA foreign_keys = ON")
        return opened

    monkeypatch.setattr(watchlist, "_write_conn", conn)
    with _raises(watchlist.MSG_NOT_SAVED):                  # an unexpected error: generic text
        watchlist.delete_list(db_path=db, list_id=quantum)
    monkeypatch.setattr(watchlist, "_write_conn", real)
    assert list(_items(db)["symbol"]) == ["QTUM"] and len(_lists(db)) == 1


def test_a_locked_store_says_busy_on_write_and_flags_reads(db, monkeypatch):
    monkeypatch.setattr(watchlist, "_BUSY_SECONDS", 0.1)
    quantum = _list(db, "Quantum")
    other = sqlite3.connect(db, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")                    # blocks writers, not readers
        with _raises(watchlist.MSG_BUSY):
            _add(db, quantum, "QTUM", None, None)
        with _raises(watchlist.MSG_BUSY):
            watchlist.write_refresh(db_path=db, interval_minutes=1, until=None)
        assert watchlist.read_lists(db_path=db)["error"] is False
        other.execute("ROLLBACK")
        other.execute("BEGIN EXCLUSIVE")                    # blocks readers too
        assert watchlist.read_lists(db_path=db)["error"] is True
        assert watchlist.read_items(db_path=db)["error"] is True
        other.execute("ROLLBACK")
    finally:
        other.close()


# ---- text rules ------------------------------------------------------------

@pytest.mark.parametrize(("text", "symbol", "key"), [
    (" qtum ", "QTUM", "QTUM"), ("brk.b", "BRK.B", "BRK-B"), ("BRK B", "BRK-B", "BRK-B"),
    ("7203.T", "7203.T", "7203-T"), ("^gspc", "^GSPC", "^GSPC"), ("btc-usd", "BTC-USD", "BTC-USD"),
    ("eurusd=x", "EURUSD=X", "EURUSD=X"),
])
def test_clean_symbol_and_match_key(text, symbol, key):
    assert watchlist.clean_symbol(text=text) == symbol
    assert watchlist.match_key(symbol=text) == key


@pytest.mark.parametrize(("text", "message"), [
    ("", "Type a ticker."), ("   ", "Type a ticker."),
    ("A" * 21, "That is not a valid ticker. Use letters, digits and . - ^ ="),
    ("QT$UM", "That is not a valid ticker. Use letters, digits and . - ^ ="),
    ("QT/UM", "That is not a valid ticker. Use letters, digits and . - ^ ="),
])
def test_clean_symbol_refuses(text, message):
    with pytest.raises(watchlist.WatchlistError) as caught:
        watchlist.clean_symbol(text=text)
    assert str(caught.value) == message


# ---- distance, alerts, table -----------------------------------------------

@pytest.mark.parametrize(("price", "target", "side", "expected"), [
    (152.4, 150.0, "buy", pytest.approx(1.6)), (150.0, 150.0, "buy", 0.0), (140.0, 150.0, "buy", 0.0),
    (152.4, 175.0, "sell", pytest.approx(12.9143, abs=1e-3)), (175.0, 175.0, "sell", 0.0),
    (180.0, 175.0, "sell", 0.0), (None, 150.0, "buy", None), (152.4, None, "sell", None),
])
def test_distance_left(price, target, side, expected):
    assert watchlist.distance_left(price=price, target=target, side=side) == expected


@pytest.mark.parametrize(("price", "buy", "sell", "expected"), [
    (41.1, 42.0, None, "buy"), (42.0, 42.0, None, "buy"), (43.0, 42.0, None, None),
    (61.3, None, 60.0, "sell"), (60.0, None, 60.0, "sell"), (59.0, None, 60.0, None),
    (50.0, 40.0, 60.0, None), (None, 42.0, 60.0, None), (50.0, None, None, None),
])
def test_alert_side(price, buy, sell, expected):
    assert watchlist.alert_side(price=price, buy_target=buy, sell_target=sell) == expected


@pytest.fixture
def three(db):
    quantum, metals = _list(db, "Quantum"), _list(db, "Metals")
    _add(db, quantum, "QTUM", 150.0, 175.0)
    _add(db, quantum, "IONQ", 42.0, None)
    _add(db, quantum, "RGTI", 9.0, None)
    _add(db, quantum, "BRK-B", None, None)
    _add(db, metals, "COPX", None, 60.0)
    quotes = {"QTUM": _quote(152.4, 150.9), "IONQ": _quote(41.1, None), "RGTI": None,
              "BRK-B": _quote(500.0, 0.0) | {"previous_close": None},
              "COPX": _quote(61.3, 61.0) | {"currency": "GBp"}}
    return {"db": db, "quantum": quantum, "metals": metals, "quotes": quotes}


def test_table_rows_numbers_and_order(three):
    items = _items(three["db"])
    positions = pd.DataFrame({"symbol": ["QTUM", "BRK B", "AAPL"], "quantity": [1.78, 2.0, 10.0]})
    frame = watchlist.table(items=items[items["list_id"] == three["quantum"]],
                            quotes=three["quotes"], positions=positions)
    assert list(frame["symbol"]) == ["IONQ", "BRK-B", "QTUM", "RGTI"]     # alert first, then symbol
    assert list(frame["alert"]) == ["Buy target reached", "", "", ""]
    by = frame.set_index("symbol")
    assert by.loc["IONQ", "above_buy"] == 0.0 and pd.isna(by.loc["IONQ", "day_pct"])   # no previous close
    assert by.loc["QTUM", "above_buy"] == pytest.approx(1.6)
    assert by.loc["QTUM", "below_sell"] == pytest.approx(12.9143, abs=1e-3)
    assert by.loc["QTUM", "day_pct"] == pytest.approx((152.4 - 150.9) / 150.9 * 100)
    assert by.loc["QTUM", "shares"] == 1.78
    assert by.loc["BRK-B", "shares"] == 2.0                  # IBKR spells it "BRK B"
    assert pd.isna(by.loc["RGTI", "price"]) and pd.isna(by.loc["RGTI", "above_buy"])
    assert pd.isna(by.loc["IONQ", "shares"])
    for col in ("price", "day_pct", "buy_target", "above_buy", "sell_target", "below_sell", "shares"):
        assert pd.api.types.is_float_dtype(frame[col]), col   # numbers, so sorting is numeric


def test_table_with_no_positions_frame(three):
    frame = watchlist.table(items=_items(three["db"]), quotes=three["quotes"], positions=pd.DataFrame())
    assert frame["shares"].isna().all()


def test_alerts_lines_missing_and_counts(three):
    found = watchlist.alerts(items=_items(three["db"]), quotes=three["quotes"])
    assert [(l["side"], l["symbol"], l["list_name"], l["target"], l["currency"]) for l in found["lines"]] == [
        ("buy", "IONQ", "Quantum", 42.0, "USD"), ("sell", "COPX", "Metals", 60.0, "GBp")]
    assert found["missing"] == ["RGTI"]                      # has a target, has no price
    assert found["all_failed"] is False
    assert found["by_list"] == {three["quantum"]: 1, three["metals"]: 1}
    assert watchlist.first_alert(items=_items(three["db"]), quotes=three["quotes"]) == {
        "list_id": three["quantum"], "symbol": "IONQ"}


def test_alerts_missing_names_only_stocks_with_a_target(three):
    quotes = dict(three["quotes"], **{"BRK-B": None})        # BRK-B has no target
    assert watchlist.alerts(items=_items(three["db"]), quotes=quotes)["missing"] == ["RGTI"]


def test_alerts_when_every_quote_failed_or_nothing_is_reached(three):
    items = _items(three["db"])
    none = watchlist.alerts(items=items, quotes={symbol: None for symbol in items["symbol"]})
    assert none["all_failed"] is True and none["lines"] == []
    calm = watchlist.alerts(items=items, quotes={s: _quote(160.0, 160.0) for s in ("QTUM", "BRK-B")}
                            | {"IONQ": _quote(50.0, 50.0), "RGTI": _quote(10.0, 10.0), "COPX": _quote(1.0, 1.0)})
    assert calm["lines"] == [] and calm["all_failed"] is False and calm["by_list"] == {}
    assert watchlist.first_alert(items=items, quotes={}) is None
    assert watchlist.alerts(items=_items(three["db"]).iloc[0:0], quotes={})["all_failed"] is False


# ---- selection -------------------------------------------------------------

ORDER = ["IONQ", "QTUM", "RGTI"]


@pytest.mark.parametrize(("stored", "seen", "order_seen", "order", "symbol", "expected"), [
    (None, None, [], ORDER, None, None),                            # first render: no stock
    ([], [], ORDER, ORDER, None, None),                             # still no stock
    ([1], [], ORDER, ORDER, None, "QTUM"),                          # the first click
    (None, None, [], ORDER, "QTUM", "QTUM"),                        # set by a test or a callback
    ([1], [1], ORDER, ORDER, "QTUM", "QTUM"),                       # nothing changed (also after a sort)
    ([2], [1], ORDER, ORDER, "QTUM", "RGTI"),                       # the user clicked row 2
    ([2], [1], ORDER, ["RGTI", "IONQ", "QTUM"], "QTUM", "RGTI"),    # click read with last run's order
    ([1], [1], ORDER, ["IONQ", "RGTI"], "QTUM", None),              # stock gone: no stock
    ([5], [1], ORDER, ORDER, "QTUM", "QTUM"),                       # stale row number
    ([0], [0], ORDER, [], "IONQ", None),                            # empty list
    ([], [1], ORDER, ORDER, "QTUM", None),                          # the user cleared the mark
])
def test_resolve_selection(stored, seen, order_seen, order, symbol, expected):
    assert watchlist.resolve_selection(stored_rows=stored, rows_seen=seen, order_seen=order_seen,
                                       order=order, symbol=symbol) == expected


# ---- schedule --------------------------------------------------------------

ONCE = {"interval_minutes": None, "until": None}
ALWAYS = {"interval_minutes": 4, "until": None}
UNTIL = NOW + dt.timedelta(hours=4)
TIMED = {"interval_minutes": 1, "until": UNTIL}
BERLIN = ZoneInfo("Europe/Berlin")


def test_is_active_and_refresh_seconds():
    assert watchlist.is_active(setting=ONCE, now=NOW) is False
    assert watchlist.refresh_seconds(setting=ONCE, now=NOW) is None
    assert watchlist.refresh_seconds(setting=TIMED, now=NOW) == 60
    assert watchlist.is_active(setting=TIMED, now=UNTIL - dt.timedelta(seconds=1)) is True
    assert watchlist.is_active(setting=TIMED, now=UNTIL) is False           # exactly at the end
    assert watchlist.refresh_seconds(setting=TIMED, now=UNTIL + dt.timedelta(days=1)) is None
    assert watchlist.refresh_seconds(setting=ALWAYS, now=NOW + dt.timedelta(days=900)) == 240


def test_a_naive_now_is_refused():
    with pytest.raises(ValueError):
        watchlist.is_active(setting=TIMED, now=dt.datetime(2026, 10, 12, 10, 0))


def test_quote_bucket_per_session_or_per_window():
    assert watchlist.quote_bucket(setting=ONCE, now=NOW, session="s1", nonce=0) == "s1|0"
    assert watchlist.quote_bucket(setting=ONCE, now=NOW, session="s2", nonce=0) != "s1|0"
    assert watchlist.quote_bucket(setting=ONCE, now=NOW, session="s1", nonce=1) == "s1|1"
    edge = dt.datetime(2026, 10, 12, 10, 4, tzinfo=UTC)      # 4-minute windows start on :00, :04 …
    before, at, after = (watchlist.quote_bucket(setting=ALWAYS, now=edge + dt.timedelta(seconds=d),
                                                session="s1", nonce=0) for d in (-1, 0, 1))
    assert before != at == after
    assert at == watchlist.quote_bucket(setting=ALWAYS, now=edge, session="other", nonce=0)   # shared
    assert at != watchlist.quote_bucket(setting=ALWAYS, now=edge, session="s1", nonce=1)
    assert watchlist.quote_bucket(setting=TIMED, now=UNTIL, session="s1", nonce=0) == "s1|0"   # ended


def test_tick_should_rerun():
    def tick(*, setting, now, seen, dialog):
        return watchlist.tick_should_rerun(setting=setting, now=now, session="s1", nonce=0,
                                           bucket_seen=seen, dialog_open=dialog)

    seen = watchlist.quote_bucket(setting=TIMED, now=NOW, session="s1", nonce=0)
    assert tick(setting=TIMED, now=NOW + dt.timedelta(seconds=30), seen=seen, dialog=False) is False
    assert tick(setting=TIMED, now=NOW + dt.timedelta(seconds=60), seen=seen, dialog=False) is True
    assert tick(setting=TIMED, now=NOW + dt.timedelta(seconds=60), seen=seen, dialog=True) is False
    assert tick(setting=TIMED, now=UNTIL, seen=seen, dialog=False) is True     # ended: one last redraw
    assert tick(setting=ONCE, now=NOW, seen="s1|0", dialog=False) is False


def test_dialog_open():
    assert watchlist.dialog_open(keys={"wl_dialog", "x"}) is True
    assert watchlist.dialog_open(keys={"connect_dialog_open"}) is True
    assert watchlist.dialog_open(keys={"perf_day_open"}) is True
    assert watchlist.dialog_open(keys={"wl_symbol", "wl_list"}) is False
    assert watchlist.dialog_open(keys=set()) is False


def test_labels():
    assert watchlist.refresh_label(setting=ONCE, now=NOW, tz=BERLIN) == "only when I open the app"
    assert watchlist.refresh_label(setting=TIMED, now=NOW, tz=BERLIN) == "every 1 min until 16:00"
    assert watchlist.refresh_label(setting=TIMED, now=UNTIL, tz=BERLIN) == "only when I open the app"
    assert watchlist.refresh_label(setting=ALWAYS, now=NOW, tz=BERLIN) == "every 4 min, no end time"
    late = dt.datetime(2026, 10, 12, 21, 50, tzinfo=UTC)     # 23:50 in Berlin
    tomorrow = {"interval_minutes": 1, "until": late + dt.timedelta(hours=2, minutes=12)}
    assert watchlist.refresh_label(setting=tomorrow, now=late, tz=BERLIN) == "every 1 min until 13 Oct 02:02"
    assert watchlist.fetched_label(fetched=NOW, now=NOW, tz=BERLIN) == "12:00"
    assert watchlist.fetched_label(fetched=NOW, now=NOW + dt.timedelta(days=1), tz=BERLIN) == "12 Oct 12:00"
    assert watchlist.requests_per_hour(stocks=12, interval_minutes=1) == 720
    assert watchlist.requests_per_hour(stocks=3, interval_minutes=4) == 45


@pytest.mark.parametrize(("zone", "start"), [
    ("Europe/Berlin", dt.datetime(2026, 10, 25, 0, 30)),     # clocks go back at 03:00
    ("America/New_York", dt.datetime(2026, 3, 8, 0, 30)),    # clocks go forward at 02:00
])
def test_four_hours_is_four_elapsed_hours_across_a_clock_change(zone, start):
    tz = ZoneInfo(zone)
    now = start.replace(tzinfo=tz).astimezone(UTC)
    setting = {"interval_minutes": 1, "until": now + dt.timedelta(hours=4)}
    assert watchlist.is_active(setting=setting, now=now + dt.timedelta(hours=3, minutes=59)) is True
    assert watchlist.is_active(setting=setting, now=now + dt.timedelta(hours=4)) is False
    shown = setting["until"].astimezone(tz)
    assert (shown.hour, shown.minute) != (4, 30)             # the wall clock moved by the hour


def test_local_tz_is_a_real_zone():
    assert watchlist.local_tz() is not None
    assert watchlist.utc_now().tzinfo is not None


def test_refresh_setting_round_trip(db):
    assert watchlist.read_refresh(db_path=db) == ONCE
    watchlist.write_refresh(db_path=db, interval_minutes=1, until=UNTIL)
    assert watchlist.read_refresh(db_path=db) == {"interval_minutes": 1, "until": UNTIL}
    later = UNTIL + dt.timedelta(hours=1)                    # saving again starts a new period
    watchlist.write_refresh(db_path=db, interval_minutes=1, until=later)
    assert watchlist.read_refresh(db_path=db)["until"] == later
    watchlist.write_refresh(db_path=db, interval_minutes=4, until=None)
    assert watchlist.read_refresh(db_path=db) == ALWAYS
    watchlist.write_refresh(db_path=db, interval_minutes=None, until=None)
    assert watchlist.read_refresh(db_path=db) == ONCE
    with pytest.raises(watchlist.WatchlistError):
        watchlist.write_refresh(db_path=db, interval_minutes=None, until=UNTIL)
    with pytest.raises(ValueError):
        watchlist.write_refresh(db_path=db, interval_minutes=1, until=dt.datetime(2026, 1, 1))


@pytest.mark.parametrize("stored", ["not a date", "2026-10-12T10:00:00"])   # garbage; no offset
def test_an_unreadable_end_time_means_only_when_opened(db, stored):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO watchlist_refresh VALUES (1, 1, ?)", (stored,))
    conn.commit()
    conn.close()
    assert watchlist.read_refresh(db_path=db) == ONCE


# ---- dialog start values ---------------------------------------------------

def test_dialog_state_gives_every_field_its_start_value():
    assert watchlist.dialog_state(kind="new_list", payload={}) == {"wl_dlg_name": ""}
    assert watchlist.dialog_state(kind="rename_list", payload={"name": "Quantum"}) == {"wl_dlg_name": "Quantum"}
    assert watchlist.dialog_state(kind="delete_list", payload={}) == {}
    assert watchlist.dialog_state(kind="add_stock", payload={}) == {
        "wl_dlg_ticker": "", "wl_dlg_buy": None, "wl_dlg_sell": None}
    assert watchlist.dialog_state(kind="edit_stock", payload={
        "list_id": 3, "buy_target": 150.0, "sell_target": None}) == {
        "wl_dlg_list": 3, "wl_dlg_buy": 150.0, "wl_dlg_sell": None}
    assert watchlist.dialog_state(kind="edit_stock", payload={
        "list_id": None, "buy_target": float("nan"), "sell_target": 2.0})["wl_dlg_buy"] is None
    assert watchlist.dialog_state(kind="schedule", payload=ONCE) == {
        "wl_dlg_mode": "once", "wl_dlg_minutes": 1, "wl_dlg_duration": "hours", "wl_dlg_hours": 4}
    assert watchlist.dialog_state(kind="schedule", payload=TIMED) == {
        "wl_dlg_mode": "every", "wl_dlg_minutes": 1, "wl_dlg_duration": "hours", "wl_dlg_hours": 4}
    assert watchlist.dialog_state(kind="schedule", payload=ALWAYS)["wl_dlg_duration"] == "always"
    with pytest.raises(ValueError):
        watchlist.dialog_state(kind="nope", payload={})
    assert set(watchlist.DIALOG_KINDS) == {"new_list", "rename_list", "delete_list", "add_stock",
                                           "edit_stock", "schedule"}
