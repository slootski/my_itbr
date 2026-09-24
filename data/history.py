"""Append-only local archive of the *windowed* Flex sections.

IBKR's Flex Web Service delivers a rolling window (this account:
``Last365CalendarDays``), so trades and cash transactions silently drop off the
back as they age — a fill from 14 months ago is simply absent from today's
statement, and the Flex Web Service cannot reach back for it. Open positions and
cost basis are unaffected (they are point-in-time as of the statement's
``toDate``), but realized P&L, the buy/sell chart markers and the whole dividend
history would lose their tail once the account is older than the window.

So every refresh folds the statement's rows into local tables keyed by a stable
identity, and the app reads the **union**: the current statement window plus
everything ever seen. Re-pulling is idempotent — a row already archived is
updated in place, never duplicated — and ``first_seen`` records when this store
first learned of it.

Identity, in order of preference:

1. IBKR's own ``tradeID`` / ``transactionID``, when the Flex Query includes those
   columns (they are opt-in per query, so they may be absent).
2. Otherwise a SHA-1 over the identifying fields. This is the fallback, not the
   default, because two partial fills of one order at the same price and second
   would hash alike and collapse into a single archived row.

NAV history gets the same treatment from the other direction: the statement's
``EquitySummaryInBase`` section carries one row *per day* over the whole window,
which is far denser than the one-per-refresh snapshots ``data/snapshots.py``
records. Backfilling it fills in every day the dashboard was never opened.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger("myitbr.history")

# Column sets mirror ibkr.parser.trades_df / cash_transactions_df exactly, so an
# archived frame is a drop-in replacement for a freshly parsed one.
TRADE_COLS = [
    "symbol", "asset_class", "trade_date", "datetime", "side", "quantity",
    "price", "proceeds", "commission", "realized_pnl", "currency",
    "trade_id", "transaction_id",
]
CASH_TX_COLS = [
    "type", "symbol", "amount", "currency", "fx_rate", "amount_base",
    "datetime", "report_date", "settle_date", "ex_date",
    "transaction_id", "action_id",
]

# Which columns must come back as datetime.date vs datetime.datetime. Rebuilding
# with the original Python types (rather than leaving ISO strings) keeps the
# archived frame byte-for-byte equivalent to the parsed one, so downstream date
# arithmetic in data/dividends.py behaves identically either way.
_TRADE_TYPES = {"trade_date": "date", "datetime": "datetime"}
_CASH_TX_TYPES = {
    "datetime": "datetime", "report_date": "date",
    "settle_date": "date", "ex_date": "date",
}
_TRADE_NUMS = {"quantity", "price", "proceeds", "commission", "realized_pnl"}
_CASH_TX_NUMS = {"amount", "fx_rate", "amount_base"}

# Fields hashed when IBKR gives us no id. Chosen to be the ones that actually
# identify the event; deliberately excludes derived figures like realized P&L,
# which IBKR may restate.
_TRADE_HASH_COLS = ["symbol", "trade_date", "datetime", "side", "quantity", "price", "proceeds"]
_CASH_TX_HASH_COLS = ["type", "symbol", "amount", "datetime", "report_date", "settle_date", "ex_date"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades_archive (
    trade_key      TEXT PRIMARY KEY,   -- IBKR tradeID when available, else a content hash
    symbol         TEXT,
    asset_class    TEXT,
    trade_date     TEXT,               -- ISO date
    datetime       TEXT,               -- ISO datetime
    side           TEXT,
    quantity       REAL,
    price          REAL,
    proceeds       REAL,
    commission     REAL,
    realized_pnl   REAL,
    currency       TEXT,
    trade_id       TEXT,
    transaction_id TEXT,
    first_seen     TEXT NOT NULL       -- ISO date this store first archived the row
);
CREATE TABLE IF NOT EXISTS cash_tx_archive (
    tx_key         TEXT PRIMARY KEY,   -- IBKR transactionID when available, else a content hash
    type           TEXT,
    symbol         TEXT,
    amount         REAL,               -- in `currency`, NOT comparable across rows
    currency       TEXT,
    fx_rate        REAL,               -- to the account's base currency
    amount_base    REAL,               -- amount * fx_rate; the only summable figure
    datetime       TEXT,               -- ISO datetime
    report_date    TEXT,               -- ISO date
    settle_date    TEXT,               -- ISO date
    ex_date        TEXT,               -- ISO date
    transaction_id TEXT,
    action_id      TEXT,
    first_seen     TEXT NOT NULL
);
"""


# Columns added after the first release. Existing stores predate them, and
# CREATE TABLE IF NOT EXISTS will not add a column to a table that already
# exists, so they are patched in on open. The row keys do not include these
# fields, so the next refresh upserts real values onto the existing rows rather
# than duplicating them.
_ADDED_COLUMNS = {
    "cash_tx_archive": [
        ("currency", "TEXT"),
        ("fx_rate", "REAL"),
        ("amount_base", "REAL"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                logger.info("history: added %s.%s", table, name)


def _connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def init_db(db_path: Path | str) -> None:
    """Create the archive tables if they do not yet exist."""
    with _connect(db_path) as conn:
        conn.commit()


# ---- value coercion -------------------------------------------------------

def _iso(value: Any) -> str | None:
    """Date/datetime -> ISO text for storage; blanks and NaT -> NULL."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    if value is None or value is pd.NaT:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def _text(value: Any) -> str | None:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _from_iso(text: Any, kind: str) -> Any:
    """Inverse of ``_iso`` — rebuild the original date/datetime object."""
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return parsed if kind == "datetime" else parsed.date()


def _row_key(row: dict, id_cols: Iterable[str], hash_cols: Iterable[str]) -> str:
    """Stable identity for an archived row.

    Prefers an IBKR-assigned id; falls back to a hash of the identifying fields
    so the archive still dedupes when the Flex Query omits the id columns.
    """
    for col in id_cols:
        ident = _text(row.get(col))
        if ident:
            return f"ib:{ident}"
    payload = "|".join(_iso(row.get(c)) or "" for c in hash_cols)
    return "sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _serialize(row: dict, cols: list[str], types: dict[str, str], nums: set[str]) -> list:
    out = []
    for col in cols:
        value = row.get(col)
        if col in types:
            out.append(_iso(value))
        elif col in nums:
            out.append(_num(value))
        else:
            out.append(_text(value))
    return out


# ---- writes ---------------------------------------------------------------

def _record(
    db_path: Path | str,
    df: pd.DataFrame,
    *,
    table: str,
    key_col: str,
    cols: list[str],
    types: dict[str, str],
    nums: set[str],
    id_cols: list[str],
    hash_cols: list[str],
    today: str | None = None,
) -> int:
    """Upsert ``df`` into ``table``; returns the number of rows written.

    ``first_seen`` is preserved on conflict — a restated row keeps the date this
    store first learned of it, so the archive can still show when coverage began.
    """
    if df is None or df.empty:
        return 0
    today = today or dt.date.today().isoformat()

    placeholders = ", ".join(["?"] * (len(cols) + 2))
    assignments = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO {table} ({key_col}, {', '.join(cols)}, first_seen) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT({key_col}) DO UPDATE SET {assignments}"
    )

    payload = []
    for row in df.to_dict("records"):
        key = _row_key(row, id_cols, hash_cols)
        payload.append([key, *_serialize(row, cols, types, nums), today])

    with _connect(db_path) as conn:
        conn.executemany(sql, payload)
        conn.commit()
    return len(payload)


def record_trades(db_path: Path | str, trades: pd.DataFrame, today: str | None = None) -> int:
    """Fold a statement's trades into the archive."""
    return _record(
        db_path, trades, table="trades_archive", key_col="trade_key",
        cols=TRADE_COLS, types=_TRADE_TYPES, nums=_TRADE_NUMS,
        id_cols=["trade_id", "transaction_id"], hash_cols=_TRADE_HASH_COLS, today=today,
    )


def record_cash_tx(db_path: Path | str, cash_tx: pd.DataFrame, today: str | None = None) -> int:
    """Fold a statement's cash transactions into the archive."""
    return _record(
        db_path, cash_tx, table="cash_tx_archive", key_col="tx_key",
        cols=CASH_TX_COLS, types=_CASH_TX_TYPES, nums=_CASH_TX_NUMS,
        id_cols=["transaction_id", "action_id"], hash_cols=_CASH_TX_HASH_COLS, today=today,
    )


def backfill_nav(db_path: Path | str, equity: pd.DataFrame, currency: str | None) -> int:
    """Seed ``nav_snapshots`` from the statement's daily equity summary.

    ``EquitySummaryInBase`` carries one row per day across the whole Flex window,
    while ``snapshots.record_snapshot`` only ever records days the dashboard was
    actually opened. Existing rows are left untouched (``DO NOTHING``): a row
    written by a live pull is the more authoritative record of that day, and
    never silently overwriting one keeps this backfill safe to re-run.
    """
    if equity is None or equity.empty or "report_date" not in equity:
        return 0

    rows = []
    for row in equity.to_dict("records"):
        date_iso = _iso(row.get("report_date"))
        if not date_iso:
            continue
        date_iso = date_iso[:10]
        rows.append((
            date_iso,
            f"{date_iso}T00:00:00",  # statement-derived: no pull happened that day
            _num(row.get("net_liq")),
            _num(row.get("cash")),
            currency,
        ))
    if not rows:
        return 0

    # snapshots.py owns this table's schema; _connect there creates it. Depend on
    # the same CREATE IF NOT EXISTS rather than duplicating the DDL.
    from data import snapshots

    snapshots.init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.executemany(
            "INSERT INTO nav_snapshots (snapshot_date, ts, net_liq, cash, currency) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(snapshot_date) DO NOTHING",
            rows,
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return written


# ---- reads ----------------------------------------------------------------

def _read(
    db_path: Path | str,
    *,
    table: str,
    cols: list[str],
    types: dict[str, str],
    order_by: str,
) -> pd.DataFrame:
    db_path = Path(db_path)
    if not db_path.exists():
        return pd.DataFrame(columns=cols)

    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order_by}"
        ).fetchall()

    rows = []
    for record in raw:
        row = dict(zip(cols, record))
        for col, kind in types.items():
            row[col] = _from_iso(row.get(col), kind)
        rows.append(row)
    # Same construction path as ibkr.parser._records_to_df, so pandas infers the
    # same dtypes (datetime64 for the datetime columns, object for plain dates).
    return pd.DataFrame(rows, columns=cols)


def read_trades(db_path: Path | str) -> pd.DataFrame:
    """Every trade ever archived, oldest first."""
    return _read(db_path, table="trades_archive", cols=TRADE_COLS,
                 types=_TRADE_TYPES, order_by="trade_date, datetime, symbol")


def read_cash_tx(db_path: Path | str) -> pd.DataFrame:
    """Every cash transaction ever archived, oldest first."""
    return _read(db_path, table="cash_tx_archive", cols=CASH_TX_COLS,
                 types=_CASH_TX_TYPES, order_by="settle_date, datetime, symbol")


def coverage(db_path: Path | str) -> dict:
    """Archive extent, for the UI caption. Never raises — display only."""
    out = {"trades": 0, "cash_tx": 0, "first_trade": None, "first_cash_tx": None}
    try:
        db_path = Path(db_path)
        if not db_path.exists():
            return out
        with _connect(db_path) as conn:
            out["trades"], out["first_trade"] = conn.execute(
                "SELECT COUNT(*), MIN(trade_date) FROM trades_archive").fetchone()
            out["cash_tx"], out["first_cash_tx"] = conn.execute(
                "SELECT COUNT(*), MIN(settle_date) FROM cash_tx_archive").fetchone()
    except sqlite3.Error as exc:
        logger.warning("coverage read failed: %s", exc)
    return out
