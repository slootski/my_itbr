"""Watchlists: named lists of stocks with optional buy and sell targets.

Everything here is typed by the user and exists nowhere else, so it lives in the
same SQLite file as the IBKR history (and is covered by its backups). A stock is
in exactly one list. The module has three parts:

  - the store: ``init_db`` plus read and write functions. Only ``init_db``
    creates anything; reads open the file read-only and return empty results
    when it, or the tables, are missing;
  - pure helpers that turn items and quotes into the table, the alerts and the
    selection the Watchlist tab shows;
  - the price-update schedule ("every N minutes, for X hours or with no end").

A broken rule or a failed write raises ``WatchlistError``; its text is written
for the user and shown as is.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import math
import re
import sqlite3
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger("myitbr.watchlist")

# How long a connection waits for a lock held by another one before giving up.
_BUSY_SECONDS = 5
_MAX_NAME = 40
_MAX_SYMBOL = 20
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]+$")

DIALOG_KINDS = ("new_list", "rename_list", "delete_list", "add_stock", "edit_stock", "schedule")
# Session-state keys that mean "a dialog is open" (the last two are app.py's).
DIALOG_FLAGS = frozenset({"wl_dialog", "connect_dialog_open", "perf_day_open"})

SCHEDULE_ONCE = "once"
SCHEDULE_EVERY = "every"
DURATION_HOURS = "hours"
DURATION_ALWAYS = "always"
_DEFAULT_MINUTES = 1
_DEFAULT_HOURS = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlists (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL CHECK (name = trim(name) AND length(name) BETWEEN 1 AND 40),
    name_key TEXT NOT NULL UNIQUE,
    created  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    symbol      TEXT PRIMARY KEY NOT NULL
                CHECK (symbol = upper(trim(symbol)) AND length(symbol) BETWEEN 1 AND 20),
    match_key   TEXT NOT NULL UNIQUE
                CHECK (match_key = replace(replace(symbol, '.', '-'), ' ', '-')),
    list_id     INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    buy_target  REAL CHECK (buy_target IS NULL OR (typeof(buy_target) IN ('real', 'integer')
                                                   AND buy_target > 0 AND buy_target < 1e308)),
    sell_target REAL CHECK (sell_target IS NULL OR (typeof(sell_target) IN ('real', 'integer')
                                                    AND sell_target > 0 AND sell_target < 1e308)),
    added       TEXT NOT NULL,
    CHECK (buy_target IS NULL OR sell_target IS NULL OR buy_target < sell_target)
);

CREATE INDEX IF NOT EXISTS watchlist_items_list ON watchlist_items(list_id);

CREATE TABLE IF NOT EXISTS watchlist_refresh (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    interval_minutes INTEGER CHECK (interval_minutes IS NULL OR
                                    (typeof(interval_minutes) = 'integer'
                                     AND interval_minutes BETWEEN 1 AND 60)),
    until            TEXT,
    CHECK (until IS NULL OR interval_minutes IS NOT NULL)
);
"""

_TABLES = ("watchlists", "watchlist_items", "watchlist_refresh")

_LIST_COLS = ["id", "name", "count"]
_ITEM_COLS = ["symbol", "list_id", "list_name", "buy_target", "sell_target", "added"]

MSG_BUSY = "The database is busy or cannot be written. Try again."
MSG_NOT_SAVED = "The watchlist could not be saved."
MSG_LIST_GONE = "That list no longer exists."
MSG_PICK_LIST = "Pick a list."


class WatchlistError(Exception):
    """A rule was broken or the write failed; str(exc) is the text shown to the user."""


# ---- clock (the one place the watchlist reads time; patched in tests) ------

def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def local_tz() -> dt.tzinfo:
    """The machine's zone as a real zone (not a fixed offset), for display."""
    try:
        from tzlocal import get_localzone_name  # optional; not a dependency
        return ZoneInfo(get_localzone_name())
    except Exception:  # noqa: BLE001
        pass
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            return ZoneInfo("/".join(parts[parts.index("zoneinfo") + 1:]))
    except Exception:  # noqa: BLE001
        pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _aware(*, now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(dt.timezone.utc)


# ---- text rules ------------------------------------------------------------

def clean_symbol(*, text: str) -> str:
    """Trim, upper-case and join inner spaces with "-" (IBKR "BRK B" -> "BRK-B")."""
    symbol = "-".join(str(text).upper().split())
    if not symbol:
        raise WatchlistError("Type a ticker.")
    if len(symbol) > _MAX_SYMBOL or not _SYMBOL_RE.match(symbol):
        raise WatchlistError("That is not a valid ticker. Use letters, digits and . - ^ =")
    return symbol


def match_key(*, symbol: str) -> str:
    """One key for every spelling of a stock: "BRK.B", "BRK B" and "BRK-B" agree."""
    return "-".join(str(symbol).upper().split()).replace(".", "-")


def clean_name(*, name: str) -> str:
    cleaned = unicodedata.normalize("NFC", str(name)).strip()
    if not cleaned:
        raise WatchlistError("Type a name.")
    if len(cleaned) > _MAX_NAME:
        raise WatchlistError(f"Use {_MAX_NAME} characters or fewer.")
    return cleaned


def name_key(*, name: str) -> str:
    return unicodedata.normalize("NFC", str(name)).strip().casefold()


def validate_targets(*, buy_target: float | None, sell_target: float | None) -> None:
    for target in (buy_target, sell_target):
        if target is None:
            continue
        try:
            value = float(target)
        except (TypeError, ValueError):
            raise WatchlistError("A target must be a number above 0.") from None
        if not math.isfinite(value) or value <= 0:
            raise WatchlistError("A target must be a number above 0.")
    if buy_target is not None and sell_target is not None and float(buy_target) >= float(sell_target):
        raise WatchlistError("Buy below must be lower than Sell above.")


def _target(value: float | None) -> float | None:
    return None if value is None else float(value)


# ---- store -----------------------------------------------------------------

def _write_conn(*, db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path), timeout=_BUSY_SECONDS)
    # Outside any transaction, or SQLite ignores it; ON DELETE CASCADE needs it.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _read_conn(*, db_path: Path | str) -> sqlite3.Connection | None:
    """A read-only connection, or None when there is no file. Never creates one."""
    path = Path(db_path)
    if not path.exists():
        return None
    # as_uri() quotes "?", "#" and "%"; joining strings would open another file.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_BUSY_SECONDS)


def _has_tables(*, conn: sqlite3.Connection) -> bool:
    marks = ",".join("?" for _ in _TABLES)
    found = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ({marks})", _TABLES,
    ).fetchone()[0]
    return found == len(_TABLES)


def init_db(*, db_path: Path | str) -> None:
    """Create the DB's directory, the file and the watchlist tables if missing."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_write_conn(db_path=path)) as conn, conn:
        conn.executescript(_SCHEMA)


def _write_error(*, exc: sqlite3.Error, unique: str, foreign: str) -> WatchlistError:
    """The user text for a failed write. ``unique`` and ``foreign`` are the texts
    of this write's duplicate and missing-parent cases."""
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"):
        return WatchlistError(unique)
    if name == "SQLITE_CONSTRAINT_FOREIGNKEY":
        return WatchlistError(foreign)
    if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_READONLY")):
        return WatchlistError(MSG_BUSY)
    logger.warning("watchlist write failed: %s (%s)", exc, name or type(exc).__name__)
    return WatchlistError(MSG_NOT_SAVED)


def read_lists(*, db_path: Path | str) -> dict:
    """``{"lists": frame(id, name, count), "error": bool}``, ordered by name."""
    empty = pd.DataFrame(columns=_LIST_COLS)
    try:
        conn = _read_conn(db_path=db_path)
        if conn is None:
            return {"lists": empty, "error": False}
        with contextlib.closing(conn):
            if not _has_tables(conn=conn):
                return {"lists": empty, "error": False}
            rows = conn.execute(
                "SELECT w.id, w.name, COUNT(i.symbol) FROM watchlists w "
                "LEFT JOIN watchlist_items i ON i.list_id = w.id "
                "GROUP BY w.id ORDER BY w.name_key"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("watchlist read failed: %s", exc)
        return {"lists": empty, "error": True}
    return {"lists": pd.DataFrame(rows, columns=_LIST_COLS), "error": False}


def read_items(*, db_path: Path | str) -> dict:
    """``{"items": frame(symbol, list_id, list_name, buy_target, sell_target, added),
    "error": bool}``, ordered by symbol. An item whose list is gone is still returned."""
    empty = pd.DataFrame(columns=_ITEM_COLS)
    try:
        conn = _read_conn(db_path=db_path)
        if conn is None:
            return {"items": empty, "error": False}
        with contextlib.closing(conn):
            if not _has_tables(conn=conn):
                return {"items": empty, "error": False}
            rows = conn.execute(
                "SELECT i.symbol, i.list_id, w.name, i.buy_target, i.sell_target, i.added "
                "FROM watchlist_items i LEFT JOIN watchlists w ON w.id = i.list_id "
                "ORDER BY i.symbol"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("watchlist read failed: %s", exc)
        return {"items": empty, "error": True}
    frame = pd.DataFrame(rows, columns=_ITEM_COLS)
    for col in ("buy_target", "sell_target"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return {"items": frame, "error": False}


def create_list(*, db_path: Path | str, name: str, now: dt.datetime) -> int:
    cleaned = clean_name(name=name)
    created = _aware(now=now).isoformat()
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO watchlists (name, name_key, created) VALUES (?, ?, ?)",
                (cleaned, name_key(name=cleaned), created),
            )
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise _write_error(
            exc=exc, unique=f'A list named "{cleaned}" already exists.', foreign=MSG_NOT_SAVED,
        ) from exc


def rename_list(*, db_path: Path | str, list_id: int, name: str) -> None:
    cleaned = clean_name(name=name)
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            cursor = conn.execute(
                "UPDATE watchlists SET name = ?, name_key = ? WHERE id = ?",
                (cleaned, name_key(name=cleaned), int(list_id)),
            )
            if cursor.rowcount == 0:
                raise WatchlistError(MSG_LIST_GONE)
    except sqlite3.Error as exc:
        raise _write_error(
            exc=exc, unique=f'A list named "{cleaned}" already exists.', foreign=MSG_NOT_SAVED,
        ) from exc


def delete_list(*, db_path: Path | str, list_id: int) -> int:
    """Delete a list with its stocks and targets. Returns how many stocks went."""
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            # Explicit, so the result does not depend on the foreign-key pragma.
            removed = conn.execute(
                "DELETE FROM watchlist_items WHERE list_id = ?", (int(list_id),),
            ).rowcount
            cursor = conn.execute("DELETE FROM watchlists WHERE id = ?", (int(list_id),))
            if cursor.rowcount == 0:
                raise WatchlistError(MSG_LIST_GONE)
            return int(removed)
    except sqlite3.Error as exc:
        raise _write_error(exc=exc, unique=MSG_NOT_SAVED, foreign=MSG_NOT_SAVED) from exc


def duplicate_text(*, items: pd.DataFrame, symbol: str, list_id: int) -> str | None:
    """Why ``symbol`` cannot be added to ``list_id``, or None when it is not watched."""
    if items is None or items.empty:
        return None
    key = match_key(symbol=symbol)
    for row in items.itertuples(index=False):
        if match_key(symbol=row.symbol) != key:
            continue
        list_name = row.list_name if isinstance(row.list_name, str) else ""
        if row.list_id == list_id:
            return f'{row.symbol} is already in "{list_name}".'
        return (f'{row.symbol} is already in "{list_name}". Open that list and use '
                "Edit / move / remove to move it here.")
    return None


def add_item(
    *, db_path: Path | str, list_id: int, symbol: str,
    buy_target: float | None, sell_target: float | None, now: dt.datetime,
) -> None:
    cleaned = clean_symbol(text=symbol)
    validate_targets(buy_target=buy_target, sell_target=sell_target)
    added = _aware(now=now).isoformat()
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO watchlist_items (symbol, match_key, list_id, buy_target, sell_target, added) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cleaned, match_key(symbol=cleaned), int(list_id),
                 _target(buy_target), _target(sell_target), added),
            )
    except sqlite3.Error as exc:
        name = getattr(exc, "sqlite_errorname", "") or ""
        unique = f"{cleaned} is already on the watchlist."
        if name in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"):
            unique = duplicate_text(
                items=read_items(db_path=db_path)["items"], symbol=cleaned, list_id=int(list_id),
            ) or unique
        raise _write_error(exc=exc, unique=unique, foreign=MSG_LIST_GONE) from exc


def update_item(
    *, db_path: Path | str, symbol: str, list_id: int,
    buy_target: float | None, sell_target: float | None,
) -> None:
    """Set a stock's list and targets in one write (move, retarget, or both)."""
    validate_targets(buy_target=buy_target, sell_target=sell_target)
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            cursor = conn.execute(
                "UPDATE watchlist_items SET list_id = ?, buy_target = ?, sell_target = ? WHERE symbol = ?",
                (int(list_id), _target(buy_target), _target(sell_target), str(symbol)),
            )
            if cursor.rowcount == 0:
                raise WatchlistError(f"{symbol} is no longer on the watchlist.")
    except sqlite3.Error as exc:
        raise _write_error(exc=exc, unique=MSG_NOT_SAVED, foreign=MSG_LIST_GONE) from exc


def remove_item(*, db_path: Path | str, symbol: str) -> None:
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            cursor = conn.execute("DELETE FROM watchlist_items WHERE symbol = ?", (str(symbol),))
            if cursor.rowcount == 0:
                raise WatchlistError(f"{symbol} is no longer on the watchlist.")
    except sqlite3.Error as exc:
        raise _write_error(exc=exc, unique=MSG_NOT_SAVED, foreign=MSG_NOT_SAVED) from exc


def read_refresh(*, db_path: Path | str) -> dict:
    """``{"interval_minutes": int | None, "until": aware datetime | None}``.

    No row, no interval, or an ``until`` that cannot be read all mean "only when
    the app is opened".
    """
    once = {"interval_minutes": None, "until": None}
    try:
        conn = _read_conn(db_path=db_path)
        if conn is None:
            return once
        with contextlib.closing(conn):
            if not _has_tables(conn=conn):
                return once
            row = conn.execute(
                "SELECT interval_minutes, until FROM watchlist_refresh WHERE id = 1"
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("watchlist schedule read failed: %s", exc)
        return once
    if row is None or row[0] is None:
        return once
    until = None
    if row[1] is not None:
        try:
            until = dt.datetime.fromisoformat(row[1])
        except (TypeError, ValueError):
            return once
        if until.tzinfo is None:
            return once
    return {"interval_minutes": int(row[0]), "until": until}


def write_refresh(*, db_path: Path | str, interval_minutes: int | None, until: dt.datetime | None) -> None:
    if interval_minutes is None and until is not None:
        raise WatchlistError(MSG_NOT_SAVED)
    stored = None if until is None else _aware(now=until).isoformat()
    try:
        with contextlib.closing(_write_conn(db_path=db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO watchlist_refresh (id, interval_minutes, until) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET interval_minutes = excluded.interval_minutes, "
                "until = excluded.until",
                (None if interval_minutes is None else int(interval_minutes), stored),
            )
    except sqlite3.Error as exc:
        raise _write_error(exc=exc, unique=MSG_NOT_SAVED, foreign=MSG_NOT_SAVED) from exc


# ---- table, alerts, selection ----------------------------------------------

def distance_left(*, price: float | None, target: float | None, side: str) -> float | None:
    """Percent still to go to a target: above a buy target, below a sell target.

    0.0 means reached. None when the price or the target is missing.
    """
    if price is None or target is None or pd.isna(price) or pd.isna(target) or target <= 0:
        return None
    gap = (price - target) if side == "buy" else (target - price)
    return max(gap / target * 100.0, 0.0)


def alert_side(*, price: float | None, buy_target: float | None, sell_target: float | None) -> str | None:
    if price is None or pd.isna(price):
        return None
    if buy_target is not None and not pd.isna(buy_target) and price <= buy_target:
        return "buy"
    if sell_target is not None and not pd.isna(sell_target) and price >= sell_target:
        return "sell"
    return None


def _quote(*, quotes: dict, symbol: str) -> dict:
    quote = quotes.get(symbol) if quotes else None
    return quote if quote else {"price": None, "previous_close": None, "currency": None}


def _opt(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


TABLE_COLS = ["symbol", "alert", "price", "currency", "day_pct", "buy_target", "above_buy",
              "sell_target", "below_sell", "shares"]
ALERT_TEXT = {"buy": "At or below buy price", "sell": "At or above sell price"}


def table(*, items: pd.DataFrame, quotes: dict, positions: pd.DataFrame) -> pd.DataFrame:
    """The rows of one list: numbers only, alert rows first, then by symbol."""
    held: dict[str, float] = {}
    if positions is not None and not positions.empty and {"symbol", "quantity"} <= set(positions.columns):
        for row in positions.itertuples(index=False):
            quantity = _opt(row.quantity)
            if quantity is not None and isinstance(row.symbol, str):
                held[match_key(symbol=row.symbol)] = quantity
    rows = []
    for item in items.itertuples(index=False):
        quote = _quote(quotes=quotes, symbol=item.symbol)
        price, previous = _opt(quote["price"]), _opt(quote["previous_close"])
        buy, sell = _opt(item.buy_target), _opt(item.sell_target)
        side = alert_side(price=price, buy_target=buy, sell_target=sell)
        rows.append({
            "symbol": item.symbol,
            "alert": ALERT_TEXT[side] if side else "",
            "price": price,
            "currency": quote["currency"],
            "day_pct": (price - previous) / previous * 100.0 if price is not None and previous else None,
            "buy_target": buy,
            "above_buy": distance_left(price=price, target=buy, side="buy"),
            "sell_target": sell,
            "below_sell": distance_left(price=price, target=sell, side="sell"),
            "shares": held.get(match_key(symbol=item.symbol)),
        })
    frame = pd.DataFrame(rows, columns=TABLE_COLS)
    for col in ("price", "day_pct", "buy_target", "above_buy", "sell_target", "below_sell", "shares"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["_no_alert"] = frame["alert"] == ""
    frame = frame.sort_values(["_no_alert", "symbol"], kind="stable").drop(columns="_no_alert")
    return frame.reset_index(drop=True)


def alerts(*, items: pd.DataFrame, quotes: dict) -> dict:
    """What the Overview box says.

    ``lines``: one dict per reached target (side, symbol, price, currency, target,
    list_name), buy first, then by symbol. ``missing``: stocks that have a target
    and no price. ``all_failed``: a target exists and no watched stock has a price.
    ``by_list``: alert count per list id.
    """
    lines: list[dict] = []
    missing: list[str] = []
    by_list: dict[int, int] = {}
    any_target, any_price = False, False
    for item in items.itertuples(index=False):
        quote = _quote(quotes=quotes, symbol=item.symbol)
        price = _opt(quote["price"])
        buy, sell = _opt(item.buy_target), _opt(item.sell_target)
        has_target = buy is not None or sell is not None
        any_target = any_target or has_target
        if price is None:
            if has_target:
                missing.append(item.symbol)
            continue
        any_price = True
        side = alert_side(price=price, buy_target=buy, sell_target=sell)
        if side is None:
            continue
        by_list[item.list_id] = by_list.get(item.list_id, 0) + 1
        lines.append({
            "side": side, "symbol": item.symbol, "price": price, "currency": quote["currency"],
            "target": buy if side == "buy" else sell,
            "list_id": item.list_id,
            "list_name": item.list_name if isinstance(item.list_name, str) else "",
        })
    lines.sort(key=lambda line: (line["side"] != "buy", line["symbol"]))
    return {
        "lines": lines, "missing": sorted(missing),
        "all_failed": any_target and not any_price and len(items) > 0,
        "by_list": by_list,
    }


def first_alert(*, items: pd.DataFrame, quotes: dict) -> dict | None:
    """The list and stock a new session opens on, or None when nothing is reached."""
    lines = alerts(items=items, quotes=quotes)["lines"]
    if not lines:
        return None
    return {"list_id": lines[0]["list_id"], "symbol": lines[0]["symbol"]}


def resolve_selection(
    *, stored_rows: list[int] | None, rows_seen: list[int] | None,
    order_seen: list[str], order: list[str], symbol: str | None,
) -> str | None:
    """Which stock is selected in this run, or None when no stock is.

    ``stored_rows`` is the table's selection as the browser last sent it (None
    before the first render); ``rows_seen`` is what the app itself wrote there
    last run. When they differ, the user clicked a row, and the row number is read
    with last run's order (``order_seen``) because an alert can reorder the rows.
    An empty ``stored_rows`` after a marked row means the user cleared the mark.
    Otherwise the current ``symbol`` stays if it is still listed; nothing is
    selected by default.
    """
    if not order:
        return None
    if stored_rows is not None and stored_rows != rows_seen:
        if not stored_rows:
            return None
        index = stored_rows[0]
        if 0 <= index < len(order_seen) and order_seen[index] in order:
            return order_seen[index]
    return symbol if symbol in order else None


# ---- schedule --------------------------------------------------------------

def is_active(*, setting: dict, now: dt.datetime) -> bool:
    moment = _aware(now=now)
    if not setting or setting.get("interval_minutes") is None:
        return False
    until = setting.get("until")
    return until is None or moment < until


def refresh_seconds(*, setting: dict, now: dt.datetime) -> int | None:
    """Seconds between timed redraws, or None when there is no active schedule."""
    return int(setting["interval_minutes"]) * 60 if is_active(setting=setting, now=now) else None


def quote_bucket(*, setting: dict, now: dt.datetime, session: str, nonce: int) -> str:
    """Cache key part for quotes: one fetch per session, or one per time window."""
    if not is_active(setting=setting, now=now):
        return f"{session}|{nonce}"
    window = int(_aware(now=now).timestamp()) // (int(setting["interval_minutes"]) * 60)
    return f"{nonce}|{window}"


def dialog_open(*, keys: set[str]) -> bool:
    return bool(DIALOG_FLAGS & set(keys))


def tick_should_rerun(
    *, setting: dict, now: dt.datetime, session: str, nonce: int,
    bucket_seen: str, dialog_open: bool,
) -> bool:
    """Whether a timer tick redraws the page: a new window started (or the
    schedule ended) and no dialog is open."""
    if dialog_open:
        return False
    return quote_bucket(setting=setting, now=now, session=session, nonce=nonce) != bucket_seen


def _clock(*, moment: dt.datetime, now: dt.datetime, tz: dt.tzinfo) -> str:
    local, today = moment.astimezone(tz), _aware(now=now).astimezone(tz).date()
    if local.date() == today:
        return local.strftime("%H:%M")
    return f"{local.day} {local.strftime('%b %H:%M')}"


def fetched_label(*, fetched: dt.datetime, now: dt.datetime, tz: dt.tzinfo) -> str:
    """Local time of a fetch; with the date when it was not today."""
    return _clock(moment=fetched, now=now, tz=tz)


def refresh_label(*, setting: dict, now: dt.datetime, tz: dt.tzinfo) -> str:
    if not is_active(setting=setting, now=now):
        return "only when I open the app"
    minutes = int(setting["interval_minutes"])
    if setting.get("until") is None:
        return f"every {minutes} min, no end time"
    return f"every {minutes} min until {_clock(moment=setting['until'], now=now, tz=tz)}"


def requests_per_hour(*, stocks: int, interval_minutes: int) -> int:
    return int(stocks) * 60 // int(interval_minutes)


def dialog_state(*, kind: str, payload: dict) -> dict:
    """Start value for every field of a dialog, by its ``wl_dlg_*`` widget key.

    Assigned to session state when the dialog opens: Streamlit ignores a widget's
    ``value=`` once its key exists, so the keys are what the fields show.
    """
    if kind == "new_list":
        return {"wl_dlg_name": ""}
    if kind == "rename_list":
        return {"wl_dlg_name": payload["name"]}
    if kind == "delete_list":
        return {}
    if kind == "add_stock":
        return {"wl_dlg_ticker": "", "wl_dlg_buy": None, "wl_dlg_sell": None}
    if kind == "edit_stock":
        return {
            "wl_dlg_list": payload["list_id"],
            "wl_dlg_buy": _opt(payload["buy_target"]),
            "wl_dlg_sell": _opt(payload["sell_target"]),
        }
    if kind == "schedule":
        minutes = payload["interval_minutes"]
        active = minutes is not None
        return {
            "wl_dlg_mode": SCHEDULE_EVERY if active else SCHEDULE_ONCE,
            "wl_dlg_minutes": int(minutes) if active else _DEFAULT_MINUTES,
            "wl_dlg_duration": DURATION_ALWAYS if active and payload["until"] is None else DURATION_HOURS,
            "wl_dlg_hours": _DEFAULT_HOURS,
        }
    raise ValueError(f"unknown dialog kind: {kind}")
