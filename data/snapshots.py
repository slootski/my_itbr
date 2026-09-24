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


def _connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
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
