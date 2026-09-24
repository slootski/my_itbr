"""Tests for charts.figures — every builder returns a Figure, empty or populated."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import pytest

from charts import figures


@pytest.fixture
def positions():
    return pd.DataFrame({
        "symbol": ["AAPL", "MSFT"],
        "market_value": [1500.0, -500.0],
        "unrealized_pnl": [500.0, -100.0],
    })


@pytest.fixture
def trades():
    return pd.DataFrame({
        "symbol": ["AAPL", "AAPL", "MSFT"],
        "realized_pnl": [50.0, -20.0, 30.0],
        "datetime": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01"]),
        "trade_date": [None, None, None],
    })


@pytest.fixture
def ohlc():
    return pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
        "open": [1.0, 1.5], "high": [2.0, 2.5], "low": [0.5, 1.0],
        "close": [1.5, 2.0], "volume": [100, 200],
    })


@pytest.fixture
def history():
    return pd.DataFrame({
        "snapshot_date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
        "net_liq": [1000.0, 1100.0],
        "cash": [200.0, 210.0],
    })


EMPTY = pd.DataFrame()


def test_allocation_treemap(positions):
    assert isinstance(figures.allocation_treemap(EMPTY), go.Figure)
    assert isinstance(figures.allocation_treemap(positions), go.Figure)


def test_unrealized_pnl_bar(positions):
    assert isinstance(figures.unrealized_pnl_bar(EMPTY), go.Figure)
    assert isinstance(figures.unrealized_pnl_bar(positions), go.Figure)


def test_realized_pnl_bar(trades):
    assert isinstance(figures.realized_pnl_bar(EMPTY), go.Figure)
    assert isinstance(figures.realized_pnl_bar(trades), go.Figure)


def test_cumulative_pnl_line(trades):
    assert isinstance(figures.cumulative_pnl_line(EMPTY), go.Figure)
    assert isinstance(figures.cumulative_pnl_line(trades), go.Figure)


def test_candlestick(ohlc):
    assert isinstance(figures.candlestick(EMPTY, "AAPL"), go.Figure)
    assert isinstance(figures.candlestick(ohlc, "AAPL"), go.Figure)


@pytest.fixture
def long_ohlc():
    """30 bars — long enough for an MA20 overlay, short enough to skip MA50."""
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    close = [10.0 + i for i in range(30)]
    return pd.DataFrame({
        "date": dates,
        "open": [c - 0.5 for c in close],
        "high": [c + 1.0 for c in close],
        "low": [c - 1.0 for c in close],
        "close": close,
        "volume": [1000] * 30,
    })


@pytest.fixture
def price_trades():
    return pd.DataFrame({
        "symbol": ["AAPL", "AAPL", "AAPL"],
        "side": ["BUY", "SELL", "BUY"],
        "price": [12.0, 20.0, 15.0],
        "quantity": [10.0, -5.0, 3.0],
        # The last fill is years outside the chart window and must be clipped.
        "datetime": pd.to_datetime(["2024-01-05", "2024-01-10", "2030-01-01"]),
        "trade_date": [None, None, None],
    })


def _trace_names(fig):
    return [t.name for t in fig.data]


def test_candlestick_ma_overlay_skips_windows_longer_than_data(long_ohlc):
    fig = figures.candlestick(long_ohlc, "AAPL", ma_windows=(20, 50))
    names = _trace_names(fig)
    assert "MA20" in names
    assert "MA50" not in names  # only 30 bars


def test_candlestick_hlines_for_cost_and_mean(long_ohlc):
    fig = figures.candlestick(long_ohlc, "AAPL", avg_cost=15.0, mean_close=24.5)
    ys = {shape.y0 for shape in fig.layout.shapes}
    assert {15.0, 24.5} <= ys


def test_candlestick_trade_markers_clipped_to_window(long_ohlc, price_trades):
    fig = figures.candlestick(long_ohlc, "AAPL", trades=price_trades)
    by_name = {t.name: t for t in fig.data if t.name in ("Buy", "Sell")}
    assert set(by_name) == {"Buy", "Sell"}
    # The 2030 buy falls outside the 30-day window, so only one buy survives.
    assert len(by_name["Buy"].x) == 1
    assert len(by_name["Sell"].x) == 1


def test_candlestick_no_overlays_matches_bare_chart(long_ohlc):
    fig = figures.candlestick(long_ohlc, "AAPL")
    assert len(fig.data) == 2  # candles + volume only
    assert not fig.layout.shapes


def test_candlestick_skips_non_trading_days():
    # Fri 2024-01-12, Tue 2024-01-16 (Mon is MLK holiday), Wed 2024-01-17.
    dates = pd.to_datetime(["2024-01-12", "2024-01-16", "2024-01-17"]).tz_localize("America/New_York")
    ohlc = pd.DataFrame({"date": dates, "open": [1.0] * 3, "high": [2.0] * 3,
                         "low": [0.5] * 3, "close": [1.5] * 3, "volume": [100] * 3})
    fig = figures.candlestick(ohlc, "AAPL")
    expected = ["2024-01-13", "2024-01-14", "2024-01-15"]
    assert list(fig.layout.xaxis.rangebreaks[0].values) == expected
    assert list(fig.layout.xaxis2.rangebreaks[0].values) == expected


def test_candlestick_hides_volume_row(long_ohlc):
    fig = figures.candlestick(long_ohlc, "AAPL", show_volume=False)
    assert len(fig.data) == 1


def test_candlestick_trade_markers_tolerate_tz_aware_prices(long_ohlc, price_trades):
    """yfinance returns tz-aware dates; Flex trade dates are naive."""
    tz_ohlc = long_ohlc.copy()
    tz_ohlc["date"] = tz_ohlc["date"].dt.tz_localize("America/New_York")
    fig = figures.candlestick(tz_ohlc, "AAPL", trades=price_trades)
    assert isinstance(fig, go.Figure)


def test_candlestick_empty_trades_frame(long_ohlc):
    assert isinstance(figures.candlestick(long_ohlc, "AAPL", trades=EMPTY), go.Figure)


def test_nav_line(history):
    assert isinstance(figures.nav_line(EMPTY), go.Figure)
    assert isinstance(figures.nav_line(history), go.Figure)


def test_profit_heatmap_treemap(positions):
    assert isinstance(figures.profit_heatmap_treemap(EMPTY), go.Figure)
    fig = figures.profit_heatmap_treemap(positions)
    assert isinstance(fig, go.Figure)
    trace = fig.data[0]
    # Size stays market value: the loser must not shrink because it is a loser.
    assert list(trace.values) == [1500.0, 500.0]
    # Color scale is symmetric around zero and clipped, never data-fitted.
    assert trace.marker.cmid == 0
    assert trace.marker.cmin == -trace.marker.cmax


def test_profit_heatmap_pct_uses_cost_basis():
    df = pd.DataFrame({
        "symbol": ["WIN", "LOSE"],
        "market_value": [1200.0, 600.0],
        "cost_basis": [1000.0, 1000.0],
        "unrealized_pnl": [200.0, -400.0],
    })
    colors = list(figures.profit_heatmap_treemap(df).data[0].marker.colors)
    assert colors == pytest.approx([20.0, -40.0])


def test_profit_heatmap_derives_basis_when_column_missing():
    # Flex Query may omit costBasisMoney; market value - P&L is the same figure.
    df = pd.DataFrame({
        "symbol": ["AAPL"], "market_value": [1200.0], "unrealized_pnl": [200.0],
    })
    assert list(figures.profit_heatmap_treemap(df).data[0].marker.colors) == pytest.approx([20.0])


def test_profit_heatmap_short_return_follows_pnl_sign():
    # Short: negative market value and basis. A profitable short must read green.
    df = pd.DataFrame({
        "symbol": ["SHORT"],
        "market_value": [-800.0], "cost_basis": [-1000.0], "unrealized_pnl": [200.0],
    })
    assert list(figures.profit_heatmap_treemap(df).data[0].marker.colors) == pytest.approx([20.0])


def test_profit_heatmap_zero_basis_is_neutral_not_invented():
    df = pd.DataFrame({
        "symbol": ["FREE"], "market_value": [500.0],
        "cost_basis": [0.0], "unrealized_pnl": [500.0],
    })
    trace = figures.profit_heatmap_treemap(df).data[0]
    assert list(trace.marker.colors) == [0.0]
    assert "—" in trace.text[0]


def test_profit_heatmap_dividends_lift_return():
    # A distribution-heavy ETF bleeds NAV by design; income is what makes its
    # return readable, so the toggle must move it out of the red.
    df = pd.DataFrame({
        "symbol": ["MSTY"], "market_value": [560.0],
        "cost_basis": [1000.0], "unrealized_pnl": [-440.0],
    })
    price_only = list(figures.profit_heatmap_treemap(df).data[0].marker.colors)
    assert price_only == pytest.approx([-44.0])
    total = figures.profit_heatmap_treemap(df, dividends={"MSTY": 500.0})
    assert list(total.data[0].marker.colors) == pytest.approx([6.0])
    assert "total return" in total.layout.title.text


def test_profit_heatmap_ignores_dividends_for_unheld_symbols(positions):
    fig = figures.profit_heatmap_treemap(positions, dividends={"NOTHELD": 999.0})
    # Unknown symbols contribute nothing; known ones default to zero income.
    assert list(fig.data[0].marker.colors) == pytest.approx([50.0, -25.0])


def test_profit_heatmap_empty_dividends_stays_price_only(positions):
    fig = figures.profit_heatmap_treemap(positions, dividends={})
    assert "unrealized return" in fig.layout.title.text


@pytest.fixture
def split_positions():
    return pd.DataFrame({
        "symbol": ["MSTY", "XOM", "SPY"],
        "market_value": [560.0, 4400.0, 6500.0],
        "cost_basis": [1000.0, 3435.0, 6360.0],
        "unrealized_pnl": [-440.0, 965.0, 140.0],
    })


def test_return_split_bar_filters_non_payers(split_positions):
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0, "XOM": 134.0})
    symbols = set(fig.data[0].y)
    assert symbols == {"MSTY", "XOM"}  # SPY pays nothing here


def test_return_split_bar_keeps_mixed_signs_unsummed(split_positions):
    # A -44% price move and a +50% payout must stay two opposing segments, not
    # collapse into one +6% bar — that offset is the whole point of the chart.
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0})
    assert fig.layout.barmode == "relative"
    price, divs = fig.data
    assert price.name == "Price" and divs.name == "Dividends"
    assert list(price.x) == pytest.approx([-44.0])
    assert list(divs.x) == pytest.approx([50.0])


def test_return_split_bar_sorts_worst_total_return_on_top(split_positions):
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0, "XOM": 134.0})
    # Plotly draws the first row at the bottom, so descending order = losers up top.
    assert list(fig.data[0].y) == ["XOM", "MSTY"]


def test_return_split_bar_negative_net_dividends_not_clipped(split_positions):
    # Withholding exceeding the payment is rare but real — render it, don't hide it.
    fig = figures.return_split_bar(split_positions, {"XOM": -68.7})
    assert list(fig.data[1].x) == pytest.approx([-2.0])


def test_return_split_bar_empty_cases(split_positions):
    assert isinstance(figures.return_split_bar(EMPTY, {"X": 1.0}), go.Figure)
    for divs in ({}, None, {"NOTHELD": 10.0}):
        fig = figures.return_split_bar(split_positions, divs)
        assert not fig.data
        assert "No dividend-paying holdings" in fig.layout.annotations[0].text


def test_return_split_bar_derives_basis_when_column_missing():
    df = pd.DataFrame({
        "symbol": ["KO"], "market_value": [1200.0], "unrealized_pnl": [200.0],
    })
    fig = figures.return_split_bar(df, {"KO": 50.0})
    assert list(fig.data[0].x) == pytest.approx([20.0])
    assert list(fig.data[1].x) == pytest.approx([5.0])


def test_return_split_bar_labels_segments_and_totals(split_positions):
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0, "XOM": 134.0})
    price, divs = fig.data
    # Labels are pre-formatted in Python (the frontend's plotly.js drops d3
    # format specs from texttemplate and prints raw floats).
    assert price.texttemplate == divs.texttemplate == "%{text}"
    assert all(t.endswith("%") and t[0] in "+-" for t in price.text)
    assert all("." in t and len(t.split(".")[1]) == 3 for t in price.text)  # 2 digits + '%'
    # Unreadably small segment labels are dropped, not shrunk.
    assert fig.layout.uniformtext.mode == "hide"
    totals = {a.y: a.text for a in fig.layout.annotations}
    assert totals["MSTY"] == "<b>+6.00%</b>"   # -44% price + 50% income
    assert totals["XOM"] == "<b>+31.99%</b>"  # 18.66% price + 13.33% income


def test_return_split_bar_total_label_anchors_to_bar_end_not_sum(split_positions):
    # MSTY nets +6% but its stack still reaches +50% on the right; the label
    # belongs at the bar's end, or it prints on top of the gold segment.
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0})
    msty = next(a for a in fig.layout.annotations if a.y == "MSTY")
    assert msty.x == pytest.approx(50.0)
    assert msty.xanchor == "left"


def test_return_split_bar_range_leaves_room_for_outside_labels(split_positions):
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0, "XOM": 134.0})
    low, high = fig.layout.xaxis.range
    assert low < -44.0 and high > 50.0


def test_return_split_bar_legend_clears_the_title(split_positions):
    fig = figures.return_split_bar(split_positions, {"MSTY": 500.0})
    assert fig.layout.legend.y > 1  # above the plot area, not across the title
    assert fig.layout.title.yref == "container"
    assert fig.layout.margin.t > 60


@pytest.fixture
def composition():
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-03-01", "2026-06-01"]).date,
        "net_liq": [100.0, 470.0, 600.0],
        "contributed": [108.0, 458.0, 458.0],
        "earnings": [-8.0, 12.0, 142.0],
    })


def test_nav_composition_area_draws_both_bands(composition):
    fig = figures.nav_composition_area(composition, "USD")
    contributed, net_liq = fig.data
    assert contributed.name == "Contributed"
    assert contributed.fill == "tozeroy"
    # tonexty fills the gap to the contribution band — that gap is the earnings.
    assert net_liq.fill == "tonexty"
    assert "USD" in fig.layout.yaxis.title.text


def test_nav_composition_area_flags_a_losing_account(composition):
    losing = composition.copy()
    losing["earnings"] = [-8.0, -20.0, -50.0]
    fill = figures.nav_composition_area(losing).data[1].fillcolor
    assert "178,24,43" in fill  # red band: worth less than what was paid in


def test_nav_composition_area_empty():
    fig = figures.nav_composition_area(pd.DataFrame())
    assert not fig.data
    assert "No contribution history" in fig.layout.annotations[0].text


def test_nav_composition_area_splits_out_dividends(composition):
    comp = composition.copy()
    comp["dividends"] = [0.0, 5.0, 40.0]
    fig = figures.nav_composition_area(comp, "USD", show_dividends=True)
    assert [t.name for t in fig.data] == ["Contributed", "Dividends", "Net liquidation"]
    # The gold band sits on top of contributed capital, not on top of NAV.
    assert list(fig.data[1].y) == pytest.approx([108.0, 463.0, 498.0])
    assert list(fig.data[2].y) == pytest.approx(list(comp["net_liq"]))


def test_nav_composition_area_skips_the_band_when_no_income(composition):
    comp = composition.copy()
    comp["dividends"] = [0.0, 0.0, 0.0]
    fig = figures.nav_composition_area(comp, "USD", show_dividends=True)
    assert [t.name for t in fig.data] == ["Contributed", "Net liquidation"]
