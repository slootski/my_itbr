"""HTML for the daily performance calendar (Performance tab).

Pure string builders: they take the structures ``data.performance`` produces
and return markup for ``st.html``. No Streamlit calls, like ``charts.figures``.

The page lays the pieces out itself (one tile per day, one row per month) so
that each can carry a Streamlit button: ``st.html`` has no way to report a
click. The stylesheet stretches that button, invisible, over its tile or row,
which makes the whole tile the click target and keeps it keyboard-reachable.

How a day's result is encoded, so colour is never the only signal:

* direction: tint hue (green / red), an arrow (up / down) and the sign;
* size: tint strength, in four fixed steps of absolute return.

Tints are low-alpha fills and every figure stays in text ink, so text contrast
holds on the strongest step in both themes (``tests/test_perf_calendar.py``
computes it).

A month row draws the month as one line, the running return day by day. Its
direction is also in the arrow and sign of the month's return beside it.

The line is an SVG, and ``st.html`` removes inline ``<svg>`` (its sanitizer
allows HTML tags only). So the row carries it as an ``<img>`` with a ``data:``
URI. An image cannot read the page's CSS variables, which is why the line's
colours are constants here and not only in the stylesheet.
"""
from __future__ import annotations

import base64
import calendar as _calendar
import datetime as dt
import html

from data import performance

# Surface and ink per theme; the same values as the dividend calendar so the
# two calendars read as one family.
SURFACE = {"light": "#fcfcfb", "dark": "#16181d"}
INK = {"light": "#0b0b0b", "dark": "#f5f5f3"}
INK_2 = {"light": "#52514e", "dark": "#c3c2b7"}
# Direction ink and the stronger rule, per theme. The stylesheet and the month
# line (an image, so it cannot use the stylesheet's variables) both read these.
GAIN_INK = {"light": "#0ca30c", "dark": "#2fbf4a"}
LOSS_INK = {"light": "#d03b3b", "dark": "#f0616d"}
LINE_STRONG = {"light": "#c3c2b7", "dark": "#3a3e47"}

# Tint hue per direction (r, g, b) and alpha per strength step 1..4. Dark steps
# are chosen for the dark surface, not derived from the light ones.
TINT_RGB = {performance.GAIN: (12, 163, 12), performance.LOSS: (208, 59, 59)}
# The strongest step is the most the secondary ink can take at 4.5:1 contrast.
TINT_ALPHA = {"light": (0.08, 0.15, 0.24, 0.34), "dark": (0.16, 0.25, 0.35, 0.46)}

_MINUS = "−"
_UP, _DOWN = "▲", "▼"
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri")
# Streamlit keys the stylesheet finds by prefix (Streamlit adds an
# ``st-key-<key>`` class): the metric row, a clickable day tile or month row,
# and the button laid over each.
METRICS_KEY = "perf_metrics"
DAY_KEY_PREFIX = "perf_day_"
DAY_OPEN_KEY_PREFIX = "perf_dayopen_"
ROW_KEY_PREFIX = "perf_row_"
OPEN_KEY_PREFIX = "perf_open_"
# The line of a month row: drawn in a fixed box and stretched to the width the
# row gives it, so every row is the same height. ``LABEL_BAND_PX`` is the room
# above and below that box for the day labels; under ``LABELS_MIN_WIDTH_PX`` of
# chart a day is too narrow a slot (the box holds 31 of them) and labels on
# neighbouring days would run into each other, so they are not shown.
# ``_SPARK_PAD`` keeps the stroke inside the box at the top and bottom,
# ``_SPARK_MARGIN`` keeps the end dot inside it at the sides (an image clips
# what leaves its box).
SPARK_HEIGHT_PX = 44
LABEL_BAND_PX = 15
LABELS_MIN_WIDTH_PX = 880
_SPARK_WIDTH, _SPARK_HEIGHT, _SPARK_PAD, _SPARK_MARGIN = 100.0, 40.0, 4.0, 3.0
_SPARK_STROKE = {performance.GAIN: GAIN_INK, performance.LOSS: LOSS_INK}
# The box is stretched to the row's width; this keeps a stroke its own width.
_NON_SCALING = 'vector-effect="non-scaling-stroke"'
# Week rows from the bottom whose hover panel opens upward, and day columns
# from this index on whose panel is right-aligned, so it stays on screen.
PANEL_UP_ROWS = 2
PANEL_RIGHT_FROM = 3


def _tint_vars(*, theme: str) -> str:
    """CSS custom properties ``--g1..--g4`` / ``--l1..--l4`` for one theme."""
    parts = []
    for prefix, kind in (("g", performance.GAIN), ("l", performance.LOSS)):
        red, green, blue = TINT_RGB[kind]
        for step, alpha in enumerate(TINT_ALPHA[theme], start=1):
            parts.append(f"--{prefix}{step}: rgba({red},{green},{blue},{alpha:.2f});")
    return " ".join(parts)


def _tint_rules() -> str:
    """Tint classes: a layer over the tile's surface, a plain fill elsewhere."""
    rules = []
    for prefix in ("g", "l"):
        for step in range(1, 5):
            name = f"{prefix}{step}"
            rules.append(
                f".perftile.{name} {{ background-image: linear-gradient(var(--{name}), var(--{name})); }}\n"
                f".perfyear .{name}, .perfcal-legend .{name} {{ background-color: var(--{name}); }}"
            )
    return "\n".join(rules)


def _overlay_rules(*, box_prefix: str, button_prefix: str) -> str:
    """Stretch the Streamlit button of a keyed container over the whole container."""
    box, button = f'[class*="st-key-{box_prefix}"]', f'[class*="st-key-{button_prefix}"]'
    return (
        f"{box} {{ position: relative; }}\n"
        f"{button} {{ position: absolute; inset: 0; z-index: 5; width: auto; }}\n"
        f"{button} .stButton, {button} button {{ width: 100%; height: 100%; }}\n"
        f"{button} button {{ opacity: 0; cursor: pointer; }}"
    )


_PERF_CSS = f"""
<style>
.perfcal-root {{
  --surface: {SURFACE["light"]}; --surface-2: #f4f4f1; --ink: {INK["light"]}; --ink-2: {INK_2["light"]};
  --muted: #898781; --line: #e1e0d9; --line-strong: {LINE_STRONG["light"]}; --accent: #2a78d6;
  --gain: {GAIN_INK["light"]}; --loss: {LOSS_INK["light"]}; --hatch: rgba(11,11,11,0.07);
  {_tint_vars(theme="light")}
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink);
}}
.perfcal-root.dark {{
  --surface: {SURFACE["dark"]}; --surface-2: #1d2027; --ink: {INK["dark"]}; --ink-2: {INK_2["dark"]};
  --muted: #898781; --line: #2a2d34; --line-strong: {LINE_STRONG["dark"]}; --accent: #3987e5;
  --gain: {GAIN_INK["dark"]}; --loss: {LOSS_INK["dark"]}; --hatch: rgba(245,245,243,0.08);
  {_tint_vars(theme="dark")}
}}
.perfcal-legend {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px 18px; margin: 0 0 2px;
  font-size: 12.5px; color: var(--ink-2); }}
.perfcal-legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
.perfcal-legend i {{ width: 14px; height: 14px; border-radius: 4px; display: inline-block;
  box-shadow: inset 0 0 0 1px var(--line-strong); }}
.perfcal-legend .scale {{ gap: 2px; }}
.perfcal-legend .scale b {{ font-weight: 500; margin: 0 5px; }}
.perfcal-legend .sw-nodata {{ background: repeating-linear-gradient(135deg, var(--hatch) 0 3px, transparent 3px 6px); }}
.perfcal-legend .sw-pending {{ box-shadow: none; outline: 1px dashed var(--line-strong); outline-offset: -1px; }}
.perfcal-legend .sw-closed {{ background: var(--surface-2); box-shadow: none; }}
.perfcal-legend .sw-flow {{ width: 9px; height: 9px; border-radius: 50%; box-shadow: inset 0 0 0 2px var(--accent); }}
.perfcal-legend .sw-line {{ width: 18px; height: 2px; border-radius: 1px; box-shadow: none; background: var(--ink-2); }}
.perfcal-legend .sw-zero {{ width: 18px; height: 0; border-radius: 0; box-shadow: none;
  border-top: 1px dashed var(--line-strong); }}

/* Metric row: six cards. Values a step smaller than Streamlit's default so
   none is cut off, and one height whether or not a card has a second line. */
.st-key-{METRICS_KEY} [data-testid="stMetric"] {{ min-height: 7.2rem; }}
.st-key-{METRICS_KEY} [data-testid="stMetricValue"] {{ font-size: 1.55rem; }}

/* Month view: one tile per trading day, Mon..Fri, plus a Week total. */
.perfhead {{ font-size: 11.5px; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase;
  color: var(--ink-2); padding: 0 10px; }}
.perftile {{ position: relative; box-sizing: border-box; min-height: 80px; padding: 6px 10px 8px;
  border: 1px solid var(--line); border-radius: 10px; background-color: var(--surface);
  color: var(--ink); font-variant-numeric: tabular-nums; transition: border-color .12s, box-shadow .12s; }}
.perftile .num {{ font-size: 11.5px; color: var(--ink-2); line-height: 1.2; }}
.perftile .pct {{ font-size: 15px; font-weight: 650; margin-top: 7px; white-space: nowrap; }}
.perftile .money {{ font-size: 12px; color: var(--ink-2); margin-top: 2px; white-space: nowrap; }}
.perftile .arrow {{ font-size: 10px; margin-right: 4px; vertical-align: 1px; }}
.perftile.gain .arrow {{ color: var(--gain); }}
.perftile.loss .arrow {{ color: var(--loss); }}
.perftile .note {{ font-size: 11.5px; color: var(--ink-2); margin-top: 9px; }}
.perftile.empty {{ border-color: transparent; background: none; }}
.perftile.nodata {{ background-image: repeating-linear-gradient(135deg, var(--hatch) 0 6px, transparent 6px 12px); }}
.perftile.pending {{ border-style: dashed; border-color: var(--line-strong); }}
.perftile.closed {{ border-color: transparent; background-color: var(--surface-2); }}
.perftile.week {{ background-color: var(--surface-2); }}
.perftile.week .pct {{ font-size: 14px; margin-top: 21px; }}
.perftile .flow {{ position: absolute; top: 7px; right: 9px; width: 8px; height: 8px; border-radius: 50%;
  box-shadow: inset 0 0 0 2px var(--accent); }}

/* Hover panel: the day's numbers. Opens below the tile; upward on the last
   rows and right-aligned on the last columns so it never leaves the grid. */
.perftile .tip {{ display: none; position: absolute; top: calc(100% + 4px); left: 0; z-index: 20;
  min-width: 232px; padding: 9px 11px; border-radius: 8px; white-space: nowrap; text-align: left;
  background: var(--surface-2); color: var(--ink); border: 1px solid var(--line-strong);
  box-shadow: 0 6px 18px rgba(0,0,0,.25); font-size: 12px; font-weight: 400; line-height: 1.55; }}
.perftile.up .tip {{ top: auto; bottom: calc(100% + 4px); }}
.perftile.right .tip {{ left: auto; right: 0; }}
.perftile .tip b {{ font-weight: 650; }}
.perftile .tip .row {{ display: flex; justify-content: space-between; gap: 18px; }}
.perftile .tip .row span:first-child {{ color: var(--ink-2); }}
.perftile .tip .sep {{ border-top: 1px solid var(--line-strong); margin: 5px 0 4px; }}
.perftile .tip .hint {{ color: var(--ink-2); font-size: 11.5px; margin-top: 4px; white-space: normal; }}

{_tint_rules()}

/* A day tile opens that day, a month row opens that month. */
{_overlay_rules(box_prefix=DAY_KEY_PREFIX, button_prefix=DAY_OPEN_KEY_PREFIX)}
{_overlay_rules(box_prefix=ROW_KEY_PREFIX, button_prefix=OPEN_KEY_PREFIX)}
[class*="st-key-{DAY_KEY_PREFIX}"]:has(button:hover) .tip,
[class*="st-key-{DAY_KEY_PREFIX}"]:has(button:focus-visible) .tip {{ display: block; }}
[class*="st-key-{DAY_KEY_PREFIX}"]:has(button:hover) .perftile,
[class*="st-key-{ROW_KEY_PREFIX}"]:has(button:hover) .month {{
  border-color: var(--accent); box-shadow: 0 2px 10px rgba(0,0,0,.10); }}
[class*="st-key-{DAY_KEY_PREFIX}"]:has(button:focus-visible) .perftile,
[class*="st-key-{ROW_KEY_PREFIX}"]:has(button:focus-visible) .month {{
  outline: 2px solid var(--accent); outline-offset: 2px; }}
/* The hovered tile's panel must lie over the tiles and buttons after it. */
[data-testid="stColumn"]:has([class*="st-key-{DAY_KEY_PREFIX}"] button:hover),
[data-testid="stColumn"]:has([class*="st-key-{DAY_KEY_PREFIX}"] button:focus-visible),
[data-testid="stHorizontalBlock"]:has([class*="st-key-{DAY_KEY_PREFIX}"] button:hover),
[data-testid="stHorizontalBlock"]:has([class*="st-key-{DAY_KEY_PREFIX}"] button:focus-visible) {{
  position: relative; z-index: 30; }}

/* Several months: one row per month, the same columns in every row. */
.perfyear .month {{ display: grid; grid-template-columns: 9.5em minmax(90px, 1fr) 6em 7em 8.5em;
  align-items: center; column-gap: 16px; border: 1px solid var(--line); border-radius: 10px;
  background: var(--surface); padding: 6px 14px; font-variant-numeric: tabular-nums;
  transition: border-color .12s, box-shadow .12s; }}
.perfyear .name {{ font-size: 13px; font-weight: 600; color: var(--ink-2); }}
/* The month's line: an image, stretched to the column's width, with a band
   above and below it for the day labels. */
/* The chart lies over the row's open-month button so a day label can be hovered; it lets
   every other pointer event through, so a click anywhere but on a label still opens the month. */
.perfyear .chart {{ container-type: inline-size; padding: {LABEL_BAND_PX}px 0;
  position: relative; z-index: 6; pointer-events: none; }}
.perfyear .spark {{ display: block; width: 100%; height: {SPARK_HEIGHT_PX}px; }}
.perfyear .lbls {{ position: relative; height: 0; top: -{SPARK_HEIGHT_PX}px; }}
/* The padding is the hover area; the offsets take it back, so the text stays where it was. */
.perfyear .lbl {{ position: absolute; font-size: 10px; line-height: 1; white-space: nowrap;
  color: var(--ink-2); padding: 3px 4px; pointer-events: auto; cursor: default; }}
.perfyear .lbl.above {{ transform: translate(-50%, calc(-100% - 3px)); }}
.perfyear .lbl.below {{ transform: translate(-50%, 4px); }}
.perfyear .lbl:hover {{ color: var(--ink); font-weight: 650; z-index: 40; }}
/* The hovered day's date, over the label. */
.perfyear .lbl:hover::after {{ content: attr(data-date); position: absolute; left: 50%;
  bottom: calc(100% + 2px); transform: translateX(-50%); padding: 4px 8px; border-radius: 6px;
  background: var(--surface-2); color: var(--ink); border: 1px solid var(--line-strong);
  box-shadow: 0 4px 12px rgba(0,0,0,.22); font-size: 11.5px; font-weight: 500; }}
@container (max-width: {LABELS_MIN_WIDTH_PX}px) {{ .perfyear .lbl {{ display: none; }} }}
.perfyear .totals {{ display: contents; }}
.perfyear .pct {{ font-size: 14px; font-weight: 650; text-align: right; }}
.perfyear .money {{ font-size: 13px; color: var(--ink-2); text-align: right; }}
.perfyear .fees {{ display: flex; justify-content: space-between; gap: 10px; font-size: 12px;
  color: var(--ink-2); padding-left: 14px; border-left: 1px solid var(--line); }}
.perfyear .arrow {{ font-size: 9px; margin-right: 4px; vertical-align: 1px; }}
.perfyear .gain .arrow {{ color: var(--gain); }}
.perfyear .loss .arrow {{ color: var(--loss); }}
.perfyear .none {{ color: var(--muted); }}
</style>
"""


# ---- formatting -------------------------------------------------------------

def fmt_pct(*, ret: float | None) -> str:
    """Signed percent with two decimals; an em dash when undefined."""
    if ret is None:
        return "—"
    value = ret * 100
    if abs(value) < 0.005:
        return "0.00%"
    return f"{'+' if value > 0 else _MINUS}{abs(value):,.2f}%"


def fmt_money(*, value: float | None) -> str:
    """Signed amount with two decimals; an em dash when missing."""
    if value is None:
        return "—"
    if abs(value) < performance.FLAT_EPSILON:
        return "0.00"
    return f"{'+' if value > 0 else _MINUS}{abs(value):,.2f}"


def fmt_date(*, date: dt.date) -> str:
    return f"{date:%d-%m-%Y}"


def fmt_weekday_date(*, date: dt.date) -> str:
    """A date with its weekday, for the hover text of a day on a month's line."""
    return f"{_calendar.day_abbr[date.weekday()]} {fmt_date(date=date)}"


def fmt_mover(*, mover: dict, currency: str | None) -> str:
    """A ticker's day in one line: ``PLTR +3.21% (+27.47 USD)``."""
    unit = f" {currency}" if currency else ""
    return f"{mover['symbol']} {fmt_pct(ret=mover['pct'])} ({fmt_money(value=mover['pnl'])}{unit})"


def _root(*, dark: bool, body: str) -> str:
    return f'<div class="{"perfcal-root dark" if dark else "perfcal-root"}">{body}</div>'


def _arrow(*, state: str) -> str:
    if state == performance.GAIN:
        return f'<span class="arrow">{_UP}</span>'
    if state == performance.LOSS:
        return f'<span class="arrow">{_DOWN}</span>'
    return ""


def _tint_class(*, state: str, step: int) -> str:
    if step and state == performance.GAIN:
        return f"g{step}"
    if step and state == performance.LOSS:
        return f"l{step}"
    return ""


def _figures(*, state: str, ret: float | None, pnl: float | None) -> str:
    """The two lines of a result: arrow + percent, then money."""
    return (
        f'<div class="pct">{_arrow(state=state)}{html.escape(fmt_pct(ret=ret))}</div>'
        f'<div class="money">{html.escape(fmt_money(value=pnl))}</div>'
    )


def styles_html() -> str:
    """The calendar stylesheet; the page draws it once, before the parts."""
    return _PERF_CSS


def legend_html(*, dark: bool) -> str:
    """Scale and state key."""
    loss = "".join(f'<i class="l{step}"></i>' for step in (4, 3, 2, 1))
    gain = "".join(f'<i class="g{step}"></i>' for step in (1, 2, 3, 4))
    top = performance.TINT_BOUNDS[-1] * 100
    return _root(dark=dark, body=(
        '<div class="perfcal-legend">'
        f'<span class="scale"><b>{_DOWN} {_MINUS}{top:g}% or more</b>{loss}<i></i>{gain}'
        f'<b>{_UP} +{top:g}% or more</b></span>'
        '<span><i class="sw-nodata"></i>No data</span>'
        '<span><i class="sw-pending"></i>Pending (no close yet)</span>'
        '<span><i class="sw-closed"></i>Market holiday</span>'
        '<span><i class="sw-flow"></i>Deposit or withdrawal</span>'
        '<span>Weekends are not shown: markets are closed.</span>'
        "</div>"
    ))


def rows_legend_html(*, dark: bool) -> str:
    """Key for the month rows: what the line and the dashed rule are."""
    return _root(dark=dark, body=(
        '<div class="perfcal-legend">'
        '<span><i class="sw-line"></i>Return since the month\'s start, day by day</span>'
        '<span><i class="sw-zero"></i>Month\'s start (0%)</span>'
        '<span>Numbers: each day\'s own return, in percent</span>'
        '<span>Each day sits at its date, so the rows line up. Days without a close have no point.</span>'
        '<span>All rows share one scale.</span>'
        "</div>"
    ))


# ---- month view: day tiles ----------------------------------------------------

def _tip_row(*, label: str, value: str) -> str:
    return f'<div class="row"><span>{html.escape(label)}</span><span>{html.escape(value)}</span></div>'


_SEP = '<div class="sep"></div>'


def _tip(*, cell: dict, currency: str | None) -> str:
    unit = f" {currency}" if currency else ""
    rows = [f"<b>{fmt_date(date=cell['date'])}</b>"]
    rows.append(_tip_row(label="Close", value=f"{cell['nav']:,.2f}{unit}"))
    if cell["prev_nav"] is not None:
        rows.append(_tip_row(label="Previous close", value=f"{cell['prev_nav']:,.2f}{unit}"))
        rows.append(_tip_row(
            label="Change",
            value=f"{fmt_money(value=cell['pnl'])}{unit} ({fmt_pct(ret=cell['ret'])})",
        ))
    if cell["best"] is not None:
        rows.append(_SEP)
        rows.append(_tip_row(label="Best ticker", value=fmt_mover(mover=cell["best"], currency=currency)))
        if cell["worst"] is not None:
            rows.append(_tip_row(label="Worst ticker",
                                 value=fmt_mover(mover=cell["worst"], currency=currency)))
        rows.append(_SEP)
    if abs(cell["flow"]) >= performance.FLAT_EPSILON:
        label = "Deposit" if cell["flow"] > 0 else "Withdrawal"
        rows.append(_tip_row(label=label, value=f"{abs(cell['flow']):,.2f}{unit}"))
    if abs(cell["dividends"]) >= performance.FLAT_EPSILON:
        rows.append(_tip_row(label="Dividends received", value=f"{cell['dividends']:,.2f}{unit}"))
    if abs(cell["fees"]) >= performance.FLAT_EPSILON:
        rows.append(_tip_row(label="Fees", value=f"{cell['fees']:,.2f}{unit}"))
    trades = [f"{count} {word}" for count, word in ((cell["bought"], "bought"), (cell["sold"], "sold"))
              if count]
    if trades:
        rows.append(_tip_row(label="Trades", value=", ".join(trades)))
    if rows[-1] == _SEP:
        rows.pop()

    hints = []
    if cell["prev_nav"] is None:
        hints.append("First close on record: nothing to compare it with.")
    if abs(cell["flow"]) >= performance.FLAT_EPSILON:
        hints.append("The deposit or withdrawal is not counted in the change.")
    if cell["gap"]:
        hints.append(f"Change since {fmt_date(date=cell['prev_date'])}: closes in between are missing.")
    if cell["prev_nav"] is not None and cell["ret"] is None:
        hints.append("No capital at the start of the day, so there is no percent.")
    hints.append("Click for the day's details.")
    rows.extend(f'<div class="hint">{html.escape(hint)}</div>' for hint in hints)
    return f'<div class="tip">{"".join(rows)}</div>'


def has_result(*, cell: dict) -> bool:
    """True for a day with a close: the tiles that open a day's details."""
    return cell["state"] in (performance.GAIN, performance.LOSS, performance.FLAT)


def weekday_head_html(*, name: str, dark: bool) -> str:
    """A column heading of the month grid."""
    return _root(dark=dark, body=f'<div class="perfhead">{html.escape(name)}</div>')


def day_tile_html(*, cell: dict, currency: str | None, position: str, dark: bool) -> str:
    """One day of the month grid. ``position`` is the hover panel's placement:
    any of ``up`` and ``right``, space-separated, or empty."""
    state = cell["state"]
    if state == performance.EMPTY:
        return _root(dark=dark, body='<div class="perftile empty"></div>')
    number = f'<div class="num">{cell["date"].day}</div>'
    if state == performance.NO_DATA:
        return _root(dark=dark, body=f'<div class="perftile nodata">{number}<div class="note">no data</div></div>')
    if state == performance.PENDING:
        return _root(dark=dark, body=f'<div class="perftile pending">{number}<div class="note">pending</div></div>')
    if state == performance.CLOSED:
        return _root(dark=dark, body=f'<div class="perftile closed">{number}<div class="note">market closed</div></div>')

    classes = " ".join(
        part for part in ("perftile", state, _tint_class(state=state, step=cell["step"]), position) if part
    )
    flow = '<span class="flow"></span>' if abs(cell["flow"]) >= performance.FLAT_EPSILON else ""
    return _root(dark=dark, body=(
        f'<div class="{classes}">{number}{flow}'
        f'{_figures(state=state, ret=cell["ret"], pnl=cell["pnl"])}'
        f'{_tip(cell=cell, currency=currency)}</div>'
    ))


def week_tile_html(*, summary: dict, dark: bool) -> str:
    """The Week column: the week's linked return and money."""
    if not summary["days"]:
        return _root(dark=dark, body='<div class="perftile week"></div>')
    state = performance.state(pnl=summary["pnl"])
    return _root(dark=dark, body=(
        f'<div class="perftile week {state}">'
        f'{_figures(state=state, ret=summary["ret"], pnl=summary["pnl"])}</div>'
    ))


# ---- several months: month rows -----------------------------------------------

def _line_days(*, month: dict) -> list[dict]:
    """The month's days that the line draws: those with a return, in day order.
    A day with no close (a holiday, a day before the account's first close, a
    day still to come) has nothing to draw and gets no point."""
    return [cell for week in month["weeks"] for cell in week["days"]
            if cell["date"].month == month["month"] and has_result(cell=cell) and cell["ret"] is not None]


def month_line(*, month: dict) -> list[tuple[float, float]]:
    """The month's running return as ``(x, value)`` points, in day order.

    ``x`` is the day's date as a share of the month (day / days in month), so a
    date sits at the same place in every row and a month still running ends
    part way. The line starts at 0 at the close it is measured from: the end
    of the previous month (``x = 0``), or the day before the first result when
    the account's data starts inside the month. ``value`` links the daily
    returns (``prod(1 + r) - 1``), so the last one is the month's return. A
    month with no such day gives no points.
    """
    days = _line_days(month=month)
    if not days:
        return []
    length = _calendar.monthrange(month["year"], month["month"])[1]
    before = days[0]["prev_date"]
    start = before.day / length if before is not None and before.month == month["month"] else 0.0
    points: list[tuple[float, float]] = [(start, 0.0)]
    linked = 1.0
    for cell in days:
        linked *= 1.0 + cell["ret"]
        points.append((cell["date"].day / length, linked - 1.0))
    return points


def period_scale(*, months: list[dict]) -> tuple[float, float]:
    """The lowest and highest running return over every month's line, 0
    included: the one vertical scale all the rows of a period are drawn on, so
    a big month looks bigger than a small one."""
    values = [value for month in months for _, value in month_line(month=month)]
    return min([0.0, *values]), max([0.0, *values])


def fmt_day_label(*, ret: float) -> str:
    """A day's return for a label on the line: percent, one decimal, no percent sign."""
    text = f"{abs(ret) * 100:.1f}"
    if text == "0.0":
        return text
    return f"{'+' if ret > 0 else _MINUS}{text}"


def _spark_coords(
    *, points: list[tuple[float, float]], scale: tuple[float, float],
) -> tuple[float, list[tuple[float, float]]]:
    """The zero rule's y and the points of ``month_line``, in the line box's
    units; ``scale`` is the (low, high) value range the box's height spans."""
    low, high = scale
    span = high - low
    drawable = _SPARK_HEIGHT - 2 * _SPARK_PAD

    def y_of(*, value: float) -> float:
        if not span:
            return _SPARK_HEIGHT / 2
        return _SPARK_PAD + (high - value) / span * drawable

    return y_of(value=0.0), [(x * _SPARK_WIDTH, y_of(value=value)) for x, value in points]


def spark_svg(*, month: dict, scale: tuple[float, float], dark: bool) -> tuple[str, str, str]:
    """The month's line over a dashed zero rule, as a standalone SVG.

    Returns ``(svg, kind, label)``: ``kind`` is the direction the line ends in
    (empty when the month has no line) and ``label`` says where it ends, for
    the image's alt text. Colours are written into the SVG: it is shown as an
    image, which does not see the page's stylesheet. ``scale`` is
    ``period_scale``.
    """
    theme = "dark" if dark else "light"
    points = month_line(month=month)
    zero, coords = _spark_coords(points=points, scale=scale)
    parts = [f'<path class="zero" d="M0 {zero:.2f} H{_SPARK_WIDTH:g}" stroke="{LINE_STRONG[theme]}" '
             f'stroke-width="1" stroke-dasharray="3 3" {_NON_SCALING}/>']
    kind, label = "", "no results"
    if points:
        path = " ".join(f"{'M' if i == 0 else 'L'}{x:.2f} {y:.2f}" for i, (x, y) in enumerate(coords))
        end_x, end_y = coords[-1]
        # Colour follows where the line itself ends (the linked return), not the
        # month's money: after a deposit the two can have opposite signs.
        end = fmt_pct(ret=points[-1][1])
        kind = {"+": performance.GAIN, _MINUS: performance.LOSS}.get(end[0], performance.FLAT)
        stroke = _SPARK_STROKE[kind][theme] if kind in _SPARK_STROKE else INK_2[theme]
        parts.append(f'<path class="line" d="{path}" stroke="{stroke}" stroke-width="2" '
                     f'stroke-linejoin="round" stroke-linecap="round" {_NON_SCALING}/>')
        # The end dot is a zero-length round cap, so it stays round when stretched.
        parts.append(f'<path class="end" d="M{end_x:.2f} {end_y:.2f} h0" stroke="{stroke}" '
                     f'stroke-width="6" stroke-linecap="round" {_NON_SCALING}/>')
        label = f"ends at {end}"
    view = f"{-_SPARK_MARGIN:g} 0 {_SPARK_WIDTH + 2 * _SPARK_MARGIN:g} {_SPARK_HEIGHT:g}"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" preserveAspectRatio="none" '
        f'fill="none">{"".join(parts)}</svg>'
    )
    return svg, kind, label


# Box units under which a point counts as on its neighbours' chord.
_LABEL_SIDE_EPSILON = 0.01


def _label_side(*, coords: list[tuple[float, float]], index: int, ret: float) -> str:
    """Which side of point ``coords[index]`` its label goes: the side the line
    bends away from, so neither segment at the point runs through the text.

    The point is compared with the straight line between its neighbours, taken
    at the point's x (the last point has one neighbour: level with it). Above
    that chord the line is a peak there and the label goes above; below it, a
    valley, and the label goes below. On the chord, the day's sign decides.
    """
    x, y = coords[index]
    prev_x, prev_y = coords[index - 1]
    if index + 1 < len(coords):
        next_x, next_y = coords[index + 1]
        chord = prev_y + (next_y - prev_y) * (x - prev_x) / (next_x - prev_x)
    else:
        chord = prev_y
    if abs(y - chord) < _LABEL_SIDE_EPSILON:
        return "below" if ret < 0 else "above"
    return "above" if y < chord else "below"


def day_labels(*, month: dict, scale: tuple[float, float]) -> list[dict]:
    """One label per day on the line: ``{"date", "text", "left", "top", "side"}``.

    ``text`` is that day's own return (the line's height is the running one).
    ``left`` is a percent of the chart's width and ``top`` is pixels down the
    line's box: the image is stretched sideways only, so text over it cannot
    be part of it. ``side`` is ``_label_side``. ``scale`` is ``period_scale``.
    """
    days = _line_days(month=month)
    _, coords = _spark_coords(points=month_line(month=month), scale=scale)
    full = _SPARK_WIDTH + 2 * _SPARK_MARGIN
    return [
        {
            "date": cell["date"],
            "text": fmt_day_label(ret=cell["ret"]),
            "left": (x + _SPARK_MARGIN) / full * 100,
            "top": y / _SPARK_HEIGHT * SPARK_HEIGHT_PX,
            "side": _label_side(coords=coords, index=index, ret=cell["ret"]),
        }
        for index, (cell, (x, y)) in enumerate(zip(days, coords[1:]), start=1)
    ]


def _spark(*, month: dict, scale: tuple[float, float], dark: bool) -> str:
    """The month's line as an image, with each day's return written at its
    point. Hovering a return shows that day's date."""
    svg, kind, label = spark_svg(month=month, scale=scale, dark=dark)
    name = f"{_calendar.month_name[month['month']]} {month['year']}"
    data = base64.b64encode(svg.encode()).decode()
    labels = "".join(
        f'<span class="lbl {item["side"]}" data-date="{fmt_weekday_date(date=item["date"])}" '
        f'style="left:{item["left"]:.2f}%;top:{item["top"]:.1f}px">'
        f'{html.escape(item["text"])}</span>'
        for item in day_labels(month=month, scale=scale)
    )
    return (
        '<div class="chart">'
        f'<img class="{f"spark {kind}".strip()}" src="data:image/svg+xml;base64,{data}" '
        f'alt="{name} running return: {html.escape(label)}">'
        f'<div class="lbls">{labels}</div></div>'
    )


def month_row_html(*, month: dict, scale: tuple[float, float], dark: bool) -> str:
    """One month as a row: its name, the running return as a line, then the
    month's return, money and fees.

    ``month`` is one item of ``performance.period_months``; ``scale`` is
    ``period_scale`` of all the rows shown with it.
    """
    summary = month["summary"]
    if summary["days"]:
        state = performance.state(pnl=summary["pnl"])
        figures = _figures(state=state, ret=summary["ret"], pnl=summary["pnl"])
        totals = f'<div class="totals {state}">{figures}</div>'
    else:
        totals = '<div class="totals"><div class="pct none">—</div><div class="money none">no results</div></div>'
    fees = f'<div class="fees"><span>Fees</span><span>{month["fees"]:,.2f}</span></div>'
    return _root(dark=dark, body=(
        '<div class="perfyear"><div class="month">'
        f'<div class="name">{_calendar.month_name[month["month"]]} {month["year"]}</div>'
        f'{_spark(month=month, scale=scale, dark=dark)}{totals}{fees}</div></div>'
    ))
