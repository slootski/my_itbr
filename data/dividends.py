"""Build the dividend-projection calendar from parsed Flex DataFrames.

Turns IBKR paid dividends (cash transactions) and declared-but-unpaid dividends
(open accruals) into a per-ticker / per-month grid the Dividends tab renders.

Rows are held tickers; columns are chronological months. Each cell carries a
status that drives its colour (status, not value):

  - ``paid``     (green)  — actual dividend received.        amount shown.
  - ``declared`` (yellow) — open accrual, known but unpaid.  amount shown.
  - ``declare``  (red)    — projected upcoming declaration.  shows "Declare".
  - ``pay``      (red)    — projected upcoming payment.      shows "Pay" (the
                            amount is unknown until declared — never a number).

When the projected declaration and payment fall in different months, BOTH red
cells appear; when they coincide, a single "Declare" cell marks the horizon.
Red cells are ESTIMATES projected from each ticker's historical cadence; IBKR
exposes no forward projection of undeclared dividends. The cadence fixes the
month; the day within it is the median day-of-month of that ticker's past
ex-dates/payments, and the tooltip marks it "~ ... (estimated)" so it never
reads as a confirmed date. A ticker is projected only
when it has no open accrual covering a payment in the current month or later, and
only up to that single next declaration/payment — nothing beyond (the forward
horizon).

Past months are paid-only: declared accruals render in their payment month,
current month or later — a stale accrual never rewrites history.

Net amounts: declared/paid use the real Flex net (paid net folds in WHTAX, which
is negative). When IBKR withheld nothing (net equals gross), ``DEFAULT_TAX_RATE``
is applied instead, so after-tax never shows an untaxed gross figure. Red cells
carry no amount, so tax mode does not affect them.

Tickers with no Flex events at all can optionally be backfilled from an
external source (``external=`` in :func:`build_calendar`): Polygon
(``data.div_provider``) supplies a real declared per-share amount, rendered as a
yellow ``declared`` cell (amount x shares held); yfinance (``data.div_external``)
supplies dates only — concrete upcoming ex/pay dates render as red cells, else
the external ex-date history drives the same cadence projection used for Flex
data. Tooltips name the source (polygon / yfinance).
"""
from __future__ import annotations

import calendar as _calendar
import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger("myitbr.dividends")

# Cash-transaction types that are dividend income (positive) vs withholding tax.
_DIVIDEND_TYPES = {"DIVIDEND", "PAYMENTINLIEU"}
_WHTAX_TYPE = "WHTAX"
# Tax rate for after-tax mode when IBKR withheld nothing on a dividend (e.g.
# HAFN, a Singapore issuer with 0% withholding), and for external (Polygon)
# amounts with no real withholding history to derive a rate from.
DEFAULT_TAX_RATE = 0.25
# Minimum number of distinct paid months needed to infer a cadence to project.
_MIN_PAID_FOR_PROJECTION = 2
# Real (day-level) pay-date gap at/under which a ticker is treated as a
# weekly payer (e.g. CONY, MSTY) rather than projected on whole-month cadence.
_WEEKLY_GAP_MAX_DAYS = 10
# Distinct paid dates needed before a short gap is trusted as weekly cadence,
# rather than one early/late payment skewing a thin history.
_MIN_PAY_DATES_FOR_WEEKLY = 3


@dataclass
class Calendar:
    """Render-ready calendar matrices.

    ``values``, ``status`` and ``tooltips`` share the same index (symbols) and
    columns (``YYYY-MM`` strings, chronological). ``values`` holds display strings
    ("" when empty); ``status`` holds one of paid/declared/pay/declare ("" when
    empty); ``tooltips`` holds hover text ("" when none). ``totals`` is the
    per-month sum of displayed paid+declared amounts (formatted, "" when none).
    ``paid_sum`` is the trailing sum of paid dividends over the lookback window
    ending at (and including) the current month. ``no_data`` lists held tickers
    with no dividend events at all (their rows are empty placeholders).
    ``projected`` lists tickers carrying a red projected (declare/pay) cell —
    the ones an external declared-amount lookup can still upgrade.
    """
    values: pd.DataFrame
    status: pd.DataFrame
    tooltips: pd.DataFrame
    current_ym: str
    totals: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))
    paid_sum: float = 0.0
    no_data: set[str] = field(default_factory=set)
    projected: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return self.values.empty


# ---- small helpers --------------------------------------------------------

def _to_date(value) -> dt.date | None:
    # NaT must be caught before the datetime isinstance check: pd.NaT IS a
    # datetime instance, and letting it through breaks date fallback chains.
    if value is None or value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else ts.date()


def _ym(date: dt.date) -> str:
    return f"{date.year:04d}-{date.month:02d}"


def _idx(ym: str) -> int:
    """Month index (year*12 + month-1) for ordering and gap arithmetic."""
    y, m = ym.split("-")
    return int(y) * 12 + (int(m) - 1)


def _ym_from_idx(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _add_months(ym: str, n: int) -> str:
    return _ym_from_idx(_idx(ym) + n)


def month_label(ym: str) -> str:
    """Display label for a ``YYYY-MM`` key: "Jun 2026"."""
    y, m = ym.split("-")
    return f"{_calendar.month_abbr[int(m)]} {y}"


def _mode(values) -> int | None:
    """Most common value; ties broken toward the smallest."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    counts = Counter(vals)
    top = max(counts.values())
    return min(v for v, c in counts.items() if c == top)


def _held_symbols(positions_df: pd.DataFrame) -> set[str]:
    if positions_df is None or positions_df.empty or "symbol" not in positions_df:
        return set()
    return {str(s) for s in positions_df["symbol"].dropna().unique()}


def _shares_by_symbol(positions_df: pd.DataFrame) -> dict[str, float]:
    """symbol -> total held quantity, for converting a per-share external amount
    into an account cash figure. Sums duplicate rows; ignores unparseable qtys."""
    out: dict[str, float] = {}
    if positions_df is None or positions_df.empty or "symbol" not in positions_df:
        return out
    if "quantity" not in positions_df:
        return out
    for sym, qty in zip(positions_df["symbol"], positions_df["quantity"]):
        if sym is None or pd.isna(sym) or qty is None or pd.isna(qty):
            continue
        out[str(sym)] = out.get(str(sym), 0.0) + float(qty)
    return out


def _pay_date_of_tx(row) -> dt.date | None:
    """Payment date for a cash transaction: prefer settle, then datetime/report."""
    return (
        _to_date(row.get("settle_date"))
        or _to_date(row.get("datetime"))
        or _to_date(row.get("report_date"))
    )


# ---- aggregation ----------------------------------------------------------

def _paid_by_symbol_month(cash_tx: pd.DataFrame) -> dict[str, dict[str, dict]]:
    """Per symbol -> {ym: {gross, net}} for actually-paid dividends.

    Gross sums DIVIDEND + PAYMENTINLIEU; net folds in WHTAX (negative) for the
    same symbol+month.
    """
    out: dict[str, dict[str, dict]] = {}
    if cash_tx is None or cash_tx.empty:
        return out

    # Pass 1: place dividend income in its payment month, and remember which
    # month each (symbol, ex_date) landed in so the matching WHTAX can follow it.
    div_month: dict[tuple[str, dt.date | None], str] = {}
    for _, row in cash_tx.iterrows():
        sym = row.get("symbol")
        if not sym or row.get("type") not in _DIVIDEND_TYPES:
            continue
        amount = row.get("amount")
        if amount is None or pd.isna(amount):
            continue
        pay = _pay_date_of_tx(row)
        if pay is None:
            continue
        ym = _ym(pay)
        cell = out.setdefault(str(sym), {}).setdefault(ym, {"gross": 0.0, "net": 0.0})
        cell["gross"] += float(amount)
        cell["net"] += float(amount)
        div_month[(str(sym), _to_date(row.get("ex_date")))] = ym

    # Pass 2: fold withholding tax (negative) into the matching dividend's month
    # via (symbol, ex_date). IBKR may post WHTAX in a different month/settle-date
    # than the dividend it offsets; keying off the WHTAX row's own date splits net
    # across months (dividend month shows gross-as-net, tax month shows a negative
    # phantom cell). Fall back to the WHTAX row's own month for orphan tax with no
    # matching dividend, so it is never silently dropped.
    for _, row in cash_tx.iterrows():
        sym = row.get("symbol")
        if not sym or row.get("type") != _WHTAX_TYPE:
            continue
        amount = row.get("amount")
        if amount is None or pd.isna(amount):
            continue
        ym = div_month.get((str(sym), _to_date(row.get("ex_date"))))
        if ym is None:
            pay = _pay_date_of_tx(row)
            if pay is None:
                continue
            ym = _ym(pay)
        cell = out.setdefault(str(sym), {}).setdefault(ym, {"gross": 0.0, "net": 0.0})
        cell["net"] += float(amount)
    return out


def _declared_by_symbol(accruals: pd.DataFrame) -> dict[str, dict[str, dict]]:
    """Per symbol -> {ym: {gross, net, events}} for open accruals.

    Bucketed by payment month (ex-date month is the fallback when ``pay_date``
    is missing). A weekly-pay ticker can have two or more accruals open at
    once whose payments both land in the same calendar month — gross/net are
    summed across all of them (never overwritten) and each row's dates are
    kept in ``events`` for the hover tooltip. ``ex_date`` doubles as the
    declaration-date proxy (Flex carries no declaration date).
    """
    out: dict[str, dict[str, dict]] = {}
    if accruals is None or accruals.empty:
        return out
    for _, row in accruals.iterrows():
        sym = row.get("symbol")
        ex = _to_date(row.get("ex_date"))
        pay_date = _to_date(row.get("pay_date"))
        pay = pay_date or ex
        if not sym or pay is None:
            continue
        gross = row.get("gross_amount")
        net = row.get("net_amount")
        gross = float(gross) if gross is not None and not pd.isna(gross) else None
        net = float(net) if net is not None and not pd.isna(net) else None
        cell = out.setdefault(str(sym), {}).setdefault(
            _ym(pay), {"gross": None, "net": None, "events": []}
        )
        if gross is not None:
            cell["gross"] = gross if cell["gross"] is None else cell["gross"] + gross
        if net is not None:
            cell["net"] = net if cell["net"] is None else cell["net"] + net
        cell["events"].append({"ex_date": ex, "pay_date": pay_date})
    return out


def _accrual_tip(cell: dict) -> str:
    """Tooltip for a declared (yellow) cell: declaration and/or pay date(s).

    Numbers each event only when a month holds more than one open accrual
    (weekly-pay tickers); a single event keeps the plain "declaration: ...
    / pay: ..." form.
    """
    events = cell.get("events") or [
        {"ex_date": cell.get("ex_date"), "pay_date": cell.get("pay_date")}
    ]
    multi = len(events) > 1
    parts = []
    for i, ev in enumerate(events, 1):
        decl_label = f"declaration {i}" if multi else "declaration"
        pay_label = f"pay {i}" if multi else "pay"
        if ev.get("ex_date"):
            parts.append(f"{decl_label}: {ev['ex_date']:%d-%m-%Y}")
        if ev.get("pay_date"):
            parts.append(f"{pay_label}: {ev['pay_date']:%d-%m-%Y}")
    return "\n".join(parts)


def _pay_gap_days(cash_tx: pd.DataFrame) -> dict[str, tuple[int, dt.date]]:
    """Per symbol: (median day-gap between consecutive paid dividends, last
    paid date), from real distinct payment dates.

    Month-level cadence (``_project``) can't see anything shorter than a
    month — a ticker paid every month still measures as a 1-month cadence
    even when it actually pays every week within each of those months. This
    looks at real dates instead, so a weekly payer's short gap is visible.
    Needs at least ``_MIN_PAY_DATES_FOR_WEEKLY`` distinct dates so one early
    or late payment can't masquerade as a fast cadence.
    """
    dates: dict[str, set[dt.date]] = {}
    if cash_tx is None or cash_tx.empty:
        return {}
    for _, row in cash_tx.iterrows():
        if row.get("type") not in _DIVIDEND_TYPES:
            continue
        sym = row.get("symbol")
        pay = _pay_date_of_tx(row)
        if sym and pay:
            dates.setdefault(str(sym), set()).add(pay)
    out: dict[str, tuple[int, dt.date]] = {}
    for sym, ds in dates.items():
        ordered = sorted(ds)
        if len(ordered) < _MIN_PAY_DATES_FOR_WEEKLY:
            continue
        gaps = sorted((b - a).days for a, b in zip(ordered, ordered[1:]))
        gap = gaps[len(gaps) // 2]
        if gap > 0:
            out[sym] = (gap, ordered[-1])
    return out


def _decl_to_pay_offset(cash_tx: pd.DataFrame) -> dict[str, int]:
    """Per symbol, the typical month gap between ex-date and payment (paid divs)."""
    gaps: dict[str, list[int]] = {}
    if cash_tx is None or cash_tx.empty:
        return {}
    for _, row in cash_tx.iterrows():
        if row.get("type") not in _DIVIDEND_TYPES:
            continue
        ex = _to_date(row.get("ex_date"))
        pay = _pay_date_of_tx(row)
        if not row.get("symbol") or ex is None or pay is None:
            continue
        gaps.setdefault(str(row["symbol"]), []).append(_idx(_ym(pay)) - _idx(_ym(ex)))
    return {sym: (_mode(g) or 0) for sym, g in gaps.items()}


def _typical_days(cash_tx: pd.DataFrame) -> tuple[dict[str, int], dict[str, int]]:
    """Per symbol, the typical day-of-month of (ex-date, payment) in paid history.

    Median, not mode: real dates drift by a day or two around weekends and
    holidays, so no single day-of-month repeats often enough for a mode to be
    stable, while the median sits in the middle of that drift.

    Returns ``(ex_days, pay_days)``; a symbol is absent when it has no usable
    dates, which leaves its projection at month granularity.
    """
    ex_days: dict[str, list[int]] = {}
    pay_days: dict[str, list[int]] = {}
    if cash_tx is None or cash_tx.empty:
        return {}, {}
    for _, row in cash_tx.iterrows():
        sym = row.get("symbol")
        if not sym or row.get("type") not in _DIVIDEND_TYPES:
            continue
        ex = _to_date(row.get("ex_date"))
        pay = _pay_date_of_tx(row)
        if ex is not None:
            ex_days.setdefault(str(sym), []).append(ex.day)
        if pay is not None:
            pay_days.setdefault(str(sym), []).append(pay.day)

    def _median(days: list[int]) -> int:
        ordered = sorted(days)
        return ordered[len(ordered) // 2]

    return (
        {s: _median(d) for s, d in ex_days.items()},
        {s: _median(d) for s, d in pay_days.items()},
    )


def _day_in_month(ym: str, day: int) -> dt.date:
    """``day`` clamped into ``ym`` — day 31 in a 30-day month lands on the 30th."""
    year, month = (int(part) for part in ym.split("-"))
    last = _calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day, last))


def _projection_tip(label: str, ym: str, day: int | None) -> str:
    """Tooltip line for a red projected cell.

    Falls back to the month label when the ticker's history carries no usable
    day-of-month — an estimated month is still worth showing, an invented day
    is not.
    """
    if day is None:
        return f"{label}: {month_label(ym)}"
    return f"{label}: ~{_day_in_month(ym, day):%d-%m-%Y} (estimated)"


def _net_ratio_by_symbol(paid: dict[str, dict[str, dict]]) -> dict[str, float]:
    """Per-symbol net/gross withholding ratio from paid dividend history.

    Symbols with degenerate data (zero gross, non-positive net, or net above
    gross — e.g. orphan tax rows) are skipped rather than yielding a bogus rate.
    """
    out: dict[str, float] = {}
    for sym, months in paid.items():
        gross = sum(c["gross"] for c in months.values())
        net_sum = sum(c["net"] for c in months.values())
        if gross > 0 and 0 < net_sum <= gross:
            out[sym] = net_sum / gross
    return out


# ---- projection -----------------------------------------------------------

def _project(
    paid_months: dict[str, dict],
    offset_months: int,
    current_ym: str,
) -> tuple[str, str] | None:
    """Estimate the next declaration/payment months from paid-month cadence.

    Returns ``(decl_ym, pay_ym)`` — equal when declaration and payment fall in
    the same month. ``None`` when cadence is unknown.
    """
    months = sorted(_idx(m) for m in paid_months)
    if len(months) < _MIN_PAID_FOR_PROJECTION:
        return None
    cadence = _mode([b - a for a, b in zip(months, months[1:])])
    if not cadence or cadence <= 0:
        return None

    cur = _idx(current_ym)
    next_pay = months[-1] + cadence
    # A same-month payer (declare+pay in one month, offset 0) whose payment is
    # due in the CURRENT month and has not arrived yet shows its red "Declare"
    # IN the current month (mock MSTY/CONY semantics: red = not yet happened).
    # Once this month's payment lands — paid is ground truth — or for
    # split-month cadences (unchanged), advance to the first future payment.
    allow_current = offset_months == 0 and current_ym not in paid_months
    floor = cur - 1 if allow_current else cur
    while next_pay <= floor:  # advance past stale data
        next_pay += cadence

    return _ym_from_idx(next_pay - offset_months), _ym_from_idx(next_pay)


def _project_weekly(
    gap_days: int,
    last_pay: dt.date,
    offset_months: int,
    today: dt.date,
) -> tuple[str, str, dt.date] | None:
    """Estimate the next payment for a weekly-pay ticker from a real day-level
    cadence, not whole-month cadence.

    ``_project`` advances a month at a time and treats "current month already
    paid" as reason enough to skip to the next month — true for a monthly
    payer, wrong for a weekly one, where another payment days later in the
    SAME month is normal. This instead steps forward from the last real
    payment date by ``gap_days`` until the result is in the future, so a
    payment due later this month still lands in this month's column.

    Returns ``(decl_ym, pay_ym, pay_date)``; ``pay_date`` lets the caller show
    an exact estimated day instead of a generic historical median.
    """
    if gap_days <= 0:
        return None
    next_pay = last_pay
    while next_pay <= today:
        next_pay += dt.timedelta(days=gap_days)
    pay_ym = _ym(next_pay)
    decl_ym = _add_months(pay_ym, -offset_months)
    return decl_ym, pay_ym, next_pay


def _apply_external(cells: dict[str, dict], ext: dict, current_ym: str) -> None:
    """Backfill one no-Flex-data ticker's cells from yfinance dates (red-only).

    Prefers concrete published ex/pay dates (current month or later); falls
    back to projecting the next ex month from the external ex-date history via
    the same cadence inference as Flex data. Mutates ``cells`` in place; cells
    are red-only ("Declare"/"Pay", no amounts). Polygon's real declared amount
    is handled earlier in ``build_calendar`` via ``_polygon_declared_by_symbol``.
    """
    ex = ext.get("ex_date")
    pay = ext.get("pay_date")
    ex_ym = _ym(ex) if ex else None
    pay_ym = _ym(pay) if pay else None

    # Concrete dates beat inference, but only forward-looking ones: a stale
    # calendar entry for a past distribution must not rewrite history.
    if ex_ym and ex_ym < current_ym:
        ex_ym = None
    if pay_ym and pay_ym < current_ym:
        pay_ym = None

    if ex_ym or pay_ym:
        if ex_ym and (pay_ym is None or pay_ym == ex_ym):
            tip = f"ex-dividend: {ex:%d-%m-%Y}"
            if pay:
                tip += f"\npay: {pay:%d-%m-%Y}"
            cells[ex_ym] = {"status": "declare", "value": None, "tip": f"{tip}\nsource: yfinance"}
            return
        if ex_ym:
            cells[ex_ym] = {
                "status": "declare", "value": None,
                "tip": f"ex-dividend: {ex:%d-%m-%Y}\nsource: yfinance",
            }
        cells[pay_ym] = {
            "status": "pay", "value": None,
            "tip": f"pay: {pay:%d-%m-%Y}\nsource: yfinance",
        }
        return

    # No published dates — infer cadence from external ex-date months. Ex and
    # pay months are assumed to coincide (offset 0): without published dates
    # there is nothing better, and a single "Declare" marker is the honest claim.
    history = ext.get("history_months") or []
    proj = _project({m: {} for m in history}, 0, current_ym)
    if proj is not None:
        _, next_ym = proj
        cells[next_ym] = {
            "status": "declare", "value": None,
            "tip": f"projected from yfinance distribution history: {month_label(next_ym)}",
        }


def _polygon_declared_by_symbol(
    external: dict[str, dict], shares: dict[str, float], current_ym: str, net: bool,
    net_ratios: dict[str, float] | None = None,
) -> dict[str, dict[str, dict]]:
    """symbol -> {ym: declared-cell} from external entries carrying a real
    per-share ``amount`` (Polygon). Bucketed by pay month (ex-date fallback),
    current month or later only — same convention as a Flex open accrual.

    Polygon reports gross, so the gross (before-tax) cell carries the exact cash
    figure. The net (after-tax) cell is an ESTIMATE (marked ``approx``): gross
    scaled by the symbol's historical net/gross withholding ratio, or by
    ``1 - DEFAULT_TAX_RATE`` when no paid history exists or that history shows no
    withholding. Shorts / zero holdings are skipped.
    """
    net_ratios = net_ratios or {}
    out: dict[str, dict[str, dict]] = {}
    for sym, ext in external.items():
        amount = ext.get("amount")
        if amount is None:
            continue
        qty = shares.get(sym, 0.0)
        if qty <= 0:  # short or not actually held — no honest cash figure
            continue
        ex = ext.get("ex_date")
        pay = ext.get("pay_date")
        bucket = (_ym(pay) if pay else None) or (_ym(ex) if ex else None)
        if bucket is None or bucket < current_ym:
            continue
        decl = ext.get("decl_date")
        decl_line = f"\ndeclared: {decl:%d-%m-%Y}" if decl else ""
        approx = False
        if net:
            approx = True
            ratio = net_ratios.get(sym)
            if ratio is not None and ratio < 1:
                value = amount * qty * ratio
                tip = (
                    f"declared {amount:g}/sh x {qty:g} sh{decl_line}"
                    f"\nnet ≈ estimated (withholding {100 * (1 - ratio):.0f}% from paid history)"
                    f"\nsource: polygon"
                )
            else:
                value = amount * qty * (1 - DEFAULT_TAX_RATE)
                tip = (
                    f"declared {amount:g}/sh x {qty:g} sh{decl_line}"
                    f"\nnet ≈ estimated (default {DEFAULT_TAX_RATE:.0%} tax)"
                    f"\nsource: polygon"
                )
        else:
            value = amount * qty
            tip = f"declared {amount:g}/sh x {qty:g} sh{decl_line}\ngross (pre-tax)\nsource: polygon"
        cell = {"status": "declared", "value": value, "tip": tip}
        if approx:
            cell["approx"] = True
        out.setdefault(sym, {})[bucket] = cell
    return out


# ---- public API -----------------------------------------------------------

def coverage(cash_tx_df: pd.DataFrame | None) -> dict:
    """Statement coverage of the cash-transactions section, for UI warnings.

    Returns ``{"rows": int, "min_date": date | None, "max_date": date | None}``.
    Dates use the same per-row payment-date preference as the calendar
    (settle -> datetime -> report), so coverage reflects exactly the history the
    paid (green) cells are built from. None/empty/undated input yields
    ``rows`` of what's there and ``None`` dates — never raises.
    """
    out: dict = {"rows": 0, "min_date": None, "max_date": None}
    try:
        if cash_tx_df is None or cash_tx_df.empty:
            return out
        out["rows"] = int(len(cash_tx_df))
        dates = []
        for _, row in cash_tx_df.iterrows():
            d = _pay_date_of_tx(row)
            # pandas renders None as NaT in mixed date columns, and NaT slips
            # through _to_date (it is a datetime instance) — drop it here.
            if d is not None and not pd.isna(d):
                dates.append(d)
        if dates:
            out["min_date"] = min(dates)
            out["max_date"] = max(dates)
    except Exception:  # noqa: BLE001 — a diagnostics helper must never crash the UI
        logger.warning("coverage: failed to inspect cash transactions", exc_info=True)
    return out


def _amount(cell: dict, net: bool) -> float | None:
    gross = cell.get("gross")
    if not net:
        return gross
    val = cell.get("net")
    # IBKR withheld nothing (no WHTAX row / accrual tax=0): apply the default
    # rate rather than showing the untaxed gross as the after-tax figure.
    if gross is not None and gross > 0 and (val is None or val >= gross):
        return gross * (1 - DEFAULT_TAX_RATE)
    return val


def _fmt(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.2f}"


_WORDS = {"declare": "Declare", "pay": "Pay"}


def build_calendar(
    positions_df: pd.DataFrame,
    cash_tx_df: pd.DataFrame,
    accruals_df: pd.DataFrame,
    today: dt.date | None = None,
    net: bool = False,
    lookback_months: int | None = None,
    external: dict[str, dict] | None = None,
) -> Calendar:
    """Assemble the per-ticker / per-month dividend calendar.

    ``lookback_months`` clips HISTORY to the trailing N months ending at (and
    including) the current month; future columns are never clipped. It also
    scopes ``paid_sum``. ``None`` keeps the full history.

    ``external`` maps symbol -> external info (``data.div_provider.fetch_declared``
    fields merged with ``data.div_external.fetch_upcoming``). A real per-share
    ``amount`` (Polygon) renders a yellow declared cell for any held ticker
    lacking a Flex accrual and suppresses that ticker's red projection; in net
    mode the amount is an estimate ("≈"-marked): gross scaled by the symbol's
    historical withholding ratio, or plain gross when no paid history exists.
    The yfinance ex/pay dates are a red-only fallback used only when a ticker is
    otherwise empty. External data never overrides Flex-derived cells.

    Cell precedence: paid (green) is ground truth; declared (yellow, Flex then
    Polygon) renders in the current month or later only; projections (red) never
    clobber real data.
    """
    today = today or dt.date.today()
    current_ym = _ym(today)

    paid = _paid_by_symbol_month(cash_tx_df)
    declared = _declared_by_symbol(accruals_df)
    offsets = _decl_to_pay_offset(cash_tx_df)
    ex_days, pay_days = _typical_days(cash_tx_df)
    pay_gaps = _pay_gap_days(cash_tx_df)

    held = _held_symbols(positions_df)
    data_symbols = set(paid) | set(declared)
    # Rows are ALL held tickers (no-data ones become placeholder rows); if
    # there are no open positions, every ticker that has dividend data.
    symbols = sorted(held) if held else sorted(data_symbols)

    # Real declared amounts (Polygon) for tickers Flex has no accrual on — a
    # confirmed declaration that, like a Flex accrual, suppresses the red guess.
    shares = _shares_by_symbol(positions_df)
    poly_declared = (
        _polygon_declared_by_symbol(
            external, shares, current_ym, net, _net_ratio_by_symbol(paid)
        )
        if external else {}
    )

    # events[sym][ym] = {"status": str, "value": float|None, "tip": str}
    events: dict[str, dict[str, dict]] = {sym: {} for sym in symbols}

    for sym in symbols:
        # 1) paid history (green)
        for ym, cell in paid.get(sym, {}).items():
            events[sym][ym] = {"status": "paid", "value": _amount(cell, net), "tip": ""}
        # 2) declared accruals (yellow) — payment month, current-or-later only;
        #    the past is paid-only and received cash beats a lagging accrual.
        for ym, cell in declared.get(sym, {}).items():
            if ym < current_ym:
                continue
            if events[sym].get(ym, {}).get("status") == "paid":
                continue
            events[sym][ym] = {
                "status": "declared", "value": _amount(cell, net), "tip": _accrual_tip(cell),
            }
        # 2b) external declared (Polygon, yellow) — fills any held ticker lacking
        #     a Flex accrual; never overrides real Flex data (paid/declared).
        for ym, cell in poly_declared.get(sym, {}).items():
            if events[sym].get(ym, {}).get("status") in ("paid", "declared"):
                continue
            events[sym][ym] = cell
        # 3) projection (red) — only when no open accrual AND no external declared
        #    covers a payment in the current month or later (a stale past accrual
        #    must not block it; a confirmed declaration makes the guess redundant)
        has_future_accrual = (
            any(ym >= current_ym for ym in declared.get(sym, {}))
            or any(ym >= current_ym for ym in poly_declared.get(sym, {}))
        )
        if not has_future_accrual and sym in paid:
            gap_info = pay_gaps.get(sym)
            weekly_pay_date = None
            if gap_info and gap_info[0] <= _WEEKLY_GAP_MAX_DAYS:
                gap_days, last_pay = gap_info
                weekly = _project_weekly(gap_days, last_pay, offsets.get(sym, 0), today)
                proj = weekly[:2] if weekly else None
                weekly_pay_date = weekly[2] if weekly else None
            else:
                proj = _project(paid[sym], offsets.get(sym, 0), current_ym)
            if proj is not None:
                decl_ym, pay_ym = proj
                ex_day, pay_day = ex_days.get(sym), pay_days.get(sym)
                if weekly_pay_date is not None:  # exact date beats historical median
                    pay_day = weekly_pay_date.day
                if decl_ym == pay_ym:
                    if pay_ym not in events[sym]:  # never clobber real data
                        # One month, two events: give each its own estimated day
                        # rather than a single date that is right for neither.
                        events[sym][pay_ym] = {
                            "status": "declare", "value": None,
                            "tip": "\n".join([
                                _projection_tip("projected declaration", decl_ym, ex_day),
                                _projection_tip("projected payment", pay_ym, pay_day),
                            ]),
                        }
                else:
                    if decl_ym >= current_ym and decl_ym not in events[sym]:
                        events[sym][decl_ym] = {
                            "status": "declare", "value": None,
                            "tip": _projection_tip("projected declaration", decl_ym, ex_day),
                        }
                    if pay_ym not in events[sym]:
                        events[sym][pay_ym] = {
                            "status": "pay", "value": None,
                            "tip": _projection_tip("projected payment", pay_ym, pay_day),
                        }

    # 4) yfinance dates-only fallback (red) — only for tickers still empty after
    #    Flex and Polygon produced nothing.
    if external:
        for sym in symbols:
            if not events[sym] and external.get(sym):
                _apply_external(events[sym], external[sym], current_ym)

    no_data = {s for s in symbols if not events[s]}
    projected = {
        s for s in symbols
        if any(ev["status"] in ("declare", "pay") for ev in events[s].values())
    }
    if len(no_data) == len(symbols):  # no events anywhere — nothing to render
        logger.info(
            "build_calendar: no events (held=%d, paid_symbols=%d, declared_symbols=%d, "
            "today=%s, lookback=%s)",
            len(held), len(paid), len(declared), today, lookback_months,
        )
        empty = pd.DataFrame()
        return Calendar(values=empty, status=empty, tooltips=empty, current_ym=current_ym)

    # Columns: trailing lookback window (or full history) through the last event,
    # with the current month always present as the "today" anchor.
    cur_idx = _idx(current_ym)
    all_idx = sorted({_idx(ym) for sym in symbols for ym in events[sym]} | {cur_idx})
    start_idx = cur_idx - (lookback_months - 1) if lookback_months else all_idx[0]
    columns = [_ym_from_idx(i) for i in range(start_idx, all_idx[-1] + 1)]
    colset = set(columns)

    # Trailing paid-only sum over [window start .. current month], green cells only.
    paid_sum = sum(
        ev["value"]
        for sym in symbols
        for ym, ev in events[sym].items()
        if ev["status"] == "paid" and ev["value"] is not None
        and start_idx <= _idx(ym) <= cur_idx
    )

    values = pd.DataFrame("", index=symbols, columns=columns)
    status = pd.DataFrame("", index=symbols, columns=columns)
    tooltips = pd.DataFrame("", index=symbols, columns=columns)
    totals_num: dict[str, float] = {}
    for sym in symbols:
        for ym, ev in events[sym].items():
            if ym not in colset:  # history clipped by the lookback window
                continue
            status.at[sym, ym] = ev["status"]
            text = _WORDS.get(ev["status"]) or _fmt(ev["value"])
            if ev.get("approx") and ev["value"] is not None:
                text = f"≈{text}"
            values.at[sym, ym] = text
            tooltips.at[sym, ym] = ev["tip"]
            if ev["status"] in ("paid", "declared") and ev["value"] is not None:
                totals_num[ym] = totals_num.get(ym, 0.0) + ev["value"]

    totals = pd.Series(
        {c: _fmt(totals_num[c]) if c in totals_num else "" for c in columns}, dtype=object
    )

    try:
        status_counts = Counter(
            ev["status"] for sym in symbols for ev in events[sym].values()
        )
        logger.info(
            "build_calendar: symbols=%d held=%d window=%s..%s (%d cols) today=%s "
            "lookback=%s net=%s | events paid=%d declared=%d declare=%d pay=%d | "
            "paid_sum=%.2f",
            len(symbols), len(held), columns[0], columns[-1], len(columns), today,
            lookback_months, net,
            status_counts.get("paid", 0), status_counts.get("declared", 0),
            status_counts.get("declare", 0), status_counts.get("pay", 0),
            float(paid_sum),
        )
    except Exception:  # noqa: BLE001 — logging must never break the calendar
        pass

    for df in (values, status, tooltips):
        df.index.name = "Ticker"
    return Calendar(
        values=values, status=status, tooltips=tooltips,
        current_ym=current_ym, totals=totals, paid_sum=float(paid_sum),
        no_data=no_data, projected=projected,
    )
