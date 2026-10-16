"""Daily portfolio performance: what the account earned or lost on each day.

A day's result is the change in net asset value between two consecutive
end-of-day closes, with external cash movement taken out — a deposit is new
capital, not a gain::

    pnl = nav - prev_nav - flow
    ret = nav / (prev_nav + flow) - 1

The flow sits in the denominator because that is the arithmetic IBKR's own
time-weighted return uses, so linked daily returns line up with IBKR's figures.
Periods (week, month, year) link daily returns geometrically; percents are
never summed.

Three rules the data forces:

* Only real end-of-day closes feed the series. ``nav_snapshots`` also holds
  live-pull rows (the latest figure filed under the pull date); those are not
  closes and are ignored here.
* A deposit moves NAV on its **report date**, not its settle date, so flows are
  dated by report date. ``capital._event_date`` is settle-first and is not used.
* The previous close is the previous *row*. Weekends have no close, so Monday
  is measured against Friday; a real gap in the data widens the same way and
  is exposed through ``prev_date``.
* IBKR files a row on a market holiday too, with NAV moved only by interest.
  It is not a trading day: it is dropped, and the next day is measured against
  the close before the holiday (``data.market_calendar``).

Dividends, payments in lieu and interest are part of the return, not a flow.
"""
from __future__ import annotations

import calendar as _calendar
import datetime as dt
import logging

import pandas as pd

from data import capital, div_ledger, market_calendar

logger = logging.getLogger("myitbr.performance")

CLOSE_COLS = ["date", "nav"]
DAILY_COLS = ["date", "nav", "prev_date", "prev_nav", "flow", "pnl", "ret"]

# Statement-derived nav_snapshots rows carry this time part; live pulls do not.
_STATEMENT_TS_SUFFIX = "T00:00:00"

# A deposit is dated by the day IBKR reports it: that is the day NAV moves.
_FLOW_DATE_COLS = ("report_date", "datetime", "settle_date")

# Cash transaction types that are a charge by the broker (a refund is the same
# type with the opposite sign). Trade commissions come from the trades instead.
FEE_TYPES = frozenset({"FEES", "COMMADJ", "ADVISORFEES"})

# Fee kinds the Fees card splits the total into, in display order. IBKR files
# currency conversions (e.g. USD.ILS) under asset class CASH.
FEE_TRADE, FEE_FX, FEE_OTHER = "trades", "fx", "other"
FEE_KINDS = (FEE_TRADE, FEE_FX, FEE_OTHER)
FX_ASSET_CLASS = "CASH"

# A result smaller than half a cent rounds to 0.00 on screen: that day is flat.
FLAT_EPSILON = 0.005

# Upper bounds of tint steps 1..3 by absolute daily return; anything at or
# above the last bound is step 4. Fixed (not relative to the data) so a colour
# means the same move in every month.
TINT_BOUNDS = (0.0025, 0.0075, 0.015)

# Calendar columns: Monday..Friday. Markets are closed on weekends, so those
# days never have a close and are not drawn.
WEEKDAYS = 5

GAIN, LOSS, FLAT = "gain", "loss", "flat"
NO_DATA, PENDING, EMPTY, CLOSED = "nodata", "pending", "empty", "closed"


def _to_date(*, value) -> dt.date | None:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        stamp = pd.to_datetime(value)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def _num(*, value) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def flows_by_day(*, cash_tx: pd.DataFrame | None) -> dict[dt.date, float]:
    """Net deposits and withdrawals per report date, in base currency.

    Rows without ``amount_base`` are skipped (their currency is unknown), as in
    ``capital.contributions``.
    """
    out: dict[dt.date, float] = {}
    if cash_tx is None or cash_tx.empty or "type" not in cash_tx:
        return out
    skipped = 0
    for row in cash_tx[cash_tx["type"].astype(str) == capital.DEPOSIT_TYPE].to_dict("records"):
        amount = _num(value=row.get("amount_base"))
        date = next(
            (d for d in (_to_date(value=row.get(col)) for col in _FLOW_DATE_COLS) if d is not None),
            None,
        )
        if amount is None or date is None:
            skipped += 1
            continue
        out[date] = out.get(date, 0.0) + amount
    if skipped:
        logger.warning("performance: %d deposit rows without a base amount or date skipped", skipped)
    return out


def closes(
    *, equity: pd.DataFrame | None, stored: pd.DataFrame | None, nav_history: pd.DataFrame | None,
) -> pd.DataFrame:
    """One end-of-day NAV per date, oldest first: ``date``, ``nav``.

    Sources, first one to have a date wins: the statement just pulled
    (``equity``), the stored closes (``equity_daily``), then the statement rows
    of ``nav_snapshots``. Live-pull rows are never used.
    """
    by_date: dict[dt.date, float] = {}

    def add(*, frame: pd.DataFrame | None, date_col: str) -> None:
        if frame is None or frame.empty or date_col not in frame or "net_liq" not in frame:
            return
        for row in frame.to_dict("records"):
            date, nav = _to_date(value=row.get(date_col)), _num(value=row.get("net_liq"))
            if date is not None and nav is not None and date not in by_date:
                by_date[date] = nav

    add(frame=equity, date_col="report_date")
    add(frame=stored, date_col="report_date")
    if nav_history is not None and not nav_history.empty and "ts" in nav_history:
        statement_rows = nav_history[
            nav_history["ts"].astype(str).str.endswith(_STATEMENT_TS_SUFFIX)
        ]
        add(frame=statement_rows, date_col="snapshot_date")

    rows = [{"date": date, "nav": nav} for date, nav in sorted(by_date.items())]
    return pd.DataFrame(rows, columns=CLOSE_COLS)


def daily(*, closes: pd.DataFrame, flows: dict[dt.date, float]) -> pd.DataFrame:
    """Per-close result, oldest first; see the module docstring for the formulas.

    The series starts at the first close with money in the account; that first
    row has no previous close inside the series only when no earlier close
    exists, and then carries no result. A flow dated on a day without a close
    is counted on the next close. ``ret`` is None when there was no capital to
    earn on (``prev_nav + flow <= 0``). Closes on a market holiday are dropped,
    so a flow on one counts on the next trading day.
    """
    if closes is None or closes.empty:
        return pd.DataFrame(columns=DAILY_COLS)
    records = [row for row in closes.to_dict("records")
               if not market_calendar.is_holiday(date=row["date"])]
    funded = next((i for i, row in enumerate(records) if row["nav"] > 0), None)
    if funded is None:
        return pd.DataFrame(columns=DAILY_COLS)

    flow_dates = sorted(flows)
    rows = []
    for index in range(funded, len(records)):
        row = records[index]
        prev = records[index - 1] if index > 0 else None
        out = {"date": row["date"], "nav": row["nav"], "prev_date": None, "prev_nav": None,
               "flow": 0.0, "pnl": None, "ret": None}
        if prev is not None:
            flow = sum(flows[d] for d in flow_dates if prev["date"] < d <= row["date"])
            base = prev["nav"] + flow
            out.update(
                prev_date=prev["date"], prev_nav=prev["nav"], flow=flow,
                pnl=row["nav"] - prev["nav"] - flow,
                ret=(row["nav"] / base - 1.0) if base > 0 else None,
            )
        rows.append(out)
    return pd.DataFrame(rows, columns=DAILY_COLS)


def state(*, pnl: float | None) -> str:
    """Gain, loss or flat for a day that has a result."""
    if pnl is None or abs(pnl) < FLAT_EPSILON:
        return FLAT
    return GAIN if pnl > 0 else LOSS


def tint_step(*, ret: float | None) -> int:
    """Colour strength 1..4 for a daily return; 0 when the return is undefined."""
    if ret is None:
        return 0
    size = abs(ret)
    for step, bound in enumerate(TINT_BOUNDS, start=1):
        if size < bound:
            return step
    return len(TINT_BOUNDS) + 1


def _records(*, daily: pd.DataFrame, start: dt.date, end: dt.date) -> list[dict]:
    """Rows of ``daily`` with a result, dated within ``start``..``end``."""
    if daily is None or daily.empty:
        return []
    return [
        row for row in daily.to_dict("records")
        if start <= row["date"] <= end and row["pnl"] is not None and not pd.isna(row["pnl"])
    ]


def _ret(*, row: dict) -> float | None:
    value = row.get("ret")
    return None if value is None or pd.isna(value) else float(value)


def summary(*, daily: pd.DataFrame, start: dt.date, end: dt.date) -> dict:
    """Totals for ``start``..``end``, both included.

    ``ret`` links the daily returns (``prod(1 + r) - 1``) and is None when no
    day in the range has one. ``best`` / ``worst`` are the days with the highest
    and lowest return (``date``, ``ret``, ``pnl``), or None.
    """
    rows = _records(daily=daily, start=start, end=end)
    rated = [row for row in rows if _ret(row=row) is not None]
    linked = None
    if rated:
        linked = 1.0
        for row in rated:
            linked *= 1.0 + _ret(row=row)
        linked -= 1.0

    def pick(*, row: dict | None) -> dict | None:
        if row is None:
            return None
        return {"date": row["date"], "ret": _ret(row=row), "pnl": float(row["pnl"])}

    states = [state(pnl=float(row["pnl"])) for row in rows]
    return {
        "days": len(rows),
        "pnl": sum(float(row["pnl"]) for row in rows) if rows else None,
        "ret": linked,
        "best": pick(row=max(rated, key=lambda row: _ret(row=row)) if rated else None),
        "worst": pick(row=min(rated, key=lambda row: _ret(row=row)) if rated else None),
        "up_days": states.count(GAIN),
        "down_days": states.count(LOSS),
    }


def months(*, daily: pd.DataFrame) -> list[tuple[int, int]]:
    """(year, month) for every month that has a close, newest first."""
    if daily is None or daily.empty:
        return []
    seen = {(date.year, date.month) for date in daily["date"]}
    return sorted(seen, reverse=True)


def _fee_items(
    *, trades: pd.DataFrame | None, cash_tx: pd.DataFrame | None,
) -> list[tuple[dt.date, str, float]]:
    """Every charge as ``(date, kind, cost)``, cost positive. ``kind`` is
    ``FEE_TRADE`` for a commission on a security trade, ``FEE_FX`` for one on
    a currency conversion (IBKR files those under asset class ``CASH``) and
    ``FEE_OTHER`` for a fee-type cash transaction."""
    out: list[tuple[dt.date, str, float]] = []
    if trades is not None and not trades.empty and "commission" in trades:
        for row in trades.to_dict("records"):
            date = _to_date(value=row.get("trade_date")) or _to_date(value=row.get("datetime"))
            commission = _num(value=row.get("commission"))
            if date is not None and commission:
                kind = FEE_FX if row.get("asset_class") == FX_ASSET_CLASS else FEE_TRADE
                out.append((date, kind, -commission))
    if cash_tx is not None and not cash_tx.empty and "type" in cash_tx:
        for row in cash_tx[cash_tx["type"].astype(str).isin(FEE_TYPES)].to_dict("records"):
            amount = _num(value=row.get("amount_base"))
            date = next(
                (d for d in (_to_date(value=row.get(col)) for col in _FLOW_DATE_COLS) if d is not None),
                None,
            )
            if date is not None and amount:
                out.append((date, FEE_OTHER, -amount))
    return out


def fees_by_day(
    *, trades: pd.DataFrame | None, cash_tx: pd.DataFrame | None,
) -> dict[dt.date, float]:
    """What the broker charged per day, as a positive cost: trade commissions
    (by trade date) plus fee-type cash transactions (by report date).

    Fees are already inside the day's result — NAV is net of them — so this is
    a breakdown, never something to subtract again. Commissions are taken as
    base currency: IBKR bills them in ``ibCommissionCurrency``, which the trade
    archive does not store, and for this account it is always the base currency.
    """
    out: dict[dt.date, float] = {}
    for date, _kind, cost in _fee_items(trades=trades, cash_tx=cash_tx):
        out[date] = out.get(date, 0.0) + cost
    return out


def fees_split(
    *, trades: pd.DataFrame | None, cash_tx: pd.DataFrame | None, start: dt.date, end: dt.date,
) -> dict[str, dict[str, float | int]]:
    """Fees within ``start``..``end`` (both included) by kind: ``{kind:
    {"amount": cost, "count": charges}}`` for each of ``FEE_KINDS``. The
    amounts add up to ``fees_total`` over the same days."""
    out: dict[str, dict[str, float | int]] = {kind: {"amount": 0.0, "count": 0} for kind in FEE_KINDS}
    for date, kind, cost in _fee_items(trades=trades, cash_tx=cash_tx):
        if start <= date <= end:
            out[kind]["amount"] += cost
            out[kind]["count"] += 1
    return out


def fees_total(*, fees: dict[dt.date, float], start: dt.date, end: dt.date) -> float:
    """Fees charged within ``start``..``end``, both included."""
    return sum(amount for date, amount in fees.items() if start <= date <= end)


MOVER_COLS = ["date", "symbol", "description", "price", "prev_price", "pct", "pnl"]


def symbol_days(*, statement: pd.DataFrame | None, stored: pd.DataFrame | None) -> pd.DataFrame:
    """Per day and symbol, oldest first: closing ``price``, ``prev_price`` (that
    symbol's previous row), ``pct`` (price change) and ``pnl`` (the day's
    mark-to-market P&L on the shares held coming into the day).

    The statement just pulled wins over the stored rows for the same day and
    symbol. ``pct`` is None on a symbol's first day and when either price is
    missing or not positive.
    """
    rows: dict[tuple[dt.date, str], dict] = {}
    for frame in (statement, stored):
        if frame is None or frame.empty or "date" not in frame or "symbol" not in frame:
            continue
        for row in frame.to_dict("records"):
            date, symbol = _to_date(value=row.get("date")), row.get("symbol")
            if date is None or not symbol or (date, str(symbol)) in rows:
                continue
            rows[(date, str(symbol))] = {
                "date": date, "symbol": str(symbol), "description": row.get("description"),
                "price": _num(value=row.get("price")), "pnl": _num(value=row.get("mtm_pnl")),
            }

    out, last_price = [], {}
    for (date, symbol), row in sorted(rows.items()):
        prev, price = last_price.get(symbol), row["price"]
        ok = prev is not None and price is not None and prev > 0 and price > 0
        out.append({**row, "prev_price": prev, "pct": (price / prev - 1.0) if ok else None})
        if price is not None:
            last_price[symbol] = price
    return pd.DataFrame(out, columns=MOVER_COLS)


def day_symbols(*, symbol_days: pd.DataFrame, date: dt.date) -> list[dict]:
    """One day's symbols, biggest gain in money first."""
    if symbol_days is None or symbol_days.empty:
        return []
    rows = symbol_days[symbol_days["date"] == date].to_dict("records")
    for row in rows:
        row["pct"] = None if row["pct"] is None or pd.isna(row["pct"]) else float(row["pct"])
        row["pnl"] = 0.0 if row["pnl"] is None or pd.isna(row["pnl"]) else float(row["pnl"])
    return sorted(rows, key=lambda row: row["pnl"], reverse=True)


def movers(*, symbol_days: pd.DataFrame) -> dict[dt.date, dict]:
    """Per day, the best and worst symbol by price change: ``{"best", "worst"}``,
    each ``{"symbol", "pct", "pnl"}``. ``worst`` is None on a day with a single
    rated symbol. Days without a rated symbol are absent."""
    out: dict[dt.date, dict] = {}
    if symbol_days is None or symbol_days.empty:
        return out
    by_day: dict[dt.date, list[dict]] = {}
    for row in symbol_days.to_dict("records"):
        if row["pct"] is None or pd.isna(row["pct"]):
            continue
        by_day.setdefault(row["date"], []).append({
            "symbol": row["symbol"], "pct": float(row["pct"]),
            "pnl": 0.0 if pd.isna(row["pnl"]) else float(row["pnl"]),
        })
    for date, rows in by_day.items():
        rows.sort(key=lambda row: row["pct"])
        out[date] = {"best": rows[-1], "worst": rows[0] if len(rows) > 1 else None}
    return out


def day_details(
    *, cash_tx: pd.DataFrame | None, trades: pd.DataFrame | None, movers: dict[dt.date, dict],
) -> dict[dt.date, dict]:
    """What else happened on a day: ``dividends`` (net, base currency), ``fees``
    (see ``fees_by_day``), ``bought`` and ``sold`` (number of fills), ``best``
    and ``worst`` (see ``movers``). Only days with something are present."""
    out: dict[dt.date, dict] = {}

    def entry(*, date: dt.date) -> dict:
        return out.setdefault(date, {"dividends": 0.0, "fees": 0.0, "bought": 0, "sold": 0,
                                     "best": None, "worst": None})

    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    for row in div_ledger.by_day(ledger=ledger, net=True).to_dict("records"):
        date = _to_date(value=row.get("date"))
        if date is not None:
            entry(date=date)["dividends"] += float(row["amount"])

    for date, amount in fees_by_day(trades=trades, cash_tx=cash_tx).items():
        entry(date=date)["fees"] += amount

    if trades is not None and not trades.empty and "side" in trades:
        for row in trades.to_dict("records"):
            date = _to_date(value=row.get("trade_date")) or _to_date(value=row.get("datetime"))
            side = str(row.get("side")).upper()
            if date is None or side not in ("BUY", "SELL"):
                continue
            entry(date=date)["bought" if side == "BUY" else "sold"] += 1

    for date, pair in movers.items():
        entry(date=date).update(pair)
    return out


def _cell(
    *, date: dt.date, in_month: bool, by_date: dict[dt.date, dict], details: dict[dt.date, dict],
    first: dt.date | None, last: dt.date | None, today: dt.date,
) -> dict:
    cell = {"date": date, "state": EMPTY, "step": 0, "ret": None, "pnl": None, "nav": None,
            "prev_nav": None, "prev_date": None, "flow": 0.0, "gap": False,
            "dividends": 0.0, "fees": 0.0, "bought": 0, "sold": 0, "best": None, "worst": None}
    if not in_month:
        return cell
    row = by_date.get(date)
    if row is None:
        if first is None or date < first or date > today:
            return cell
        if market_calendar.is_holiday(date=date):
            cell["state"] = CLOSED
        else:
            cell["state"] = PENDING if (last is not None and date > last) else NO_DATA
        return cell

    cell.update(details.get(date, {}))
    cell["nav"] = float(row["nav"])
    cell["flow"] = float(row["flow"])
    if row["pnl"] is None or pd.isna(row["pnl"]):
        # The series' very first close: nothing to compare it with.
        cell["state"] = FLAT
        return cell
    ret = _ret(row=row)
    cell.update(
        pnl=float(row["pnl"]), ret=ret, prev_nav=float(row["prev_nav"]), prev_date=row["prev_date"],
        state=state(pnl=float(row["pnl"])),
    )
    cell["step"] = tint_step(ret=ret) if cell["state"] != FLAT else 0
    # More than a weekend or a holiday between the two closes: a hole in the
    # data, so the figure covers several trading days.
    cell["gap"] = _trading_days_between(start=row["prev_date"], end=date) > 0
    return cell


def _trading_days_between(*, start: dt.date, end: dt.date) -> int:
    """Trading days (weekdays that are not market holidays) strictly between two dates."""
    count, day = 0, start + dt.timedelta(days=1)
    while day < end:
        if day.weekday() < WEEKDAYS and not market_calendar.is_holiday(date=day):
            count += 1
        day += dt.timedelta(days=1)
    return count


def month_weeks(
    *, daily: pd.DataFrame, details: dict[dt.date, dict], year: int, month: int, today: dt.date,
) -> list[dict]:
    """The month as calendar rows: ``{"days": [5 cells, Mon..Fri], "summary": {...}}``.

    Each cell has ``date``, ``state`` (gain / loss / flat / nodata / pending /
    closed / empty), ``step`` (tint strength), ``ret``, ``pnl``, ``nav``,
    ``prev_nav``, ``prev_date``, ``flow``, ``gap`` and the ``day_details``
    fields. A market holiday inside the covered range is ``closed``; any other
    weekday there with no close is ``nodata``; one after the last close, up to
    today, is ``pending``; days outside the month, before the first close or in
    the future are ``empty``. ``summary`` covers the week's days
    that belong to the month.
    """
    records = [] if daily is None or daily.empty else daily.to_dict("records")
    by_date = {row["date"]: row for row in records}
    first = records[0]["date"] if records else None
    last = records[-1]["date"] if records else None

    weeks = []
    for week in _calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        dates = week[:WEEKDAYS]
        in_month = [date for date in dates if date.month == month]
        if not in_month:
            continue
        cells = [
            _cell(date=date, in_month=date.month == month, by_date=by_date, details=details,
                  first=first, last=last, today=today)
            for date in dates
        ]
        weeks.append({
            "days": cells,
            "summary": summary(daily=daily, start=in_month[0], end=in_month[-1]),
        })
    return weeks


def month_range(*, year: int, month: int) -> tuple[dt.date, dt.date]:
    """First and last calendar day of a month."""
    return dt.date(year, month, 1), dt.date(year, month, _calendar.monthrange(year, month)[1])


def period_months(
    *, daily: pd.DataFrame, details: dict[dt.date, dict], fees: dict[dt.date, float],
    months: list[tuple[int, int]], today: dt.date,
) -> list[dict]:
    """One entry per ``(year, month)``: ``{"year", "month", "weeks", "summary",
    "fees"}``; ``fees`` is the month's total from ``fees_by_day``."""
    out = []
    for year, month in months:
        start, end = month_range(year=year, month=month)
        out.append({
            "year": year, "month": month,
            "weeks": month_weeks(daily=daily, details=details, year=year, month=month, today=today),
            "summary": summary(daily=daily, start=start, end=end),
            "fees": fees_total(fees=fees, start=start, end=end),
        })
    return out
