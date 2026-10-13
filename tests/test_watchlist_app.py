"""App-level tests of the Watchlist tab, the Overview alert box and the price schedule.

Driven through ``AppTest`` on a tmp database. yfinance is stubbed (conftest), the
watchlist clock is fixed, and stocks are selected by setting ``wl_symbol`` or
with the ▲ ▼ buttons, because ``AppTest`` cannot click a table row.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from data import backup, prices, watchlist
from ibkr import flex
from tests.test_app_smoke import (  # noqa: F401 — smoke_app is a fixture
    APP_PATH, _FAKE_OHLC, _flex_code_boom, _texts, _wire_app, smoke_app,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 12, 10, 7, tzinfo=UTC)
USD = {"previous_close": None, "currency": "USD"}


class Watch:
    """The app under test plus what the test controls: quotes, clock, the DB."""

    def __init__(self, *, at: AppTest, db_path: Path, ctx: dict) -> None:
        self.at, self.db, self.ctx = at, db_path, ctx
        self.quotes: dict[str, dict | None] = {}
        self.quote_calls: list[tuple[str, ...]] = []
        self.now = NOW

    def seed(self, *, lists: dict[str, list[tuple[str, float | None, float | None]]]) -> dict[str, int]:
        watchlist.init_db(db_path=self.db)
        ids = {}
        for name, stocks in lists.items():
            ids[name] = watchlist.create_list(db_path=self.db, name=name, now=NOW)
            for symbol, buy, sell in stocks:
                watchlist.add_item(db_path=self.db, list_id=ids[name], symbol=symbol,
                                   buy_target=buy, sell_target=sell, now=NOW)
        return ids

    def run(self) -> AppTest:
        self.at.run()
        assert not self.at.exception, f"app raised: {[e.value for e in self.at.exception]}"
        return self.at

    def click(self, key: str) -> AppTest:
        self.at.button(key=key).click()
        return self.run()

    def has(self, key: str) -> bool:
        return any(button.key == key for button in self.at.button)

    def items(self) -> pd.DataFrame:
        return watchlist.read_items(db_path=self.db)["items"]

    def lists(self) -> list[str]:
        return list(watchlist.read_lists(db_path=self.db)["lists"]["name"])

    def table(self) -> pd.DataFrame:
        return next(d.value for d in self.at.dataframe if "To buy" in d.value.columns)

    def tab(self):
        return next(tab for tab in self.at.tabs if tab.label == "Watchlist")

    def metrics(self) -> dict:
        return {m.label: (m.value, m.delta) for m in self.tab().metric}

    def texts(self) -> list[str]:
        return _texts(self.at)

    def dialog_text(self) -> list[str]:
        return [str(w.value) for w in self.at.warning if "Prices could not be fetched" not in str(w.value)]


def _wire(monkeypatch, watch: Watch) -> Watch:
    def fake_quotes(symbols, workers):
        watch.quote_calls.append(tuple(symbols))
        return {symbol: watch.quotes[symbol] if symbol in watch.quotes else None for symbol in symbols}

    monkeypatch.setattr(prices, "fetch_quotes", fake_quotes)
    monkeypatch.setattr(prices, "fetch_quote",
                        lambda symbol: watch.quotes[symbol] if symbol in watch.quotes else None)
    monkeypatch.setattr(watchlist, "utc_now", lambda: watch.now)
    monkeypatch.setattr(watchlist, "local_tz", lambda: UTC)
    return watch


@pytest.fixture
def watch(smoke_app, monkeypatch):
    at, ctx = smoke_app
    return _wire(monkeypatch, Watch(at=at, db_path=ctx["db_path"], ctx=ctx))


def _q(price: float, previous: float | None) -> dict:
    return {"price": price, "previous_close": previous, "currency": "USD"}


# ---- empty states ----------------------------------------------------------

def test_no_lists_shows_the_start_text_and_asks_yfinance_nothing(watch):
    at = watch.run()
    assert "You have no watchlists yet. Create one to start watching stocks." in [i.value for i in at.info]
    assert watch.has("wl_new_list") and not watch.has("wl_schedule") and not watch.has("wl_add_stock")
    assert watch.quote_calls == []
    assert not [m for m in at.markdown if "Watchlist alerts" in m.value or "Watchlist prices" in m.value]


def test_create_rename_delete_a_list(watch):
    watch.run()
    at = watch.click("wl_new_list")
    assert at.text_input(key="wl_dlg_name").value == ""
    at.text_input(key="wl_dlg_name").set_value("Quantum")
    at = watch.click("wl_dlg_save")
    assert watch.lists() == ["Quantum"]
    assert '"Quantum" is empty. Add a stock to see its price and set targets.' in [i.value for i in at.info]
    assert not watch.has("wl_schedule")                       # nothing watched: no schedule line
    edit = at.button(key="wl_edit_stock")                     # an empty list: shown, plain and disabled
    assert edit.label == "Edit / move / remove" and edit.disabled and edit.proto.type == "secondary"
    assert at.button(key="wl_add_stock").proto.type == "secondary" and not at.button(key="wl_add_stock").disabled

    at = watch.click("wl_rename_list")
    assert at.text_input(key="wl_dlg_name").value == "Quantum"    # opens with the current name
    at.text_input(key="wl_dlg_name").set_value("Qubits")
    watch.click("wl_dlg_save")
    assert watch.lists() == ["Qubits"]
    at = watch.click("wl_rename_list")                        # again, right after a close
    assert at.text_input(key="wl_dlg_name").value == "Qubits"
    watch.click("wl_dlg_cancel")

    at = watch.click("wl_delete_list")
    assert 'Delete list "Qubits"? It is empty.' in watch.texts()
    watch.click("wl_dlg_cancel")
    assert watch.lists() == ["Qubits"]                        # cancel deletes nothing
    watch.click("wl_delete_list")
    at = watch.click("wl_dlg_save")
    assert watch.lists() == []
    assert "You have no watchlists yet. Create one to start watching stocks." in [i.value for i in at.info]


@pytest.mark.parametrize(("typed", "text"), [
    ("", "Type a name."), ("x" * 41, "Use 40 characters or fewer."),
    ("metals", 'A list named "metals" already exists.'),
])
def test_a_refused_list_name_keeps_the_dialog_open(watch, typed, text):
    watch.seed(lists={"Metals": []})
    watch.run()
    at = watch.click("wl_new_list")
    at.text_input(key="wl_dlg_name").set_value(typed)
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == [text]
    assert watch.has("wl_dlg_save") and watch.lists() == ["Metals"]


def test_delete_list_names_its_stocks(watch):
    stocks = [(f"S{i:02d}", None, None) for i in range(12)]
    watch.seed(lists={"Big": stocks, "One": [("QTUM", None, None)]})
    watch.run()
    watch.click("wl_delete_list")
    shown = ", ".join(f"S{i:02d}" for i in range(10))
    assert (f'Delete list "Big"? It holds 12 stocks: {shown} and 2 more. Their buy and sell '
            "targets are deleted too. This cannot be undone.") in watch.texts()
    watch.click("wl_dlg_save")
    assert watch.lists() == ["One"] and list(watch.items()["symbol"]) == ["QTUM"]


# ---- add ---------------------------------------------------------------------

def _open_add(watch, *, ticker: str, buy: float | None, sell: float | None) -> AppTest:
    at = watch.click("wl_add_stock")
    at.text_input(key="wl_dlg_ticker").set_value(ticker)
    at.number_input(key="wl_dlg_buy").set_value(buy)
    at.number_input(key="wl_dlg_sell").set_value(sell)
    return at


def test_add_a_stock(watch, monkeypatch):
    watch.seed(lists={"Quantum": []})
    watch.quotes = {"BRK-B": _q(515.62, 511.05)}
    asked: list[str] = []

    def resolve(text):
        asked.append(text)
        return {"status": "ok", "symbol": "BRK-B"}          # yfinance's spelling of BRK.B

    monkeypatch.setattr(prices, "resolve_symbol", resolve)
    watch.run()
    _open_add(watch, ticker=" brk.b ", buy=500.0, sell=None)
    at = watch.click("wl_dlg_save")
    assert asked == ["BRK.B"]
    row = watch.items().iloc[0]
    assert (row["symbol"], row["buy_target"]) == ("BRK-B", 500.0) and pd.isna(row["sell_target"])
    assert not watch.has("wl_dlg_save")                       # the dialog closed
    assert at.session_state["wl_symbol"] == "BRK-B"
    assert watch.metrics()["Current price (delayed)"][0] == "515.62 USD"


def test_add_and_add_another_keeps_the_dialog_and_empties_it(watch):
    watch.seed(lists={"Quantum": []})
    watch.quotes = {"QTUM": _q(152.06, 150.0)}
    watch.run()
    _open_add(watch, ticker="qtum", buy=None, sell=None)
    at = watch.click("wl_dlg_add_another")
    assert "Added QTUM at 152.06 USD." in watch.texts()
    assert at.text_input(key="wl_dlg_ticker").value == "" and at.number_input(key="wl_dlg_buy").value is None
    at.text_input(key="wl_dlg_ticker").set_value("RGTI")
    watch.click("wl_dlg_add_another")
    assert "Added RGTI (no price yet)." in watch.texts()
    watch.click("wl_dlg_cancel")
    assert list(watch.items()["symbol"]) == ["QTUM", "RGTI"] and not watch.has("wl_dlg_save")


@pytest.mark.parametrize(("ticker", "buy", "sell", "status", "text", "asks"), [
    ("", None, None, "ok", "Type a ticker.", 0),
    ("QT$UM", None, None, "ok", "That is not a valid ticker. Use letters, digits and . - ^ =", 0),
    ("QTUMM", None, None, "unknown", '"QTUMM" was not found on yfinance. Check the ticker.', 1),
    ("RGTI", None, None, "unreachable", 'Could not check "RGTI" (yfinance did not answer). Try again.', 1),
    ("ionq", None, None, "ok", 'IONQ is already in "Quantum".', 0),
    ("copx", None, None, "ok",
     'COPX is already in "Metals". Open that list and use Edit / move / remove to move it here.', 0),
    ("brk.b", None, None, "ok", 'BRK-B is already in "Quantum".', 0),
    ("NEW", 0.0, None, "ok", "A target must be a number above 0.", 0),
    ("NEW", 10.0, 5.0, "ok", "Buy below must be lower than Sell above.", 0),
])
def test_a_refused_stock_keeps_the_dialog_open_and_adds_nothing(
        watch, monkeypatch, ticker, buy, sell, status, text, asks):
    watch.seed(lists={"Quantum": [("IONQ", None, None), ("BRK-B", None, None)],
                      "Metals": [("COPX", None, None)]})
    asked: list[str] = []

    def resolve(text):
        asked.append(text)
        return {"status": status, "symbol": text if status == "ok" else None}

    monkeypatch.setattr(prices, "resolve_symbol", resolve)
    watch.run()
    watch.at.session_state["wl_list"] = watchlist.read_lists(db_path=watch.db)["lists"].set_index(
        "name").loc["Quantum", "id"].item()
    watch.run()
    _open_add(watch, ticker=ticker, buy=buy, sell=sell)
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == [text]
    assert len(asked) == asks                                 # yfinance asked only when needed
    assert watch.has("wl_dlg_save") and len(watch.items()) == 3


# ---- edit, move, remove ------------------------------------------------------

@pytest.fixture
def two_lists(watch):
    ids = watch.seed(lists={"Quantum": [("IONQ", None, None), ("QTUM", 150.0, 175.0), ("RGTI", None, None)],
                            "ETFs": []})
    watch.quotes = {"IONQ": _q(41.10, 42.0), "QTUM": _q(152.40, 150.90), "RGTI": None}
    watch.run()
    watch.at.session_state["wl_list"] = ids["Quantum"]
    watch.at.session_state["wl_symbol"] = "QTUM"
    watch.run()
    return watch, ids


def test_edit_opens_with_the_saved_values_and_saves_changes(two_lists):
    watch, ids = two_lists
    at = watch.click("wl_edit_stock")
    assert "**QTUM** · 152.40 USD now" in [m.value for m in at.markdown]
    assert at.selectbox(key="wl_dlg_list").value == ids["Quantum"]
    assert (at.number_input(key="wl_dlg_buy").value, at.number_input(key="wl_dlg_sell").value) == (150.0, 175.0)
    assert at.number_input(key="wl_dlg_buy").label == "Buy below (USD)"
    watch.click("wl_dlg_save")                                # saved untouched: nothing changes
    row = watch.items().set_index("symbol").loc["QTUM"]
    assert (row["buy_target"], row["sell_target"]) == (150.0, 175.0) and not watch.at.toast

    at = watch.click("wl_edit_stock")
    at.number_input(key="wl_dlg_buy").set_value(140.0)
    at.number_input(key="wl_dlg_sell").set_value(None)        # emptied = no target
    at = watch.click("wl_dlg_save")
    row = watch.items().set_index("symbol").loc["QTUM"]
    assert row["buy_target"] == 140.0 and pd.isna(row["sell_target"])
    assert [t.value for t in at.toast] == ["Saved targets for QTUM."]
    at = watch.click("wl_edit_stock")                         # right after a close: still prefilled
    assert (at.number_input(key="wl_dlg_buy").value, at.number_input(key="wl_dlg_sell").value) == (140.0, None)


@pytest.mark.parametrize(("buy", "sell", "text"), [
    (-5.0, None, "A target must be a number above 0."),
    (200.0, 175.0, "Buy below must be lower than Sell above."),
])
def test_edit_refuses_bad_targets(two_lists, buy, sell, text):
    watch, _ = two_lists
    at = watch.click("wl_edit_stock")
    at.number_input(key="wl_dlg_buy").set_value(buy)
    at.number_input(key="wl_dlg_sell").set_value(sell)
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == [text] and watch.has("wl_dlg_save")
    assert watch.items().set_index("symbol").loc["QTUM", "buy_target"] == 150.0


@pytest.mark.parametrize(("buy", "sell", "line"), [
    (160.0, None, "QTUM: the price is already at or below your Buy below price."),
    (None, 150.0, "QTUM: the price is already at or above your Sell above price."),
])
def test_a_target_already_reached_is_said_once(two_lists, buy, sell, line):
    watch, _ = two_lists
    at = watch.click("wl_edit_stock")
    at.number_input(key="wl_dlg_buy").set_value(buy)
    at.number_input(key="wl_dlg_sell").set_value(sell)
    at = watch.click("wl_dlg_save")
    assert line in [i.value for i in at.info]
    assert line not in [i.value for i in watch.run().info]    # once


def test_move_a_stock_leaves_no_stock_selected_and_says_so(two_lists):
    watch, ids = two_lists
    at = watch.click("wl_edit_stock")
    at.selectbox(key="wl_dlg_list").set_value(ids["ETFs"])
    at = watch.click("wl_dlg_save")
    assert watch.items().set_index("symbol").loc["QTUM", "list_name"] == "ETFs"
    assert [t.value for t in at.toast] == ["Moved QTUM to ETFs."]
    assert list(watch.table()["Symbol"]) == ["IONQ", "RGTI"]
    assert at.session_state["wl_symbol"] is None              # D21: no stock is selected
    assert at.button(key="wl_edit_stock").disabled

    at.session_state["wl_symbol"] = "IONQ"
    watch.run()
    at = watch.click("wl_edit_stock")                         # move and retarget in one save
    at.selectbox(key="wl_dlg_list").set_value(ids["ETFs"])
    at.number_input(key="wl_dlg_buy").set_value(30.0)
    at = watch.click("wl_dlg_save")
    assert [t.value for t in at.toast] == ["Moved IONQ to ETFs and saved its targets."]


def test_remove_needs_a_second_click(two_lists):
    watch, _ = two_lists
    watch.click("wl_edit_stock")
    assert not watch.has("wl_dlg_remove_confirm")
    at = watch.click("wl_dlg_remove")
    assert len(watch.items()) == 3                            # armed only
    assert 'This deletes QTUM and its targets from "Quantum".' in watch.dialog_text()
    assert at.button(key="wl_dlg_remove_confirm").label == "Remove QTUM and its targets"
    watch.click("wl_dlg_cancel")
    watch.click("wl_edit_stock")
    assert watch.has("wl_dlg_remove") and not watch.has("wl_dlg_remove_confirm")   # disarmed
    watch.click("wl_dlg_remove")
    at = watch.click("wl_dlg_remove_confirm")
    assert list(watch.items()["symbol"]) == ["IONQ", "RGTI"]
    assert at.session_state["wl_symbol"] is None and not watch.has("wl_dlg_save")


def test_edit_of_a_stock_or_list_removed_elsewhere(two_lists):
    watch, ids = two_lists
    watch.click("wl_edit_stock")
    watchlist.remove_item(db_path=watch.db, symbol="QTUM")    # another browser tab
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == ["QTUM is no longer on the watchlist."]
    watch.click("wl_dlg_cancel")

    watch.at.session_state["wl_symbol"] = "IONQ"
    watch.run()
    at = watch.click("wl_edit_stock")
    at.selectbox(key="wl_dlg_list").set_value(ids["ETFs"])
    watchlist.delete_list(db_path=watch.db, list_id=ids["ETFs"])
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == ["That list no longer exists."]


def test_lists_changed_in_another_browser_tab(two_lists):
    watch, ids = two_lists
    watch.click("wl_rename_list")
    watchlist.delete_list(db_path=watch.db, list_id=ids["Quantum"])
    watch.at.text_input(key="wl_dlg_name").set_value("Other")
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == ["That list no longer exists."]
    at = watch.click("wl_dlg_cancel")
    assert at.session_state["wl_list"] == ids["ETFs"]         # the list it was on is gone
    watch.click("wl_new_list")
    watchlist.create_list(db_path=watch.db, name="Same", now=NOW)
    watch.at.text_input(key="wl_dlg_name").set_value("same")
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == ['A list named "same" already exists.']


def test_a_busy_database_keeps_the_dialog_open(two_lists, monkeypatch):
    watch, _ = two_lists

    def busy(**kwargs):
        raise watchlist.WatchlistError(watchlist.MSG_BUSY)

    watch.click("wl_edit_stock")
    monkeypatch.setattr(watchlist, "update_item", busy)
    watch.click("wl_dlg_save")
    assert watch.dialog_text() == [watchlist.MSG_BUSY] and watch.has("wl_dlg_save")


# ---- table, selection, detail ------------------------------------------------

def test_table_values_and_order(two_lists):
    watch, _ = two_lists
    frame = watch.table()
    assert list(frame.columns) == ["Symbol", "Alert", "Price", "Day", "Buy below", "To buy",
                                   "Sell above", "To sell", "Shares held"]
    assert list(frame["Symbol"]) == ["IONQ", "QTUM", "RGTI"]
    by = frame.set_index("Symbol")
    assert by.loc["QTUM", "To buy"] == pytest.approx(1.6)
    assert by.loc["QTUM", "To sell"] == pytest.approx(12.914, abs=1e-3)
    assert pd.isna(by.loc["RGTI", "Price"]) and by.loc["RGTI", "Alert"] == ""
    watchlist.update_item(db_path=watch.db, symbol="RGTI", list_id=by.shape[0] and
                          watch.at.session_state["wl_list"], buy_target=9.0, sell_target=None)
    watch.quotes["RGTI"] = _q(8.0, 8.0)
    watch.click("wl_update_now")
    frame = watch.table()
    assert list(frame["Symbol"]) == ["RGTI", "IONQ", "QTUM"]  # the alert row first
    assert frame.loc[0, "Alert"] == "At or below buy price" and frame.loc[0, "To buy"] == 0.0
    assert watch.at.session_state["wl_symbol"] == "QTUM"      # the selection stayed on its stock


def test_arrows_and_a_set_symbol_pick_the_stock(two_lists):
    watch, _ = two_lists
    assert watch.at.button(key="wl_edit_stock").label == "Edit / move / remove: QTUM"
    # The selected stock's action is the emphasized button; the others stay plain.
    assert watch.at.button(key="wl_edit_stock").proto.type == "primary"
    assert watch.at.button(key="wl_add_stock").proto.type == "secondary"
    at = watch.click("wl_symbol_next")
    assert at.session_state["wl_symbol"] == "RGTI" and at.button(key="wl_symbol_next").disabled
    watch.click("wl_symbol_prev")
    at = watch.click("wl_symbol_prev")
    assert at.session_state["wl_symbol"] == "IONQ" and at.button(key="wl_symbol_prev").disabled
    at.session_state["wl_symbol"] = "RGTI"
    assert watch.run().session_state["wl_symbol"] == "RGTI"   # kept across a plain rerun
    at.session_state["wl_symbol"] = "GONE"
    assert watch.run().session_state["wl_symbol"] is None     # not in the list: no stock


def test_list_arrows_step_through_the_lists_and_stop_at_the_ends(watch):
    ids = watch.seed(lists={"Alpha": [("IONQ", None, None)], "Beta": [], "Gamma": [("QTUM", None, None)]})
    at = watch.run()
    assert at.session_state["wl_list"] == ids["Alpha"]
    assert at.button(key="wl_list_prev").disabled and not at.button(key="wl_list_next").disabled
    at = watch.click("wl_list_next")
    assert at.session_state["wl_list"] == ids["Beta"] and at.selectbox(key="wl_list_select").value == f"list:{ids['Beta']}"
    assert '"Beta" is empty. Add a stock to see its price and set targets.' in [i.value for i in at.info]
    at = watch.click("wl_list_next")
    assert at.session_state["wl_list"] == ids["Gamma"] and at.button(key="wl_list_next").disabled
    assert list(watch.table()["Symbol"]) == ["QTUM"] and at.session_state["wl_symbol"] is None
    at.selectbox(key="wl_list_select").set_value(f"list:{ids['Beta']}")   # picked in the dropdown, then an arrow
    watch.run()
    assert watch.click("wl_list_prev").session_state["wl_list"] == ids["Alpha"]


def test_list_picker_css_hides_the_ticker_rows_until_text_is_typed():
    """Opened without typing, the dropdown shows the lists only. The rules apply
    while the box's text is empty (``value=""``), to the rows after the lists, and
    shorten the dropdown to the list rows. Checked in a browser against Streamlit
    1.58: opened -> lists only; typed -> the matching lists and tickers."""
    import app

    css = app._list_picker_css(lists=2, options=5)
    idle = ('body:has(.st-key-wl_list_select input[aria-expanded="true"][value=""]) '
            '[data-testid="stSelectboxVirtualDropdown"]')
    for index in (2, 3, 4):
        assert f'{idle} li[id$="val-{index}"]' in css
    for index in (0, 1, 5):
        assert f'li[id$="val-{index}"]' not in css             # the lists stay; no row past the last
    assert "{ display: none; }" in css
    assert f"{idle} > div {{ max-height: calc(2 * 2.5rem); }}" in css
    assert f"{idle} > div > div {{ height: calc(2 * 2.5rem) !important; }}" in css
    assert css.count(idle) == 5                                # every rule waits for an empty box


def test_list_picker_css_is_empty_without_tickers():
    import app

    assert app._list_picker_css(lists=3, options=3) == ""


def test_the_list_box_opens_on_lists_only(watch):
    """The page carries the stylesheet for its own counts: three lists, three tickers."""
    import app

    watch.seed(lists={"Alpha": [("IONQ", None, None), ("QTUM", None, None)], "Beta": [],
                      "Gamma": [("COPX", None, None)]})
    at = watch.run()
    assert app._list_picker_css(lists=3, options=6) in [block.proto.body for block in at.get("html")]


def test_the_list_box_searches_lists_and_tickers(watch):
    ids = watch.seed(lists={"Alpha": [("IONQ", None, None), ("QTUM", None, None)], "Beta": [],
                            "Gamma": [("COPX", None, None)]})
    at = watch.run()
    box = at.selectbox(key="wl_list_select")
    assert box.placeholder == "Search a list or a ticker"
    # What typing is matched against: every list, then every watched ticker with its list.
    assert box.options == ["Alpha", "Beta", "Gamma", "COPX · in Gamma", "IONQ · in Alpha", "QTUM · in Alpha"]
    box.set_value(f"stock:COPX|{ids['Gamma']}")               # a ticker found by the search
    at = watch.run()
    assert at.session_state["wl_list"] == ids["Gamma"] and at.session_state["wl_symbol"] == "COPX"
    assert at.selectbox(key="wl_list_select").value == f"list:{ids['Gamma']}"   # the box shows the list
    at.selectbox(key="wl_list_select").set_value(f"stock:QTUM|{ids['Alpha']}")
    at = watch.run()
    assert at.session_state["wl_list"] == ids["Alpha"] and at.session_state["wl_symbol"] == "QTUM"
    assert at.button(key="wl_edit_stock").label == "Edit / move / remove: QTUM"


def test_list_actions_are_one_menu_and_update_actions_are_quiet(two_lists):
    watch, _ = two_lists
    at = watch.at
    assert [b.label for b in (at.button(key=k) for k in ("wl_new_list", "wl_rename_list", "wl_delete_list"))] == [
        "New list", "Rename Quantum", "Delete Quantum"]
    quiet = ("wl_new_list", "wl_rename_list", "wl_delete_list", "wl_update_now", "wl_schedule")
    assert {at.button(key=key).proto.type for key in quiet} == {"tertiary"}
    at.session_state["wl_list_menu"] = True                    # the menu is open
    at = watch.click("wl_rename_list")
    assert at.session_state["wl_list_menu"] is False           # the dialog closed it
    assert at.text_input(key="wl_dlg_name").value == "Quantum"


def test_list_arrows_are_hidden_with_one_list(watch):
    watch.seed(lists={"Only": []})
    watch.run()
    assert not watch.has("wl_list_prev") and not watch.has("wl_list_next")


def test_a_click_anywhere_in_a_row_selects_that_stock(two_lists):
    watch, ids = two_lists
    key = f"wl_table_{ids['Quantum']}"
    assert watch.at.session_state["wl_symbol"] == "QTUM"      # row 1 of IONQ, QTUM, RGTI
    # What the browser sends for a click on RGTI's Price cell: the row mark stays, a cell is added.
    watch.at.session_state[key] = {"selection": {"rows": [1], "columns": [], "cells": [(2, "Price")]}}
    at = watch.run()
    assert at.session_state["wl_symbol"] == "RGTI"
    assert at.button(key="wl_edit_stock").label == "Edit / move / remove: RGTI"
    state = at.session_state[key]["selection"]
    assert list(state["rows"]) == [2] and list(state["cells"]) == []   # the row is marked, the cell cleared
    assert watch.run().session_state["wl_symbol"] == "RGTI"   # and it stays
    watch.at.session_state[key] = {"selection": {"rows": [2], "columns": [], "cells": [(2, "Day")]}}
    assert watch.run().session_state["wl_symbol"] == "RGTI"   # a click on the selected row changes nothing
    watch.at.session_state[key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
    assert watch.run().session_state["wl_symbol"] == "IONQ"   # the circle still works
    watch.at.session_state[key] = {"selection": {"rows": [], "columns": [], "cells": []}}
    at = watch.run()                                           # the circle clicked again: no stock
    assert at.session_state["wl_symbol"] is None and list(at.session_state[key]["selection"]["rows"]) == []
    assert watch.run().session_state["wl_symbol"] is None     # and it stays


def test_no_stock_is_selected_until_one_is_picked(watch):
    ids = watch.seed(lists={"Quantum": [("IONQ", None, None), ("QTUM", None, None)], "ETFs": [("VOO", None, None)]})
    watch.quotes = {"IONQ": _q(41.10, 42.0), "QTUM": _q(152.40, 150.90), "VOO": _q(500.0, 499.0)}
    at = watch.run()
    at.session_state["wl_list"] = ids["Quantum"]
    at = watch.run()
    key = f"wl_table_{ids['Quantum']}"
    assert at.session_state["wl_symbol"] is None
    assert list(at.session_state[key]["selection"]["rows"]) == []
    edit = at.button(key="wl_edit_stock")
    assert edit.label == "Edit / move / remove" and edit.disabled and edit.proto.type == "secondary"
    assert watch.metrics() == {} and not any(s.key == "wl_period" for s in at.selectbox)   # no detail
    assert at.button(key="wl_symbol_prev").disabled and not at.button(key="wl_symbol_next").disabled
    at = watch.click("wl_symbol_next")                         # the first step lands on the first row
    assert at.session_state["wl_symbol"] == "IONQ"
    assert at.button(key="wl_edit_stock").proto.type == "primary" and "Current price (delayed)" in watch.metrics()
    # Another list's stored rows are not read as a click or as a cleared mark in this one.
    at.session_state["wl_list"] = ids["ETFs"]
    assert watch.run().session_state["wl_symbol"] is None
    at.session_state["wl_list"], at.session_state["wl_symbol"] = ids["Quantum"], "QTUM"
    assert watch.run().session_state["wl_symbol"] == "QTUM"


def test_detail_of_an_unheld_stock(two_lists):
    watch, _ = two_lists
    assert list(watch.metrics()) == [
        "Current price (delayed)", "Buy below", "Sell above", "Period return (1y)",
        "Period mean (1y)", "Period high / low (1y)", "52-week high / low", "Avg volume",
        "Volatility (ann.)", "ATR(14)", "Expense Ratio"]
    metrics = watch.metrics()
    assert metrics["Current price (delayed)"] == ("152.40 USD", "+1.50 (+0.99%)")
    assert metrics["Buy below"] == ("150.00", "price 1.60% above target")
    assert metrics["Sell above"] == ("175.00", "price 12.91% below target")
    boxes = {c.key: c for c in watch.at.checkbox if str(c.key).startswith("wl_")}
    assert boxes["wl_cost"].disabled and boxes["wl_trades"].disabled and not boxes["wl_targets"].disabled
    assert boxes["wl_trades"].label == "My trades"


def test_detail_of_a_stock_without_targets_or_quote(two_lists):
    watch, _ = two_lists
    watch.at.session_state["wl_symbol"] = "RGTI"
    watch.run()
    metrics = watch.metrics()
    assert metrics["Current price (delayed)"][0] == "—"
    assert metrics["Buy below"] == ("—", "")
    assert next(c for c in watch.at.checkbox if c.key == "wl_targets").disabled


def test_a_held_and_watched_stock_shows_the_position_tiles_on_both_tabs(watch):
    watch.seed(lists={"Mine": [("AAPL", None, None)]})        # AAPL is held in the synthetic statement
    watch.quotes = {"AAPL": _q(110.0, 100.0)}
    watch.run()
    watch.at.session_state["wl_symbol"] = "AAPL"
    at = watch.run()                                           # two equal charts in one run: no clash
    assert list(watch.metrics()) == [
        "Current price (delayed)", "Buy below", "Sell above", "Position",
        "My avg cost", "Unrealized P&L", "Total return incl. divs", "Period return (1y)",
        "Period mean (1y)", "Period high / low (1y)", "52-week high / low", "Avg volume",
        "Volatility (ann.)", "ATR(14)", "Expense Ratio", "Yield (TTM) / on cost",
        "Dividends received (net)", "Dividends received (gross)", "Last dividend"]
    assert watch.metrics()["Unrealized P&L"][0] == "100.00 USD"    # (110 - 100) x 10, off the quote
    assert watch.table().loc[0, "Shares held"] == 10.0
    prices_tab = next(tab for tab in at.tabs if tab.label == "Prices")
    current = next(m for m in prices_tab.metric if m.label == "Current price")
    assert current.value.startswith("103.00")                 # Prices keeps the last close
    assert {"px_cost", "px_ma", "px_mean", "px_trades"} <= {c.key for c in at.checkbox}
    assert not next(c for c in at.checkbox if c.key == "wl_cost").disabled


# ---- alerts ------------------------------------------------------------------

def _box(watch) -> list[str]:
    overview = next(tab for tab in watch.at.tabs if tab.label == "Overview")
    return [m.value for m in overview.markdown] + [str(c.value) for c in overview.caption]


def test_overview_alert_box(watch):
    ids = watch.seed(lists={"Quantum": [("IONQ", 42.0, None), ("QTUM", 150.0, 175.0), ("RGTI", 9.0, None),
                                        ("NOTGT", None, None)],
                            "Metals": [("COPX", None, 60.0)]})
    watch.quotes = {"IONQ": _q(41.10, 42.0), "QTUM": _q(152.40, 150.90), "COPX": _q(61.30, 61.0)}
    at = watch.run()
    box = _box(watch)
    assert "**Watchlist alerts (2)**" in box
    lines = [text for text in box if "badge" in text]
    assert lines == [
        ":green-badge[BUY] **IONQ** 41.10 USD is at or below your buy target 42.00 · Quantum",
        ":red-badge[SELL] **COPX** 61.30 USD is at or above your sell target 60.00 · Metals"]
    assert "No price for: RGTI. Its target was not checked." in _texts(at)   # NOTGT has no target
    assert any(text.startswith("Fetched 10:07.") for text in box)
    assert not [b for b in next(t for t in at.tabs if t.label == "Overview").button]   # D16
    # D14: a new session opens on the first alert's list and stock.
    assert at.session_state["wl_list"] == ids["Quantum"] and at.session_state["wl_symbol"] == "IONQ"
    assert at.selectbox(key="wl_list_select").options[:2] == ["Metals (1 alert)", "Quantum (1 alert)"]
    assert "Alerts in other lists: Metals (1 alert)" in _texts(at)
    watch.quotes["QTUM"] = _q(140.0, 150.0)                   # a second alert appears later
    at = watch.click("wl_update_now")
    assert at.session_state["wl_symbol"] == "IONQ"            # the user is not moved
    assert "Quantum (2 alerts)" in at.selectbox(key="wl_list_select").options
    assert not at.error


def test_overview_box_when_prices_are_missing(watch):
    watch.seed(lists={"Quantum": [("IONQ", 42.0, None), ("RGTI", 9.0, None)]})
    watch.quotes = {"IONQ": _q(50.0, 50.0)}
    watch.run()
    assert "**Watchlist prices**" in _box(watch)              # no alert, but a target was not checked
    assert "No price for: RGTI. Its target was not checked." in watch.texts()
    watch.quotes = {}
    at = watch.click("wl_update_now")
    assert ("Watchlist prices could not be fetched. Targets were not checked. "
            "Open the Watchlist tab and click Update now.") in watch.texts()
    assert "Prices could not be fetched (yfinance). Click **Update now** to try again." in [
        w.value for w in at.warning]


def test_no_box_when_nothing_is_reached(watch):
    watch.seed(lists={"Quantum": [("IONQ", 42.0, None)]})
    watch.quotes = {"IONQ": _q(50.0, 50.0)}
    watch.run()
    assert not [text for text in _box(watch) if "Watchlist" in text]


# ---- quotes and the schedule -------------------------------------------------

def test_quotes_are_fetched_once_per_session_and_on_request(watch):
    watch.seed(lists={"Quantum": [("IONQ", None, None), ("QTUM", None, None)]})
    watch.run()
    assert watch.quote_calls == [("IONQ", "QTUM")]            # once, for the box and the table
    watch.run()
    assert len(watch.quote_calls) == 1                        # a plain rerun reuses it
    downloads, candles = len(watch.ctx["download_calls"]), len(watch.ctx["fetch_calls"])
    watch.click("wl_update_now")
    assert len(watch.quote_calls) == 2
    assert (len(watch.ctx["download_calls"]), len(watch.ctx["fetch_calls"])) == (downloads, candles)
    refresh = next(b for b in watch.at.sidebar.button if "Refresh" in b.label)
    refresh.click()
    watch.run()
    assert len(watch.quote_calls) == 3                        # "Refresh data" fetches again too


def test_a_new_session_fetches_again(watch, monkeypatch):
    watch.seed(lists={"Quantum": [("IONQ", None, None)]})
    watch.run()
    other = AppTest.from_file(str(APP_PATH), default_timeout=30)
    other.run()
    assert not other.exception and len(watch.quote_calls) == 2


def test_schedule_dialog_and_line(watch):
    watch.seed(lists={"Quantum": [(f"S{i}", None, None) for i in range(12)]})
    at = watch.run()
    assert "Prices fetched 10:07 · updates: only when I open the app" in _texts(at)
    at = watch.click("wl_schedule")
    assert "Now: only when I open the app." in _texts(at)
    assert at.radio(key="wl_dlg_mode").value == "once"
    assert at.number_input(key="wl_dlg_minutes").disabled and at.number_input(key="wl_dlg_hours").disabled
    at.radio(key="wl_dlg_mode").set_value("every")
    at = watch.run()
    assert not at.number_input(key="wl_dlg_minutes").disabled and not at.number_input(key="wl_dlg_hours").disabled
    texts = _texts(at)
    assert "Save starts a new 4 hours from now." in texts
    assert "With 12 watched stocks this is about 720 yfinance requests an hour." in texts
    assert "Works only while this page is open." in texts
    at.radio(key="wl_dlg_duration").set_value("always")
    assert watch.run().number_input(key="wl_dlg_hours").disabled
    at.radio(key="wl_dlg_duration").set_value("hours")
    watch.run()
    at = watch.click("wl_dlg_save")
    assert watchlist.read_refresh(db_path=watch.db) == {
        "interval_minutes": 1, "until": NOW + dt.timedelta(hours=4)}
    assert ("Prices fetched 10:07 · updates: every 1 min until 14:07 · works only while this page "
            "is open") in _texts(at)
    at = watch.click("wl_schedule")
    assert at.radio(key="wl_dlg_mode").value == "every" and "Now: every 1 min until 14:07." in _texts(at)
    watch.click("wl_dlg_cancel")

    watch.now = NOW + dt.timedelta(hours=1)                   # saving again starts a new 4 hours
    watch.click("wl_schedule")
    watch.click("wl_dlg_save")
    assert watchlist.read_refresh(db_path=watch.db)["until"] == NOW + dt.timedelta(hours=5)


def test_a_schedule_survives_a_restart_and_ends_by_itself(watch):
    watch.seed(lists={"Quantum": [("IONQ", None, None)]})
    watchlist.write_refresh(db_path=watch.db, interval_minutes=1, until=NOW + dt.timedelta(hours=4))
    restarted = AppTest.from_file(str(APP_PATH), default_timeout=30)
    restarted.run()
    assert not restarted.exception
    assert any("updates: every 1 min until 14:07" in text for text in _texts(restarted))
    watch.now = NOW + dt.timedelta(hours=4)                   # the end time has come
    restarted.run()
    assert any("updates: only when I open the app" in text for text in _texts(restarted))
    assert any("Prices fetched" in text for text in _texts(restarted))
    watchlist.write_refresh(db_path=watch.db, interval_minutes=4, until=None)
    watch.now = NOW + dt.timedelta(days=400)
    restarted.run()
    texts = _texts(restarted)
    assert any("updates: every 4 min, no end time" in text for text in texts)
    assert any("Prices fetched 16 Nov 10:07" in text or "Prices fetched" in text for text in texts)


def test_a_schedule_window_fetches_once_and_the_next_window_again(watch):
    watch.seed(lists={"Quantum": [("IONQ", None, None)]})
    watchlist.write_refresh(db_path=watch.db, interval_minutes=4, until=None)
    watch.now = dt.datetime(2026, 10, 12, 10, 4, 1, tzinfo=UTC)
    watch.run()
    watch.now = dt.datetime(2026, 10, 12, 10, 7, 59, tzinfo=UTC)
    watch.run()
    assert len(watch.quote_calls) == 1
    watch.now = dt.datetime(2026, 10, 12, 10, 8, 0, tzinfo=UTC)
    at = watch.run()
    assert len(watch.quote_calls) == 2
    assert at.session_state["wl_tick_armed"] is True          # the timer only arms itself in a full run


def test_the_fetch_time_carries_the_date_when_it_is_not_today(watch):
    watch.seed(lists={"Quantum": [("IONQ", 42.0, None)]})
    watch.quotes = {"IONQ": _q(41.0, 41.0)}
    watch.run()
    watch.now = NOW + dt.timedelta(days=1)                    # the page stayed open overnight
    at = watch.run()
    assert "Prices fetched 12 Oct 10:07 · updates: only when I open the app" in _texts(at)
    assert any(text.startswith("Fetched 12 Oct 10:07.") for text in _box(watch))


def test_a_watchlist_change_refreshes_todays_backup(watch):
    watch.seed(lists={"Quantum": []})
    watch.run()
    target = Path(os.environ["BACKUP_DIR"])
    main, presync = target / "snapshots-2026-10-12.sqlite", target / "snapshots-2026-10-12-presync.sqlite"
    assert main.exists() and presync.exists()                 # taken before the IBKR write, patched date
    before = presync.read_bytes()
    watch.click("wl_new_list")
    watch.at.text_input(key="wl_dlg_name").set_value("Fresh")
    watch.click("wl_dlg_save")
    assert list(watchlist.read_lists(db_path=main)["lists"]["name"]) == ["Fresh", "Quantum"]
    assert presync.read_bytes() == before                     # the pre-sync copy is left alone
    assert backup.status(target)["count"] == 1


# ---- IBKR not connected or failed --------------------------------------------

def _counting(code: str, message: str, calls: list):
    def boom(token, query_id, date_range):
        calls.append(code)
        raise flex.FlexError(f"IBKR Flex error {code}: {message} token={token}", code=code)
    return boom


def _offline(monkeypatch, tmp_path, download) -> Watch:
    at = _wire_app(monkeypatch, tmp_path, download)
    return _wire(monkeypatch, Watch(at=at, db_path=tmp_path / "cov-snapshots.sqlite", ctx={}))


def test_watchlist_works_without_credentials(monkeypatch, tmp_path):
    watch = _offline(monkeypatch, tmp_path, None)
    monkeypatch.setenv("FLEX_TOKEN", "")
    watch.seed(lists={"Quantum": [("IONQ", 42.0, None)]})
    watch.quotes = {"IONQ": _q(41.10, 42.0)}
    at = watch.run()
    assert [tab.label for tab in at.tabs] == ["Watchlist", "Net Worth"]
    assert [w.value for w in at.warning] == ["IBKR: not connected"]      # none from the watchlist
    texts = _texts(at)
    assert "IBKR not connected: positions not shown." in texts
    assert "Save a token above to connect." in texts
    assert any("Net Worth appears here" in text for text in texts)
    assert ":green-badge[BUY] **IONQ** 41.10 USD is at or below your buy target 42.00 · Quantum" in texts
    assert pd.isna(watch.table().loc[0, "Shares held"])
    assert "flex_failed" not in at.session_state
    watch.click("wl_edit_stock")                               # the tab is usable
    watch.at.number_input(key="wl_dlg_buy").set_value(40.0)
    watch.click("wl_dlg_save")
    assert watch.items().iloc[0]["buy_target"] == 40.0


def test_a_failed_pull_is_not_retried_until_refresh(monkeypatch, tmp_path):
    calls: list = []
    watch = _offline(monkeypatch, tmp_path, _counting("1014", "Query is invalid.", calls))
    ids = watch.seed(lists={"A": [("IONQ", None, None), ("QTUM", None, None)], "B": []})
    at = watch.run()
    assert calls == ["1014"] and [tab.label for tab in at.tabs] == ["Watchlist", "Net Worth"]
    assert at.session_state["flex_failed"]["code"] == "1014"
    assert "smoke-test-token" not in at.session_state["flex_failed"]["message"]
    assert any("Fix the token/query" in str(i.value) and "Refresh data" in str(i.value) for i in at.info)
    watch.run()                                                # a plain rerun
    watch.click("wl_symbol_next")                              # another stock
    at.selectbox(key="wl_list_select").set_value(f"list:{ids['B']}")     # another list
    assert watch.run().session_state["wl_list"] == ids["B"]
    watch.click("wl_update_now")
    assert calls == ["1014"] and len(watch.quote_calls) == 2   # quotes yes, IBKR no
    refresh = next(b for b in at.sidebar.button if "Refresh" in b.label)
    refresh.click()
    watch.run()
    assert calls == ["1014", "1014"]


def test_a_rejected_token_shows_the_card_and_a_saved_token_retries(monkeypatch, tmp_path):
    calls: list = []
    watch = _offline(monkeypatch, tmp_path, _counting("1012", "Token has expired.", calls))
    at = watch.run()
    assert "Save a token above to connect." in _texts(at)
    watch.run()
    assert calls == ["1012"]
    watch.click("connect_open")
    at.text_input(key="connect_token").set_value("9" * 24)
    at.button(key="connect_save").click().run()               # the last action on this app
    assert not at.exception and calls == ["1012", "1012"]


def test_losing_the_credentials_forgets_the_failure(monkeypatch, tmp_path):
    calls: list = []
    watch = _offline(monkeypatch, tmp_path, _counting("1014", "Query is invalid.", calls))
    watch.run()
    monkeypatch.setenv("FLEX_TOKEN", "")
    at = watch.run()
    assert "flex_failed" not in at.session_state and calls == ["1014"]
    monkeypatch.setenv("FLEX_TOKEN", "smoke-test-token")
    watch.run()
    assert calls == ["1014", "1014"]


# ---- database trouble --------------------------------------------------------

def test_an_unreadable_watchlist_costs_only_its_tab(watch, monkeypatch):
    watch.seed(lists={"Quantum": [("IONQ", 42.0, None)]})
    monkeypatch.setattr(watchlist, "read_items",
                        lambda db_path: {"items": pd.DataFrame(columns=watchlist._ITEM_COLS), "error": True})
    at = watch.run()
    assert "The watchlist could not be read from the database." in [w.value for w in at.warning]
    assert "Watchlist could not be read. Targets were not checked." in _texts(at)
    assert not watch.has("wl_new_list") and watch.quote_calls == []
    assert any(m.label == "Net liquidation" for m in at.metric)   # the other tabs rendered
    assert not at.error


def test_a_watchlist_that_cannot_be_set_up_costs_only_its_tab(watch, monkeypatch):
    def boom(db_path):
        raise OSError("read-only file system")

    monkeypatch.setattr(watchlist, "init_db", boom)
    at = watch.run()
    assert "The watchlist could not be set up in the database." in [w.value for w in at.warning]
    assert any(m.label == "Net liquidation" for m in at.metric)


def test_a_fresh_install_without_a_db_directory(monkeypatch, tmp_path):
    at = _wire_app(monkeypatch, tmp_path, None)
    monkeypatch.setenv("FLEX_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "not" / "there" / "store.sqlite"))
    at.run()
    assert not at.exception
    assert "You have no watchlists yet. Create one to start watching stocks." in [i.value for i in at.info]


def test_a_stale_day_dialog_flag_is_dropped(watch):
    watch.run()
    watch.at.session_state["perf_day_open"] = "1999-01-01"    # a day with no row
    at = watch.run()
    assert "perf_day_open" not in at.session_state


def test_a_watchlist_dialog_flag_beside_another_dialog_is_dropped(watch):
    watch.seed(lists={"Quantum": []})
    watch.run()
    watch.at.session_state["wl_dialog"] = {"kind": "nope"}
    assert "wl_dialog" not in watch.run().session_state


# ---- the no-defaults rule ----------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_NEW = {
    "data/watchlist.py": None,                                # every function
    "data/prices.py": {"fetch_quote", "fetch_quotes", "resolve_symbol", "_positive"},
    "data/backup.py": {"refresh", "_write_copy", "_sweep_parts", "_report", "_presync_path"},
    "app.py": {"_render_price_detail", "_watchlist_state", "tab_watchlist", "_watchlist_body",
               "load_quotes", "_save_watchlist", "_open_watchlist_dialog", "_render_watchlist_alerts",
               "_step_watch_symbol", "_step_watchlist", "_target_field", "_dialog_buttons", "_target_notice",
               "_save_add_stock", "_render_offline_tabs", "_watchlist_styler", "_fmt_price"},
}


def test_new_functions_have_no_default_parameter_values():
    offenders = []
    for path, names in _NEW.items():
        tree = ast.parse((_ROOT / path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.Lambda)):
                continue
            name = node.name if isinstance(node, ast.FunctionDef) else "<lambda>"
            if names is not None and name not in names:
                continue
            if node.args.defaults or any(d is not None for d in node.args.kw_defaults):
                offenders.append(f"{path}:{node.lineno} {name}")
    assert not offenders, offenders
    from charts import figures
    from data import price_stats
    for func, added in ((price_stats.summarize, ("live_price", "live_previous_close")),
                        (figures.candlestick, ("buy_target", "sell_target"))):
        params = inspect.signature(func).parameters
        assert all(params[name].default is inspect.Parameter.empty for name in added)
