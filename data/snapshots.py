"""SQLite store for net-worth-over-time.

IBKR exposes no NAV history, so we record one snapshot per successful Flex pull
(deduplicated by calendar date) and chart the trend from this local table.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nav_snapshots (
    snapshot_date TEXT PRIMARY KEY,   -- ISO date (YYYY-MM-DD), one row per day
    ts            TEXT NOT NULL,       -- full ISO timestamp of the pull
    net_liq       REAL,
    cash          REAL,
    currency      TEXT
);
"""

# IBKR's own end-of-day close per trading day, from the statement's
# EquitySummaryInBase. Kept apart from nav_snapshots because that table also
# holds live-pull rows (the latest figure filed under the pull date), which are
# not a close and, being insert-only, keep the true close for that date out.
# Daily performance needs real closes, so only statement rows are written here.
EQUITY_DAILY_COLS = ["report_date", "net_liq", "cash", "stock", "currency"]

_EQUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_daily (
    report_date TEXT PRIMARY KEY,   -- ISO date (YYYY-MM-DD), one row per trading day
    net_liq     REAL,
    cash        REAL,
    stock       REAL,
    currency    TEXT,
    first_seen  TEXT NOT NULL       -- ISO date the row was first written
);
"""

# Per day and symbol: closing price and the day's mark-to-market P&L, from the
# statement's PriorPeriodPositions. Like the closes, IBKR serves these for the
# last 365 days only, so they are kept here to explain older days too.
SYMBOL_DAILY_COLS = ["date", "symbol", "description", "price", "mtm_pnl", "currency"]

_SYMBOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_daily (
    day_key     TEXT PRIMARY KEY,   -- "<ISO date>|<symbol>"
    date        TEXT NOT NULL,      -- ISO date (YYYY-MM-DD)
    symbol      TEXT NOT NULL,
    description TEXT,
    price       REAL,
    mtm_pnl     REAL,
    currency    TEXT,
    first_seen  TEXT NOT NULL       -- ISO date the row was first written
);
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    conn.execute(_EQUITY_SCHEMA)
    conn.execute(_SYMBOL_SCHEMA)
    return conn


def init_db(db_path: Path | str) -> None:
    """Create the snapshots table if it does not yet exist."""
    with _connect(db_path) as conn:
        conn.commit()


def record_snapshot(
    db_path: Path | str,
    *,
    net_liq: float | None,
    cash: float | None,
    currency: str | None,
    snapshot_date: str | None = None,
    ts: str | None = None,
) -> None:
    """Upsert today's NAV snapshot (one row per calendar date).

    Re-pulling the same day overwrites that day's row rather than duplicating it.
    """
    import datetime as dt

    snapshot_date = snapshot_date or dt.date.today().isoformat()
    ts = ts or dt.datetime.now().isoformat(timespec="seconds")

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO nav_snapshots (snapshot_date, ts, net_liq, cash, currency)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                ts=excluded.ts,
                net_liq=excluded.net_liq,
                cash=excluded.cash,
                currency=excluded.currency
            """,
            (snapshot_date, ts, net_liq, cash, currency),
        )
        conn.commit()


def read_history(db_path: Path | str) -> pd.DataFrame:
    """Return all NAV snapshots ordered by date (empty DataFrame if none)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return pd.DataFrame(columns=["snapshot_date", "ts", "net_liq", "cash", "currency"])

    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT snapshot_date, ts, net_liq, cash, currency "
            "FROM nav_snapshots ORDER BY snapshot_date",
            conn,
        )
    if not df.empty:
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df


def read_equity_daily(*, db_path: Path | str) -> pd.DataFrame:
    """Every stored end-of-day close, oldest first (empty DataFrame if none).

    ``report_date`` comes back as ``datetime.date``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return pd.DataFrame(columns=EQUITY_DAILY_COLS)

    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            f"SELECT {', '.join(EQUITY_DAILY_COLS)} FROM equity_daily ORDER BY report_date",
            conn,
        )
    if not df.empty:
        df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
    return df


def read_symbol_daily(*, db_path: Path | str) -> pd.DataFrame:
    """Every stored per-symbol day, oldest first (empty DataFrame if none).

    ``date`` comes back as ``datetime.date``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return pd.DataFrame(columns=SYMBOL_DAILY_COLS)

    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            f"SELECT {', '.join(SYMBOL_DAILY_COLS)} FROM symbol_daily ORDER BY date, symbol",
            conn,
        )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df
