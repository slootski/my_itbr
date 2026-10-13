"""Build the dividend-projection calendar from parsed Flex DataFrames.

Turns IBKR paid dividends (cash transactions) and declared-but-unpaid dividends
(open accruals) into a per-ticker / per-month grid the Dividends tab renders.

Rows are held tickers plus tickers sold since they paid (``sold`` — past
payments only, never projected); columns are chronological months. Each cell carries a
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

Paid amounts come from ``data.div_ledger`` — the one definition of dividend
income every screen shares. After tax is the cash that actually landed (gross
plus IBKR's withholding); when IBKR withheld nothing it equals gross. Declared
accruals use IBKR's own net. Only an external (Polygon) amount for a ticker with
no paid history uses ``DEFAULT_TAX_RATE``, marked as an estimate. Red cells
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

from data import div_ledger

logger = logging.getLogger("myitbr.dividends")

# Estimated tax rate for an external (Polygon) declared amount on a ticker with
# no paid history to derive its real withholding from. Never applied to money
# IBKR paid or declared.
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
    per-month sum of paid amounts only (formatted, "" when none) — money not yet
    received never feeds a sum. ``ticker_totals`` is the per-symbol paid sum over
    the same window as ``paid_sum`` (formatted, "" when none). ``declared_sum``
    is the total of declared-but-unpaid (yellow) amounts from the current month
    on — shown on its own, never added to any paid total.
    ``paid_sum`` is the trailing sum of paid dividends over the lookback window
    ending at (and including) the current month. ``no_data`` lists held tickers
    with no dividend events at all (their rows are empty placeholders).
    ``projected`` lists tickers carrying a red projected (declare/pay) cell —
    the ones an external declared-amount lookup can still upgrade. ``sold``
    lists rows for tickers no longer held that IBKR paid or still owes: their
    payments count in every total, but they are never projected. ``held_count`` is the
    number of held tickers (rows minus ``sold``). ``shares`` is the quantity held
    now per held ticker (sold tickers are absent).
    """
    values: pd.DataFrame
    status: pd.DataFrame
    tooltips: pd.DataFrame
    current_ym: str
    ticker_totals: pd.Series
    declared_sum: float
    sold: set[str]
    held_count: int
    shares: dict[str, float]
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
    return div_ledger.pay_date(row=row)


# ---- aggregation ----------------------------------------------------------

def _paid_events(cash_tx: pd.DataFrame) -> pd.DataFrame:
    """Dividend payments from the shared ledger (only rows that paid income)."""
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    return ledger[ledger["payments"] > 0] if not ledger.empty else ledger


def _paid_by_symbol_month(cash_tx: pd.DataFrame) -> dict[str, dict[str, dict]]:
    """Per symbol -> {ym: {gross, net}} for actually-paid dividends (ledger)."""
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    return div_ledger.by_symbol_month(ledger=ledger)


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
        tax = row.get("tax")
        gross = float(gross) if gross is not None and not pd.isna(gross) else None
        net = float(net) if net is not None and not pd.isna(net) else None
        tax = float(tax) if tax is not None and not pd.isna(tax) else None
        # IBKR's own net; when only gross and tax are given, gross minus tax
        # (accrual tax is posted as a positive amount).
        if net is None and gross is not None and tax is not None:
            net = gross - abs(tax)
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
    for event in _paid_events(cash_tx).to_dict("records"):
        dates.setdefault(event["symbol"], set()).add(event["pay_date"])
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
    for event in _paid_events(cash_tx).to_dict("records"):
        ex = _to_date(event["ex_date"])
        if ex is None:
            continue
        gaps.setdefault(event["symbol"], []).append(
            _idx(_ym(event["pay_date"])) - _idx(_ym(ex)))
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
    for event in _paid_events(cash_tx).to_dict("records"):
        sym = event["symbol"]
        ex = _to_date(event["ex_date"])
        if ex is not None:
            ex_days.setdefault(sym, []).append(ex.day)
        pay_days.setdefault(sym, []).append(event["pay_date"].day)

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
    ``1 - DEFAULT_TAX_RATE`` only when no paid history exists. A history with no
    withholding gives ratio 1 (net = gross). Shorts / zero holdings are skipped.
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
            if ratio is not None:
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
    """IBKR's own figure: gross, or net (cash that lands). No rate is assumed —
    when IBKR withheld nothing, net is gross."""
    gross = cell.get("gross")
    if not net:
        return gross
    val = cell.get("net")
    return gross if val is None else val


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
    # Rows are ALL held tickers (no-data ones become placeholder rows) plus every
    # sold ticker that paid, so the paid total matches every other screen. With
    # no open positions, every ticker that has dividend data.
    if held:
        # A ticker sold after its ex-date still gets paid, so an open accrual
        # keeps a sold row too.
        sold = data_symbols - held
        symbols = sorted(held) + sorted(sold)  # sold rows go last
    else:
        sold = set()
        symbols = sorted(data_symbols)

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
        if sym in sold:  # no longer held: IBKR data only, never projected
            continue
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
            if sym not in sold and not events[sym] and external.get(sym):
                _apply_external(events[sym], external[sym], current_ym)

    # A sold ticker whose only data is a stale past accrual has nothing to show.
    symbols = [s for s in symbols if s not in sold or events[s]]
    sold = {s for s in sold if s in symbols}
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
        return Calendar(
            values=empty, status=empty, tooltips=empty, current_ym=current_ym,
            ticker_totals=pd.Series(dtype=object), declared_sum=0.0,
            sold=set(), held_count=len(held), shares=shares,
        )

    # Columns: trailing lookback window (or full history) through the last event,
    # with the current month always present as the "today" anchor.
    cur_idx = _idx(current_ym)
    all_idx = sorted({_idx(ym) for sym in symbols for ym in events[sym]} | {cur_idx})
    start_idx = cur_idx - (lookback_months - 1) if lookback_months else all_idx[0]
    columns = [_ym_from_idx(i) for i in range(start_idx, all_idx[-1] + 1)]
    colset = set(columns)

    # Trailing paid-only sums over [window start .. current month], green cells only.
    ticker_paid = {
        sym: sum(
            ev["value"]
            for ym, ev in events[sym].items()
            if ev["status"] == "paid" and ev["value"] is not None
            and start_idx <= _idx(ym) <= cur_idx
        )
        for sym in symbols
    }
    paid_sum = sum(ticker_paid.values())
    ticker_totals = pd.Series(
        {sym: _fmt(v) if v else "" for sym, v in ticker_paid.items()}, dtype=object
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
            if ev["status"] == "paid" and ev["value"] is not None:
                totals_num[ym] = totals_num.get(ym, 0.0) + ev["value"]

    declared_sum = sum(
        ev["value"]
        for sym in symbols
        for ym, ev in events[sym].items()
        if ev["status"] == "declared" and ev["value"] is not None
        and ym in colset and ym >= current_ym
    )

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
        current_ym=current_ym, ticker_totals=ticker_totals,
        declared_sum=float(declared_sum), sold=sold, held_count=len(held) or len(symbols),
        shares=shares,
        totals=totals,
        paid_sum=float(paid_sum),
        no_data=no_data, projected=projected,
    )


# ---- trade markers ----------------------------------------------------------

def _num(*, value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_qty(*, value: float) -> str:
    """Share count with at most two decimals: 17 -> "17", 2.5 -> "2.5", 2.056 -> "2.06"."""
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _holding_after(
    *, trades: pd.DataFrame, symbol: str, date: dt.date, current: float,
) -> float:
    """Shares held at the end of ``date``: today's holding minus every later trade.

    Counting back from today's IBKR position (rather than forward from the first
    archived trade) stays right even when the trade archive does not reach back
    to the first buy. Sell quantities are negative in Flex.
    """
    later = 0.0
    for row in trades.to_dict("records"):
        if str(row.get("symbol")) != symbol:
            continue
        when = _to_date(row.get("trade_date")) or _to_date(row.get("datetime"))
        qty = _num(value=row.get("quantity"))
        if when is not None and when > date and qty is not None:
            later += qty
    return current - later


def trade_marks(
    *, trades: pd.DataFrame | None, symbols: list[str], columns: list[str],
    shares: dict[str, float],
) -> dict[str, dict[str, list[dict]]]:
    """symbol -> {"YYYY-MM": [trade]} for the calendar's rows and month columns.

    Buys and sells are marked separately; ``side`` is "BUY" or "SELL". Fills of
    the same symbol, side and day are one trade: quantities, amount, commission
    and realized P&L are summed and the price is the quantity-weighted average.
    ``amount`` is the cost of a buy or the proceeds of a sell (always positive).
    Each trade carries ``pos`` (0..1), where its day falls within the month, so
    the marker sits at that point of the cell's timeline, and ``holding`` —
    shares held at the end of that day, counted back from ``shares`` (held now;
    a ticker absent from it is held at 0).
    """
    out: dict[str, dict[str, list[dict]]] = {}
    if trades is None or trades.empty or "side" not in trades or "symbol" not in trades:
        return out
    wanted, colset = set(symbols), set(columns)
    days: dict[tuple[str, dt.date, str], dict] = {}
    for row in trades.to_dict("records"):
        side = str(row.get("side")).upper()
        if side not in ("BUY", "SELL"):
            continue
        sym = row.get("symbol")
        date = _to_date(row.get("trade_date")) or _to_date(row.get("datetime"))
        qty = _num(value=row.get("quantity"))
        if not sym or str(sym) not in wanted or date is None or not qty:
            continue
        if _ym(date) not in colset:
            continue
        price = _num(value=row.get("price"))
        proceeds = _num(value=row.get("proceeds"))
        trade = days.setdefault((str(sym), date, side), {
            "side": side, "date": date, "quantity": 0.0, "amount": 0.0, "commission": 0.0,
            "realized_pnl": None, "currency": row.get("currency"), "fills": 0, "_value": 0.0,
        })
        qty = abs(qty)
        trade["quantity"] += qty
        trade["fills"] += 1
        if proceeds is not None:
            trade["amount"] += abs(proceeds)
        elif price is not None:
            trade["amount"] += qty * price
        if price is not None:
            trade["_value"] += qty * price
        commission = _num(value=row.get("commission"))
        if commission is not None:
            trade["commission"] += abs(commission)
        pnl = _num(value=row.get("realized_pnl"))
        if pnl is not None:
            trade["realized_pnl"] = (trade["realized_pnl"] or 0.0) + pnl

    for (sym, date, _side), trade in sorted(days.items(), key=lambda item: item[0][1]):
        value = trade.pop("_value")
        trade["price"] = value / trade["quantity"] if value else None
        last_day = _calendar.monthrange(date.year, date.month)[1]
        trade["pos"] = (date.day - 0.5) / last_day
        trade["holding"] = _holding_after(trades=trades, symbol=sym, date=date,
                                          current=shares.get(sym, 0.0))
        out.setdefault(sym, {}).setdefault(_ym(date), []).append(trade)
    return out


def first_buy_month(*, trades: pd.DataFrame | None, symbols: list[str]) -> str | None:
    """Earliest ``YYYY-MM`` with a buy of any calendar row, or None."""
    if trades is None or trades.empty or "side" not in trades or "symbol" not in trades:
        return None
    wanted = set(symbols)
    months = [
        _ym(date)
        for row in trades.to_dict("records")
        if str(row.get("side")).upper() == "BUY" and str(row.get("symbol")) in wanted
        for date in [_to_date(row.get("trade_date")) or _to_date(row.get("datetime"))]
        if date is not None
    ]
    return min(months) if months else None


def extend_back(*, cal: Calendar, first_ym: str) -> Calendar:
    """Prepend empty month columns so the calendar starts at ``first_ym``.

    Used for the "All" view, so buys made before the first dividend still get
    their markers. Adds no money: new columns are empty and totals unchanged.
    """
    if cal.empty:
        return cal
    columns = list(cal.values.columns)
    if first_ym >= columns[0]:
        return cal
    new = [_ym_from_idx(i) for i in range(_idx(first_ym), _idx(columns[0]))]

    def widen(df: pd.DataFrame) -> pd.DataFrame:
        pad = pd.DataFrame("", index=df.index, columns=new)
        out = pd.concat([pad, df], axis=1)
        out.index.name = df.index.name
        return out

    totals = pd.concat([pd.Series("", index=new, dtype=object), cal.totals])
    return Calendar(
        values=widen(cal.values), status=widen(cal.status), tooltips=widen(cal.tooltips),
        current_ym=cal.current_ym, ticker_totals=cal.ticker_totals,
        declared_sum=cal.declared_sum, sold=cal.sold, held_count=cal.held_count,
        shares=cal.shares,
        totals=totals, paid_sum=cal.paid_sum, no_data=cal.no_data, projected=cal.projected,
    )


def drop_absent_sold(*, cal: Calendar, marks: dict[str, dict[str, list[dict]]]) -> Calendar:
    """Remove sold tickers that were not part of the portfolio in the months shown.

    A sold ticker keeps its row when the displayed months hold something of it:
    a dividend cell, or a trade marker (``marks``, from ``trade_marks`` for the
    same columns) — a ticker held into the period always has at least the sell
    that closed it. One bought and sold before the first month shown has
    neither, and its empty row only adds noise. Removes no money: a dropped row
    has no cell in these months, so no total changes. Held tickers always stay.
    """
    if cal.empty:
        return cal
    absent = {
        sym for sym in cal.sold
        if not marks.get(sym) and not (cal.values.loc[sym] != "").any()
    }
    if not absent:
        return cal
    keep = [sym for sym in cal.values.index if sym not in absent]
    return Calendar(
        values=cal.values.loc[keep], status=cal.status.loc[keep], tooltips=cal.tooltips.loc[keep],
        current_ym=cal.current_ym, ticker_totals=cal.ticker_totals.drop(labels=list(absent), errors="ignore"),
        declared_sum=cal.declared_sum, sold=cal.sold - absent, held_count=cal.held_count,
        shares=cal.shares,
        totals=cal.totals, paid_sum=cal.paid_sum, no_data=cal.no_data, projected=cal.projected,
    )
