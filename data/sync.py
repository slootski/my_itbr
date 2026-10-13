"""Insert-only sync of a Flex statement into the local store.

Every load pulls everything IBKR still serves (the last 365 days). This module
compares that statement with the DB in two steps:

1. ``plan`` is read-only. It lists what IBKR has that the DB does not: missing
   trades, cash transactions and NAV days, plus NULL columns in existing rows
   that IBKR now fills. It also reports rows where IBKR and the DB disagree.
2. ``apply`` writes only the missing data, in one transaction. Existing values
   are never overwritten — the DB is the only copy of history older than the
   Flex window, so a restated IBKR row is reported, not written.

Row identity is ``history._row_key`` with the same id/hash columns the archive
has always used. Computing it any other way would make every archived row look
missing and insert duplicates under new keys.

NAV differences are compared only for statement-derived rows (``ts`` at
midnight, as ``history.backfill_nav`` writes them). A live-pull row records the
statement's latest equity figure under the day of the pull, so it legitimately
differs from IBKR's end-of-day value for that date and is not a conflict.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from data import history, snapshots

logger = logging.getLogger("myitbr.sync")

# Numbers equal within this tolerance are the same value (REAL round-trips).
_NUM_TOLERANCE = 1e-6

_NAV_COLS = ["ts", "net_liq", "cash", "currency"]
_NAV_NUMS = {"net_liq", "cash"}
# Statement-derived NAV rows carry this time part (see history.backfill_nav).
_STATEMENT_TS_SUFFIX = "T00:00:00"

# equity_daily value columns (everything but the report_date key).
_EQUITY_COLS = [c for c in snapshots.EQUITY_DAILY_COLS if c != "report_date"]
_EQUITY_NUMS = {"net_liq", "cash", "stock"}

# symbol_daily value columns (the key is "<date>|<symbol>").
_SYMBOL_COLS = snapshots.SYMBOL_DAILY_COLS
_SYMBOL_NUMS = {"price", "mtm_pnl"}
# The key columns can never differ on a row found by its key.
_SYMBOL_COMPARE_COLS = [c for c in _SYMBOL_COLS if c not in ("date", "symbol")]
# Tables whose added rows are counted in the log, not listed: one load can add
# thousands, which would push the rest of the history out of the rotating log.
_BULK_TABLES = frozenset({"symbol_daily"})


def _table_specs() -> list[dict]:
    """Archive tables in the same shape ``history._record`` writes them."""
    return [
        {
            "name": "trades", "table": "trades_archive", "key_col": "trade_key",
            "cols": history.TRADE_COLS, "types": history._TRADE_TYPES,
            "nums": history._TRADE_NUMS, "id_cols": ["trade_id", "transaction_id"],
            "hash_cols": history._TRADE_HASH_COLS,
        },
        {
            "name": "cash_tx", "table": "cash_tx_archive", "key_col": "tx_key",
            "cols": history.CASH_TX_COLS, "types": history._CASH_TX_TYPES,
            "nums": history._CASH_TX_NUMS, "id_cols": ["transaction_id"],
            "hash_cols": history._CASH_TX_HASH_COLS,
        },
    ]


def _same(*, old, new, numeric: bool) -> bool:
    if numeric:
        return abs(float(old) - float(new)) <= _NUM_TOLERANCE
    return str(old) == str(new)


def _compare(
    *, key: str, db_row: dict, new_row: dict, cols: list[str], nums: set[str],
    fills: list[dict], diffs: list[dict], table: str, label: dict,
) -> None:
    """Sort each column of a row present on both sides into a fill or a diff."""
    for col in cols:
        new, old = new_row.get(col), db_row.get(col)
        if new is None:
            continue
        if old is None:
            fills.append({"table": table, "key": key, "column": col, "value": new, **label})
        elif not _same(old=old, new=new, numeric=col in nums):
            diffs.append({"table": table, "key": key, "column": col,
                          "db": old, "ibkr": new, **label})


def _label(*, row: dict) -> dict:
    """The fields that let a person recognise a row in the report."""
    date = row.get("trade_date") or row.get("settle_date") or row.get("report_date")
    amount = row.get("proceeds") if "proceeds" in row else row.get("amount")
    return {"date": date, "symbol": row.get("symbol"), "type": row.get("type") or row.get("side"),
            "amount": amount}


def _plan_archive(*, conn: sqlite3.Connection, spec: dict, df: pd.DataFrame) -> dict:
    out = {"missing": [], "fills": [], "diffs": []}
    if df is None or df.empty:
        return out

    # Same-key rows collapse to the last one, as history._record's upsert did,
    # so the report counts exactly what apply will write.
    incoming: dict[str, dict] = {}
    for row in df.to_dict("records"):
        key = history._row_key(row=row, id_cols=spec["id_cols"], hash_cols=spec["hash_cols"])
        values = history._serialize(row=row, cols=spec["cols"], types=spec["types"], nums=spec["nums"])
        incoming[key] = dict(zip(spec["cols"], values))

    cols_sql = ", ".join(spec["cols"])
    existing = {
        record[0]: dict(zip(spec["cols"], record[1:]))
        for record in conn.execute(f"SELECT {spec['key_col']}, {cols_sql} FROM {spec['table']}")
    }
    for key, row in incoming.items():
        if key not in existing:
            out["missing"].append({"key": key, "row": row, **_label(row=row)})
        else:
            _compare(key=key, db_row=existing[key], new_row=row, cols=spec["cols"],
                     nums=spec["nums"], fills=out["fills"], diffs=out["diffs"],
                     table=spec["table"], label=_label(row=row))
    return out


def _nav_rows(*, equity: pd.DataFrame, currency: str | None) -> dict[str, dict]:
    """Statement NAV rows keyed by ISO date, in the backfill_nav shape."""
    rows: dict[str, dict] = {}
    if equity is None or equity.empty or "report_date" not in equity:
        return rows
    for row in equity.to_dict("records"):
        date_iso = history._iso(value=row.get("report_date"))
        if not date_iso:
            continue
        date_iso = date_iso[:10]
        rows[date_iso] = {
            "ts": f"{date_iso}{_STATEMENT_TS_SUFFIX}",
            "net_liq": history._num(value=row.get("net_liq")),
            "cash": history._num(value=row.get("cash")),
            "currency": currency,
        }
    return rows


def _plan_nav(
    *, conn: sqlite3.Connection, equity: pd.DataFrame, nav: dict, today: str, now: str,
) -> dict:
    out = {"missing": [], "fills": [], "diffs": []}
    incoming = _nav_rows(equity=equity, currency=nav.get("currency"))
    # Today's live snapshot joins as one more candidate row: insert-only, like
    # every other day, so the first pull of the day sets it.
    if today not in incoming and (nav.get("net_liq") is not None or nav.get("cash") is not None):
        incoming[today] = {"ts": now, "net_liq": history._num(value=nav.get("net_liq")),
                           "cash": history._num(value=nav.get("cash")), "currency": nav.get("currency")}

    existing = {
        record[0]: dict(zip(_NAV_COLS, record[1:]))
        for record in conn.execute(
            f"SELECT snapshot_date, {', '.join(_NAV_COLS)} FROM nav_snapshots")
    }
    for date_iso, row in sorted(incoming.items()):
        label = {"date": date_iso, "symbol": None, "type": "NAV", "amount": row["net_liq"]}
        db_row = existing.get(date_iso)
        if db_row is None:
            out["missing"].append({"key": date_iso, "row": row, **label})
            continue
        statement_row = str(db_row.get("ts") or "").endswith(_STATEMENT_TS_SUFFIX)
        # ts is bookkeeping, never a fill or a difference.
        cols = [c for c in _NAV_COLS if c != "ts"]
        diffs: list[dict] = []
        _compare(key=date_iso, db_row=db_row, new_row=row, cols=cols, nums=_NAV_NUMS,
                 fills=out["fills"], diffs=diffs, table="nav_snapshots", label=label)
        if statement_row:
            out["diffs"].extend(diffs)
    return out


def _plan_equity(*, conn: sqlite3.Connection, equity: pd.DataFrame, currency: str | None) -> dict:
    """End-of-day closes the statement has and ``equity_daily`` lacks.

    Statement rows only: today's live figure is not a close and never goes in.
    """
    out = {"missing": [], "fills": [], "diffs": []}
    if equity is None or equity.empty or "report_date" not in equity:
        return out
    incoming: dict[str, dict] = {}
    for row in equity.to_dict("records"):
        date_iso = history._iso(value=row.get("report_date"))
        if not date_iso:
            continue
        incoming[date_iso[:10]] = {
            "net_liq": history._num(value=row.get("net_liq")),
            "cash": history._num(value=row.get("cash")),
            "stock": history._num(value=row.get("stock")),
            "currency": currency,
        }

    existing = {
        record[0]: dict(zip(_EQUITY_COLS, record[1:]))
        for record in conn.execute(
            f"SELECT report_date, {', '.join(_EQUITY_COLS)} FROM equity_daily")
    }
    for date_iso, row in sorted(incoming.items()):
        label = {"date": date_iso, "symbol": None, "type": "CLOSE", "amount": row["net_liq"]}
        db_row = existing.get(date_iso)
        if db_row is None:
            out["missing"].append({"key": date_iso, "row": row, **label})
        else:
            _compare(key=date_iso, db_row=db_row, new_row=row, cols=_EQUITY_COLS,
                     nums=_EQUITY_NUMS, fills=out["fills"], diffs=out["diffs"],
                     table="equity_daily", label=label)
    return out


def _plan_symbols(*, conn: sqlite3.Connection, symbols: pd.DataFrame | None) -> dict:
    """Per-symbol days the statement has and ``symbol_daily`` lacks."""
    out = {"missing": [], "fills": [], "diffs": []}
    if symbols is None or symbols.empty or "date" not in symbols or "symbol" not in symbols:
        return out
    incoming: dict[str, dict] = {}
    for row in symbols.to_dict("records"):
        date_iso, symbol = history._iso(value=row.get("date")), history._text(value=row.get("symbol"))
        if not date_iso or not symbol:
            continue
        date_iso = date_iso[:10]
        incoming[f"{date_iso}|{symbol}"] = {
            "date": date_iso, "symbol": symbol,
            "description": history._text(value=row.get("description")),
            "price": history._num(value=row.get("price")),
            "mtm_pnl": history._num(value=row.get("mtm_pnl")),
            "currency": history._text(value=row.get("currency")),
        }

    existing = {
        record[0]: dict(zip(_SYMBOL_COLS, record[1:]))
        for record in conn.execute(f"SELECT day_key, {', '.join(_SYMBOL_COLS)} FROM symbol_daily")
    }
    for key, row in sorted(incoming.items()):
        label = {"date": row["date"], "symbol": row["symbol"], "type": "MTM", "amount": row["mtm_pnl"]}
        db_row = existing.get(key)
        if db_row is None:
            out["missing"].append({"key": key, "row": row, **label})
        else:
            _compare(key=key, db_row=db_row, new_row=row, cols=_SYMBOL_COMPARE_COLS,
                     nums=_SYMBOL_NUMS, fills=out["fills"], diffs=out["diffs"],
                     table="symbol_daily", label=label)
    return out


def make_window(
    *, requested: tuple[dt.date, dt.date] | None, returned_from, returned_to,
) -> dict:
    """Requested vs returned statement dates, for the report."""
    def iso(value) -> str | None:
        return value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else (
            str(value) if value else None)

    days = None
    if isinstance(returned_from, dt.date) and isinstance(returned_to, dt.date):
        days = (returned_to - returned_from).days + 1
    return {
        "requested_from": iso(requested[0]) if requested else None,
        "requested_to": iso(requested[1]) if requested else None,
        "returned_from": iso(returned_from),
        "returned_to": iso(returned_to),
        "days": days,
    }


def plan(
    *,
    db_path: Path | str,
    trades: pd.DataFrame,
    cash_tx: pd.DataFrame,
    equity: pd.DataFrame,
    symbols: pd.DataFrame | None,
    nav: dict,
    window: dict,
    today: str,
    now: str,
) -> dict:
    """Compare the statement with the DB; read-only.

    Returns plain dicts and lists (st.cache_data serializable)::

        {"tables": {"trades"|"cash_tx"|"nav_snapshots"|"equity_daily"|"symbol_daily":
                       {"missing": [...], "fills": [...], "diffs": [...]}},
         "window": {...}, "written": 0, "error": None}
    """
    snapshots.init_db(db_path=db_path)
    with history._connect(db_path=db_path) as conn:
        tables = {spec["name"]: _plan_archive(conn=conn, spec=spec, df=df)
                  for spec, df in zip(_table_specs(), (trades, cash_tx))}
        tables["nav_snapshots"] = _plan_nav(conn=conn, equity=equity, nav=nav, today=today, now=now)
        tables["equity_daily"] = _plan_equity(conn=conn, equity=equity, currency=nav.get("currency"))
        tables["symbol_daily"] = _plan_symbols(conn=conn, symbols=symbols)
    return {"tables": tables, "window": window, "written": 0, "error": None}


def pending(*, report: dict) -> int:
    """Rows to insert plus fields to fill."""
    return sum(len(t["missing"]) + len(t["fills"]) for t in report["tables"].values())


def differences(*, report: dict) -> int:
    return sum(len(t["diffs"]) for t in report["tables"].values())


def apply(*, db_path: Path | str, report: dict, today: str) -> int:
    """Write the plan's missing rows and NULL fills in one transaction.

    Inserts use ``ON CONFLICT DO NOTHING`` and fills use ``WHERE col IS NULL``,
    so a row written between plan and apply is still never overwritten. Returns
    the number of rows inserted plus fields filled; raises on a DB error, after
    the transaction has rolled back.
    """
    tables = report["tables"]
    written = 0
    conn = history._connect(db_path=db_path)
    try:
        with conn:  # commits on success, rolls back on any exception
            for spec in _table_specs():
                part = tables[spec["name"]]
                cols = spec["cols"]
                placeholders = ", ".join(["?"] * (len(cols) + 2))
                insert = (
                    f"INSERT INTO {spec['table']} ({spec['key_col']}, {', '.join(cols)}, first_seen) "
                    f"VALUES ({placeholders}) ON CONFLICT({spec['key_col']}) DO NOTHING"
                )
                for item in part["missing"]:
                    cur = conn.execute(insert, [item["key"], *[item["row"][c] for c in cols], today])
                    written += max(cur.rowcount, 0)
                for fill in part["fills"]:
                    cur = conn.execute(
                        f"UPDATE {spec['table']} SET {fill['column']} = ? "
                        f"WHERE {spec['key_col']} = ? AND {fill['column']} IS NULL",
                        (fill["value"], fill["key"]),
                    )
                    written += max(cur.rowcount, 0)

            nav = tables["nav_snapshots"]
            for item in nav["missing"]:
                row = item["row"]
                cur = conn.execute(
                    "INSERT INTO nav_snapshots (snapshot_date, ts, net_liq, cash, currency) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(snapshot_date) DO NOTHING",
                    (item["key"], row["ts"], row["net_liq"], row["cash"], row["currency"]),
                )
                written += max(cur.rowcount, 0)
            for fill in nav["fills"]:
                cur = conn.execute(
                    f"UPDATE nav_snapshots SET {fill['column']} = ? "
                    f"WHERE snapshot_date = ? AND {fill['column']} IS NULL",
                    (fill["value"], fill["key"]),
                )
                written += max(cur.rowcount, 0)

            closes = tables["equity_daily"]
            for item in closes["missing"]:
                row = item["row"]
                cur = conn.execute(
                    f"INSERT INTO equity_daily (report_date, {', '.join(_EQUITY_COLS)}, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(report_date) DO NOTHING",
                    (item["key"], *[row[c] for c in _EQUITY_COLS], today),
                )
                written += max(cur.rowcount, 0)
            for fill in closes["fills"]:
                cur = conn.execute(
                    f"UPDATE equity_daily SET {fill['column']} = ? "
                    f"WHERE report_date = ? AND {fill['column']} IS NULL",
                    (fill["value"], fill["key"]),
                )
                written += max(cur.rowcount, 0)

            per_symbol = tables["symbol_daily"]
            placeholders = ", ".join(["?"] * (len(_SYMBOL_COLS) + 2))
            for item in per_symbol["missing"]:
                cur = conn.execute(
                    f"INSERT INTO symbol_daily (day_key, {', '.join(_SYMBOL_COLS)}, first_seen) "
                    f"VALUES ({placeholders}) ON CONFLICT(day_key) DO NOTHING",
                    (item["key"], *[item["row"][c] for c in _SYMBOL_COLS], today),
                )
                written += max(cur.rowcount, 0)
            for fill in per_symbol["fills"]:
                cur = conn.execute(
                    f"UPDATE symbol_daily SET {fill['column']} = ? "
                    f"WHERE day_key = ? AND {fill['column']} IS NULL",
                    (fill["value"], fill["key"]),
                )
                written += max(cur.rowcount, 0)
    finally:
        conn.close()
    return written


def log_report(*, report: dict) -> None:
    """One INFO summary line, then one line per added row and per difference."""
    parts = []
    for name, part in report["tables"].items():
        parts.append(f"{name}: {len(part['missing'])} missing, {len(part['fills'])} fills, "
                     f"{len(part['diffs'])} diffs")
    window = report["window"]
    logger.info(
        "sync: window %s..%s (%s days) | %s | written %d%s",
        window["returned_from"], window["returned_to"], window["days"], "; ".join(parts),
        report["written"], f" | error: {report['error']}" if report["error"] else "",
    )
    for name, part in report["tables"].items():
        for item in ([] if name in _BULK_TABLES else part["missing"]):
            logger.info("sync: missing %s %s %s %s %s", name, item["date"], item["symbol"] or "",
                        item["type"] or "", item["amount"])
        for fill in part["fills"]:
            logger.info("sync: fill %s %s.%s = %s", name, fill["key"], fill["column"], fill["value"])
        for diff in part["diffs"]:
            logger.warning("sync: IBKR differs %s %s %s.%s db=%s ibkr=%s (kept db)", name,
                           diff["date"], diff["key"], diff["column"], diff["db"], diff["ibkr"])
