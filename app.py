"""IBKR Dashboard — Streamlit entry point.

Orchestrates: pull (Flex Web Service) -> parse -> DataFrames -> Plotly figures.
Heavy work is cached so Streamlit reruns (every widget interaction) don't re-hit
IBKR; the sidebar Refresh button clears the cache to force a fresh pull.
"""
from __future__ import annotations

import calendar
import datetime as dt
import html
import logging
import uuid

import pandas as pd
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

import logsetup
from charts import figures, perf_calendar
from config import ENV_PATH, is_real_value, load_settings, save_env_values, validate_credentials
from data import (
    backup, capital, div_external, div_provider, dividends, history, performance,
    price_stats, prices, snapshots, sync, watchlist,
)
from ibkr import flex, parser

st.set_page_config(page_title="IBKR Dashboard", page_icon="📈", layout="wide")

# Wire the myitbr pipeline loggers (stderr + rotating logs/app.log). Idempotent,
# so Streamlit reruns of this module never duplicate handlers or log lines.
logsetup.setup_logging()
logger = logging.getLogger("myitbr.app")

settings = load_settings()


# ---- data loading (cached) ------------------------------------------------

# One cache entry per browser session: a new session (app start, page reload)
# pulls again, while widget reruns inside a session reuse the pull. Bounded so
# old sessions' entries do not pile up in memory.
_SESSION_KEY = "load_session"
# The last IBKR failure of this session. While it is set the app does not ask
# IBKR again; only "Refresh data" or a newly saved token clears it.
_FLEX_FAILED = "flex_failed"
_MAX_CACHED_SESSIONS = 4


def session_key() -> str:
    if _SESSION_KEY not in st.session_state:
        st.session_state[_SESSION_KEY] = uuid.uuid4().hex
    return st.session_state[_SESSION_KEY]


def _pull(*, token: str, query_id: str) -> tuple[object, tuple[dt.date, dt.date] | None]:
    """Pull everything IBKR still serves: the last MAX_WINDOW_DAYS days.

    First the query's saved period. If that comes back shorter than the maximum
    (the query was set to a shorter period), pull again with an explicit range
    ending on the returned toDate — a date IBKR has data for. Ending on today or
    a weekend instead gets error 1003.
    """
    xml = flex.download_statement(token=token, query_id=query_id, date_range=None)
    stmt = parser.parse_statement(xml)
    if stmt is None:
        raise flex.FlexError("Flex statement contained no data. Check the query definition.")

    from_date, to_date = getattr(stmt, "fromDate", None), getattr(stmt, "toDate", None)
    if not (isinstance(from_date, dt.date) and isinstance(to_date, dt.date)):
        return stmt, None
    if (to_date - from_date).days + 1 >= flex.MAX_WINDOW_DAYS:
        return stmt, None

    requested = (to_date - dt.timedelta(days=flex.MAX_WINDOW_DAYS - 1), to_date)
    logger.info(
        "statement covers %s..%s only; pulling the full %d days",
        from_date, to_date, flex.MAX_WINDOW_DAYS,
    )
    try:
        wide = parser.parse_statement(
            flex.download_statement(token=token, query_id=query_id, date_range=requested)
        )
    except flex.FlexError as exc:
        # The short statement is still good data; keep it rather than fail the load.
        logger.warning("full-window pull failed, keeping the query period: %s", exc)
        return stmt, None
    return (wide, requested) if wide is not None else (stmt, None)


@st.cache_data(show_spinner="Pulling from IBKR Flex…", max_entries=_MAX_CACHED_SESSIONS)
def load_portfolio(token: str, query_id: str, db_path_str: str, session: str) -> dict:
    """Download + parse the full Flex window and sync it into the local store.

    ``session`` only keys the cache: each browser session pulls once, and
    Refresh clears the cache to pull again. Returns plain DataFrames/dicts
    (serializable for caching).
    """
    stmt, requested = _pull(token=token, query_id=query_id)

    nav = parser.latest_nav(stmt)
    equity = parser.equity_summary_df(stmt)
    trades = parser.trades_df(stmt)
    cash_tx = parser.cash_transactions_df(stmt)
    symbols = parser.symbol_daily_df(stmt)
    window = sync.make_window(
        requested=requested,
        returned_from=getattr(stmt, "fromDate", None),
        returned_to=getattr(stmt, "toDate", None),
    )
    # The Flex window rolls forward, so trades and cash transactions age out of
    # the statement and cannot be re-fetched afterwards. Sync adds only what the
    # store is missing and serves the union, so history keeps growing past the
    # window. A store failure must never cost the user the live statement, so the
    # in-window frames stay the fallback.
    trades, cash_tx, report = _sync_and_read(
        db_path_str=db_path_str, trades=trades, cash_tx=cash_tx,
        equity=equity, symbols=symbols, nav=nav, window=window,
    )

    return {
        "account": parser.account_info(stmt),
        "positions": parser.positions_df(stmt),
        "trades": trades,
        "cash": parser.cash_df(stmt),
        "equity": equity,
        "symbols": symbols,
        "cash_tx": cash_tx,
        "accruals": parser.dividend_accruals_df(stmt),
        "nav": nav,
        "sync": report,
    }


def _sync_and_read(
    *,
    db_path_str: str,
    trades: pd.DataFrame,
    cash_tx: pd.DataFrame,
    equity: pd.DataFrame,
    symbols: pd.DataFrame,
    nav: dict,
    window: dict,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Find what the store is missing, write only that, read the full history back.

    Degrades to the statement's own frames on any store error — a broken archive
    should cost the user history, never the dashboard.
    """
    now = dt.datetime.now()
    empty = {"tables": {}, "window": window, "written": 0, "error": None}
    try:
        report = sync.plan(
            db_path=db_path_str, trades=trades, cash_tx=cash_tx, equity=equity,
            symbols=symbols, nav=nav, window=window, today=now.date().isoformat(),
            now=now.isoformat(timespec="seconds"),
        )
    except Exception as exc:  # noqa: BLE001 — the check is best-effort by design
        logger.exception("sync plan failed; falling back to the statement window only")
        return trades, cash_tx, {**empty, "error": f"check failed: {exc}"}

    if sync.pending(report=report):
        # The store holds history IBKR can no longer re-serve, so back it up
        # before writing. backup.run swallows its own failures.
        backup.run(db_path_str, settings.backup_dir, keep=settings.backup_keep,
                   today=_backup_day())
        try:
            report["written"] = sync.apply(
                db_path=db_path_str, report=report, today=now.date().isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — rolled back; keep the dashboard
            logger.exception("sync write failed; nothing was written")
            report["error"] = f"write failed: {exc}"
    sync.log_report(report=report)

    try:
        archived_trades = history.read_trades(db_path_str)
        archived_cash = history.read_cash_tx(db_path_str)
    except Exception:  # noqa: BLE001
        logger.exception("history read failed; falling back to the statement window only")
        return trades, cash_tx, report
    # An empty archive read (fresh DB that failed to write) must not blank a tab.
    return (
        archived_trades if not archived_trades.empty else trades,
        archived_cash if not archived_cash.empty else cash_tx,
        report,
    )


@st.cache_data(show_spinner=False)
def load_prices(symbol: str, period: str) -> pd.DataFrame:
    return prices.fetch_ohlc(symbol, period=period)


@st.cache_data(show_spinner=False)
def load_expense_ratio(symbol: str) -> float | None:
    return prices.fetch_expense_ratio(symbol=symbol)


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

def render_sidebar() -> DeltaGenerator:
    """Render the sidebar; return the empty status slot main() fills once the
    load outcome (connected / missing / expired / invalid) is known."""
    st.sidebar.header("IBKR Dashboard")
    status_slot = st.sidebar.empty()
    if st.sidebar.button("🔄 Refresh data", width="stretch"):
        # Clear the cache and let THIS run continue — the button click itself
        # already triggered a rerun, and load_portfolio (called after this in
        # main()) cache-misses and refetches in the same run. Do NOT add
        # st.rerun() here: it aborts the run before the keyed Dividends radios
        # (div_basis / div_lookback) render, and Streamlit drops session state
        # for keyed widgets not rendered in a run — so Refresh would silently
        # reset the user's selections to defaults (e4 FE-1 regression).
        st.cache_data.clear()
        # The one click (besides a newly saved token) that asks IBKR again after a failure.
        if _FLEX_FAILED in st.session_state:
            del st.session_state[_FLEX_FAILED]
    db_state = "ready" if settings.db_path.exists() else "empty (created on first refresh)"
    st.sidebar.caption(f"Database: {db_state}", help=str(settings.db_path))

    # The DB is the only copy of history older than the Flex window, so surface
    # the backup state rather than leaving it to be discovered when it's too late.
    state = backup.status(settings.backup_dir)
    if state["count"]:
        st.sidebar.caption(
            f"Backups: {state['count']} (latest {state['latest_date']})",
            help=str(settings.backup_dir),
        )
    else:
        st.sidebar.caption("Backups: none yet", help=str(settings.backup_dir))
    return status_slot


# ---- tabs -----------------------------------------------------------------

def _fmt_money(v, ccy: str | None) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:,.2f} {ccy or ''}".strip()


def tab_overview(data: dict, *, watch_state: dict) -> None:
    _render_watchlist_alerts(state=watch_state, tab_key=_TABS_MAIN)
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


_PX_SYMBOL_KEY = "px_symbol"


def _closes_since_first_trade(
    *, symbol: str, trades: pd.DataFrame, frames: list[pd.DataFrame],
) -> pd.DataFrame | None:
    """Daily closes reaching back to my first trade of ``symbol``.

    Reuses one of the already-loaded ``frames`` when it goes back far enough, so
    the full-history pull happens only for a holding older than the chart.
    """
    first = price_stats.first_trade_date(trades=trades, symbol=symbol)
    if first is None:
        return None
    for frame in frames:
        if not frame.empty and "date" in frame:
            if pd.to_datetime(frame["date"]).min().date() <= first:
                return frame
    return load_prices(symbol, "max")


def _step_price_symbol(symbols: list[str], step: int) -> None:
    """Move the Prices tab's symbol ``step`` places along ``symbols``, stopping at the ends."""
    index = symbols.index(st.session_state[_PX_SYMBOL_KEY]) + step
    st.session_state[_PX_SYMBOL_KEY] = symbols[max(0, min(index, len(symbols) - 1))]


def tab_prices(data: dict) -> None:
    positions = data["positions"]
    symbols = sorted(positions["symbol"].dropna().unique()) if not positions.empty else []
    if not symbols:
        st.info("No symbols available (no open positions). Prices use yfinance.")
        return
    # A symbol kept from an earlier run can be gone after a refresh (position closed).
    if _PX_SYMBOL_KEY not in st.session_state or st.session_state[_PX_SYMBOL_KEY] not in symbols:
        st.session_state[_PX_SYMBOL_KEY] = symbols[0]
    c1, up, down, c2 = st.columns([10, 0.6, 0.6, 5], vertical_alignment="bottom")
    symbol = c1.selectbox("Symbol", symbols, key=_PX_SYMBOL_KEY)
    if len(symbols) > 1:
        up.button(
            "▲", key="px_symbol_prev", help="Previous symbol", disabled=symbol == symbols[0],
            on_click=_step_price_symbol, kwargs={"symbols": symbols, "step": -1}, width="stretch",
        )
        down.button(
            "▼", key="px_symbol_next", help="Next symbol", disabled=symbol == symbols[-1],
            on_click=_step_price_symbol, kwargs={"symbols": symbols, "step": 1}, width="stretch",
        )
    period = c2.selectbox("Period", _PRICE_PERIODS, index=3)
    _render_price_detail(
        symbol=symbol, period=period, positions=positions, cash_tx=data["cash_tx"],
        trades=data["trades"], key_prefix="px", layout="prices", quote=None,
        buy_target=None, sell_target=None, show_position=True,
    )


_PRICE_PERIODS = ["1mo", "3mo", "6mo", "1y", "2y", "5y"]

# Tile rows of the price detail, per layout. The Watchlist puts the targets
# first and drops the position tiles for a stock that is not held.
_DETAIL_ROWS = {
    ("prices", True): [
        ["current", "avg_cost", "unrealized", "position"],
        ["mean", "high_low", "range_52w", "period_return"],
        ["volume", "volatility", "atr", "total_return"],
        ["div_net", "div_gross", "last_div", "yield", "expense"],
    ],
    ("watchlist", True): [
        ["current", "buy_target", "sell_target", "position"],
        ["avg_cost", "unrealized", "total_return", "period_return"],
        ["mean", "high_low", "range_52w", "volume"],
        ["volatility", "atr", "expense", "yield"],
        ["div_net", "div_gross", "last_div"],
    ],
    ("watchlist", False): [
        ["current", "buy_target", "sell_target", "period_return"],
        ["mean", "high_low", "range_52w", "volume"],
        ["volatility", "atr", "expense"],
    ],
}


def _fmt_price(v) -> str:
    """A price or target: 2 decimals from 1 up, 4 significant digits below (FX, small coins)."""
    if v is None or pd.isna(v):
        return "—"
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4g}"


def _target_caption(*, price: float | None, target: float | None, side: str) -> str | None:
    left = watchlist.distance_left(price=price, target=target, side=side)
    if left is None:
        return None
    if left == 0:
        return "reached"
    return f"price {left:.2f}% {'above' if side == 'buy' else 'below'} target"


def _render_price_detail(
    *, symbol: str, period: str, positions: pd.DataFrame, cash_tx: pd.DataFrame,
    trades: pd.DataFrame, key_prefix: str, layout: str, quote: dict | None,
    buy_target: float | None, sell_target: float | None, show_position: bool,
) -> None:
    """KPI tiles, overlay toggles and candlestick chart for one symbol.

    Shared by the Prices tab (``layout="prices"``, last close) and the Watchlist
    tab (``layout="watchlist"``, delayed intraday ``quote`` and targets). Both
    render in the same run, so every widget and the chart are keyed by
    ``key_prefix``.
    """
    watch = layout == "watchlist"
    ohlc = load_prices(symbol, period)
    # The 52-week range must not move when the period selector narrows, so it is
    # computed off a separate 1y pull (cached, and free when period is already 1y).
    year_ohlc = ohlc if period == "1y" else load_prices(symbol, "1y")
    stats = price_stats.summarize(
        ohlc, symbol, positions=positions, cash_tx=cash_tx, year_ohlc=year_ohlc,
        live_price=quote["price"] if quote else None,
        live_previous_close=quote["previous_close"] if quote else None,
    )
    ccy = stats.get("currency") or (quote["currency"] if quote else None)
    div = stats["dividends"]
    price = quote["price"] if quote else None

    expense_ratio = load_expense_ratio(symbol)
    expense_cost = price_stats.expense_cost_per_year(
        expense_ratio_pct=expense_ratio, market_value=stats["market_value_live"],
    )
    expense_paid = price_stats.expense_paid(
        closes=_closes_since_first_trade(
            symbol=symbol, trades=trades, frames=[ohlc, year_ohlc],
        ) if expense_ratio is not None and show_position else None,
        trades=trades, symbol=symbol, quantity=stats["quantity"],
        expense_ratio_pct=expense_ratio, today=dt.date.today(),
    )

    def current(col: DeltaGenerator) -> None:
        if watch:
            # No quote: the last close is not the "current" price on this tab.
            col.metric("Current price (delayed)",
                       _fmt_money(price, ccy) if price is not None else "—",
                       _fmt_delta(stats["day_change"], stats["day_change_pct"])
                       if price is not None else None)
        else:
            col.metric("Current price", _fmt_money(stats["current"], ccy),
                       _fmt_delta(stats["day_change"], stats["day_change_pct"]))

    def target(col: DeltaGenerator, label: str, value: float | None, side: str) -> None:
        col.metric(label, _fmt_price(value),
                   _target_caption(price=price, target=value, side=side),
                   delta_color="off", delta_arrow="off")

    tiles = {
        "current": current,
        "buy_target": lambda col: target(col, "Buy below", buy_target, "buy"),
        "sell_target": lambda col: target(col, "Sell above", sell_target, "sell"),
        "avg_cost": lambda col: col.metric(
            "My avg cost", _fmt_money(stats["avg_cost"], ccy),
            f"basis {_fmt_money(stats['cost_basis'], ccy)}"
            if stats["cost_basis"] is not None else None, delta_color="off"),
        "unrealized": lambda col: col.metric(
            "Unrealized P&L", _fmt_money(stats["unrealized_live"], ccy),
            _fmt_delta(None, stats["vs_cost_pct"])),
        "position": lambda col: col.metric(
            "Position", _fmt_qty(stats["quantity"]),
            f"{_fmt_money(stats['market_value_live'], ccy)} mkt value"
            if stats["market_value_live"] is not None else None, delta_color="off"),
        "mean": lambda col: col.metric(
            f"Period mean ({period})", _fmt_money(stats["period_mean"], ccy)),
        "high_low": lambda col: col.metric(
            f"Period high / low ({period})",
            f"{_fmt_num(stats['period_high'])} / {_fmt_num(stats['period_low'])}",
            _fmt_delta(None, stats["off_period_high_pct"])),
        "range_52w": lambda col: col.metric(
            "52-week high / low",
            f"{_fmt_num(stats['high_52w'])} / {_fmt_num(stats['low_52w'])}"),
        "period_return": lambda col: col.metric(
            f"Period return ({period})", _fmt_num(stats["period_return_pct"], "%")),
        "volume": lambda col: col.metric("Avg volume", _fmt_num(stats["avg_volume"], digits=0)),
        "volatility": lambda col: col.metric(
            "Volatility (ann.)", _fmt_num(stats["volatility_pct"], "%")),
        "atr": lambda col: col.metric("ATR(14)", _fmt_money(stats["atr"], ccy)),
        "total_return": lambda col: col.metric(
            "Total return incl. divs", _fmt_money(stats["total_return"], ccy),
            _fmt_delta(None, stats["total_return_pct"])),
        "div_net": lambda col: col.metric(
            "Dividends received (net)", _fmt_money(div["net"], ccy),
            f"{div['count']} payments" if div["count"] else None, delta_color="off"),
        "div_gross": lambda col: col.metric(
            "Dividends received (gross)", _fmt_money(div["gross"], ccy)),
        "last_div": lambda col: col.metric(
            "Last dividend", _fmt_money(div["last_amount"], ccy),
            str(div["last_date"]) if div["last_date"] else None, delta_color="off"),
        "yield": lambda col: col.metric(
            "Yield (TTM) / on cost",
            f"{_fmt_num(stats['yield_pct'], '%')} / {_fmt_num(stats['yield_on_cost_pct'], '%')}"),
        "expense": lambda col: col.metric(
            "Expense Ratio", _fmt_num(expense_ratio, "%"),
            f"≈ {_fmt_money(expense_paid['amount'], ccy)} paid since {expense_paid['since']}"
            if expense_paid["amount"] is not None else None,
            delta_color="off", delta_arrow="off",
            help="Yearly fee the fund takes from its own assets (yfinance net expense "
                 "ratio). It is already inside the price and is never charged to the "
                 "account, so the paid amount is an estimate: for each day held, "
                 "shares × close × ratio ÷ 365, at today's ratio. "
                 + (f"At today's value it is about {_fmt_money(expense_cost, ccy)} a year. "
                    if expense_cost is not None else "")
                 + "ETFs and funds only; a stock has none."),
    }
    for names in _DETAIL_ROWS[(layout, show_position)]:
        for col, name in zip(st.columns(max(4, len(names))), names):
            tiles[name](col)

    # ---- overlay toggles --------------------------------------------------
    has_targets = buy_target is not None or sell_target is not None
    if watch:
        t0, t1, t2, t3, t4 = st.columns(5)
        # Disabled, not hidden: a keyed widget that is not rendered loses its state.
        show_targets = t0.checkbox("Target lines", value=True, key=f"{key_prefix}_targets",
                                   disabled=not has_targets) and has_targets
    else:
        t1, t2, t3, t4 = st.columns(4)
        show_targets = False
    show_cost = t1.checkbox("Avg cost line", value=True, key=f"{key_prefix}_cost",
                            disabled=not show_position) and show_position
    show_ma = t2.checkbox("MA 20/50", value=True, key=f"{key_prefix}_ma")
    show_mean = t3.checkbox("Period mean", value=False, key=f"{key_prefix}_mean")
    show_trades = t4.checkbox("My trades" if watch else "Buy/Sell markers", value=True,
                              key=f"{key_prefix}_trades",
                              disabled=not show_position) and show_position

    st.plotly_chart(
        figures.candlestick(
            ohlc, symbol,
            avg_cost=stats["avg_cost"] if show_cost else None,
            ma_windows=(20, 50) if show_ma else (),
            mean_close=stats["period_mean"] if show_mean else None,
            trades=(trades[trades["symbol"].astype(str) == str(symbol)]
                    if show_trades and not trades.empty else None),
            buy_target=buy_target if show_targets else None,
            sell_target=sell_target if show_targets else None,
        ),
        width="stretch", key=f"{key_prefix}_chart",
    )
    if watch:
        st.caption(
            '"Current price (delayed)" on this tab is the yfinance quote, which may be '
            "delayed (about 15 minutes on most exchanges), so P&L here can differ from the "
            "Prices tab, which uses the last close. The chart is daily candles."
        )
        return
    as_of = f" ({stats['as_of']})" if stats["as_of"] else ""
    st.caption(
        "Price, ranges and volatility: yfinance (Flex provides no market data); "
        f"current price is the last close{as_of}. "
        "Cost, position and dividends come from the Flex statement — dividend totals "
        "cover the statement's date range only. Yield (TTM) = trailing-12-month "
        "dividends ÷ current price; on cost uses my average cost. "
        "Expense ratio: yfinance; the paid amount is an estimate from my trades and "
        "daily closes — no statement carries it."
    )


# ---- watchlist --------------------------------------------------------------

_WL_LIST = "wl_list"
_WL_SYMBOL = "wl_symbol"
# Column widths of the stock buttons' row: Add stock, Edit / move / remove, the rest.
_WL_STOCK_ROW = [1.2, 2.4, 5]
_WL_DIALOG = "wl_dialog"
# The main tabs track their state, so the app knows the open tab. A button opens a tab
# (the alert box's link, the bell) by giving the tabs a new key and that tab as the default.
# Writing the tab to the tabs' own key does not hold: the browser keeps the last tab the
# user clicked and sends it back on the next click anywhere, which opened Overview again.
_TABS_MAIN = "tabs_main"
_TAB_JUMP = "tab_jump"
_FIRST_TAB = "Overview"


def _tab_jump() -> dict:
    """The last tab a button opened: ``{"nonce": int, "label": str}``."""
    return st.session_state[_TAB_JUMP] if _TAB_JUMP in st.session_state else {"nonce": 0, "label": _FIRST_TAB}


def _tabs_key() -> str:
    return f"{_TABS_MAIN}_{_tab_jump()['nonce']}"


def _current_tab() -> str:
    """The open main tab: the user's last click, else the tab a button opened."""
    key = _tabs_key()
    return st.session_state[key] if key in st.session_state else _tab_jump()["label"]


def _jump_to_tab(*, label: str) -> None:
    st.session_state[_TAB_JUMP] = {"nonce": _tab_jump()["nonce"] + 1, "label": label}
_WL_LIST_MENU = "wl_list_menu"
_QUOTE_WORKERS = 6

_WL_READ_ERROR = "The watchlist could not be read from the database."
_WL_SETUP_ERROR = "The watchlist could not be set up in the database."
_ALERT_COLORS = {watchlist.ALERT_TEXT["buy"]: "background-color: rgba(26, 127, 55, 0.25)",
                 watchlist.ALERT_TEXT["sell"]: "background-color: rgba(178, 24, 43, 0.25)"}
_SCHEDULE_MODES = {watchlist.SCHEDULE_ONCE: "Only when I open the app",
                   watchlist.SCHEDULE_EVERY: "Every N minutes"}
_SCHEDULE_DURATIONS = {watchlist.DURATION_HOURS: "For the hours below",
                       watchlist.DURATION_ALWAYS: "No end time"}


@st.cache_data(show_spinner=False, max_entries=8)
def load_quotes(symbols: tuple[str, ...], bucket: str) -> dict:
    """Delayed intraday quotes for every watched symbol.

    ``bucket`` is only a cache key (see ``watchlist.quote_bucket``): one fetch per
    browser session, or one per schedule window.
    """
    return {"quotes": prices.fetch_quotes(symbols=symbols, workers=_QUOTE_WORKERS),
            "fetched": watchlist.utc_now()}


def _backup_day() -> str:
    """Today's local date, from the watchlist clock, as the backup file's day."""
    return watchlist.utc_now().astimezone(watchlist.local_tz()).date().isoformat()


def _watchlist_state() -> dict:
    """Lists, stocks, schedule and quotes for this run. Built once, in main()."""
    now = watchlist.utc_now()
    if "wl_nonce" not in st.session_state:
        st.session_state["wl_nonce"] = 0
    state = {"now": now, "error": None, "quotes": {}, "fetched": None, "mutes": {}}
    try:
        watchlist.init_db(db_path=settings.db_path)
    except Exception:  # noqa: BLE001 — a broken watchlist must not cost the other tabs
        logger.exception("watchlist init failed")
        state["error"] = _WL_SETUP_ERROR
    lists = watchlist.read_lists(db_path=settings.db_path)
    items = watchlist.read_items(db_path=settings.db_path)
    if state["error"] is None and (lists["error"] or items["error"]):
        state["error"] = _WL_READ_ERROR
    state["lists"], state["items"] = lists["lists"], items["items"]
    state["setting"] = watchlist.read_refresh(db_path=settings.db_path)
    state["mutes"] = watchlist.read_mutes(db_path=settings.db_path, now=now)
    state["bucket"] = watchlist.quote_bucket(
        setting=state["setting"], now=now, session=session_key(),
        nonce=st.session_state["wl_nonce"],
    )
    symbols = tuple(sorted(state["items"]["symbol"]))
    if symbols and state["error"] is None:
        if "wl_fetched_once" in st.session_state:
            loaded = load_quotes(symbols=symbols, bucket=state["bucket"])
        else:
            with st.spinner("Fetching watchlist prices…"):
                loaded = load_quotes(symbols=symbols, bucket=state["bucket"])
            st.session_state["wl_fetched_once"] = True
        state["quotes"], state["fetched"] = loaded["quotes"], loaded["fetched"]
    return state


def _watchlist_tick() -> None:
    """Timer fragment: redraw the whole page once per schedule window.

    Its body also runs inside every full run; there it only arms itself, because
    a rerun requested during a full run would reset widgets not yet rendered.
    """
    if not st.session_state["wl_tick_armed"]:
        st.session_state["wl_tick_armed"] = True
        return
    if watchlist.tick_should_rerun(
        setting=watchlist.read_refresh(db_path=settings.db_path), now=watchlist.utc_now(),
        session=session_key(), nonce=st.session_state["wl_nonce"],
        bucket_seen=st.session_state["wl_bucket_seen"],
        dialog_open=watchlist.dialog_open(keys={str(key) for key in st.session_state}),
    ):
        st.rerun()


def _fetched_text(*, state: dict) -> str:
    return watchlist.fetched_label(fetched=state["fetched"], now=state["now"], tz=watchlist.local_tz())


# The alert box must be seen without being looked for: an amber fill and a thick
# left bar, in tints that read on the light and on the dark theme.
_WL_ALERT_CSS = """
<style>
.st-key-wl_alert_box {
  background: rgba(250, 178, 25, 0.16);
  border: 1px solid #d99a0e !important;
  border-left: 8px solid #d99a0e !important;
  border-radius: 0.5rem;
}
.st-key-wl_alert_box p { font-size: 1.1rem; }
</style>
"""


def _alert_lines(*, state: dict) -> dict:
    """The reached targets of this run, split into shown and hidden ones."""
    if state["error"] is not None or state["items"].empty:
        return {"shown": [], "hidden": []}
    found = watchlist.alerts(items=state["items"], quotes=state["quotes"])
    return watchlist.visible_alerts(lines=found["lines"], mutes=state["mutes"])


def _open_alert_stock(*, symbol: str, list_id: int, tab_key: str | None) -> None:
    """Button callback: open the Watchlist tab on the alert's list and stock."""
    st.session_state[_WL_LIST] = list_id
    st.session_state[_WL_SYMBOL] = symbol
    if tab_key is not None:
        _jump_to_tab(label="Watchlist")


def _mute_alert(*, symbol: str, side: str, kind: str) -> None:
    """Button callback: hide one alert for an hour or until tomorrow."""
    now, tz = watchlist.utc_now(), watchlist.local_tz()
    until = watchlist.mute_until(kind=kind, now=now, tz=tz)
    error = _save_watchlist(action=lambda: watchlist.write_mute(
        db_path=settings.db_path, symbol=symbol, side=side, until=until, now=now))
    if error is not None:
        st.session_state["wl_toast"] = error
    elif kind == watchlist.MUTE_TODAY:
        st.session_state["wl_toast"] = f"{symbol} alert hidden until tomorrow."
    else:
        st.session_state["wl_toast"] = (
            f"{symbol} alert hidden until {watchlist.until_label(until=until, now=now, tz=tz)}.")


def _show_hidden_alerts() -> None:
    error = _save_watchlist(action=lambda: watchlist.clear_mutes(db_path=settings.db_path))
    if error is not None:
        st.session_state["wl_toast"] = error


def _render_hidden_alerts(*, hidden: list[dict], state: dict) -> None:
    """One quiet line for the alerts the user hid, with the way to get them back."""
    if not hidden:
        return
    tz = watchlist.local_tz()
    parts = [f"{line['symbol']} until {watchlist.until_label(until=line['until'], now=state['now'], tz=tz)}"
             for line in hidden]
    row = st.container(horizontal=True, vertical_alignment="center")
    row.caption(f"Hidden: {', '.join(parts)}.")
    row.button("Show hidden alerts", key="wl_alert_show_hidden", type="tertiary",
               icon=":material/visibility:", on_click=_show_hidden_alerts)


def _render_watchlist_alerts(*, state: dict, tab_key: str | None) -> None:
    """The box at the top of Overview: every stock at its buy or sell target.

    ``tab_key`` is the key of the tabs that hold a Watchlist tab to open, or None
    when the Watchlist is already the first tab.
    """
    # The slot always exists, so the box coming and going cannot shift the tabs' identity.
    slot = st.container()
    if state["error"] is not None:
        with slot.container(border=True):
            st.markdown("**Watchlist prices**")
            st.write("Watchlist could not be read. Targets were not checked.")
        return
    items = state["items"]
    if items.empty:
        return
    found = watchlist.alerts(items=items, quotes=state["quotes"])
    if found["all_failed"]:
        with slot.container(border=True):
            st.markdown("**Watchlist prices**")
            st.write("Watchlist prices could not be fetched. Targets were not checked. "
                     "Open the Watchlist tab and click Update now.")
        return
    split = watchlist.visible_alerts(lines=found["lines"], mutes=state["mutes"])
    shown, hidden = split["shown"], split["hidden"]
    if not shown and not found["missing"]:
        with slot:
            _render_hidden_alerts(hidden=hidden, state=state)
        return
    if shown:
        slot.html(_WL_ALERT_CSS)
    # Only a reached price gets the loud box; a missing price stays a plain one.
    with slot.container(border=True, key="wl_alert_box" if shown else "wl_prices_box"):
        # No title on the alerts: each line says what it is.
        if not shown:
            st.markdown("**Watchlist prices**")
        for line in shown:
            symbol, side = line["symbol"], line["side"]
            buy = side == "buy"
            badge = ":green-badge[BUY]" if buy else ":red-badge[SELL]"
            text, actions = st.columns([3, 2], vertical_alignment="center")
            text.markdown(
                f":material/notifications_active: {badge} **{symbol}** "
                f"{_fmt_money(line['price'], line['currency'])} is at or "
                f"{'below' if buy else 'above'} your {'Buy below' if buy else 'Sell above'} price "
                f"{_fmt_price(line['target'])} · {line['list_name']}"
            )
            row = actions.container(horizontal=True, horizontal_alignment="right")
            row.button("Open in Watchlist", key=f"wl_alert_open_{symbol}_{side}", type="tertiary",
                       icon=":material/open_in_new:", help="See the chart and figures, and edit it",
                       on_click=_open_alert_stock,
                       kwargs={"symbol": symbol, "list_id": int(line["list_id"]), "tab_key": tab_key})
            row.button("Snooze 1 hour", key=f"wl_alert_snooze_{symbol}_{side}", type="tertiary",
                       icon=":material/snooze:", help="Hide this alert; it comes back in one hour",
                       on_click=_mute_alert,
                       kwargs={"symbol": symbol, "side": side, "kind": watchlist.MUTE_HOUR})
            row.button("Hide for today", key=f"wl_alert_today_{symbol}_{side}", type="tertiary",
                       icon=":material/notifications_off:", help="Hide this alert until tomorrow",
                       on_click=_mute_alert,
                       kwargs={"symbol": symbol, "side": side, "kind": watchlist.MUTE_TODAY})
        if found["missing"]:
            many = len(found["missing"]) > 1
            st.write(f"No price for: {', '.join(found['missing'])}. "
                     + ("Their targets were not checked." if many else "Its target was not checked."))
        if state["fetched"] is not None:
            st.caption(f"Fetched {_fetched_text(state=state)}. Quotes may be delayed (about 15 "
                       "minutes on most exchanges). See the Watchlist tab.")
        _render_hidden_alerts(hidden=hidden, state=state)


# The bell rings while an alert is shown and the Watchlist tab is not the open one.
# It is pinned to the app's top bar, left of Streamlit's own status widget and "Deploy",
# above the bar's layer.
_WL_BELL_CSS = """
<style>
@keyframes wl-ring {
  0%, 55%, 100% { transform: rotate(0); }
  8% { transform: rotate(20deg); } 16% { transform: rotate(-18deg); }
  24% { transform: rotate(14deg); } 32% { transform: rotate(-10deg); }
  40% { transform: rotate(6deg); } 48% { transform: rotate(-3deg); }
}
.st-key-wl_bell {
  /* Left of the "Running... Stop" widget, which shows left of Deploy while the app runs. */
  position: fixed; top: 0.35rem; right: 14rem; z-index: 1000001; width: auto !important;
}
/* !important: the theme's hover, focus and active colours are more specific. */
.st-key-wl_bell button, .st-key-wl_bell button * { color: #e02424 !important; }
.st-key-wl_bell button { gap: 0.15rem; white-space: nowrap; }
.st-key-wl_bell button:focus-visible { outline-color: #e02424; box-shadow: none; }
.st-key-wl_bell button p { font-size: 1.25rem; font-weight: 800; line-height: 1; }
/* The icon has a fixed box; a bigger glyph must grow it or it covers the count. */
.st-key-wl_bell button [data-testid="stIconMaterial"] {
  font-size: 2.2rem !important; width: 2.2rem !important; height: 2.2rem !important;
  line-height: 1 !important; margin: 0 !important;
  transform-origin: 50% 10%; animation: wl-ring 1.6s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .st-key-wl_bell button [data-testid="stIconMaterial"] { animation: none; }
}
</style>
"""


def _render_alert_bell(*, state: dict) -> None:
    """A ringing bell in the top bar that opens the Watchlist on the first shown alert."""
    shown = _alert_lines(state=state)["shown"]
    if not shown or _current_tab() == "Watchlist":
        return
    count, first = len(shown), shown[0]
    st.html(_WL_BELL_CSS)
    st.button(str(count), key="wl_bell", type="tertiary", icon=":material/notifications_active:",
              help=f"{_alert_count(count=count)} on your watchlist. Click to open the Watchlist.",
              on_click=_open_alert_stock,
              kwargs={"symbol": first["symbol"], "list_id": int(first["list_id"]), "tab_key": _TABS_MAIN})


# -- dialogs: one at a time, opened by a flag and closed by callbacks ---------

def _open_watchlist_dialog(*, kind: str, payload: dict) -> None:
    """Button callback: remember which dialog is open and give its fields their
    start values (a keyed field shows its key's value, not ``value=``)."""
    _close_watchlist_dialog()
    # The dialog replaces the list menu it may have been opened from.
    st.session_state[_WL_LIST_MENU] = False
    st.session_state[_WL_DIALOG] = {"kind": kind, **payload}
    for key, value in watchlist.dialog_state(kind=kind, payload=payload).items():
        st.session_state[key] = value


def _close_watchlist_dialog() -> None:
    for key in (_WL_DIALOG, "wl_dlg_error", "wl_dlg_note", "wl_dlg_remove_armed"):
        if key in st.session_state:
            del st.session_state[key]


def _save_watchlist(*, action) -> str | None:
    """Run one watchlist write. Returns the text to show when it was refused,
    else None after refreshing today's backup (the data exists nowhere else)."""
    try:
        action()
    except watchlist.WatchlistError as exc:
        return str(exc)
    backup.refresh(db_path=settings.db_path, backup_dir=settings.backup_dir,
                   keep=settings.backup_keep, today=_backup_day())
    return None


def _dialog_failed(*, error: str | None) -> bool:
    if error is None:
        return False
    st.session_state["wl_dlg_error"] = error
    return True


def _save_new_list() -> None:
    made: dict = {}
    name = st.session_state["wl_dlg_name"]
    if _dialog_failed(error=_save_watchlist(action=lambda: made.update(id=watchlist.create_list(
            db_path=settings.db_path, name=name, now=watchlist.utc_now())))):
        return
    st.session_state[_WL_LIST] = made["id"]
    _close_watchlist_dialog()


def _save_rename_list() -> None:
    payload, name = st.session_state[_WL_DIALOG], st.session_state["wl_dlg_name"]
    if _dialog_failed(error=_save_watchlist(action=lambda: watchlist.rename_list(
            db_path=settings.db_path, list_id=payload["list_id"], name=name))):
        return
    _close_watchlist_dialog()


def _save_delete_list() -> None:
    payload = st.session_state[_WL_DIALOG]
    if _dialog_failed(error=_save_watchlist(action=lambda: watchlist.delete_list(
            db_path=settings.db_path, list_id=payload["list_id"]))):
        return
    _close_watchlist_dialog()


def _target_notice(*, symbol: str, price: float | None, buy_target: float | None,
                   sell_target: float | None) -> str | None:
    """The one-time line for a target that is already reached when it is saved."""
    if price is None:
        return None
    if buy_target is not None and price <= buy_target:
        return f"{symbol}: the price is already at or below your Buy below price."
    if sell_target is not None and price >= sell_target:
        return f"{symbol}: the price is already at or above your Sell above price."
    return None


def _save_add_stock(*, another: bool) -> None:
    payload = st.session_state[_WL_DIALOG]
    buy, sell = st.session_state["wl_dlg_buy"], st.session_state["wl_dlg_sell"]
    for key in ("wl_dlg_error", "wl_dlg_note"):
        if key in st.session_state:
            del st.session_state[key]
    try:
        typed = watchlist.clean_symbol(text=st.session_state["wl_dlg_ticker"])
        watchlist.validate_targets(buy_target=buy, sell_target=sell)
    except watchlist.WatchlistError as exc:
        _dialog_failed(error=str(exc))
        return
    # Before yfinance: "7203-T" against a watched "7203.T" is a duplicate, not unknown.
    watched = watchlist.read_items(db_path=settings.db_path)["items"]
    if _dialog_failed(error=watchlist.duplicate_text(
            items=watched, symbol=typed, list_id=payload["list_id"])):
        return
    found = prices.resolve_symbol(text=typed)
    if found["status"] == "unknown":
        _dialog_failed(error=f'"{typed}" was not found on yfinance. Check the ticker.')
        return
    if found["status"] != "ok":
        _dialog_failed(error=f'Could not check "{typed}" (yfinance did not answer). Try again.')
        return
    symbol = found["symbol"]
    if _dialog_failed(error=_save_watchlist(action=lambda: watchlist.add_item(
            db_path=settings.db_path, list_id=payload["list_id"], symbol=symbol,
            buy_target=buy, sell_target=sell, now=watchlist.utc_now()))):
        return
    quote = prices.fetch_quote(symbol=symbol)
    notice = _target_notice(symbol=symbol, price=quote["price"] if quote else None,
                            buy_target=buy, sell_target=sell)
    if notice:
        st.session_state["wl_notice"] = notice
    st.session_state[_WL_SYMBOL] = symbol
    if not another:
        _close_watchlist_dialog()
        return
    st.session_state["wl_dlg_note"] = (
        f"Added {symbol} at {_fmt_money(quote['price'], quote['currency'])}." if quote
        else f"Added {symbol} (no price yet)."
    )
    for key, value in watchlist.dialog_state(kind="add_stock", payload=payload).items():
        st.session_state[key] = value


def _save_edit_stock() -> None:
    payload = st.session_state[_WL_DIALOG]
    symbol, list_id = payload["symbol"], st.session_state["wl_dlg_list"]
    buy, sell = st.session_state["wl_dlg_buy"], st.session_state["wl_dlg_sell"]
    if list_id is None:
        _dialog_failed(error=watchlist.MSG_PICK_LIST)
        return
    if _dialog_failed(error=_save_watchlist(action=lambda: watchlist.update_item(
            db_path=settings.db_path, symbol=symbol, list_id=list_id,
            buy_target=buy, sell_target=sell))):
        return
    moved = list_id != payload["list_id"]
    old_buy, old_sell = payload["buy_target"], payload["sell_target"]
    retargeted = (buy, sell) != (old_buy, old_sell)
    names = dict(payload["lists"])
    if moved and retargeted:
        st.session_state["wl_toast"] = f"Moved {symbol} to {names[list_id]} and saved its targets."
    elif moved:
        st.session_state["wl_toast"] = f"Moved {symbol} to {names[list_id]}."
    elif retargeted:
        st.session_state["wl_toast"] = f"Saved targets for {symbol}."
    notice = _target_notice(
        symbol=symbol, price=payload["price"],
        buy_target=buy if buy != old_buy else None,
        sell_target=sell if sell != old_sell else None,
    )
    if notice:
        st.session_state["wl_notice"] = notice
    _close_watchlist_dialog()


def _arm_remove_stock() -> None:
    st.session_state["wl_dlg_remove_armed"] = True


def _confirm_remove_stock() -> None:
    payload = st.session_state[_WL_DIALOG]
    if _dialog_failed(error=_save_watchlist(action=lambda: watchlist.remove_item(
            db_path=settings.db_path, symbol=payload["symbol"]))):
        return
    _close_watchlist_dialog()


def _save_schedule() -> None:
    now = watchlist.utc_now()
    if st.session_state["wl_dlg_mode"] == watchlist.SCHEDULE_ONCE:
        minutes, until = None, None
    else:
        minutes = int(st.session_state["wl_dlg_minutes"])
        until = (None if st.session_state["wl_dlg_duration"] == watchlist.DURATION_ALWAYS
                 else now + dt.timedelta(hours=int(st.session_state["wl_dlg_hours"])))
    if _dialog_failed(error=_save_watchlist(action=lambda: watchlist.write_refresh(
            db_path=settings.db_path, interval_minutes=minutes, until=until))):
        return
    _close_watchlist_dialog()


def _dialog_guard() -> None:
    """First statement of every dialog body: a callback that closed the dialog
    ran inside the dialog's own fragment, so the page has to be redrawn."""
    if _WL_DIALOG not in st.session_state:
        st.rerun()


def _dialog_messages() -> None:
    if "wl_dlg_note" in st.session_state:
        st.caption(st.session_state["wl_dlg_note"])
    if "wl_dlg_error" in st.session_state:
        st.warning(st.session_state["wl_dlg_error"])


def _dialog_buttons(*, label: str, on_save, save_kwargs: dict) -> None:
    cancel, save = st.columns(2)
    cancel.button("Cancel", key="wl_dlg_cancel", on_click=_close_watchlist_dialog, width="stretch")
    save.button(label, key="wl_dlg_save", type="primary", on_click=on_save,
                kwargs=save_kwargs, width="stretch")


def _name_dialog_body(*, payload: dict) -> None:
    _dialog_guard()
    st.text_input("List name", key="wl_dlg_name")
    _dialog_messages()
    if payload["kind"] == "new_list":
        _dialog_buttons(label="Create", on_save=_save_new_list, save_kwargs={})
    else:
        _dialog_buttons(label="Save", on_save=_save_rename_list, save_kwargs={})


def _delete_list_dialog_body(*, payload: dict) -> None:
    _dialog_guard()
    symbols = payload["symbols"]
    if symbols:
        shown = ", ".join(symbols[:10]) + (f" and {len(symbols) - 10} more" if len(symbols) > 10 else "")
        noun = "stock" if len(symbols) == 1 else "stocks"
        st.write(f'Delete list "{payload["name"]}"? It holds {len(symbols)} {noun}: {shown}. '
                 "Their buy and sell targets are deleted too. This cannot be undone.")
    else:
        st.write(f'Delete list "{payload["name"]}"? It is empty.')
    _dialog_messages()
    _dialog_buttons(label="Delete list", on_save=_save_delete_list, save_kwargs={})


def _target_field(*, label: str, key: str, price: float | None, help_text: str | None) -> None:
    small = price is not None and price < 1
    # value=None only makes an empty field possible; the key decides what is shown.
    st.number_input(label, key=key, value=None, placeholder="Leave empty for no target",
                    format="%.4f" if small else "%.2f", step=0.0001 if small else 0.01,
                    help=help_text)


def _add_stock_dialog_body(*, payload: dict) -> None:
    _dialog_guard()
    st.text_input("Ticker", key="wl_dlg_ticker")
    currency_help = "In the quote's currency. London stocks quote in pence."
    _target_field(label="Buy below", key="wl_dlg_buy", price=None, help_text=currency_help)
    _target_field(label="Sell above", key="wl_dlg_sell", price=None, help_text=currency_help)
    _dialog_messages()
    cancel, another, add = st.columns(3)
    cancel.button("Cancel", key="wl_dlg_cancel", on_click=_close_watchlist_dialog, width="stretch")
    another.button("Add and add another", key="wl_dlg_add_another", on_click=_save_add_stock,
                   kwargs={"another": True}, width="stretch")
    add.button("Add", key="wl_dlg_save", type="primary", on_click=_save_add_stock,
               kwargs={"another": False}, width="stretch")


def _edit_stock_dialog_body(*, payload: dict) -> None:
    _dialog_guard()
    symbol, price, ccy = payload["symbol"], payload["price"], payload["currency"]
    st.markdown(f"**{symbol}** · {_fmt_money(price, ccy)} now" if price is not None
                else f"**{symbol}** · no price")
    names = dict(payload["lists"])
    st.selectbox("List", list(names), format_func=lambda list_id: names[list_id],
                 key="wl_dlg_list", index=None, placeholder="Pick a list")
    unit = f" ({ccy})" if ccy else ""
    _target_field(label=f"Buy below{unit}", key="wl_dlg_buy", price=price, help_text=None)
    _target_field(label=f"Sell above{unit}", key="wl_dlg_sell", price=price, help_text=None)
    _dialog_messages()
    _dialog_buttons(label="Save", on_save=_save_edit_stock, save_kwargs={})
    st.divider()
    if "wl_dlg_remove_armed" not in st.session_state:
        st.button("Remove from watchlist", key="wl_dlg_remove", on_click=_arm_remove_stock)
        return
    # Another label and the other side, so a double click cannot remove.
    st.warning(f'This deletes {symbol} and its targets from "{payload["list_name"]}".')
    _, confirm = st.columns([1, 2])
    confirm.button(f"Remove {symbol} and its targets", key="wl_dlg_remove_confirm",
                   type="primary", on_click=_confirm_remove_stock, width="stretch")


def _schedule_dialog_body(*, payload: dict) -> None:
    _dialog_guard()
    st.caption(f"Now: {payload['label']}.")
    mode = st.radio("Update watchlist prices", list(_SCHEDULE_MODES), key="wl_dlg_mode",
                    format_func=lambda value: _SCHEDULE_MODES[value])
    every = mode == watchlist.SCHEDULE_EVERY
    minutes = st.number_input("Minute(s)", min_value=1, max_value=60, step=1,
                              key="wl_dlg_minutes", disabled=not every)
    duration = st.radio("Duration", list(_SCHEDULE_DURATIONS), key="wl_dlg_duration",
                        format_func=lambda value: _SCHEDULE_DURATIONS[value],
                        horizontal=True, disabled=not every)
    timed = every and duration == watchlist.DURATION_HOURS
    hours = st.number_input("Hour(s)", min_value=1, max_value=24, step=1,
                            key="wl_dlg_hours", disabled=not timed)
    if timed:
        st.caption(f"Save starts a new {int(hours)} hours from now.")
    if every:
        per_hour = watchlist.requests_per_hour(stocks=payload["watched"], interval_minutes=int(minutes))
        st.caption(f"With {payload['watched']} watched stocks this is about {per_hour:,} "
                   "yfinance requests an hour.")
        st.caption("Works only while this page is open.")
    _dialog_messages()
    _dialog_buttons(label="Save", on_save=_save_schedule, save_kwargs={})


def _dialog_title(*, payload: dict) -> str:
    kind = payload["kind"]
    if kind == "new_list":
        return "New list"
    if kind == "rename_list":
        return f'Rename list "{payload["name"]}"'
    if kind == "delete_list":
        return f'Delete list "{payload["name"]}"'
    if kind == "add_stock":
        return f'Add stock to "{payload["list_name"]}"'
    if kind == "edit_stock":
        return f"Edit {payload['symbol']}"
    return "Update schedule"


_DIALOG_BODIES = {
    "new_list": _name_dialog_body, "rename_list": _name_dialog_body,
    "delete_list": _delete_list_dialog_body, "add_stock": _add_stock_dialog_body,
    "edit_stock": _edit_stock_dialog_body, "schedule": _schedule_dialog_body,
}


def _watchlist_dialog() -> None:
    """Open the dialog the flag names. Streamlit allows one dialog per run, so a
    flag left beside another app dialog, or of an unknown kind, is dropped."""
    if _WL_DIALOG not in st.session_state:
        return
    payload = st.session_state[_WL_DIALOG]
    others = watchlist.DIALOG_FLAGS - {_WL_DIALOG}
    if payload["kind"] not in _DIALOG_BODIES or others & {str(key) for key in st.session_state}:
        _close_watchlist_dialog()
        return
    st.dialog(_dialog_title(payload=payload), on_dismiss=_close_watchlist_dialog)(
        _DIALOG_BODIES[payload["kind"]]
    )(payload=payload)


def _pick_watchlist() -> None:
    """The search box picked a list, or a stock (then its list opens on that stock)."""
    kind, _, value = st.session_state[_WL_PICKER_KEY].partition(":")
    if kind == "list":
        st.session_state[_WL_LIST] = int(value)
        return
    symbol, _, list_id = value.partition("|")
    st.session_state[_WL_LIST] = int(list_id)
    st.session_state[_WL_SYMBOL] = symbol


_WL_PICKER_KEY = "wl_list_select"
# Height of one dropdown row, as Streamlit draws it.
_WL_PICKER_ROW = "2.5rem"


def _list_picker_css(*, lists: int, options: int) -> str:
    """Stylesheet that keeps the tickers out of the list dropdown until the user types.

    The dropdown holds the lists first, then every watched ticker, so that one
    box searches both. Opened without typing it would show all of them; a
    selectbox has no setting for that, so this hides the ticker rows while the
    box's text is empty, and shortens the dropdown to the list rows.

    It leans on how Streamlit builds the dropdown: the input mirrors the typed
    text in its ``value`` attribute, and row ``n`` of what is shown has an id
    ending in ``val-n``. Nothing is typed, so nothing is filtered, and row ``n``
    is option ``n``. If a Streamlit upgrade changes either, the rules stop
    matching and the tickers show again when the box opens; nothing else breaks.
    """
    if options <= lists:
        return ""
    idle = (f'body:has(.st-key-{_WL_PICKER_KEY} input[aria-expanded="true"][value=""]) '
            '[data-testid="stSelectboxVirtualDropdown"]')
    rows = ", ".join(f'{idle} li[id$="val-{index}"]' for index in range(lists, options))
    height = f"calc({lists} * {_WL_PICKER_ROW})"
    return (
        "<style>"
        f"{rows} {{ display: none; }} "
        f"{idle} > div {{ max-height: {height}; }} "
        f"{idle} > div > div {{ height: {height} !important; }}"
        "</style>"
    )


def _step_watchlist(*, ids: list[int], step: int) -> None:
    """Move to the list ``step`` places along ``ids`` (dropdown order), stopping at the ends."""
    index = ids.index(st.session_state[_WL_LIST]) + step
    st.session_state[_WL_LIST] = ids[max(0, min(index, len(ids) - 1))]


def _step_watch_symbol(*, order: list[str], step: int) -> None:
    """Move the watchlist's selected stock ``step`` rows, stopping at the ends.

    With no stock selected, a step lands on the first row.
    """
    symbol = st.session_state[_WL_SYMBOL] if _WL_SYMBOL in st.session_state else None
    index = order.index(symbol) + step if symbol in order else 0
    st.session_state[_WL_SYMBOL] = order[max(0, min(index, len(order) - 1))]


def _update_quotes_now() -> None:
    st.session_state["wl_nonce"] += 1


def _watchlist_styler(*, frame: pd.DataFrame):
    """The list's table: numbers underneath (so sorting is numeric), text on top."""
    shown = frame.rename(columns={
        "symbol": "Symbol", "alert": "Alert", "price": "Price", "day_pct": "Day",
        "buy_target": "Buy below", "above_buy": "To buy", "sell_target": "Sell above",
        "below_sell": "To sell", "shares": "Shares held",
    })
    currencies = list(shown["currency"])
    shown = shown.drop(columns="currency")

    def left(value) -> str:
        return "reached" if value == 0 else f"{value:.2f}%"

    # na_rep, not the callables: Styler does not pass a missing value to them.
    styler = shown.style.format({
        "Day": lambda value: f"{value:+.2f}%",
        "Buy below": _fmt_price, "Sell above": _fmt_price,
        "To buy": left, "To sell": left, "Shares held": _fmt_qty,
    }, na_rep="—")
    # One format per row: the currency belongs to the row, not to the column.
    for index, currency in zip(shown.index, currencies):
        styler = styler.format(
            lambda value, ccy=currency: _fmt_money(value, ccy), na_rep="no data",
            subset=pd.IndexSlice[[index], ["Price"]],
        )
    return styler.map(lambda value: _ALERT_COLORS[value] if value in _ALERT_COLORS else "",
                      subset=["Alert"])


def _alert_count(*, count: int) -> str:
    return f"{count} alert" if count == 1 else f"{count} alerts"


def tab_watchlist(
    *, state: dict, positions: pd.DataFrame, cash_tx: pd.DataFrame, trades: pd.DataFrame,
    connected: bool,
) -> None:
    if not connected:
        st.caption("IBKR not connected: positions not shown.")
    if state["error"] is not None:
        st.warning(state["error"])
        _close_watchlist_dialog()
        return
    _watchlist_body(state=state, positions=positions, cash_tx=cash_tx, trades=trades)
    _watchlist_dialog()


def _watchlist_body(
    *, state: dict, positions: pd.DataFrame, cash_tx: pd.DataFrame, trades: pd.DataFrame,
) -> None:
    lists, items, quotes = state["lists"], state["items"], state["quotes"]
    if lists.empty:
        st.info("You have no watchlists yet. Create one to start watching stocks.")
        st.button("＋ New list", key="wl_new_list", on_click=_open_watchlist_dialog,
                  kwargs={"kind": "new_list", "payload": {}})
        return

    ids = [int(list_id) for list_id in lists["id"]]
    names = dict(zip(ids, lists["name"]))
    found = watchlist.alerts(items=items, quotes=quotes)
    if _WL_LIST not in st.session_state:
        # A new session opens on the first alert, so it is seen without a search.
        first = watchlist.first_alert(items=items, quotes=quotes)
        st.session_state[_WL_LIST] = first["list_id"] if first else ids[0]
        if first:
            st.session_state[_WL_SYMBOL] = first["symbol"]
    if st.session_state[_WL_LIST] not in ids:
        st.session_state[_WL_LIST] = ids[0]
    list_id = st.session_state[_WL_LIST]
    list_name = names[list_id]

    def label(option: int) -> str:
        count = found["by_list"][option] if option in found["by_list"] else 0
        return f"{names[option]} ({_alert_count(count=count)})" if count else names[option]

    # One search box for lists and tickers: typing filters both, and a ticker opens
    # its list on that stock. Options are keys, not labels, so a label that gains
    # "(1 alert)" does not reset the pick; the box always shows the open list.
    watched = [(row.symbol, int(row.list_id)) for row in items.itertuples(index=False)
               if row.list_id in names]
    options = [f"list:{option}" for option in ids] + [f"stock:{symbol}|{owner}" for symbol, owner in watched]

    def option_label(option: str) -> str:
        kind, _, value = option.partition(":")
        if kind == "list":
            return label(int(value))
        symbol, _, owner = value.partition("|")
        return f"{symbol} · in {names[int(owner)]}"

    st.session_state[_WL_PICKER_KEY] = f"list:{list_id}"
    picker_css = _list_picker_css(lists=len(ids), options=len(options))
    if picker_css:
        st.html(picker_css)
    pick, up, down, _, menu_col = st.columns([5, 0.4, 0.4, 1.8, 1.4], vertical_alignment="bottom")
    pick.selectbox("List", options, format_func=option_label, key=_WL_PICKER_KEY,
                   on_change=_pick_watchlist, placeholder="Search a list or a ticker",
                   help="Opens on your lists. Type to search your lists and watched tickers.")
    if len(ids) > 1:
        position = ids.index(list_id)
        up.button("▲", key="wl_list_prev", help="Previous list", disabled=position == 0,
                  on_click=_step_watchlist, kwargs={"ids": ids, "step": -1}, width="stretch")
        down.button("▼", key="wl_list_next", help="Next list", disabled=position == len(ids) - 1,
                    on_click=_step_watchlist, kwargs={"ids": ids, "step": 1}, width="stretch")
    list_items = items[items["list_id"] == list_id]
    # One menu for the three list actions: they are rare, and Delete is not one
    # stray click away. It tracks its state so that a dialog can close it.
    menu = menu_col.popover("List actions", key=_WL_LIST_MENU, on_change="rerun", width="stretch")
    menu.button("New list", key="wl_new_list", icon=":material/add:", type="tertiary",
                on_click=_open_watchlist_dialog, kwargs={"kind": "new_list", "payload": {}})
    menu.button(f"Rename {list_name}", key="wl_rename_list", icon=":material/edit:", type="tertiary",
                on_click=_open_watchlist_dialog,
                kwargs={"kind": "rename_list", "payload": {"list_id": list_id, "name": list_name}})
    menu.button(f"Delete {list_name}", key="wl_delete_list", icon=":material/delete:", type="tertiary",
                on_click=_open_watchlist_dialog,
                kwargs={"kind": "delete_list", "payload": {
                    "list_id": list_id, "name": list_name,
                    "symbols": list(list_items["symbol"])}})

    if not items.empty:
        setting, now = state["setting"], state["now"]
        active = watchlist.is_active(setting=setting, now=now)
        label_text = watchlist.refresh_label(setting=setting, now=now, tz=watchlist.local_tz())
        line, links_col = st.columns([3, 2], vertical_alignment="center")
        # Quiet text buttons: the status line stays a line, not a second toolbar.
        links = links_col.container(horizontal=True, horizontal_alignment="right")
        fetched = f"Prices fetched {_fetched_text(state=state)}" if state["fetched"] else "Prices not fetched"
        line.caption(f"{fetched} · updates: {label_text}"
                     + (" · works only while this page is open" if active else ""))
        links.button("Update now", key="wl_update_now", icon=":material/refresh:", type="tertiary",
                     on_click=_update_quotes_now)
        links.button("Update schedule", key="wl_schedule", icon=":material/schedule:", type="tertiary",
                     on_click=_open_watchlist_dialog,
                     kwargs={"kind": "schedule", "payload": {
                         "label": label_text, "watched": len(items),
                         "interval_minutes": setting["interval_minutes"] if active else None,
                         "until": setting["until"] if active else None}})
        elsewhere = [f"{names[other]} ({_alert_count(count=count)})"
                     for other, count in found["by_list"].items() if other != list_id and other in names]
        if elsewhere:
            st.caption(f"Alerts in other lists: {', '.join(sorted(elsewhere))}")

    add_kwargs = {"kind": "add_stock", "payload": {"list_id": list_id, "list_name": list_name}}
    if list_items.empty:
        st.info(f'"{list_name}" is empty. Add a stock to see its price and set targets.')
        # The same row as a filled list, so the buttons do not move when a stock is added.
        add, edit, _ = st.columns(_WL_STOCK_ROW)
        add.button("＋ Add stock", key="wl_add_stock", on_click=_open_watchlist_dialog,
                   kwargs=add_kwargs, width="stretch")
        edit.button("Edit / move / remove", key="wl_edit_stock", type="secondary", disabled=True,
                    help="Add a stock first", width="stretch")
        return

    if all(quotes[symbol] is None for symbol in items["symbol"] if symbol in quotes) and quotes:
        st.warning("Prices could not be fetched (yfinance). Click **Update now** to try again.")
    if "wl_notice" in st.session_state:
        st.info(st.session_state["wl_notice"])
        del st.session_state["wl_notice"]

    frame = watchlist.table(items=list_items, quotes=quotes, positions=positions)
    order = list(frame["symbol"])
    table_key = f"wl_table_{list_id}"
    stored = (list(st.session_state[table_key]["selection"]["rows"])
              if table_key in st.session_state else None)
    # A click anywhere in a row arrives as a selected cell; it counts as a click on
    # that row. The cell is cleared below, so any cell here is a new click.
    cells = (st.session_state[table_key]["selection"]["cells"]
             if table_key in st.session_state and "cells" in st.session_state[table_key]["selection"]
             else [])
    if cells:
        stored = [int(cells[0][0])]
    # What the app wrote is compared only for the same table: another list's rows
    # would read as a click here.
    same_table = "wl_table_seen" in st.session_state and st.session_state["wl_table_seen"] == table_key
    symbol = watchlist.resolve_selection(
        stored_rows=stored,
        rows_seen=st.session_state["wl_rows_seen"] if same_table else stored,
        order_seen=st.session_state["wl_order_seen"] if "wl_order_seen" in st.session_state else [],
        order=order,
        symbol=st.session_state[_WL_SYMBOL] if _WL_SYMBOL in st.session_state else None,
    )
    st.session_state[_WL_SYMBOL] = symbol
    # No stock is selected until the user picks one (a row click, ▲ ▼, a search, an alert).
    index = order.index(symbol) if symbol is not None else None
    marked = [index] if index is not None else []
    # Written every run: the highlight follows the stock when rows reorder.
    st.session_state[table_key] = {"selection": {"rows": marked, "columns": [], "cells": []}}
    st.session_state["wl_rows_seen"], st.session_state["wl_order_seen"] = marked, order
    st.session_state["wl_table_seen"] = table_key

    add, edit, up, down, _ = st.columns([*_WL_STOCK_ROW[:2], 0.5, 0.5, 4])
    add.button("＋ Add stock", key="wl_add_stock", on_click=_open_watchlist_dialog,
               kwargs=add_kwargs, width="stretch")
    if len(order) > 1:
        up.button("▲", key="wl_symbol_prev", help="Previous stock", disabled=index is None or index == 0,
                  on_click=_step_watch_symbol, kwargs={"order": order, "step": -1}, width="stretch")
        down.button("▼", key="wl_symbol_next", help="Next stock", disabled=index == len(order) - 1,
                    on_click=_step_watch_symbol, kwargs={"order": order, "step": 1}, width="stretch")
    item = quote = buy_target = sell_target = None
    if symbol is None:
        edit.button("Edit / move / remove", key="wl_edit_stock", type="secondary", disabled=True,
                    help="Click a stock's row first", width="stretch")
    else:
        item = list_items[list_items["symbol"] == symbol].iloc[0]
        quote = quotes[symbol] if symbol in quotes else None
        buy_target = None if pd.isna(item["buy_target"]) else float(item["buy_target"])
        sell_target = None if pd.isna(item["sell_target"]) else float(item["sell_target"])
        # Primary only while a stock is selected: its action is the one the eye lands on.
        edit.button(f"Edit / move / remove: {symbol}", key="wl_edit_stock", type="primary",
                    on_click=_open_watchlist_dialog, width="stretch",
                    kwargs={"kind": "edit_stock", "payload": {
                        "symbol": symbol, "list_id": list_id, "list_name": list_name,
                        "buy_target": buy_target, "sell_target": sell_target,
                        "price": quote["price"] if quote else None,
                        "currency": quote["currency"] if quote else None,
                        "lists": list(names.items())}})

    st.dataframe(
        _watchlist_styler(frame=frame), key=table_key, on_select="rerun",
        selection_mode=["single-row", "single-cell"], hide_index=True, width="stretch",
        # Streamlit draws its own text in a missing cell, whatever the Styler says.
        placeholder="—",
        column_config={
            "To buy": st.column_config.Column(help="How far the price is above your Buy below price"),
            "To sell": st.column_config.Column(help="How far the price is below your Sell above price"),
            "Price": st.column_config.Column(help="yfinance quote; may be delayed"),
        },
    )
    st.caption("Click a stock's row, or use ▲ ▼, to see its chart and figures and to edit it.")
    if symbol is None:
        return

    st.divider()
    head, period_col = st.columns([2, 1], vertical_alignment="bottom")
    head.subheader(symbol)
    period = period_col.selectbox("Period", _PRICE_PERIODS, index=3, key="wl_period")
    held = bool(pd.notna(frame.loc[index, "shares"]))
    _render_price_detail(
        symbol=symbol, period=period, positions=positions, cash_tx=cash_tx, trades=trades,
        key_prefix="wl", layout="watchlist", quote=quote,
        buy_target=buy_target, sell_target=sell_target, show_position=held,
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
  --sell: #d93a4a;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink);
}
.divcal-root.dark {
  --surface: #16181d; --surface-2: #1d2027; --ink: #f5f5f3; --ink-2: #c3c2b7;
  --muted: #898781; --line: #2a2d34; --line-strong: #3a3e47;
  --paid: rgba(12,163,12,0.20); --paid-dot: #0ca30c;
  --declared: rgba(250,178,25,0.18); --declared-dot: #fab219;
  --expected: #ec835a; --accent: #3987e5; --accent-tint: rgba(57,135,229,0.10);
  --bar: #256abf; --hover: color-mix(in srgb, var(--surface) 94%, var(--ink));
  --sell: #f0616d;
}
.divcal-legend { display: flex; flex-wrap: wrap; gap: 18px; margin: 0 0 10px; font-size: 12.5px; color: var(--ink-2); }
.divcal-legend span { display: inline-flex; align-items: center; gap: 7px; }
.divcal-legend i { width: 14px; height: 14px; border-radius: 4px; display: inline-block; }
.divcal-legend .sw-paid { background: var(--paid); box-shadow: inset 0 0 0 1px var(--paid-dot); }
.divcal-legend .sw-declared { background: var(--declared); box-shadow: inset 0 0 0 1px var(--declared-dot); }
.divcal-legend .sw-expected { outline: 1px dashed var(--expected); outline-offset: -1px; }
.divcal-legend .sw-buy, .divcal-legend .sw-sell { width: 0; height: 0; background: none; border-radius: 0;
  border-left: 5px solid transparent; border-right: 5px solid transparent; }
.divcal-legend .sw-buy { border-bottom: 8px solid var(--accent); }
.divcal-legend .sw-sell { border-top: 8px solid var(--sell); }
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

/* Trade markers: a small triangle on the cell's bottom edge at the day of the
   month the trade happened — blue pointing up for a buy, red pointing down for
   a sell; hover opens a panel with the trade details. */
.divcal tbody td { position: relative; }
.divcal .trade { position: absolute; bottom: 2px; width: 10px; height: 8px; margin-left: -5px;
  cursor: pointer; z-index: 4; }
.divcal .trade::before { content: ""; position: absolute; left: 1px; bottom: 0; border-left: 4px solid transparent;
  border-right: 4px solid transparent; }
.divcal .trade.buy::before { border-bottom: 6px solid var(--accent); }
.divcal .trade.sell::before { border-top: 6px solid var(--sell); }
.divcal .tradetip { display: none; position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
  z-index: 20; min-width: 180px; padding: 8px 10px; border-radius: 8px; text-align: left; white-space: nowrap;
  background: var(--surface-2); color: var(--ink); border: 1px solid var(--line-strong);
  box-shadow: 0 6px 18px rgba(0,0,0,.25); font-size: 12px; font-weight: 400; line-height: 1.5; }
.divcal .tradetip b { font-weight: 650; }
.divcal .trade:hover .tradetip { display: block; }
.divcal tr.sold th, .divcal tr.sold td { color: var(--muted); }
.divcal .held { font-size: 11px; font-weight: 500; color: var(--muted); margin-left: 4px; }
.divcal .nodiv { font-size: 11px; font-weight: 400; font-style: italic; color: var(--muted); margin-left: 6px; }
.divcal .soldtag { font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-left: 4px; }

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
    '<span><i class="sw-buy"></i>Bought</span>'
    '<span><i class="sw-sell"></i>Sold (hover a marker for details)</span>'
    "</div>"
)


def _pct(total: str, top: float) -> str:
    """Bar length (0..1) for a formatted ticker total relative to the largest."""
    if not total or top <= 0:
        return ""
    return f"{float(total.replace(',', '')) / top:.3f}"


def _money(*, value: float | None, currency: str | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} {currency or ''}".strip()


def _trade_marker(*, trade: dict) -> str:
    """One buy or sell triangle with its hover panel."""
    qty = dividends.format_qty(value=trade["quantity"])
    ccy = trade.get("currency")
    sell = trade["side"] == "SELL"
    details = [
        f"Holding after: {dividends.format_qty(value=trade['holding'])} sh",
        f"Price: {_money(value=trade['price'], currency=ccy)}",
        f"{'Proceeds' if sell else 'Cost'}: {_money(value=trade['amount'], currency=ccy)}",
        f"Commission: {_money(value=trade['commission'], currency=ccy)}",
    ]
    if sell and trade["realized_pnl"] is not None:
        details.append(f"Realized P&L: {_money(value=trade['realized_pnl'], currency=ccy)}")
    if trade["fills"] > 1:
        details.append(f"{trade['fills']} fills (average price)")
    head = f"<b>{'Sold' if sell else 'Bought'} {html.escape(qty)} sh</b> · {trade['date']:%d-%m-%Y}"
    body = "<br>".join([head, *(html.escape(line) for line in details)])
    return (f'<span class="trade {"sell" if sell else "buy"}" style="left:{100 * trade["pos"]:.1f}%">'
            f'<span class="tradetip">{body}</span></span>')


def _ticker_label(*, cal: dividends.Calendar, sym: str) -> str:
    """Ticker plus the shares held now, e.g. "HAFN 22 sh"."""
    name = html.escape(str(sym))
    qty = cal.shares.get(sym)
    if qty is None:
        return name
    held = html.escape(dividends.format_qty(value=qty))
    return f'{name} <span class="held" title="Shares held now">{held} sh</span>'


def _calendar_html(cal: dividends.Calendar, dark: bool, marks: dict) -> str:
    """Render the calendar as an HTML table: status tints, current-month
    highlight, native hover tooltips (title), a per-ticker Total column with a
    magnitude bar, and a Total footer row. ``dark`` follows the app theme.
    ``marks`` (``dividends.trade_marks``) adds a hoverable triangle per buy or sell."""
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
        sym_marks = marks.get(sym, {})
        # A ticker that pays nothing still has buys and sells worth showing: with
        # trades in view it gets normal month cells, so each marker sits in its month.
        if sym in cal.no_data and not sym_marks:
            rows.append(
                f'<tr><th>{_ticker_label(cal=cal, sym=sym)}</th>{total}'
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
            trades = "".join(_trade_marker(trade=t) for t in sym_marks.get(c, []))
            cells.append(
                f'<td class="{klass}"{title}>{html.escape(str(cal.values.at[sym, c]))}{trades}</td>'
            )
        if sym in cal.no_data:
            label = f'{_ticker_label(cal=cal, sym=sym)} <span class="nodiv">no dividends</span>'
            rows.append(f'<tr><th>{label}</th>{total}{"".join(cells)}</tr>')
        elif sym in cal.sold:
            label = f'{html.escape(str(sym))} <span class="soldtag">sold</span>'
            rows.append(f'<tr class="sold"><th>{label}</th>{total}{"".join(cells)}</tr>')
        else:
            rows.append(f'<tr><th>{_ticker_label(cal=cal, sym=sym)}</th>{total}{"".join(cells)}</tr>')
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
        "Amounts after tax: the cash that landed (dividend minus IBKR's withholding)."
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
    paying = sum(1 for sym, v in cal.ticker_totals.items() if v and sym not in cal.sold)
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
    m4.metric("Paying tickers", f"{paying} of {cal.held_count}", border=True,
              help="Held tickers that paid in the selected period. Sold tickers "
                   "are listed below the calendar rows and counted in every total.")

    # "All" reaches back to the first buy too, so every buy gets its marker.
    first_buy = dividends.first_buy_month(trades=data.get("trades"), symbols=list(cal.values.index))
    if months is None and first_buy is not None:
        cal = dividends.extend_back(cal=cal, first_ym=first_buy)
    marks = dividends.trade_marks(
        trades=data.get("trades"), symbols=list(cal.values.index),
        columns=list(cal.values.columns), shares=cal.shares,
    )
    # A ticker sold before the first month shown was not in the portfolio then.
    cal = dividends.drop_absent_sold(cal=cal, marks=marks)
    st.html(_calendar_html(cal, dark=st.context.theme.type == "dark", marks=marks))

    with st.expander("How to read this calendar"):
        st.markdown(
            "- **Rows** are held tickers (and sold tickers that paid), **columns** are months. **Total** "
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
            "- “No dividends” rows are held tickers that pay none.\n"
            "- **Triangles** on a cell's bottom edge mark trades on that day of the "
            "month: blue pointing up = bought, red pointing down = sold. Hover one "
            "for price, amount and shares held after.\n"
            "- Rows marked **sold** are tickers you no longer hold. A sold ticker is listed "
            "only when the months shown include a dividend or a trade of it. Their past "
            "payments count in every total; nothing is projected for them.\n"
            "- After tax is the cash that landed: the dividend minus IBKR's "
            "withholding. The same numbers feed Net Worth, Prices and Positions."
        )


# ---- performance calendar ---------------------------------------------------

# The chosen period, (first month, last month), kept outside the slider widget.
# Buttons and month rows change the period by writing it here and bumping the
# sequence number in the slider's key: Streamlit then builds a fresh slider
# that starts on the new period. Writing to the slider's own key instead would
# clash with its ``value`` — which a range slider cannot do without — and
# Streamlit logs a warning for that on every such change.
_PERF_PERIOD_KEY = "perf_period"
_PERF_SLIDER_SEQ_KEY = "perf_period_seq"
_PERF_SLIDER_KEY_PREFIX = "perf_period_slider_"


def _set_perf_period(*, period: tuple[str, str]) -> None:
    """Callback helper: choose a period and make the slider restart on it."""
    st.session_state[_PERF_PERIOD_KEY] = period
    seq = st.session_state[_PERF_SLIDER_SEQ_KEY] if _PERF_SLIDER_SEQ_KEY in st.session_state else 0
    st.session_state[_PERF_SLIDER_SEQ_KEY] = seq + 1

_PERF_PRESET_KEY = "perf_preset"
# Quick periods, all ending on the newest month: a number of months, the newest
# month's calendar year so far, or everything.
_PERF_PRESET_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}
_PERF_PRESETS = (*_PERF_PRESET_MONTHS, "YTD", "All")


def _month_key(*, year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


def _month_options(*, months: list[tuple[int, int]]) -> list[str]:
    """Every month from the first to the last one with data, oldest first, so
    the period slider has no holes."""
    (year, month), last = min(months), max(months)
    out = []
    while (year, month) <= last:
        out.append(_month_key(year=year, month=month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _preset_period(*, options: list[str], preset: str) -> tuple[str, str]:
    """First and last month of a quick period."""
    last = options[-1]
    if preset == "All":
        return options[0], last
    if preset == "YTD":
        return next(option for option in options if option[:4] == last[:4]), last
    return options[max(len(options) - _PERF_PRESET_MONTHS[preset], 0)], last


def _apply_perf_preset(*, options: list[str]) -> None:
    """Quick-period callback: move the slider to the picked period. A click on
    the already-picked period clears the pick; it is restored on the next run
    (see ``_matching_preset``), so the period in force always stays marked."""
    preset = st.session_state.get(_PERF_PRESET_KEY)
    if preset:
        _set_perf_period(period=_preset_period(options=options, preset=preset))


def _matching_preset(*, options: list[str], period: tuple[str, str], picked: str | None) -> str | None:
    """The quick period to show as selected for ``period``, or None when the
    period is none of them (set by the slider, the arrows or a month row).

    Several quick periods can be the same months (with ten months of data, YTD
    and 1Y are both everything): the one the user picked wins, else the first.
    """
    matches = [preset for preset in _PERF_PRESETS
               if _preset_period(options=options, preset=preset) == period]
    if picked in matches:
        return picked
    return matches[0] if matches else None


def _shift_perf_period(*, options: list[str], step: int) -> None:
    """Arrow callback: slide the whole period ``step`` months, keeping its length."""
    start, end = (options.index(key) for key in st.session_state[_PERF_PERIOD_KEY])
    step = max(-start, min(step, len(options) - 1 - end))
    _set_perf_period(period=(options[start + step], options[end + step]))


def _open_perf_month(*, month_key: str) -> None:
    """Month row callback: narrow the period to that month."""
    _set_perf_period(period=(month_key, month_key))


_PERF_DAY_KEY = "perf_day_open"


def _open_perf_day(*, day: str) -> None:
    """Day tile callback: show that day's details."""
    st.session_state[_PERF_DAY_KEY] = day


def _close_perf_day() -> None:
    """on_dismiss callback: the day dialog was closed with X, Esc or a click outside."""
    if _PERF_DAY_KEY in st.session_state:
        del st.session_state[_PERF_DAY_KEY]


def _day_trades(*, trades: pd.DataFrame | None, date: dt.date) -> pd.DataFrame:
    """The day's fills as a table."""
    cols = {"side": "Side", "symbol": "Ticker", "quantity": "Quantity", "price": "Price",
            "commission": "Commission"}
    if trades is None or trades.empty or "trade_date" not in trades:
        return pd.DataFrame(columns=list(cols.values()))
    rows = trades[pd.to_datetime(trades["trade_date"]).dt.date == date]
    return rows[[c for c in cols if c in rows]].rename(columns=cols)


@st.dialog("Day details", width="large", on_dismiss=_close_perf_day)
def perf_day_dialog(
    *, row: dict, detail: dict, symbols: list[dict], trades: pd.DataFrame, currency: str | None,
) -> None:
    """What moved the account on one day: per-ticker P&L, then everything else."""
    date = row["date"]
    unit = f" {currency}" if currency else ""
    st.subheader(f"{date:%A}, {date.day} {date:%B %Y}")
    pnl = None if pd.isna(row["pnl"]) else float(row["pnl"])
    ret = None if pd.isna(row["ret"]) else float(row["ret"])
    cols = st.columns(4)
    cols[0].metric("Return", perf_calendar.fmt_pct(ret=ret))
    cols[1].metric(f"P&L ({currency})" if currency else "P&L", perf_calendar.fmt_money(value=pnl))
    cols[2].metric("Close", f"{row['nav']:,.2f}")
    cols[3].metric("Previous close", "—" if pd.isna(row["prev_nav"]) else f"{row['prev_nav']:,.2f}")

    if symbols:
        st.markdown("**P&L by ticker**")
        st.plotly_chart(figures.day_symbols_bar(rows=symbols, currency=currency), width="stretch")
        held = sum(item["pnl"] for item in symbols)
        if pnl is not None:
            st.dataframe(pd.DataFrame([
                {"Source": "Shares held coming into the day", "Amount": round(held, 2)},
                {"Source": "Everything else: shares traded that day, dividends, interest, "
                           "fees, currency", "Amount": round(pnl - held, 2)},
                {"Source": "Day total", "Amount": round(pnl, 2)},
            ]), width="stretch", hide_index=True)
        st.dataframe(pd.DataFrame([{
            "Ticker": item["symbol"], "Name": item["description"],
            "Close price": item["price"], "Previous price": item["prev_price"],
            "Price change %": None if item["pct"] is None else round(item["pct"] * 100, 2),
            "P&L": round(item["pnl"], 2),
        } for item in symbols]), width="stretch", hide_index=True)
    else:
        st.info("IBKR has no per-ticker figures for this day.")

    events = []
    if abs(row["flow"]) >= performance.FLAT_EPSILON:
        events.append(f"- {'Deposit' if row['flow'] > 0 else 'Withdrawal'}: "
                      f"{abs(row['flow']):,.2f}{unit} (not counted in the day's P&L)")
    if abs(detail["dividends"]) >= performance.FLAT_EPSILON:
        events.append(f"- Dividends received: {detail['dividends']:,.2f}{unit}")
    if abs(detail["fees"]) >= performance.FLAT_EPSILON:
        events.append(f"- Fees: {detail['fees']:,.2f}{unit} (already inside the day's P&L)")
    if events:
        st.markdown("**Cash events**\n" + "\n".join(events))
    if not trades.empty:
        st.markdown("**Trades**")
        st.dataframe(trades, width="stretch", hide_index=True)


def _render_perf_month(*, weeks: list[dict], currency: str | None, dark: bool) -> None:
    """The month as a grid of day tiles; a day with a close opens its details."""
    st.html(perf_calendar.legend_html(dark=dark))
    names = (*perf_calendar.WEEKDAY_NAMES, "Week")
    with st.container(gap="xsmall"):
        for col, name in zip(st.columns(len(names), gap="xsmall"), names):
            col.html(perf_calendar.weekday_head_html(name=name, dark=dark))
        for index, week in enumerate(weeks):
            vertical = "up" if index >= max(len(weeks) - perf_calendar.PANEL_UP_ROWS, 1) else ""
            cols = st.columns(len(names), gap="xsmall")
            for column, cell in enumerate(week["days"]):
                horizontal = "right" if column >= perf_calendar.PANEL_RIGHT_FROM else ""
                tile = perf_calendar.day_tile_html(
                    cell=cell, currency=currency, dark=dark,
                    position=f"{vertical} {horizontal}".strip(),
                )
                if not perf_calendar.has_result(cell=cell):
                    cols[column].html(tile)
                    continue
                day = cell["date"].isoformat()
                suffix = day.replace("-", "_")
                with cols[column].container(key=f"{perf_calendar.DAY_KEY_PREFIX}{suffix}"):
                    st.html(tile)
                    st.button(
                        f"Open {perf_calendar.fmt_date(date=cell['date'])}",
                        key=f"{perf_calendar.DAY_OPEN_KEY_PREFIX}{suffix}",
                        on_click=_open_perf_day, kwargs={"day": day},
                    )
            cols[-1].html(perf_calendar.week_tile_html(summary=week["summary"], dark=dark))


def _render_perf_rows(*, months: list[dict], with_data: set[str], dark: bool) -> None:
    """One row per month of the period; a row with data opens its month when clicked."""
    st.html(perf_calendar.rows_legend_html(dark=dark))
    scale = perf_calendar.period_scale(months=months)
    for month in months:
        key = _month_key(year=month["year"], month=month["month"])
        suffix = key.replace("-", "_")
        with st.container(key=f"{perf_calendar.ROW_KEY_PREFIX}{suffix}"):
            st.html(perf_calendar.month_row_html(month=month, scale=scale, dark=dark))
            if key in with_data:
                st.button(
                    f"Open {_month_label(key)}", key=f"{perf_calendar.OPEN_KEY_PREFIX}{suffix}",
                    on_click=_open_perf_month, kwargs={"month_key": key},
                )


def _perf_day_text(*, day: dict | None) -> tuple[str, str | None]:
    """(value, help) for a best / worst day metric."""
    if day is None:
        return "—", None
    unit = perf_calendar.fmt_money(value=day["pnl"])
    return perf_calendar.fmt_pct(ret=day["ret"]), f"{perf_calendar.fmt_date(date=day['date'])}: {unit}"


_FEE_LABELS = {
    performance.FEE_TRADE: ("Trades", "commission on a stock or fund order"),
    performance.FEE_FX: ("FX", "commission on a currency conversion, e.g. USD.ILS"),
    performance.FEE_OTHER: ("Other", "broker fee or commission adjustment"),
}


def _perf_fee_text(*, split: dict[str, dict[str, float | int]]) -> tuple[str | None, str]:
    """(split line, help) for the Fees metric. The line names each kind that
    charged something, e.g. ``Trades 125.00 · FX 31.69``."""
    charged = [kind for kind in performance.FEE_KINDS if split[kind]["count"]]
    line = " · ".join(
        f"{_FEE_LABELS[kind][0]} {split[kind]['amount']:,.2f}" for kind in charged
    ) or None
    lines = [
        f"- {_FEE_LABELS[kind][0]}: {split[kind]['amount']:,.2f} from {split[kind]['count']} "
        f"charge{'s' if split[kind]['count'] != 1 else ''} ({_FEE_LABELS[kind][1]})"
        for kind in charged
    ]
    help_text = ("Trade commissions and broker fees charged in the period. They are already "
                 "inside the profit / loss, not on top of it.")
    if lines:
        help_text = f"{help_text}\n\n" + "\n".join(lines)
    return line, help_text


def _perf_metrics(
    *, summary: dict, fees: float, fee_split: dict[str, dict[str, float | int]], currency: str | None,
) -> None:
    cols = st.container(key=perf_calendar.METRICS_KEY).columns(6)
    cols[0].metric(
        "Return", perf_calendar.fmt_pct(ret=summary["ret"]), border=True,
        help="Daily returns linked together (time-weighted), so deposits do not count as gains.",
    )
    pnl = summary["pnl"]
    # Currency goes in the label: in the value it gets cut off at this width.
    cols[1].metric(
        f"P&L ({currency})" if currency else "P&L",
        perf_calendar.fmt_money(value=pnl),
        border=True, help="Change in net asset value, minus deposits and plus withdrawals.",
    )
    for col, label, key in ((cols[2], "Best day", "best"), (cols[3], "Worst day", "worst")):
        value, date = _perf_day_text(day=summary[key])
        col.metric(label, value, delta=date and date.split(":")[0], delta_color="off",
                   delta_arrow="off", border=True, help=date)
    cols[4].metric(
        "Up days", f"{summary['up_days']} / {summary['days']}" if summary["days"] else "—",
        border=True, help="Days that closed with a gain, out of the days with a close.",
    )
    fee_line, fee_help = _perf_fee_text(split=fee_split)
    cols[5].metric(
        f"Fees ({currency})" if currency else "Fees", f"{fees:,.2f}", delta=fee_line,
        delta_color="off", delta_arrow="off", border=True, help=fee_help,
    )


_PERF_TABLE_INFO = (
    "One row per trading day, newest first. Each row compares the account at that "
    "day's close with the close before it.\n"
    "- **Account value**: cash plus holdings at the day's close.\n"
    "- **P&L**: what the account gained or lost that day, with deposits and "
    "withdrawals taken out, so adding money is not a gain.\n"
    "- **Return**: the P&L as a percent of the previous close plus that day's deposit.\n"
    "- **Deposits**: money added (+) or taken out (−) that day.\n"
    "- **Fees**: commissions and broker fees charged that day. They are already "
    "inside the P&L.\n"
    "\nAn empty cell means none that day. The first day has no close before it, so "
    "its P&L and Return are empty."
)


def _perf_table(
    *, daily: pd.DataFrame, fees: dict[dt.date, float], start: dt.date, end: dt.date,
) -> pd.DataFrame:
    """The period's days as a plain table, newest first: the same numbers as the
    calendar. Deposits and fees are None (an empty cell) on days without one."""
    rows = daily[(daily["date"] >= start) & (daily["date"] <= end)].iloc[::-1]

    def blank_zero(value: float) -> float | None:
        return round(value, 2) if abs(value) >= performance.FLAT_EPSILON else None

    return pd.DataFrame({
        "Date": pd.to_datetime(rows["date"]).to_list(),
        "Account value": rows["nav"].round(2).to_list(),
        "P&L": pd.to_numeric(rows["pnl"]).round(2).to_list(),
        "Return": (pd.to_numeric(rows["ret"]) * 100).round(2).to_list(),
        "Deposits": [blank_zero(float(flow)) for flow in rows["flow"]],
        "Fees": [blank_zero(fees.get(d, 0.0)) for d in rows["date"]],
    })


def _perf_table_config(*, currency: str | None) -> dict:
    """Column formats and hover help for ``_perf_table``. The currency goes in
    the money headers."""
    unit = f" ({currency})" if currency else ""
    return {
        "Date": st.column_config.DateColumn(
            "Date", format="ddd DD-MM-YYYY", help="The trading day. Monday is measured from Friday."),
        "Account value": st.column_config.NumberColumn(
            f"Account value{unit}", format="%.2f", help="Cash plus holdings at the day's close."),
        "P&L": st.column_config.NumberColumn(
            f"P&L{unit}", format="%+.2f",
            help="Gain or loss since the previous close, deposits and withdrawals taken out."),
        "Return": st.column_config.NumberColumn(
            "Return", format="%+.2f%%",
            help="P&L as a percent of the previous close plus the day's deposit."),
        "Deposits": st.column_config.NumberColumn(
            f"Deposits{unit}", format="%+.2f",
            help="Money added (+) or taken out (−) that day. Empty: none."),
        "Fees": st.column_config.NumberColumn(
            f"Fees{unit}", format="%.2f",
            help="Commissions and broker fees that day, already inside the P&L. Empty: none."),
    }


def tab_performance(*, data: dict) -> None:
    st.subheader("Daily performance")

    stored = snapshots.read_equity_daily(db_path=settings.db_path)
    nav_history = snapshots.read_history(settings.db_path)
    daily = performance.daily(
        closes=performance.closes(equity=data.get("equity"), stored=stored, nav_history=nav_history),
        flows=performance.flows_by_day(cash_tx=data.get("cash_tx")),
    )
    months = performance.months(daily=daily)
    if len(daily) < 2 or not months:
        _close_perf_day()   # a flag with no dialog would block the watchlist's auto update
        st.info("Not enough daily closes yet. The calendar needs at least two trading days "
                "of net asset value from IBKR.")
        return

    with_data = {_month_key(year=year, month=month) for year, month in months}
    options = _month_options(months=months)
    st.html(perf_calendar.styles_html())

    # Period: a two-handle slider over the months, arrows that slide the whole
    # period by a month, and quick periods. Callbacks set the slider before the
    # next run; no st.rerun() (see render_sidebar).
    period = st.session_state[_PERF_PERIOD_KEY] if _PERF_PERIOD_KEY in st.session_state else None
    # A new session (or a period the data no longer covers) opens on everything.
    if period is None or period[0] not in options or period[1] not in options:
        period = _preset_period(options=options, preset="All")
        # Mark All itself: with a year of data or less, shorter quick periods
        # are the same months and would otherwise take the mark.
        st.session_state[_PERF_PRESET_KEY] = "All"
    start_key, end_key = period
    if len(options) > 1:
        older, slider, newer = st.columns([0.6, 10, 0.6], vertical_alignment="bottom")
        seq = st.session_state[_PERF_SLIDER_SEQ_KEY] if _PERF_SLIDER_SEQ_KEY in st.session_state else 0
        start_key, end_key = slider.select_slider(
            "Period", options=options, value=period, format_func=_month_label,
            key=f"{_PERF_SLIDER_KEY_PREFIX}{seq}",
            help="Drag both ends to choose the months. One month shows its days; "
                 "several months show one row per month.",
        )
    # Remember a period dragged by hand, so the arrows move from it.
    st.session_state[_PERF_PERIOD_KEY] = (start_key, end_key)
    if len(options) > 1:
        older.button(
            "◀", key="perf_older", help="One month back", disabled=start_key == options[0],
            on_click=_shift_perf_period, kwargs={"options": options, "step": -1}, width="stretch",
        )
        newer.button(
            "▶", key="perf_newer", help="One month forward", disabled=end_key == options[-1],
            on_click=_shift_perf_period, kwargs={"options": options, "step": 1}, width="stretch",
        )
        # Mark the quick period that equals the period in force, whichever
        # control set it, and none when the period is a custom one.
        st.session_state[_PERF_PRESET_KEY] = _matching_preset(
            options=options, period=(start_key, end_key),
            picked=st.session_state[_PERF_PRESET_KEY] if _PERF_PRESET_KEY in st.session_state else None,
        )
        st.pills(
            "Quick period", _PERF_PRESETS, key=_PERF_PRESET_KEY, label_visibility="collapsed",
            on_change=_apply_perf_preset, kwargs={"options": options},
        )

    chosen = [tuple(int(part) for part in key.split("-"))
              for key in options[options.index(start_key):options.index(end_key) + 1]]
    start = performance.month_range(year=chosen[0][0], month=chosen[0][1])[0]
    end = performance.month_range(year=chosen[-1][0], month=chosen[-1][1])[1]
    today = dt.date.today()
    currency = data.get("account", {}).get("currency") or data.get("nav", {}).get("currency")
    symbol_days = performance.symbol_days(
        statement=data.get("symbols"), stored=snapshots.read_symbol_daily(db_path=settings.db_path),
    )
    details = performance.day_details(
        cash_tx=data.get("cash_tx"), trades=data.get("trades"),
        movers=performance.movers(symbol_days=symbol_days),
    )
    fees = performance.fees_by_day(trades=data.get("trades"), cash_tx=data.get("cash_tx"))
    dark = st.context.theme.type == "dark"

    title = _month_label(start_key)
    if end_key != start_key:
        title = f"{title} – {_month_label(end_key)} ({len(chosen)} months)"
    st.markdown(f"**{title}**")
    _perf_metrics(
        summary=performance.summary(daily=daily, start=start, end=end),
        fees=performance.fees_total(fees=fees, start=start, end=end),
        fee_split=performance.fees_split(
            trades=data.get("trades"), cash_tx=data.get("cash_tx"), start=start, end=end,
        ),
        currency=currency,
    )
    if len(chosen) == 1:
        year, month = chosen[0]
        _render_perf_month(
            weeks=performance.month_weeks(daily=daily, details=details, year=year, month=month, today=today),
            currency=currency, dark=dark,
        )
    else:
        _render_perf_rows(
            months=performance.period_months(
                daily=daily, details=details, fees=fees, months=chosen, today=today,
            ),
            with_data=with_data, dark=dark,
        )

    if _PERF_DAY_KEY in st.session_state:
        day = dt.date.fromisoformat(st.session_state[_PERF_DAY_KEY])
        rows = daily[daily["date"] == day].to_dict("records")
        if rows:
            blank = {"dividends": 0.0, "fees": 0.0}
            perf_day_dialog(
                row=rows[0], detail=details[day] if day in details else blank,
                symbols=performance.day_symbols(symbol_days=symbol_days, date=day),
                trades=_day_trades(trades=data.get("trades"), date=day), currency=currency,
            )
        else:
            _close_perf_day()

    with st.expander("How to read this calendar"):
        st.markdown(
            "- Each cell is one trading day: the **percent** is that day's return, the "
            "amount under it is the profit or loss in the account's base currency.\n"
            "- A day is measured from the previous close to its own close. Monday is "
            "measured from Friday.\n"
            "- **Deposits and withdrawals are taken out**, so adding money never looks "
            "like a gain. A ring in the corner marks those days.\n"
            "- Dividends and interest are part of the return.\n"
            "- **Fees** are trade commissions and broker fees. They are already inside "
            "the profit or loss; the Fees figure only shows how much they were.\n"
            "- **Colour** shows direction (green up, red down) and strength: four steps at "
            "0.25%, 0.75% and 1.5%. The arrow and the sign say the same thing without colour.\n"
            "- **Week**, month and year figures link the daily returns together; they are "
            "not a sum of the percents.\n"
            "- **No data**: a weekday with no close from IBKR. The next day's figure then "
            "covers both days. **Pending**: no close yet (today). **Market closed**: a US "
            "market holiday; the next day is measured from the close before it.\n"
            "- Hover a day for the close, the best and worst ticker of the day (by price "
            "change), the deposit, dividends received, fees and trades. **Click a day** for "
            "its details: P&L by ticker and the day's trades.\n"
            "- **Period**: drag the slider's two ends, use ◀ ▶ to move the period by a "
            "month, or pick a quick period. Several months show one row per month: "
            "the line is the return since the month's start, day by day. Each day sits at its "
            "date, so the rows line up, and all rows share one scale. Click a row to open "
            "that month."
        )
    with st.expander("Daily table"):
        st.markdown(_PERF_TABLE_INFO)
        st.dataframe(_perf_table(daily=daily, fees=fees, start=start, end=end),
                     column_config=_perf_table_config(currency=currency),
                     width="stretch", hide_index=True)


# ---- net worth --------------------------------------------------------------

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


# ---- connect / reconnect IBKR ------------------------------------------------

# Current IBKR Client Portal path to the Flex Web Service token page
# (ibkrguides.com, "Configure Flex Web Service").
_IBKR_TOKEN_PATH = "Performance & Reports → Flex Queries → Flex Web Service Configuration"

_SAFE_DATA = "Your data on this computer is safe, and Net Worth still works."

# Card text per state: None = credentials missing, else the IBKR Flex error code.
_CONNECT_CARD = {
    None: (
        "Connect your IBKR account",
        "The dashboard reads your portfolio from IBKR with a Flex Web Service "
        "token. It takes about 2 minutes.",
        "Connect IBKR account",
    ),
    flex.TOKEN_EXPIRED: (
        "Your IBKR token has expired",
        "IBKR tokens stop working after the period chosen when they were "
        f"created. Enter a new token to load fresh data. {_SAFE_DATA}",
        "Enter new token",
    ),
    flex.TOKEN_INVALID: (
        "IBKR did not accept your token",
        "The token was probably replaced by a newer one, or copied with a typo. "
        f"Enter your current token. {_SAFE_DATA}",
        "Enter new token",
    ),
}

_CONNECT_STEPS = (
    f"1. In IBKR, open **{_IBKR_TOKEN_PATH}**.\n"
    "2. In **Should Expire After**, pick the longest period (up to 1 year). "
    "The default is only 6 hours.\n"
    "3. Click **Generate New Token** and copy it now. IBKR shows it only once.\n"
    "4. Paste it below and click **Save and connect**."
)

# Why a "Yodlee" token is the wrong credential: IBKR lists Yodlee under
# Third-Party Services; its token + query ID belong to the Yodlee aggregator and
# the query is defined by IBKR, so it lacks the sections parser.py reads.
_WHICH_TOKEN = (
    "Use the token from **Flex Web Service Configuration**. Do not use the token "
    "next to **Yodlee** under Third-Party Services. That one is for the Yodlee "
    "aggregator, and its query is reported to hold only cash balances. This app "
    "needs your own Flex Query, which lists your positions, trades and cash."
)

_SIDEBAR_TOKEN_STATUS = {flex.TOKEN_EXPIRED: "token expired", flex.TOKEN_INVALID: "token not accepted"}

_CONNECT_OPEN = "connect_dialog_open"
_CONNECT_SAVED = "connect_saved"


def _close_connect_dialog() -> None:
    """on_dismiss callback: the user closed the dialog with X, Esc or a click outside."""
    if _CONNECT_OPEN in st.session_state:
        del st.session_state[_CONNECT_OPEN]


@st.dialog("Connect IBKR", width="medium", on_dismiss=_close_connect_dialog)
def connect_dialog(*, query_id_missing: bool) -> None:
    """Steps + token (and query ID when missing) + Save/Cancel.

    Errors render in a slot directly under their field. The token is never
    logged or echoed back; it only goes to .env via save_env_values.
    """
    st.markdown(_CONNECT_STEPS)
    token = st.text_input(
        "Flex Web Service token",
        type="password",
        placeholder="Paste the long number from IBKR",
        key="connect_token",
    )
    token_slot = st.empty()
    token_slot.caption("Use the Flex Web Service token, not the Yodlee token.")
    query_id = ""
    query_slot = None
    if query_id_missing:
        query_id = st.text_input(
            "Flex Query ID",
            placeholder="e.g. 1537307",
            help="The short number shown in your Flex Query's details in IBKR.",
            key="connect_query_id",
        )
        query_slot = st.empty()
        query_slot.caption("Numbers only.")
    with st.expander("Which token do I need?"):
        st.markdown(_WHICH_TOKEN)

    save_col, cancel_col = st.columns([1, 1])
    save = save_col.button("Save and connect", type="primary", key="connect_save", width="stretch")
    cancel = cancel_col.button("Cancel", key="connect_cancel", width="stretch")
    if cancel:
        del st.session_state[_CONNECT_OPEN]
        st.rerun()
    if not save:
        return

    token = token.strip()
    query_id = query_id.strip()
    errors = validate_credentials(token=token, query_id=query_id, query_id_missing=query_id_missing)
    if "token" in errors:
        token_slot.markdown(f":red[{errors['token']}]")
    if "query_id" in errors:
        query_slot.markdown(f":red[{errors['query_id']}]")
    if errors:
        return

    values = {"FLEX_TOKEN": token}
    if query_id_missing:
        values["FLEX_QUERY_ID"] = query_id
    save_env_values(env_path=ENV_PATH, values=values)
    del st.session_state[_CONNECT_OPEN]
    if _FLEX_FAILED in st.session_state:
        del st.session_state[_FLEX_FAILED]
    st.session_state[_CONNECT_SAVED] = True
    st.cache_data.clear()
    st.rerun()


def render_connect_card(*, code: str | None, query_id_missing: bool) -> None:
    """One card with the state's heading, one sentence and the primary action.

    Only called when the credential is missing (``code`` None) or IBKR rejected
    the token (1012 / 1015), so the button never shows while the app works.
    """
    heading, body, action = _CONNECT_CARD[code]
    with st.container(border=True):
        st.subheader(heading)
        st.write(body)
        if code is not None:
            st.caption(f"IBKR error {code}")
        if st.button(action, type="primary", key="connect_open"):
            st.session_state[_CONNECT_OPEN] = True
    if _CONNECT_OPEN in st.session_state:
        connect_dialog(query_id_missing=query_id_missing)


def render_net_worth_or_hint() -> None:
    """Net Worth from local history on the connect screens; a one-line hint
    instead of empty controls when there is no history yet."""
    if snapshots.read_history(settings.db_path).empty:
        st.caption("Net Worth appears here after the first successful data refresh.")
        return
    tab_net_worth()


# ---- DB sync report ---------------------------------------------------------

def _sync_headline(*, report: dict) -> tuple[str, str]:
    """(level, text) for the sidebar line: success / warning / error."""
    if report["error"]:
        return "error", f"DB sync: {report['error']}"
    diffs = sync.differences(report=report)
    added = report["written"]
    text = f"DB sync: added {added:,}" if added else "DB sync: up to date"
    if diffs:
        return "warning", f"{text}, {diffs:,} differences"
    return "success", text


def render_sync_status(*, report: dict) -> None:
    level, text = _sync_headline(report=report)
    getattr(st.sidebar, level)(text)


def _sync_rows(*, items: list[dict], extra: list[str]) -> pd.DataFrame:
    # Extra columns mix text, dates and numbers across rows; as text they render
    # in one Arrow column.
    cols = ["date", "symbol", "type", "amount", *extra]
    rows = [
        {c: (str(item.get(c)) if c in extra else item.get(c)) for c in cols} for item in items
    ]
    return pd.DataFrame(rows, columns=cols)


def render_sync_details(*, report: dict) -> None:
    """What IBKR had that the DB was missing, what was written, what disagrees."""
    with st.expander("DB sync details"):
        window = report["window"]
        requested = (
            f"{window['requested_from']} → {window['requested_to']}"
            if window["requested_from"] else "query period"
        )
        st.caption(
            f"IBKR window: {window['returned_from'] or '—'} → {window['returned_to'] or '—'} "
            f"({window['days'] or '—'} days; requested: {requested}). "
            f"Written this load: {report['written']:,}."
        )
        if report["error"]:
            st.error(report["error"])
        if not report["tables"]:
            return
        summary = pd.DataFrame([
            {"table": name, "missing (added)": len(part["missing"]),
             "empty fields (filled)": len(part["fills"]), "differences (kept DB)": len(part["diffs"])}
            for name, part in report["tables"].items()
        ])
        st.dataframe(summary, width="stretch", hide_index=True)
        for name, part in report["tables"].items():
            if part["missing"]:
                st.markdown(f"**{name}: rows added**")
                st.dataframe(_sync_rows(items=part["missing"], extra=[]), width="stretch", hide_index=True)
            if part["fills"]:
                st.markdown(f"**{name}: empty fields filled**")
                st.dataframe(_sync_rows(items=part["fills"], extra=["column", "value"]),
                             width="stretch", hide_index=True)
            if part["diffs"]:
                st.markdown(f"**{name}: IBKR differs from DB (DB value kept)**")
                st.dataframe(_sync_rows(items=part["diffs"], extra=["column", "db", "ibkr"]),
                             width="stretch", hide_index=True)


# ---- main -----------------------------------------------------------------

_RETRY_HINT = ("IBKR is not asked again automatically. Click **Refresh data** in the sidebar "
               "to retry.")


def _render_offline_tabs(*, watch_state: dict) -> None:
    """Without IBKR data the watchlist (yfinance only) and Net Worth (local
    history) still work."""
    empty = pd.DataFrame()
    watch_tab, net_worth = st.tabs(["Watchlist", "Net Worth"], key="tabs_offline")
    with watch_tab:
        tab_watchlist(state=watch_state, positions=empty, cash_tx=empty, trades=empty,
                      connected=False)
    with net_worth:
        render_net_worth_or_hint()


def main() -> None:
    sidebar_status = render_sidebar()
    st.title("📈 IBKR Dashboard")
    if _CONNECT_SAVED in st.session_state:
        del st.session_state[_CONNECT_SAVED]
        st.success("Connected to IBKR.")
    if "wl_toast" in st.session_state:
        st.toast(st.session_state["wl_toast"])
        del st.session_state["wl_toast"]

    watch_state = _watchlist_state()
    # Order matters: the tick must find this run's bucket, and must not rerun
    # while this (full) run is still rendering.
    st.session_state["wl_bucket_seen"] = watch_state["bucket"]
    st.session_state["wl_tick_armed"] = False
    st.fragment(_watchlist_tick, run_every=watchlist.refresh_seconds(
        setting=watch_state["setting"], now=watch_state["now"]))()

    if not settings.is_configured:
        if _FLEX_FAILED in st.session_state:
            del st.session_state[_FLEX_FAILED]
        _close_perf_day()
        sidebar_status.warning("IBKR: not connected")
        _render_watchlist_alerts(state=watch_state, tab_key=None)
        render_connect_card(code=None, query_id_missing=not is_real_value(settings.flex_query_id))
        st.caption("Save a token above to connect.")
        _render_offline_tabs(watch_state=watch_state)
        return

    data = None
    if _FLEX_FAILED not in st.session_state:
        try:
            data = load_portfolio(
                token=settings.flex_token, query_id=settings.flex_query_id,
                db_path_str=str(settings.db_path), session=session_key(),
            )
        except flex.FlexError as exc:
            msg = str(exc)
            # ibflex embeds the token in request URLs, and the raised FlexError
            # text is intentionally unscrubbed — never keep or show the token.
            if settings.flex_token:
                msg = msg.replace(settings.flex_token, "***")
            st.session_state[_FLEX_FAILED] = {"code": exc.code, "message": msg}
    if data is None:
        failed = st.session_state[_FLEX_FAILED]
        _close_perf_day()
        _render_watchlist_alerts(state=watch_state, tab_key=None)
        if failed["code"] in flex.TOKEN_ERROR_CODES:
            sidebar_status.error(f"IBKR: {_SIDEBAR_TOKEN_STATUS[failed['code']]}")
            render_connect_card(code=failed["code"], query_id_missing=False)
            st.caption("Save a token above to connect.")
        else:
            sidebar_status.error("IBKR: download failed")
            st.error(failed["message"])
            st.info(f"Fix the token/query in `.env` if needed. {_RETRY_HINT}")
        # Watchlist and Net Worth still work — keep them reachable while the
        # credential is being fixed.
        st.sidebar.caption("DB sync: not checked (pull failed)")
        _render_offline_tabs(watch_state=watch_state)
        return

    sidebar_status.success("IBKR: connected")
    render_sync_status(report=data["sync"])
    _render_alert_bell(state=watch_state)
    overview, positions, trades, price, watch, divs, perf, net_worth = st.tabs(
        ["Overview", "Positions", "Trades", "Prices", "Watchlist", "Dividends", "Performance",
         "Net Worth"], default=_tab_jump()["label"], key=_tabs_key(), on_change="rerun",
    )
    with overview:
        tab_overview(data, watch_state=watch_state)
    with positions:
        tab_positions(data)
    with trades:
        tab_trades(data)
    with price:
        tab_prices(data)
    with watch:
        tab_watchlist(state=watch_state, positions=data["positions"], cash_tx=data["cash_tx"],
                      trades=data["trades"], connected=True)
    with divs:
        tab_dividends(data)
    with perf:
        tab_performance(data=data)
    with net_worth:
        tab_net_worth(data.get("cash_tx"))
        render_sync_details(report=data["sync"])


if __name__ == "__main__":
    main()
