"""Daily on-disk cache for external declared-dividend lookups.

Polygon's free tier rate-limits to ~5 requests/min and the app needs one lookup
per held ticker with no Flex accrual, so a naive per-refresh fetch loses most
tickers to HTTP 429. This cache persists each ticker's declared-dividend result
(or a "no upcoming dividend" marker) for the calendar day in the same SQLite file
as the NAV snapshots, so the rate-limit cost is paid roughly once per day and
survives Refresh / restarts.

Records are JSON blobs with dates stored as ISO strings; ``get``/``put`` convert
to and from ``datetime.date``. Best-effort: a missing/unwritable DB degrades to a
cache miss rather than raising.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("myitbr.div_cache")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dividend_cache (
    symbol        TEXT PRIMARY KEY,   -- ticker
    payload       TEXT NOT NULL,      -- JSON: declared record, or {} when none
    fetched_date  TEXT NOT NULL       -- ISO date the lookup was made (daily TTL)
);
"""

# Keys whose values are ISO date strings on disk and ``datetime.date`` in memory.
_DATE_FIELDS = ("ex_date", "pay_date", "decl_date")


def _connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    return conn


def _encode(payload: dict) -> str:
    out = dict(payload)
    for k in _DATE_FIELDS:
        v = out.get(k)
        if isinstance(v, dt.date):
            out[k] = v.isoformat()
    return json.dumps(out)


def _decode(text: str) -> dict:
    out = json.loads(text)
    for k in _DATE_FIELDS:
        v = out.get(k)
        if isinstance(v, str) and v:
            try:
                out[k] = dt.date.fromisoformat(v)
            except ValueError:
                out[k] = None
    return out


def get(db_path: Path | str, symbol: str) -> tuple[dict, str] | None:
    """Return ``(payload, fetched_date)`` for ``symbol`` or ``None`` on miss.

    ``payload`` is the decoded declared record (``{}`` when the cached result was
    "no upcoming dividend"). The caller decides whether ``fetched_date`` is fresh.
    """
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT payload, fetched_date FROM dividend_cache WHERE symbol = ?",
                (symbol,),
            ).fetchone()
    except (sqlite3.Error, OSError):
        logger.info("div_cache.get(%s) failed", symbol, exc_info=True)
        return None
    if row is None:
        return None
    return _decode(row[0]), row[1]


def put(db_path: Path | str, symbol: str, payload: dict, today: dt.date) -> None:
    """Upsert ``symbol``'s declared record (or ``{}``) stamped with ``today``."""
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO dividend_cache (symbol, payload, fetched_date)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    payload=excluded.payload,
                    fetched_date=excluded.fetched_date
                """,
                (symbol, _encode(payload), today.isoformat()),
            )
            conn.commit()
    except (sqlite3.Error, OSError):
        logger.info("div_cache.put(%s) failed", symbol, exc_info=True)
