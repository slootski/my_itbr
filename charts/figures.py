"""Plotly chart builders.

Each function takes a DataFrame and returns a ``plotly.graph_objects.Figure``.
No Streamlit calls here so the builders stay testable and reusable. Functions
tolerate empty/missing data by returning an empty annotated figure.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_MARGIN = dict(l=10, r=10, t=40, b=10)


def _empty(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font=dict(size=14, color="gray"))
    fig.update_layout(margin=_MARGIN, xaxis_visible=False, yaxis_visible=False)
    return fig


def allocation_treemap(positions: pd.DataFrame) -> go.Figure:
    """Treemap of portfolio allocation by absolute market value."""
    if positions.empty or "market_value" not in positions:
        return _empty("No positions")
    df = positions.copy()
    df["abs_value"] = df["market_value"].abs()
    df = df[df["abs_value"] > 0]
    if df.empty:
        return _empty("No positions with market value")

    fig = go.Figure(go.Treemap(
        labels=df["symbol"],
        parents=[""] * len(df),
        values=df["abs_value"],
        textinfo="label+value+percent root",
        hovertemplate="<b>%{label}</b><br>Value: %{value:,.0f}<extra></extra>",
    ))
    fig.update_layout(margin=_MARGIN, title="Allocation by market value")
    return fig


def unrealized_pnl_bar(positions: pd.DataFrame) -> go.Figure:
    """Bar of unrealized P&L per symbol, green/red by sign."""
    if positions.empty or "unrealized_pnl" not in positions:
        return _empty("No P&L data")
    df = positions.dropna(subset=["unrealized_pnl"]).sort_values("unrealized_pnl")
    if df.empty:
        return _empty("No P&L data")

    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in df["unrealized_pnl"]]
    fig = go.Figure(go.Bar(x=df["symbol"], y=df["unrealized_pnl"], marker_color=colors))
    fig.update_layout(margin=_MARGIN, title="Unrealized P&L by symbol",
                      yaxis_title="P&L", xaxis_title="Symbol")
    return fig


def realized_pnl_bar(trades: pd.DataFrame) -> go.Figure:
    """Bar of realized P&L summed per symbol."""
    if trades.empty or "realized_pnl" not in trades:
        return _empty("No realized P&L")
    agg = (trades.dropna(subset=["realized_pnl"])
                 .groupby("symbol", as_index=False)["realized_pnl"].sum()
                 .sort_values("realized_pnl"))
    if agg.empty:
        return _empty("No realized P&L")

    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in agg["realized_pnl"]]
    fig = go.Figure(go.Bar(x=agg["symbol"], y=agg["realized_pnl"], marker_color=colors))
    fig.update_layout(margin=_MARGIN, title="Realized P&L by symbol",
                      yaxis_title="P&L", xaxis_title="Symbol")
    return fig


def cumulative_pnl_line(trades: pd.DataFrame) -> go.Figure:
    """Cumulative realized P&L over time."""
    if trades.empty or "realized_pnl" not in trades:
        return _empty("No realized P&L")
    df = trades.dropna(subset=["realized_pnl"]).copy()
    date_col = "datetime" if "datetime" in df and df["datetime"].notna().any() else "trade_date"
    df = df.dropna(subset=[date_col]).sort_values(date_col)
    if df.empty:
        return _empty("No dated trades")

    df["cumulative"] = df["realized_pnl"].cumsum()
    fig = go.Figure(go.Scatter(x=df[date_col], y=df["cumulative"], mode="lines+markers"))
    fig.update_layout(margin=_MARGIN, title="Cumulative realized P&L",
                      yaxis_title="Cumulative P&L", xaxis_title="Date")
    return fig


def _trade_markers(fig: go.Figure, trades: pd.DataFrame, ohlc: pd.DataFrame) -> None:
    """Buy/sell triangles on the price row, clipped to the chart's date window.

    Trades outside the plotted period are dropped so the x-axis is not stretched
    by years of old fills when the user is looking at one month.
    """
    if trades is None or trades.empty:
        return
    df = trades.copy()
    date_col = "datetime" if "datetime" in df and df["datetime"].notna().any() else "trade_date"
    if date_col not in df:
        return
    df["_when"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["_when"])
    if df.empty or "price" not in df:
        return

    # Both sides are tz-normalized before comparing — yfinance dates carry a
    # timezone while Flex trade dates do not, and mixing the two raises.
    when = pd.to_datetime(ohlc["date"], errors="coerce")
    lo, hi = when.min(), when.max()
    if pd.notna(lo) and pd.notna(hi):
        marks = df["_when"].dt.tz_localize(None) if df["_when"].dt.tz is not None else df["_when"]
        lo_n, hi_n = (t.tz_localize(None) if t.tzinfo else t for t in (lo, hi))
        df = df[(marks >= lo_n) & (marks <= hi_n)]
    if df.empty:
        return

    for side, symbol_shape, color in (("BUY", "triangle-up", "#2ca02c"),
                                      ("SELL", "triangle-down", "#d62728")):
        sub = df[df["side"].astype(str).str.upper().str.startswith(side[0])] \
            if "side" in df else df.iloc[0:0]
        if sub.empty:
            continue
        qty = sub["quantity"].abs() if "quantity" in sub else None
        fig.add_trace(go.Scatter(
            x=sub["_when"], y=sub["price"], mode="markers", name=side.title(),
            marker=dict(symbol=symbol_shape, size=11, color=color,
                        line=dict(width=1, color="#fff")),
            customdata=qty if qty is not None else None,
            # ~ trims trailing zeros, so whole lots read "10" while IBKR
            # fractional shares keep their real size instead of rounding to 0.
            hovertemplate=(f"{side.title()} @ %{{y:,.2f}}"
                           + ("<br>Qty: %{customdata:,.4~f}" if qty is not None else "")
                           + "<br>%{x|%Y-%m-%d}<extra></extra>"),
        ), row=1, col=1)


def candlestick(
    ohlc: pd.DataFrame,
    symbol: str = "",
    avg_cost: float | None = None,
    ma_windows: tuple[int, ...] = (),
    mean_close: float | None = None,
    trades: pd.DataFrame | None = None,
    show_volume: bool = True,
    *,
    buy_target: float | None,
    sell_target: float | None,
) -> go.Figure:
    """Candlestick + volume subplot from a yfinance OHLC frame.

    Optional overlays (each drawn only when its argument is supplied, so the
    caller's toggles map one-to-one onto arguments):

      ``avg_cost``    horizontal line at my per-share cost basis — candles above
                      it are profit.
      ``ma_windows``  moving-average lines, e.g. ``(20, 50)``. A window longer
                      than the data is skipped rather than drawn as a gap.
      ``mean_close``  dashed line at the period's mean close.
      ``trades``      buy/sell markers from the Flex trades frame.
      ``buy_target`` / ``sell_target``  dashed lines at my watchlist targets.
    """
    if ohlc.empty:
        return _empty(f"No price data for {symbol}" if symbol else "No price data")

    rows = 2 if (show_volume and "volume" in ohlc) else 1
    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[0.75, 0.25] if rows == 2 else [1.0],
    )
    fig.add_trace(go.Candlestick(
        x=ohlc["date"], open=ohlc["open"], high=ohlc["high"],
        low=ohlc["low"], close=ohlc["close"], name=symbol or "OHLC",
    ), row=1, col=1)

    for window, color in zip(ma_windows, ("#f2a13c", "#4c9be8", "#a970d6", "#57c7b8")):
        if "close" not in ohlc or len(ohlc) < window:
            continue
        fig.add_trace(go.Scatter(
            x=ohlc["date"], y=ohlc["close"].rolling(window).mean(),
            mode="lines", name=f"MA{window}",
            line=dict(width=1.4, color=color),
            hovertemplate=f"MA{window}: %{{y:,.2f}}<extra></extra>",
        ), row=1, col=1)

    if mean_close is not None:
        fig.add_hline(y=mean_close, line=dict(color="#8899a6", width=1, dash="dot"),
                      annotation_text=f"Period mean {mean_close:,.2f}",
                      annotation_position="top left", row=1, col=1)
    if avg_cost is not None:
        fig.add_hline(y=avg_cost, line=dict(color="#e8c33c", width=1.5, dash="dash"),
                      annotation_text=f"My cost {avg_cost:,.2f}",
                      annotation_position="bottom left", row=1, col=1)

    if buy_target is not None:
        fig.add_hline(y=buy_target, line=dict(color=_PRICE_UP, width=1.5, dash="dash"),
                      annotation_text=f"Buy {buy_target:,.2f}",
                      annotation_position="bottom right", row=1, col=1)
    if sell_target is not None:
        fig.add_hline(y=sell_target, line=dict(color=_PRICE_DOWN, width=1.5, dash="dash"),
                      annotation_text=f"Sell {sell_target:,.2f}",
                      annotation_position="top right", row=1, col=1)

    _trade_markers(fig, trades, ohlc)

    if rows == 2:
        vol_colors = [_PRICE_UP if c >= o else _PRICE_DOWN
                      for o, c in zip(ohlc["open"], ohlc["close"])]
        fig.add_trace(go.Bar(x=ohlc["date"], y=ohlc["volume"], name="Volume",
                             marker_color=vol_colors), row=2, col=1)

    show_legend = bool(ma_windows) or (trades is not None and not trades.empty)
    fig.update_layout(margin=_MARGIN, title=f"{symbol} price", showlegend=show_legend,
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                      xaxis_rangeslider_visible=False, hovermode="x unified")
    # Collapse weekends/holidays so the candles sit side by side. Applied to
    # every x-axis because shared_xaxes still gives each subplot its own axis.
    fig.update_xaxes(rangebreaks=[dict(values=_non_trading_days(dates=ohlc["date"]))])
    fig.update_yaxes(title_text="Price", row=1, col=1)
    if rows == 2:
        fig.update_yaxes(title_text="Vol", row=2, col=1)
    return fig


def _non_trading_days(dates: pd.Series) -> list[str]:
    """Calendar days between the first and last bar that have no bar.

    Weekends and exchange holidays both fall out of the data itself, so no
    exchange calendar is needed. Uses wall-clock dates (tz dropped) because
    Plotly positions dates by their wall-clock value.
    """
    days = pd.to_datetime(dates).dt.tz_localize(None).dt.normalize()
    calendar = pd.date_range(days.min(), days.max(), freq="D")
    return [d.strftime("%Y-%m-%d") for d in calendar.difference(days)]


def nav_line(history: pd.DataFrame) -> go.Figure:
    """Net liquidation value over time from SQLite snapshots."""
    if history.empty:
        return _empty("No snapshots yet — refresh to record the first one")

    df = history.copy()
    y = "net_liq" if df["net_liq"].notna().any() else "cash"
    df = df.dropna(subset=[y])
    if df.empty:
        return _empty("No NAV recorded in snapshots")

    fig = go.Figure(go.Scatter(x=df["snapshot_date"], y=df[y], mode="lines+markers",
                               fill="tozeroy", line=dict(color="#1f77b4")))
    label = "Net liquidation" if y == "net_liq" else "Cash"
    fig.update_layout(margin=_MARGIN, title=f"{label} over time",
                      yaxis_title=label, xaxis_title="Date")
    return fig


# Percent return past which a tile is already at full red / full green. Without a
# clip, one -80% position stretches the scale and flattens every other tile to
# near-neutral, which is exactly the comparison the map exists to show.
_PNL_PCT_CLIP = 30.0

# Diverging red -> neutral -> green. Zero must land on the neutral middle, so the
# scale is always applied symmetrically around 0 (cmid below), never data-fitted.
_PNL_COLORSCALE = [
    [0.0, "#b2182b"],   # -clip% and worse
    [0.5, "#e8e8e8"],   # flat
    [1.0, "#1a7f37"],   # +clip% and better
]


def profit_heatmap_treemap(
    positions: pd.DataFrame,
    dividends: dict[str, float] | None = None,
) -> go.Figure:
    """Treemap sized by portfolio weight, colored by return %.

    Two independent dimensions: tile area is the position's share of the
    portfolio (absolute market value, as in allocation_treemap), tile color is
    its percent return. A big losing position therefore stays big and turns red
    rather than shrinking.

    Percent, not dollars: size already carries the money, so coloring by dollars
    would say the same thing twice and leave small heavy losers looking neutral.

    ``dividends`` maps symbol -> net cash dividends received. When given, the
    return becomes total return (price move plus income) instead of price-only.
    That is the difference between a fair and an unfair reading of a
    distribution-heavy ETF, whose NAV bleeds by design as it pays out.
    """
    if positions.empty or "market_value" not in positions or "unrealized_pnl" not in positions:
        return _empty("No positions")

    df = positions.copy()
    df["abs_value"] = df["market_value"].abs()
    df = df[df["abs_value"] > 0]
    if df.empty:
        return _empty("No positions with market value")

    # Prefer the broker's own cost basis; derive it when the Flex Query omits the
    # column, since market value minus unrealized P&L is the same figure.
    if "cost_basis" in df:
        basis = df["cost_basis"].fillna(df["market_value"] - df["unrealized_pnl"])
    else:
        basis = df["market_value"] - df["unrealized_pnl"]
    # Absolute basis so a short's percent return follows the sign of its P&L.
    basis = basis.abs()

    pnl = df["unrealized_pnl"]
    with_income = bool(dividends)
    if with_income:
        df["divs"] = [float(dividends.get(s, 0.0) or 0.0) for s in df["symbol"]]
        pnl = pnl.fillna(0.0) + df["divs"]
    # Zero/missing basis yields no honest percentage — show the tile at neutral
    # rather than inventing a return for it.
    known = basis.notna() & (basis > 0) & pnl.notna()
    df["pnl_pct"] = (pnl / basis * 100).where(known, 0.0)
    df["pnl_total"] = pnl

    total = df["abs_value"].sum()
    df["weight_pct"] = df["abs_value"] / total * 100 if total else 0.0
    df["pct_label"] = [
        f"{v:+.1f}%" if ok else "\u2014" for v, ok in zip(df["pnl_pct"], known)
    ]

    hover_pnl = (
        "Total return: %{customdata[1]:,.0f} (%{customdata[2]})<br>"
        "&nbsp;&nbsp;price: %{customdata[4]:,.0f}<br>"
        "&nbsp;&nbsp;dividends received (net): %{customdata[5]:,.0f}"
        if with_income else
        "Unrealized P&L: %{customdata[1]:,.0f} (%{customdata[2]})"
    )
    cols = ["market_value", "pnl_total", "pct_label", "weight_pct"]
    if with_income:
        cols += ["unrealized_pnl", "divs"]

    fig = go.Figure(go.Treemap(
        labels=df["symbol"],
        parents=[""] * len(df),
        values=df["abs_value"],
        text=[
            f"{w:.1f}% \u00b7 {p}" for w, p in zip(df["weight_pct"], df["pct_label"])
        ],
        texttemplate="<b>%{label}</b><br>%{text}",
        customdata=df[cols].values,
        hovertemplate=(
            "<b>%{label}</b><br>"
            "Market value: %{customdata[0]:,.0f}<br>"
            "Weight: %{customdata[3]:.1f}%<br>"
            + hover_pnl +
            "<extra></extra>"
        ),
        marker=dict(
            colors=df["pnl_pct"],
            colorscale=_PNL_COLORSCALE,
            cmin=-_PNL_PCT_CLIP,
            cmid=0,
            cmax=_PNL_PCT_CLIP,
            showscale=True,
            colorbar=dict(title="Return %", ticksuffix="%"),
        ),
    ))
    kind = "total return (price + dividends)" if with_income else "unrealized return"
    fig.update_layout(
        margin=_MARGIN,
        title=(
            f"Profit heatmap \u2014 size = portfolio weight, color = {kind} "
            f"(capped at \u00b1{_PNL_PCT_CLIP:.0f}%)"
        ),
    )
    return fig


# Income gets its own hue. Red/green is already spoken for by price direction
# across this tab, so a third colour is what keeps "gold = dividends" readable
# instead of competing with the win/loss language.
_DIVIDEND_COLOR = "#d9a441"
_PRICE_UP = "#1a7f37"
_PRICE_DOWN = "#b2182b"
_SPLIT_ROW_PX = 28


def return_split_bar(
    positions: pd.DataFrame,
    dividends: dict[str, float] | None = None,
) -> go.Figure:
    """Diverging stacked bar splitting each holding's return into price + income.

    The heatmap blends both into one colour, which hides the thing worth seeing:
    a distribution-heavy ETF whose price return is deeply negative and whose
    income more than covers it. Here the two move in opposite directions from
    zero and the offset is visible directly.

    Only dividend payers appear — a non-payer's row would be a price bar with an
    empty income segment, which the heatmap above already says.
    """
    if (
        positions.empty
        or not dividends
        or "market_value" not in positions
        or "unrealized_pnl" not in positions
    ):
        return _empty("No dividend-paying holdings")

    df = positions.copy()
    df["divs"] = [float(dividends.get(s, 0.0) or 0.0) for s in df["symbol"]]
    df = df[df["divs"] != 0]
    if df.empty:
        return _empty("No dividend-paying holdings")

    # Same basis rule as the heatmap: broker figure first, derived when the Flex
    # Query omits the column.
    if "cost_basis" in df:
        basis = df["cost_basis"].fillna(df["market_value"] - df["unrealized_pnl"])
    else:
        basis = df["market_value"] - df["unrealized_pnl"]
    basis = basis.abs()

    df = df[basis.notna() & (basis > 0)]
    basis = basis[basis.notna() & (basis > 0)]
    if df.empty:
        return _empty("No dividend-paying holdings")

    df["price_pct"] = df["unrealized_pnl"].fillna(0.0) / basis * 100
    df["div_pct"] = df["divs"] / basis * 100
    df["total_pct"] = df["price_pct"] + df["div_pct"]
    # Worst total return first. Plotly draws the first row at the bottom of a
    # horizontal bar chart, so sorting descending puts the losers on top, where
    # the split is the whole point.
    df = df.sort_values("total_pct", ascending=False)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=df["symbol"], x=df["price_pct"], orientation="h", name="Price",
        marker_color=[_PRICE_UP if v >= 0 else _PRICE_DOWN for v in df["price_pct"]],
        customdata=df["unrealized_pnl"],
        # Pre-formatted in Python: the frontend's plotly.js drops the d3 format
        # spec from "%{x:+.2f}%" and prints the raw float instead.
        text=[f"{v:+.2f}%" for v in df["price_pct"]],
        texttemplate="%{text}",
        textposition="inside",
        insidetextanchor="middle",
        textfont=dict(color="white", size=13),
        hovertemplate="<b>%{y}</b><br>Price return: %{text} (%{customdata:,.0f})<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        y=df["symbol"], x=df["div_pct"], orientation="h", name="Dividends",
        marker_color=_DIVIDEND_COLOR,
        customdata=df["divs"],
        text=[f"{v:+.2f}%" for v in df["div_pct"]],
        texttemplate="%{text}",
        textposition="inside",
        insidetextanchor="middle",
        # Dark ink on the light gold fill; white would vanish.
        textfont=dict(color="#2b2110", size=13),
        hovertemplate="<b>%{y}</b><br>Dividends: %{text} (%{customdata:,.0f})<extra></extra>",
    ))
    # Total at the end of each bar, outside the fill, so the two segments and
    # their sum can be read without arithmetic.
    for symbol, total, price, div in zip(
        df["symbol"], df["total_pct"], df["price_pct"], df["div_pct"]
    ):
        # Anchor to whichever end the stack actually reaches, not to the sum: a
        # -44% price move plus a +50% payout nets positive while the bar still
        # extends left of zero.
        right = max(0.0, price, price + div, div)
        left = min(0.0, price, price + div, div)
        at_right = total >= 0
        fig.add_annotation(
            x=right if at_right else left, y=symbol,
            text=f"<b>{total:+.2f}%</b>",
            showarrow=False, xanchor="left" if at_right else "right",
            xshift=6 if at_right else -6, font=dict(size=13, color="#9aa0a6"),
        )
    # Extent the bars actually reach, per row, in both directions.
    reach_right = (df[["price_pct", "div_pct"]].clip(lower=0).sum(axis=1)).max()
    reach_left = (df[["price_pct", "div_pct"]].clip(upper=0).sum(axis=1)).min()
    high, low = max(0.0, float(reach_right)), min(0.0, float(reach_left))
    pad = max(6.0, (high - low) * 0.12)

    # "relative", not "stack": these segments carry mixed signs and stack mode
    # would sum a -44% price move and a +50% payout into a single +6% bar,
    # erasing exactly the offset this chart exists to show.
    fig.update_layout(
        barmode="relative",
        # Taller top margin than _MARGIN: the title and the legend each need
        # their own band, or the legend prints across the title text.
        margin=dict(l=10, r=10, t=86, b=10),
        height=max(240, _SPLIT_ROW_PX * len(df) + 150),
        title=dict(
            text="Return split — price move vs dividends received",
            x=0, xanchor="left", y=1, yanchor="top",
            yref="container", pad=dict(t=12, l=10),
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, xanchor="left"),
        # Hide a segment's label rather than shrink it past legibility — the
        # sliver of dividend on a low payer has no room for text.
        uniformtext=dict(minsize=9, mode="hide"),
        xaxis=dict(
            title="Return on cost (%)",
            ticksuffix="%",
            # Plotly's autorange ignores annotations, so the outside totals would
            # clip at the extremes. Pad the range instead of relying on it.
            range=[low - pad, high + pad],
        ),
    )
    fig.add_vline(x=0, line_width=1, line_color="#888")
    return fig


_CONTRIBUTED_COLOR = "#4c78a8"
_EARNINGS_COLOR = "#1a7f37"
_EARNINGS_LOSS_COLOR = "#b2182b"


def nav_composition_area(
    comp: pd.DataFrame,
    currency: str | None = None,
    show_dividends: bool = False,
) -> go.Figure:
    """NAV over time, split into money paid in and money earned.

    Contributions are drawn as a filled band from zero; the NAV line sits above
    (or below) it and the gap between the two is the account's own earnings.
    Reading it: the blue band is what was deposited, the green wedge is what the
    market added on top.

    With ``show_dividends`` the earnings wedge splits again: a gold band for
    cumulative dividend income and the remainder for price movement. Dividends
    are carved *out of* earnings, not added to them — that cash is already
    inside net liquidation.

    Not a stacked area — earnings can be negative, and stacking would fold a
    loss into the contribution band instead of showing NAV dipping beneath it.
    """
    if comp is None or comp.empty:
        return _empty("No contribution history")

    losing = bool((comp["earnings"] < 0).any())
    axis_title = f"Net liquidation ({currency})" if currency else "Net liquidation"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=comp["date"], y=comp["contributed"], name="Contributed",
        mode="lines", line=dict(color=_CONTRIBUTED_COLOR, width=1.5),
        fill="tozeroy", fillcolor="rgba(76,120,168,0.55)",
        hovertemplate="%{x|%d-%m-%Y}<br>Contributed: %{y:,.0f}<extra></extra>",
    ))
    with_income = show_dividends and "dividends" in comp and comp["dividends"].any()
    if with_income:
        # Middle band: contributed capital plus the income sitting on top of it.
        # Drawn before the NAV line so each tonexty fills against its neighbour.
        fig.add_trace(go.Scatter(
            x=comp["date"], y=comp["contributed"] + comp["dividends"],
            name="Dividends", mode="lines",
            line=dict(color=_DIVIDEND_COLOR, width=1.5),
            fill="tonexty", fillcolor="rgba(217,164,65,0.55)",
            customdata=comp["dividends"],
            hovertemplate="%{x|%d-%m-%Y}<br>Dividends: %{customdata:,.0f}<extra></extra>",
        ))

    price_gain = comp["earnings"] - (comp["dividends"] if with_income else 0.0)
    fig.add_trace(go.Scatter(
        x=comp["date"], y=comp["net_liq"], name="Net liquidation",
        mode="lines", line=dict(color=_EARNINGS_COLOR, width=2),
        # Fills the band between this line and whichever band sits below it.
        fill="tonexty",
        fillcolor=("rgba(178,24,43,0.35)" if losing else "rgba(26,127,55,0.35)"),
        customdata=(
            pd.concat([comp["earnings"], price_gain], axis=1).values
            if with_income else comp["earnings"]
        ),
        hovertemplate=(
            (
                "%{x|%d-%m-%Y}<br>Net liq: %{y:,.0f}"
                "<br>Earnings: %{customdata[0]:,.0f}"
                "<br>&nbsp;&nbsp;of which price: %{customdata[1]:,.0f}<extra></extra>"
            )
            if with_income else
            (
                "%{x|%d-%m-%Y}<br>Net liq: %{y:,.0f}"
                "<br>Earnings: %{customdata:,.0f}<extra></extra>"
            )
        ),
    ))
    fig.update_layout(
        margin=dict(l=10, r=10, t=76, b=10),
        title=dict(
            text="Net worth — contributed capital vs earnings",
            x=0, xanchor="left", y=1, yanchor="top",
            yref="container", pad=dict(t=12, l=10),
        ),
        xaxis_title="Date",
        yaxis_title=axis_title,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, xanchor="left"),
        hovermode="x unified",
    )
    return fig


# Share of the value range kept free beyond the longest bar for its label.
_DAY_BAR_LABEL_PAD = 0.28


def day_symbols_bar(*, rows: list[dict], currency: str | None) -> go.Figure:
    """One day's P&L per ticker as horizontal bars, biggest gain on top.

    ``rows`` are ``performance.day_symbols`` items (``symbol``, ``pnl``, ``pct``).
    Direction is on the axis (right of zero is a gain) and in the label's sign,
    not only in the bar colour.
    """
    if not rows:
        return _empty("No per-ticker data for this day")
    ordered = sorted(rows, key=lambda row: row["pnl"])
    unit = f" {currency}" if currency else ""
    labels = [
        f"{row['pnl']:+,.2f}" + ("" if row["pct"] is None else f"  ({row['pct'] * 100:+.2f}%)")
        for row in ordered
    ]
    fig = go.Figure(go.Bar(
        x=[row["pnl"] for row in ordered], y=[row["symbol"] for row in ordered], orientation="h",
        marker_color=[_PRICE_UP if row["pnl"] >= 0 else _PRICE_DOWN for row in ordered],
        text=labels, textposition="outside", cliponaxis=False,
        hovertemplate="%{y}: %{x:+,.2f}" + unit + "<extra></extra>",
    ))
    fig.update_layout(
        margin=dict(l=10, r=90, t=10, b=10), height=max(160, 26 * len(ordered) + 60),
        xaxis_title=f"P&L{' (' + currency + ')' if currency else ''}", bargap=0.35,
    )
    # Value labels sit outside the bars; widen the axis on both sides so the
    # longest bar's label has room and never runs into the ticker names.
    low, high = min(0.0, ordered[0]["pnl"]), max(0.0, ordered[-1]["pnl"])
    pad = (high - low) * _DAY_BAR_LABEL_PAD or 1.0
    fig.update_xaxes(zeroline=True, zerolinewidth=1,
                     range=[low - pad if low < 0 else 0.0, high + pad if high > 0 else 0.0])
    return fig
