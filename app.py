"""IBKR Dashboard — Streamlit entry point.

Orchestrates: pull (Flex Web Service) -> parse -> DataFrames -> Plotly figures.
Heavy work is cached so Streamlit reruns (every widget interaction) don't re-hit
IBKR; the sidebar Refresh button clears the cache to force a fresh pull.
"""
from __future__ import annotations

import html
import logging

import pandas as pd
import streamlit as st

import logsetup
from charts import figures
from config import load_settings
from data import (
    backup, capital, div_external, div_provider, dividends, history, price_stats,
    prices, snapshots,
)
from ibkr import flex, parser

st.set_page_config(page_title="IBKR Dashboard", page_icon="📈", layout="wide")

# Wire the myitbr pipeline loggers (stderr + rotating logs/app.log). Idempotent,
# so Streamlit reruns of this module never duplicate handlers or log lines.
logsetup.setup_logging()
logger = logging.getLogger("myitbr.app")

settings = load_settings()


# ---- data loading (cached) ------------------------------------------------

@st.cache_data(show_spinner="Pulling from IBKR Flex…")
def load_portfolio(token: str, query_id: str, db_path_str: str) -> dict:
    """Download + parse a Flex statement and record a NAV snapshot.

    Cached by credentials, so this runs only on first load or after the cache is
    cleared (Refresh). Returns plain DataFrames/dicts (serializable for caching).
    """
    xml = flex.download_statement(token, query_id)
    stmt = parser.parse_statement(xml)
    if stmt is None:
        raise flex.FlexError("Flex statement contained no data. Check the query definition.")

    nav = parser.latest_nav(stmt)
    equity = parser.equity_summary_df(stmt)
    # Record one snapshot per pull (deduped by date inside the store).
    if nav.get("net_liq") is not None or nav.get("cash") is not None:
        snapshots.record_snapshot(
            db_path_str,
            net_liq=nav.get("net_liq"),
            cash=nav.get("cash"),
            currency=nav.get("currency"),
        )

    trades = parser.trades_df(stmt)
    cash_tx = parser.cash_transactions_df(stmt)
    # The Flex window rolls forward (Last365CalendarDays), so trades and cash
    # transactions age out of the statement and cannot be re-fetched afterwards.
    # Archive them locally and serve the union, so history keeps growing past the
    # window. A store failure must never cost the user the live statement, so the
    # in-window frames stay the fallback.
    trades, cash_tx = _archive_and_merge(db_path_str, trades, cash_tx, equity, nav)

    return {
        "account": parser.account_info(stmt),
        "positions": parser.positions_df(stmt),
        "trades": trades,
        "cash": parser.cash_df(stmt),
        "equity": equity,
        "cash_tx": cash_tx,
        "accruals": parser.dividend_accruals_df(stmt),
        "nav": nav,
    }


def _archive_and_merge(
    db_path_str: str,
    trades: pd.DataFrame,
    cash_tx: pd.DataFrame,
    equity: pd.DataFrame,
    nav: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fold this statement into the local archive and read the full history back.

    Degrades to the statement's own frames on any store error — a broken archive
    should cost the user history, never the dashboard.
    """
    try:
        n_trades = history.record_trades(db_path_str, trades)
        n_cash = history.record_cash_tx(db_path_str, cash_tx)
        n_nav = history.backfill_nav(db_path_str, equity, nav.get("currency"))
        archived_trades = history.read_trades(db_path_str)
        archived_cash = history.read_cash_tx(db_path_str)
    except Exception:  # noqa: BLE001 — archiving is best-effort by design
        logger.exception(
            "history archive failed; falling back to the statement window only"
        )
        return trades, cash_tx

    logger.info(
        "history: folded %d trades / %d cash tx / backfilled %d NAV days; "
        "archive now holds %d trades, %d cash tx",
        n_trades, n_cash, n_nav, len(archived_trades), len(archived_cash),
    )

    # The store now holds history IBKR can no longer re-serve, so back it up once
    # the fold has landed. backup.run swallows its own failures.
    backup.run(db_path_str, settings.backup_dir, keep=settings.backup_keep)
    # An empty archive read (fresh DB that failed to write) must not blank a tab.
    return (
        archived_trades if not archived_trades.empty else trades,
        archived_cash if not archived_cash.empty else cash_tx,
    )


@st.cache_data(show_spinner=False)
def load_prices(symbol: str, period: str) -> pd.DataFrame:
    return prices.fetch_ohlc(symbol, period=period)


@st.cache_data(show_spinner="Fetching upcoming dividends (yfinance)…")
def load_external_dividends(symbols: tuple[str, ...]) -> dict:
    """yfinance ex/pay dates per symbol, for tickers Flex has no data on at all.

    Cached on the symbol tuple, so it refetches only when the set of no-data
    tickers changes or the cache is cleared (Refresh). Each lookup degrades to
    empty values on failure, so one bad symbol never breaks the tab.
    """
    return {sym: div_external.fetch_upcoming(sym) for sym in symbols}


@st.cache_data(show_spinner="Fetching declared dividends (Polygon)…")
def load_polygon_declared(symbols: tuple[str, ...], polygon_key: str) -> dict:
    """Real declared-dividend info (ex/pay/decl dates + per-share amount) per
    symbol from Polygon, for held tickers with no Flex accrual yet.

    Backed by a daily on-disk cache (``db/snapshots.sqlite``) so the free-tier
    rate limit is paid roughly once per day and partial progress survives across
    refreshes; rate-limited tickers resolve on a later refresh rather than
    silently falling back to red. Each lookup degrades gracefully, never breaks
    the tab.
    """
    if not polygon_key:
        return {}
    return div_provider.fetch_declared_many(symbols, polygon_key, str(settings.db_path))


# ---- sidebar --------------------------------------------------------------

def render_sidebar() -> None:
    st.sidebar.header("IBKR Dashboard")
    if settings.is_configured:
        st.sidebar.success("Flex credentials loaded")
    else:
        st.sidebar.error("Flex credentials missing")
    if st.sidebar.button("🔄 Refresh data", width="stretch"):
        # Clear the cache and let THIS run continue — the button click itself
        # already triggered a rerun, and load_portfolio (called after this in
        # main()) cache-misses and refetches in the same run. Do NOT add
        # st.rerun() here: it aborts the run before the keyed Dividends radios
        # (div_basis / div_lookback) render, and Streamlit drops session state
        # for keyed widgets not rendered in a run — so Refresh would silently
        # reset the user's selections to defaults (e4 FE-1 regression).
        st.cache_data.clear()
    st.sidebar.caption(f"DB: `{settings.db_path}`")

    # The DB is the only copy of history older than the Flex window, so surface
    # the backup state rather than leaving it to be discovered when it's too late.
    state = backup.status(settings.backup_dir)
    if state["count"]:
        st.sidebar.caption(
            f"Backups: {state['count']} (latest {state['latest_date']}) in "
            f"`{settings.backup_dir}`"
        )
    else:
        st.sidebar.caption(f"Backups: none yet — will write to `{settings.backup_dir}`")


# ---- tabs -----------------------------------------------------------------

def _fmt_money(v, ccy: str | None) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:,.2f} {ccy or ''}".strip()


def tab_overview(data: dict) -> None:
    account, nav = data["account"], data["nav"]
    st.subheader("Account")
    cols = st.columns(2)
    cols[0].write(f"**Account:** {account.get('account_id') or '—'}")
    cols[0].write(f"**Name:** {account.get('name') or '—'}")
    cols[1].write(f"**Type:** {account.get('account_type') or '—'}")
    cols[1].write(f"**Base currency:** {account.get('currency') or '—'}")

    st.subheader("Summary")
    ccy = nav.get("currency")
    net_liq = nav.get("net_liq")
    positions = data["positions"]
    gross_exposure = (
        float(positions["market_value"].dropna().sum())
        if not positions.empty and "market_value" in positions
        else None
    )
    leverage = (
        gross_exposure / net_liq
        if gross_exposure is not None and net_liq not in (None, 0) and not pd.isna(net_liq)
        else None
    )

    m = st.columns(3)
    m[0].metric("Net liquidation", _fmt_money(net_liq, ccy))
    m[1].metric("Cash", _fmt_money(nav.get("cash"), ccy))
    m[2].metric("As of", str(nav.get("as_of") or "—"))

    m2 = st.columns(3)
    m2[0].metric("Gross exposure", _fmt_money(gross_exposure, ccy))
    m2[1].metric("Leverage", f"{leverage:.2f}×" if leverage is not None else "—")
    st.caption(
        "Gross exposure = Σ position market value; leverage = exposure ÷ net liq. "
        "Flex carries no Reg-T buying power or margin requirement, so these are "
        "derived from positions, not live margin figures."
    )

    if not data["cash"].empty:
        st.subheader("Cash by currency")
        st.dataframe(data["cash"], width="stretch", hide_index=True)


def _dividends_by_symbol(cash_tx, symbols) -> tuple[dict, object]:
    """Net dividends received per symbol, plus the earliest payment seen.

    Net (withholding already folded in by price_stats), because gross would
    credit the position with cash that never arrived. The earliest date is
    returned so the caller can say how far the income actually reaches — the
    archive is the only bound on that, and a silently short one understates
    total return.
    """
    out: dict[str, float] = {}
    first = None
    if cash_tx is None or getattr(cash_tx, "empty", True):
        return out, first
    for symbol in symbols:
        info = price_stats.dividends_received(cash_tx, symbol)
        if info.get("net"):
            out[symbol] = info["net"]
        if info.get("first_date") and (first is None or info["first_date"] < first):
            first = info["first_date"]
    return out, first


def _render_profit_heatmap(positions: pd.DataFrame, cash_tx) -> None:
    """Weight-vs-return treemap with an opt-in dividend-income toggle."""
    with_divs = st.checkbox(
        "Include dividends received (total return)",
        value=False,
        help=(
            "Adds each holding's net dividends to its return. Distribution-heavy "
            "ETFs pay out of NAV, so price-only return reads them as deep losers."
        ),
    )
    divs, first_div = ({}, None)
    if with_divs:
        divs, first_div = _dividends_by_symbol(cash_tx, positions["symbol"].tolist())

    st.plotly_chart(
        figures.profit_heatmap_treemap(positions, dividends=divs or None),
        width="stretch",
    )

    caption = (
        "Tile size is the position's share of the portfolio, tile color its "
        "return — losers stay large and turn red rather than shrinking. "
    )
    if not with_divs:
        caption += "Return is price-only; dividends received are not counted."
    elif divs:
        # Income only reaches as far back as the archive does; saying so keeps a
        # short history from quietly reading as a low-yield holding.
        span = f" back to {_fmt_cov_date(first_div)}" if first_div else ""
        caption += (
            f"Return includes net dividends received{span} "
            f"({len(divs)} of {len(positions)} holdings have paid)."
        )
    else:
        caption += (
            "No dividend payments found in the local archive for these holdings "
            "— showing price-only return."
        )
    st.caption(caption)

    if not divs:
        return
    # The heatmap blends price and income into one colour. Split them out so a
    # holding carried entirely by its payout is visible as such.
    st.plotly_chart(figures.return_split_bar(positions, dividends=divs), width="stretch")
    skipped = len(positions) - len(divs)
    st.caption(
        f"Showing {len(divs)} dividend-paying holding(s)"
        + (f"; {skipped} non-payer(s) omitted." if skipped > 0 else ".")
    )


def tab_positions(data: dict) -> None:
    positions = data["positions"]
    if positions.empty:
        st.info("No open positions in the Flex statement.")
        return
    # Flex reports prices/P&L at 4+ decimals; two is what a human reads.
    money_cols = ["cost_price", "cost_basis", "mark_price", "market_value", "unrealized_pnl"]
    st.dataframe(
        positions, width="stretch", hide_index=True,
        column_config={
            c: st.column_config.NumberColumn(format="%.2f")
            for c in money_cols if c in positions.columns
        },
    )
    c1, c2 = st.columns(2)
    c1.plotly_chart(figures.allocation_treemap(positions), width="stretch")
    c2.plotly_chart(figures.unrealized_pnl_bar(positions), width="stretch")
    _render_profit_heatmap(positions, data.get("cash_tx"))


def tab_trades(data: dict) -> None:
    trades = data["trades"]
    if trades.empty:
        st.info("No trades in the Flex statement.")
        return
    # trade_id/transaction_id exist only to key the local archive — internal
    # plumbing, not something to put in front of the user.
    st.dataframe(
        trades.drop(columns=["trade_id", "transaction_id"], errors="ignore"),
        width="stretch", hide_index=True,
    )
    c1, c2 = st.columns(2)
    c1.plotly_chart(figures.realized_pnl_bar(trades), width="stretch")
    c2.plotly_chart(figures.cumulative_pnl_line(trades), width="stretch")


def _fmt_num(v, suffix: str = "", digits: int = 2) -> str:
    """Metric-friendly number: em dash for missing, thousands separators."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def _fmt_qty(v) -> str:
    """Share count: whole lots stay integers, fractional shares keep their digits.

    IBKR supports fractional shares, so a fixed 0-decimal format would render a
    real 0.26-share position as "0".
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.4f}".rstrip("0")


def _fmt_delta(value, pct) -> str | None:
    """Streamlit metric delta: absolute value plus percent, either one optional.

    Streamlit picks the up/down arrow colour from the *first character* of the
    string, so the signed number must lead — a leading "(" would paint every
    percent green, losses included.
    """
    has_value = value is not None and not pd.isna(value)
    has_pct = pct is not None and not pd.isna(pct)
    if has_value and has_pct:
        return f"{value:+,.2f} ({pct:+.2f}%)"
    if has_value:
        return f"{value:+,.2f}"
    if has_pct:
        return f"{pct:+.2f}%"
    return None


def tab_prices(data: dict) -> None:
    positions = data["positions"]
    symbols = sorted(positions["symbol"].dropna().unique()) if not positions.empty else []
    if not symbols:
        st.info("No symbols available (no open positions). Prices use yfinance.")
        return
    c1, c2 = st.columns([2, 1])
    symbol = c1.selectbox("Symbol", symbols)
    period = c2.selectbox("Period", ["1mo", "3mo", "6mo", "1y", "2y", "5y"], index=3)

    ohlc = load_prices(symbol, period)
    # The 52-week range must not move when the period selector narrows, so it is
    # computed off a separate 1y pull (cached, and free when period is already 1y).
    year_ohlc = ohlc if period == "1y" else load_prices(symbol, "1y")
    stats = price_stats.summarize(
        ohlc, symbol, positions=positions, cash_tx=data["cash_tx"], year_ohlc=year_ohlc,
    )
    ccy = stats.get("currency")
    div = stats["dividends"]

    # ---- KPI rows ---------------------------------------------------------
    r1 = st.columns(4)
    r1[0].metric("Current price", _fmt_money(stats["current"], ccy),
                 _fmt_delta(stats["day_change"], stats["day_change_pct"]))
    r1[1].metric("My avg cost", _fmt_money(stats["avg_cost"], ccy),
                 f"basis {_fmt_money(stats['cost_basis'], ccy)}"
                 if stats["cost_basis"] is not None else None, delta_color="off")
    r1[2].metric("Unrealized P&L", _fmt_money(stats["unrealized_live"], ccy),
                 _fmt_delta(None, stats["vs_cost_pct"]))
    r1[3].metric("Position", _fmt_qty(stats["quantity"]),
                 f"{_fmt_money(stats['market_value_live'], ccy)} mkt value"
                 if stats["market_value_live"] is not None else None,
                 delta_color="off")

    r2 = st.columns(4)
    r2[0].metric(f"Period mean ({period})", _fmt_money(stats["period_mean"], ccy))
    r2[1].metric(f"Period high / low ({period})",
                 f"{_fmt_num(stats['period_high'])} / {_fmt_num(stats['period_low'])}",
                 _fmt_delta(None, stats["off_period_high_pct"]))
    r2[2].metric("52-week high / low",
                 f"{_fmt_num(stats['high_52w'])} / {_fmt_num(stats['low_52w'])}")
    r2[3].metric(f"Period return ({period})", _fmt_num(stats["period_return_pct"], "%"))

    r3 = st.columns(4)
    r3[0].metric("Avg volume", _fmt_num(stats["avg_volume"], digits=0))
    r3[1].metric("Volatility (ann.)", _fmt_num(stats["volatility_pct"], "%"))
    r3[2].metric("ATR(14)", _fmt_money(stats["atr"], ccy))
    r3[3].metric("Total return incl. divs", _fmt_money(stats["total_return"], ccy),
                 _fmt_delta(None, stats["total_return_pct"]))

    r4 = st.columns(4)
    r4[0].metric("Dividends received (net)", _fmt_money(div["net"], ccy),
                 f"{div['count']} payments" if div["count"] else None, delta_color="off")
    r4[1].metric("Dividends received (gross)", _fmt_money(div["gross"], ccy))
    r4[2].metric("Last dividend", _fmt_money(div["last_amount"], ccy),
                 str(div["last_date"]) if div["last_date"] else None, delta_color="off")
    r4[3].metric("Yield (TTM) / on cost",
                 f"{_fmt_num(stats['yield_pct'], '%')} / {_fmt_num(stats['yield_on_cost_pct'], '%')}")

    # ---- overlay toggles --------------------------------------------------
    t1, t2, t3, t4 = st.columns(4)
    show_cost = t1.checkbox("Avg cost line", value=True, key="px_cost")
    show_ma = t2.checkbox("MA 20/50", value=True, key="px_ma")
    show_mean = t3.checkbox("Period mean", value=False, key="px_mean")
    show_trades = t4.checkbox("Buy/Sell markers", value=True, key="px_trades")

    st.plotly_chart(
        figures.candlestick(
            ohlc, symbol,
            avg_cost=stats["avg_cost"] if show_cost else None,
            ma_windows=(20, 50) if show_ma else (),
            mean_close=stats["period_mean"] if show_mean else None,
            trades=(data["trades"][data["trades"]["symbol"].astype(str) == str(symbol)]
                    if show_trades and not data["trades"].empty else None),
        ),
        width="stretch",
    )
    as_of = f" ({stats['as_of']})" if stats["as_of"] else ""
    st.caption(
        "Price, ranges and volatility: yfinance (Flex provides no market data); "
        f"current price is the last close{as_of}. "
        "Cost, position and dividends come from the Flex statement — dividend totals "
        "cover the statement's date range only. Yield (TTM) = trailing-12-month "
        "dividends ÷ current price; on cost uses my average cost."
    )


# Cell CSS class per status; colors mirror the reference mock (status, not value).
_DIV_CLASSES = {"paid": "paid", "declared": "declared", "pay": "word", "declare": "word"}

_DIV_CSS = """
<style>
/* Theme tokens: one light and one dark set, picked by the wrapper class that
   follows the Streamlit theme. Status fills are low-alpha tints so the
   numbers (always in text ink, never the status color) stay readable. */
.divcal-root {
  --surface: #fcfcfb; --surface-2: #f4f4f1; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --line: #e1e0d9; --line-strong: #c3c2b7;
  --paid: rgba(12,163,12,0.14); --paid-dot: #0ca30c;
  --declared: rgba(250,178,25,0.22); --declared-dot: #d99a0e;
  --expected: #ec835a; --accent: #2a78d6; --accent-tint: rgba(42,120,214,0.08);
  --bar: #86b6ef; --hover: color-mix(in srgb, var(--surface) 95%, var(--ink));
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink);
}
.divcal-root.dark {
  --surface: #16181d; --surface-2: #1d2027; --ink: #f5f5f3; --ink-2: #c3c2b7;
  --muted: #898781; --line: #2a2d34; --line-strong: #3a3e47;
  --paid: rgba(12,163,12,0.20); --paid-dot: #0ca30c;
  --declared: rgba(250,178,25,0.18); --declared-dot: #fab219;
  --expected: #ec835a; --accent: #3987e5; --accent-tint: rgba(57,135,229,0.10);
  --bar: #256abf; --hover: color-mix(in srgb, var(--surface) 94%, var(--ink));
}
.divcal-legend { display: flex; flex-wrap: wrap; gap: 18px; margin: 0 0 10px; font-size: 12.5px; color: var(--ink-2); }
.divcal-legend span { display: inline-flex; align-items: center; gap: 7px; }
.divcal-legend i { width: 14px; height: 14px; border-radius: 4px; display: inline-block; }
.divcal-legend .sw-paid { background: var(--paid); box-shadow: inset 0 0 0 1px var(--paid-dot); }
.divcal-legend .sw-declared { background: var(--declared); box-shadow: inset 0 0 0 1px var(--declared-dot); }
.divcal-legend .sw-expected { outline: 1px dashed var(--expected); outline-offset: -1px; }
.divcal-legend .sw-current { background: var(--accent-tint); box-shadow: inset 2px 0 0 var(--accent), inset -2px 0 0 var(--accent); }

/* Scrollable viewport: past ~70% of the screen the table scrolls, with month
   header / Total footer / ticker column pinned. border-collapse:separate is
   required — collapsed borders detach from sticky cells and scroll away. */
.divcal-wrap { overflow: auto; max-height: 70vh; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
.divcal { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; color: var(--ink); font-variant-numeric: tabular-nums; }
.divcal th, .divcal td { border-bottom: 1px solid var(--line); padding: 9px 12px; text-align: right; white-space: nowrap; }
.divcal thead th { background: var(--surface-2); color: var(--muted); font-weight: 600; font-size: 11.5px;
  letter-spacing: 0.03em; text-transform: uppercase; text-align: right; position: sticky; top: 0; z-index: 2;
  border-bottom: 1px solid var(--line-strong); }
.divcal thead th:first-child, .divcal tbody th { text-align: left; }
.divcal tbody th { background: var(--surface); font-weight: 650; color: var(--ink); position: sticky; left: 0; z-index: 1; letter-spacing: 0.02em; }
.divcal thead th:first-child { left: 0; z-index: 3; }
/* Row hover: opaque (sticky cells must hide what scrolls under them) and
   never over a status tint. */
.divcal tbody tr:hover > th, .divcal tbody tr:hover > td:not(.paid):not(.declared):not(.current) { background-color: var(--hover); }
.divcal tbody tr:last-child th, .divcal tbody tr:last-child td { border-bottom: none; }

.divcal .current { background-color: var(--accent-tint); box-shadow: inset 1px 0 0 var(--accent), inset -1px 0 0 var(--accent); }
.divcal thead .current { color: var(--accent); background: var(--surface-2); box-shadow: inset 0 -2px 0 var(--accent); }

.divcal td.paid { background: var(--paid); }
.divcal td.declared { background: var(--declared); cursor: help; }
.divcal td.declared.current, .divcal td.paid.current { background-image: linear-gradient(var(--accent-tint), var(--accent-tint)); }
.divcal td.word { color: var(--expected); font-size: 11.5px; font-weight: 600; text-align: center; cursor: help;
  outline: 1px dashed var(--expected); outline-offset: -6px; border-radius: 8px; }
.divcal .nodata { color: var(--muted); font-style: italic; text-align: center; }

.divcal th.tickertotal, .divcal td.tickertotal { font-weight: 700; border-right: 1px solid var(--line-strong); }
.divcal tbody td.tickertotal { padding-bottom: 13px; background: linear-gradient(var(--bar), var(--bar)) no-repeat right 12px bottom 5px / calc((100% - 24px) * var(--pct, 0)) 3px; }

.divcal tfoot th, .divcal tfoot td { background: var(--surface-2); font-weight: 700; color: var(--ink);
  border-top: 1px solid var(--line-strong); border-bottom: none; position: sticky; bottom: 0; z-index: 2; }
.divcal tfoot td.tickertotal { background: var(--surface-2); }
.divcal tfoot th { left: 0; z-index: 3; text-align: left; }
.divcal tfoot .current { background-color: var(--surface-2); color: var(--accent); }
</style>
"""

_DIV_LEGEND = (
    '<div class="divcal-legend">'
    '<span><i class="sw-paid"></i>Paid</span>'
    '<span><i class="sw-declared"></i>Declared, not paid yet</span>'
    '<span><i class="sw-expected"></i>Expected (estimated from history)</span>'
    '<span><i class="sw-current"></i>Current month</span>'
    "</div>"
)


def _pct(total: str, top: float) -> str:
    """Bar length (0..1) for a formatted ticker total relative to the largest."""
    if not total or top <= 0:
        return ""
    return f"{float(total.replace(',', '')) / top:.3f}"


def _calendar_html(cal: dividends.Calendar, dark: bool) -> str:
    """Render the calendar as an HTML table: status tints, current-month
    highlight, native hover tooltips (title), a per-ticker Total column with a
    magnitude bar, and a Total footer row. ``dark`` follows the app theme."""
    cols = list(cal.values.columns)
    cur = cal.current_ym
    top = max(
        (float(v.replace(",", "")) for v in cal.ticker_totals if v), default=0.0
    )

    head = "".join(
        f'<th class="{"current" if c == cur else ""}">{dividends.month_label(c)}</th>'
        for c in cols
    )
    rows = []
    for sym in cal.values.index:
        tot = str(cal.ticker_totals[sym])
        pct = _pct(tot, top)
        style = f' style="--pct:{pct}"' if pct else ""
        total = f'<td class="tickertotal"{style}>{html.escape(tot)}</td>'
        if sym in cal.no_data:
            rows.append(
                f'<tr><th>{html.escape(str(sym))}</th>{total}'
                f'<td class="nodata" colspan="{len(cols)}">— no dividends —</td></tr>'
            )
            continue
        cells = []
        for c in cols:
            klass = _DIV_CLASSES.get(cal.status.at[sym, c], "")
            if c == cur:
                klass = f"{klass} current".strip()
            tip = cal.tooltips.at[sym, c]
            title = f' title="{html.escape(tip)}"' if tip else ""
            cells.append(f'<td class="{klass}"{title}>{html.escape(str(cal.values.at[sym, c]))}</td>')
        rows.append(f'<tr><th>{html.escape(str(sym))}</th>{total}{"".join(cells)}</tr>')
    foot = "".join(
        f'<td class="{"current" if c == cur else ""}">{html.escape(str(cal.totals[c]))}</td>'
        for c in cols
    )
    theme = "divcal-root dark" if dark else "divcal-root"
    return (
        f'{_DIV_CSS}<div class="{theme}">{_DIV_LEGEND}'
        f'<div class="divcal-wrap"><table class="divcal">'
        f'<thead><tr><th>Ticker</th><th class="tickertotal">Total</th>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        f'<tfoot><tr><th>Total</th>'
        f'<td class="tickertotal">{cal.paid_sum:,.2f}</td>{foot}</tr></tfoot>'
        f"</table></div></div>"
    )


# Exact IBKR navigation path for fixing a too-short Flex Query delivery period
# (root cause of an empty dividend history, confirmed in e3/B1).
_IBKR_FLEX_PATH = (
    "Account Management → Reporting → Flex Queries → edit query → "
    "Delivery Configuration → Period → 'Last 365 Calendar Days'"
)


def _fmt_cov_date(d) -> str:
    return d.strftime("%d-%m-%Y")


def _coverage_days(cov: dict) -> int | None:
    """Inclusive day span of the cash-transaction history, or None if undated."""
    if cov.get("min_date") is None or cov.get("max_date") is None:
        return None
    return (cov["max_date"] - cov["min_date"]).days + 1


def _render_coverage_notice(cov: dict, lookback_months: int | None) -> None:
    """Coverage warning banner + always-on data-coverage caption (F1).

    Warns when the statement's cash-transaction history is too short for the
    selected lookback: no dated rows at all, or a covered span under half the
    lookback window (``None`` = "All": no window, so only the no-data case
    warns). The half-window grace avoids false alarms for sparse
    payers (a quarterly payer's first/last payments rarely span the full
    window); the live failure mode this targets — Flex period =
    LastBusinessDay, a 1-day statement — sits far below any threshold.
    """
    days = _coverage_days(cov)
    threshold = lookback_months * 30 // 2 if lookback_months else 0
    if cov["rows"] == 0 or days is None:
        st.warning(
            "Flex statement contains no dated cash-transaction history — too "
            "short for the selected lookback. Extend the Flex Query period in "
            f"IBKR: {_IBKR_FLEX_PATH}, then click **Refresh data**."
        )
    elif days < threshold:
        st.warning(
            f"Flex statement covers only {days} day(s) of cash transactions "
            f"({_fmt_cov_date(cov['min_date'])} – {_fmt_cov_date(cov['max_date'])}) "
            "— too short for the selected lookback. Extend the Flex Query "
            f"period in IBKR: {_IBKR_FLEX_PATH}, then click **Refresh data**."
        )
    if cov["rows"] and days is not None:
        st.caption(
            f"Cash-transaction data: {cov['rows']} rows, "
            f"{_fmt_cov_date(cov['min_date'])} – {_fmt_cov_date(cov['max_date'])}"
        )
    elif cov["rows"]:
        st.caption(f"Cash-transaction data: {cov['rows']} rows, no dates")
    else:
        st.caption("Cash-transaction data: none")


def tab_dividends(data: dict) -> None:
    st.subheader("Dividend calendar")

    c1, c2, _ = st.columns([1, 1, 2])
    basis = c1.radio("Amounts", ["After tax", "Before tax"], horizontal=True, key="div_basis")
    lookback = c2.radio("Lookback", ["3M", "6M", "1Y", "All"], index=1, horizontal=True, key="div_lookback")
    net = basis == "After tax"
    # "All" = no window: everything since the first dividend on record.
    months = {"3M": 3, "6M": 6, "1Y": 12, "All": None}[lookback]
    st.caption(
        f"Amounts after tax (IBKR withholding per ticker; "
        f"{dividends.DEFAULT_TAX_RATE:.0%} where IBKR withheld nothing)."
        if net else "Amounts before withholding tax (gross)."
    )

    # F1: coverage banner (only when too short) + always-visible data caption.
    # Rendered before the empty-calendar early return so the caption (and the
    # likely cause of the emptiness) is visible even when there is no calendar.
    _render_coverage_notice(dividends.coverage(data["cash_tx"]), months)

    cal = dividends.build_calendar(
        data["positions"], data["cash_tx"], data["accruals"], net=net, lookback_months=months
    )

    # Backfill held tickers Flex can't fully cover from external sources:
    #  - Polygon: a REAL declared amount for any ticker with no Flex accrual —
    #    both no-data rows (COPX) and red-projected rows (paid history, next
    #    dividend declared but not yet in Flex). Fetched only for those.
    #  - yfinance: ex/pay dates as a red-only fallback for no-data rows.
    # When the whole calendar is empty every held ticker qualifies for both.
    positions = data["positions"]
    if cal.empty and not positions.empty and "symbol" in positions:
        all_held = sorted(str(s) for s in positions["symbol"].dropna().unique())
        no_data, cands = all_held, all_held
    else:
        no_data = sorted(cal.no_data)
        cands = sorted(cal.no_data | cal.projected)
    yf = load_external_dividends(tuple(no_data)) if no_data else {}
    poly = load_polygon_declared(tuple(cands), settings.polygon_api_key) if cands else {}
    external = {
        sym: {**yf.get(sym, {}), **poly.get(sym, {})} for sym in set(yf) | set(poly)
    }
    if any(
        e.get("amount") is not None or e.get("ex_date") or e.get("pay_date")
        or e.get("history_months")
        for e in external.values()
    ):
        cal = dividends.build_calendar(
            data["positions"], data["cash_tx"], data["accruals"],
            net=net, lookback_months=months, external=external,
        )

    if cal.empty:
        st.info(
            "No dividend data in the Flex statement. Enable **Cash Transactions** "
            "and **Open Dividend Accruals** in your Flex Query definition "
            "(IBKR Account Management → Reporting → Flex Web Service), then "
            "**Refresh data**."
        )
        return

    # Months elapsed in the window (first column .. current month) for the average.
    cols = list(cal.values.columns)
    elapsed = cols.index(cal.current_ym) + 1 if cal.current_ym in cols else len(cols)
    paying = sum(1 for v in cal.ticker_totals if v)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Paid, all time" if months is None
        else f"Paid, last {'1 year' if months == 12 else f'{months} months'}",
        f"{cal.paid_sum:,.2f}",
        border=True,
    )
    m2.metric("Monthly average", f"{cal.paid_sum / elapsed:,.2f}", border=True,
              help=f"Paid total over {elapsed} month(s), current month included.")
    m3.metric("Declared, not paid yet", f"{cal.declared_sum:,.2f}", border=True,
              help="Announced amounts still to be paid. Not included in any paid total.")
    m4.metric("Paying tickers", f"{paying} of {len(cal.values.index)}", border=True)

    st.html(_calendar_html(cal, dark=st.context.theme.type == "dark"))

    with st.expander("How to read this calendar"):
        st.markdown(
            "- **Rows** are held tickers, **columns** are months. **Total** "
            "(next to each ticker) sums what that ticker paid in the selected "
            "period; the bar under it compares tickers.\n"
            "- **Green** = paid. **Amber** = declared but not paid yet. "
            "**Dashed “Declare” / “Pay”** = the next declaration or payment, "
            "estimated from the ticker's history; the amount is unknown until "
            "declared.\n"
            "- Only paid money is summed. Declared and expected amounts never "
            "count toward any total.\n"
            "- Hover amber and dashed cells for dates; a “~” day is estimated, "
            "not confirmed. IBKR data always wins; gaps are filled from Polygon "
            "(declared amounts) and yfinance (dates only), and the tooltip shows "
            "the source.\n"
            "- “No dividends” rows are held tickers that pay none."
        )


def _clip_nav_table(nav_history: pd.DataFrame, range_label: str) -> pd.DataFrame:
    """The snapshot table, limited to the selected range."""
    start = capital.range_start(range_label)
    if start is None or nav_history.empty or "snapshot_date" not in nav_history:
        return nav_history
    dates = pd.to_datetime(nav_history["snapshot_date"], errors="coerce")
    return nav_history[dates.dt.date >= start]


def _nav_metrics(
    window: pd.DataFrame,
    currency: str | None,
    range_label: str,
    split_income: bool,
) -> None:
    """Headline split: what was paid in, what it is worth, what it earned."""
    last = window.iloc[-1]
    contributed, net_liq, earnings = last["contributed"], last["net_liq"], last["earnings"]

    extra = (range_label != capital.DEFAULT_RANGE) + 2 * bool(split_income)
    cols = st.columns(3 + extra)
    cols[0].metric("Contributed", _fmt_money(contributed, currency))
    cols[1].metric("Net liquidation", _fmt_money(net_liq, currency))
    # Return on contributed capital, not on NAV: the denominator is the money
    # actually put at risk.
    pct = f"{earnings / contributed * 100:+.1f}%" if contributed else None
    cols[2].metric("Earnings", _fmt_money(earnings, currency), delta=pct)
    slot = 3
    if split_income:
        income = last["dividends"]
        ex_div = earnings - income
        # Share of earnings, not of NAV: the question is how much of the gain
        # was income rather than price movement.
        share = f"{income / earnings * 100:.0f}% of earnings" if earnings else None
        cols[slot].metric("Dividends", _fmt_money(income, currency), delta=share,
                          delta_color="off")
        slot += 1
        cols[slot].metric("Earnings ex-dividends", _fmt_money(ex_div, currency))
        slot += 1
    if range_label != capital.DEFAULT_RANGE:
        # Over a window, all-time earnings say nothing about the window itself —
        # the change across it does.
        period = earnings - window.iloc[0]["earnings"]
        cols[slot].metric(f"Earned in {range_label}", _fmt_money(period, currency))


def tab_net_worth(cash_tx: pd.DataFrame | None = None) -> None:
    # Not named `history` — that would shadow the data.history module import.
    nav_history = snapshots.read_history(settings.db_path)
    controls = st.columns([1, 2])
    range_label = controls[0].selectbox(
        "Range", capital.RANGES,
        index=capital.RANGES.index(capital.DEFAULT_RANGE),
        key="nav_range",
        help="Clips the chart, table and headline figures to a trailing window.",
    )
    split_income = controls[1].checkbox(
        "Split out dividend income",
        value=False,
        key="nav_split_income",
        help=(
            "Carves the earnings wedge into dividends received and price "
            "movement. Dividends are part of earnings, not extra on top."
        ),
    )
    currency = None
    if not nav_history.empty and "currency" in nav_history:
        values = nav_history["currency"].dropna()
        currency = values.iloc[-1] if not values.empty else None

    comp = capital.composition(nav_history, cash_tx)
    if comp.empty:
        unpriced = capital.unpriced_deposits(cash_tx)
        if unpriced:
            # Deliberately not falling back to the raw amounts: those are in the
            # currency each deposit was made in, and summing them across
            # currencies is what made this split wrong in the first place.
            st.info(
                f"{unpriced} deposit(s) in the archive predate base-currency "
                "tracking, so contributed capital cannot be totalled yet. Click "
                "**Refresh data** to backfill them from the current Flex "
                "statement."
            )
        st.plotly_chart(
            figures.nav_line(_clip_nav_table(nav_history, range_label)), width="stretch"
        )
    else:
        window = capital.clip(comp, range_label)
        if window.empty:
            st.info(f"No net-worth history inside the {range_label} window.")
        else:
            _nav_metrics(window, currency, range_label, split_income)
            st.plotly_chart(
                figures.nav_composition_area(window, currency, split_income),
                width="stretch",
            )
            base = currency or "the base currency"
            st.caption(
                "The blue band is money you paid in; the wedge above it is what "
                "the account earned on top — trading gains, dividends and "
                f"interest, net of fees. Amounts are converted to {base} before "
                "summing."
                + (
                    " The gold band is dividend income (net of withholding), "
                    "carved out of that wedge — it is already inside net "
                    "liquidation, not added to it."
                    if split_income else ""
                )
            )

    table = _clip_nav_table(nav_history, range_label)
    if not table.empty:
        st.dataframe(table, width="stretch", hide_index=True)
    cover = history.coverage(settings.db_path)
    st.caption(
        "One snapshot per data refresh (deduped by date), backfilled from the "
        "statement's daily equity summary for days the dashboard was not opened. "
        f"Local archive also holds {cover['trades']:,} trades "
        f"(from {cover['first_trade'] or '—'}) and {cover['cash_tx']:,} cash "
        f"transactions (from {cover['first_cash_tx'] or '—'}) — these outlive "
        "the 365-day Flex window."
    )


# ---- token expiry ---------------------------------------------------------

# Flex tokens are issued for at most one year and cannot be renewed — IBKR only
# lets you generate a new one, which replaces the old.
_IBKR_TOKEN_PATH = "Account Management → Reporting → Flex Web Service"


def render_token_expired(code: str) -> None:
    """Actionable guidance for IBKR Flex error 1012 (token expired) / 1015
    (token invalid). Both mean the credential in ``.env`` is dead and a new one
    must be generated; nothing in the app can repair it."""
    if code == flex.TOKEN_EXPIRED:
        headline = "Your IBKR Flex token has expired (IBKR Flex error 1012)."
        why = (
            "IBKR issues Flex Web Service tokens for a fixed term (at most one "
            "year) and does not renew them. Nothing is wrong with the app or "
            "your data — the dashboard just can't fetch new statements until "
            "you replace the token."
        )
    else:
        headline = "IBKR rejected your Flex token (IBKR Flex error 1015: token is invalid)."
        why = (
            "The `FLEX_TOKEN` in `.env` is not a token IBKR recognises — it "
            "was probably regenerated, deactivated, or pasted with a typo. "
            "Nothing is wrong with the app or your data — the dashboard just "
            "can't fetch new statements until the token is fixed."
        )
    st.error(f"**{headline}**\n\n{why}")
    st.info(
        "**How to fix (about 2 minutes):**\n\n"
        f"1. Log in to IBKR and open **{_IBKR_TOKEN_PATH}**.\n"
        "2. Click **Generate a new token**. Set the longest expiry IBKR offers "
        "(1 year) — the old token stops working immediately.\n"
        "3. Copy the new token into `.env` as `FLEX_TOKEN=...` "
        "(keep `FLEX_QUERY_ID` as is — the query is unaffected).\n"
        "4. Restart the app or click **Refresh data** in the sidebar.\n\n"
        "Your NAV history and trade archive are stored locally and are not "
        "affected — the **Net Worth** tab below still works from the last "
        "snapshot. Tip: put the new expiry date in your calendar a week early."
    )


# ---- main -----------------------------------------------------------------

def main() -> None:
    render_sidebar()
    st.title("📈 IBKR Dashboard")

    if not settings.is_configured:
        st.warning(
            "Flex Web Service credentials not set.\n\n"
            "1. Copy `.env.example` to `.env`.\n"
            "2. Set `FLEX_TOKEN` and `FLEX_QUERY_ID` "
            "(IBKR Account Management → Reporting → Flex Web Service).\n"
            "3. Click **Refresh data**."
        )
        # Net Worth still works from any prior snapshots.
        tab_net_worth()
        return

    try:
        data = load_portfolio(settings.flex_token, settings.flex_query_id, str(settings.db_path))
    except flex.FlexError as exc:
        if exc.code in flex.TOKEN_ERROR_CODES:
            render_token_expired(exc.code)
        else:
            msg = str(exc)
            # ibflex embeds the token in request URLs, and the raised FlexError
            # text is intentionally unscrubbed — never show the token on screen.
            if settings.flex_token:
                msg = msg.replace(settings.flex_token, "***")
            st.error(msg)
            st.info("Fix the token/query in `.env`, then click **Refresh data**.")
        # Net Worth still works from prior snapshots — keep the history reachable
        # while the credential is being fixed.
        tab_net_worth()
        return

    overview, positions, trades, price, divs, net_worth = st.tabs(
        ["Overview", "Positions", "Trades", "Prices", "Dividends", "Net Worth"]
    )
    with overview:
        tab_overview(data)
    with positions:
        tab_positions(data)
    with trades:
        tab_trades(data)
    with price:
        tab_prices(data)
    with divs:
        tab_dividends(data)
    with net_worth:
        tab_net_worth(data.get("cash_tx"))


if __name__ == "__main__":
    main()
