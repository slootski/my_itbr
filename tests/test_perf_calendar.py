"""Tests for charts.perf_calendar — markup of the daily performance calendar."""
from __future__ import annotations

import base64
import datetime as dt
import re

import pandas as pd
import pytest

from charts import perf_calendar
from data import performance

D = dt.date
TODAY = D(2026, 3, 10)


@pytest.fixture
def daily() -> pd.DataFrame:
    closes = pd.DataFrame(
        [{"date": date, "nav": nav} for date, nav in [
            (D(2026, 2, 27), 1000.0), (D(2026, 3, 2), 1010.0), (D(2026, 3, 3), 1222.0),
            (D(2026, 3, 4), 1222.0), (D(2026, 3, 6), 1200.0), (D(2026, 3, 9), 1300.0),
        ]],
        columns=["date", "nav"],
    )
    return performance.daily(closes=closes, flows={D(2026, 3, 3): 200.0, D(2026, 3, 7): 100.0})


@pytest.fixture
def week(daily) -> dict:
    """The week of 2 March 2026, with extras on Tuesday the 3rd."""
    best = {"symbol": "PLTR", "pct": 0.0321, "pnl": 27.47}
    worst = {"symbol": "HAFN", "pct": -0.021, "pnl": -2.86}
    details = {D(2026, 3, 3): {"dividends": 4.5, "fees": 3.5, "bought": 2, "sold": 1,
                               "best": best, "worst": worst}}
    return performance.month_weeks(daily=daily, details=details, year=2026, month=3, today=TODAY)[0]


def _tile(cell: dict) -> str:
    return perf_calendar.day_tile_html(cell=cell, currency="USD", position="", dark=False)


# ---- formatting -------------------------------------------------------------

@pytest.mark.parametrize("ret, text", [
    (0.0084, "+0.84%"), (-0.0042, "−0.42%"), (0.0, "0.00%"), (-0.00001, "0.00%"), (None, "—"),
    (0.1234, "+12.34%"),
])
def test_fmt_pct(ret, text):
    assert perf_calendar.fmt_pct(ret=ret) == text


@pytest.mark.parametrize("value, text", [
    (39.2, "+39.20"), (-1234.5, "−1,234.50"), (0.004, "0.00"), (None, "—"),
])
def test_fmt_money(value, text):
    assert perf_calendar.fmt_money(value=value) == text


# ---- month view: day tiles ---------------------------------------------------

def test_gain_tile_says_it_three_ways(week):
    """Tint, arrow and sign: direction must not depend on colour alone."""
    monday = _tile(week["days"][0])
    assert 'class="perftile gain g3"' in monday
    assert "▲" in monday and "+1.00%" in monday and "+10.00" in monday


def test_loss_tile_says_it_three_ways(week):
    friday = _tile(week["days"][4])
    assert 'class="perftile loss l4"' in friday
    assert "▼" in friday and "−1.80%" in friday and "−22.00" in friday


def test_flat_tile_has_no_tint_and_no_arrow(week):
    wednesday = _tile(week["days"][2])
    assert 'class="perftile flat"' in wednesday
    assert "0.00%" in wednesday and "▲" not in wednesday and "▼" not in wednesday


def test_no_data_pending_and_empty_tiles(daily, week):
    assert ('<div class="perftile nodata"><div class="num">5</div><div class="note">no data</div></div>'
            in _tile(week["days"][3]))
    second = performance.month_weeks(daily=daily, details={}, year=2026, month=3, today=TODAY)[1]
    assert ('<div class="perftile pending"><div class="num">10</div><div class="note">pending</div></div>'
            in _tile(second["days"][1]))
    assert '<div class="perftile empty"></div>' in _tile(second["days"][2])   # 11 Mar: the future


def test_only_days_with_a_close_open_details(daily, week):
    second = performance.month_weeks(daily=daily, details={}, year=2026, month=3, today=TODAY)[1]
    assert [perf_calendar.has_result(cell=c) for c in week["days"]] == [True, True, True, False, True]
    assert [perf_calendar.has_result(cell=c) for c in second["days"]] == [True, False, False, False, False]


def test_deposit_day_has_marker_and_says_it_is_excluded(week):
    tuesday = _tile(week["days"][1])
    assert '<span class="flow"></span>' in tuesday
    assert "<span>Deposit</span><span>200.00 USD</span>" in tuesday
    assert "not counted in the change" in tuesday
    assert '<span class="flow"></span>' not in _tile(week["days"][0])


def test_hover_panel_lists_the_days_numbers(week):
    tuesday = _tile(week["days"][1])
    assert "<b>03-03-2026</b>" in tuesday
    assert "<span>Close</span><span>1,222.00 USD</span>" in tuesday
    assert "<span>Previous close</span><span>1,010.00 USD</span>" in tuesday
    assert "<span>Change</span><span>+12.00 USD (+0.99%)</span>" in tuesday
    assert "<span>Dividends received</span><span>4.50 USD</span>" in tuesday
    assert "<span>Fees</span><span>3.50 USD</span>" in tuesday
    assert "<span>Trades</span><span>2 bought, 1 sold</span>" in tuesday
    assert "Click for the day" in tuesday


def test_hover_panel_names_the_best_and_worst_ticker(week):
    tuesday = _tile(week["days"][1])
    assert "<span>Best ticker</span><span>PLTR +3.21% (+27.47 USD)</span>" in tuesday
    assert "<span>Worst ticker</span><span>HAFN −2.10% (−2.86 USD)</span>" in tuesday
    # Best before worst, both before the cash events.
    assert tuesday.index("Best ticker") < tuesday.index("Worst ticker") < tuesday.index("Deposit")


def test_hover_panel_without_ticker_data_has_no_ticker_rows(week):
    monday = _tile(week["days"][0])
    assert "Best ticker" not in monday and "Worst ticker" not in monday
    assert "Fees" not in monday and 'class="sep"' not in monday


def test_day_with_one_rated_ticker_shows_best_only(week):
    cell = dict(week["days"][0], best={"symbol": "AAPL", "pct": 0.01, "pnl": 1.0}, worst=None)
    tile = _tile(cell)
    assert "Best ticker" in tile and "Worst ticker" not in tile
    assert tile.count('class="sep"') == 1          # no dangling rule under the last row


def test_gap_day_names_the_close_it_is_measured_from(week):
    assert "Change since 04-03-2026" in _tile(week["days"][4])


def test_panel_position_classes_are_passed_through(week):
    tile = perf_calendar.day_tile_html(cell=week["days"][4], currency="USD", position="up right", dark=False)
    assert 'class="perftile loss l4 up right"' in tile


def test_week_tile_links_the_week(daily, week):
    tile = perf_calendar.week_tile_html(summary=week["summary"], dark=False)
    assert 'class="perftile week flat"' in tile and "0.00" in tile
    empty = performance.month_weeks(daily=daily, details={}, year=2026, month=3, today=TODAY)[3]
    assert '<div class="perftile week"></div>' in perf_calendar.week_tile_html(
        summary=empty["summary"], dark=False)


def test_theme_class_follows_dark_flag(week):
    cell = week["days"][0]
    assert perf_calendar.day_tile_html(cell=cell, currency="USD", position="", dark=True).startswith(
        '<div class="perfcal-root dark">')
    assert _tile(cell).startswith('<div class="perfcal-root">')
    assert '<div class="perfhead">Mon</div>' in perf_calendar.weekday_head_html(name="Mon", dark=False)


def test_currency_is_escaped(week):
    tile = perf_calendar.day_tile_html(cell=week["days"][1], currency="<b>X", position="", dark=False)
    assert "<b>X" not in tile and "&lt;b&gt;X" in tile


def test_legend_names_every_state():
    legend = perf_calendar.legend_html(dark=False)
    for text in ("No data", "Pending", "Deposit or withdrawal", "Weekends are not shown",
                 "+1.5% or more", "−1.5% or more"):
        assert text in legend


# ---- several months: month rows --------------------------------------------------

@pytest.fixture
def year(daily) -> list[dict]:
    fees = {D(2026, 3, 3): 2.0, D(2026, 3, 31): 0.5}
    return performance.period_months(daily=daily, details={}, fees=fees, today=TODAY,
                                     months=[(2026, month) for month in range(1, 13)])


def _svg(month: dict) -> str:
    return perf_calendar.spark_svg(month=month, dark=False)[0]


def _path(svg: str, name: str) -> str:
    """The ``d`` of the line image's ``name`` path."""
    found = re.search(rf'<path class="{name}" d="([^"]*)"', svg)
    assert found, f"no {name} path in {svg}"
    return found.group(1)


def _numbers(d: str) -> list[float]:
    return [float(number) for number in re.findall(r"-?\d+(?:\.\d+)?", d)]


def _image_svg(row: str) -> str:
    """The SVG a row's line image carries."""
    found = re.search(r'<img class="spark[^"]*" src="data:image/svg\+xml;base64,([^"]+)"', row)
    assert found, f"no line image in {row}"
    return base64.b64decode(found.group(1)).decode()


def test_month_row_shows_line_and_totals(year):
    march = perf_calendar.month_row_html(month=year[2], dark=False)
    assert '<div class="name">March 2026</div>' in march
    assert march.count("<img") == 1 and 'class="sq' not in march
    assert 'alt="March 2026 running return: ends at +0.17%"' in march
    # Money nets to 0.00 while the linked return does not: the deposit on 3 Mar
    # changed the account's size between the gain and the loss.
    assert '<div class="pct">+0.17%</div><div class="money">0.00</div>' in march
    assert "<style>" not in march                          # stylesheet is drawn once, apart


def test_month_row_is_one_grid_line_in_reading_order(year):
    """Name, line, return, money, fees: one row, columns set once in the stylesheet."""
    march = perf_calendar.month_row_html(month=year[2], dark=False)
    order = [march.index(mark) for mark in ('class="name"', "<img", 'class="pct"', 'class="money"',
                                            'class="fees"')]
    assert order == sorted(order)
    assert ".perfyear .month { display: grid; grid-template-columns:" in perf_calendar.styles_html()


def test_line_is_an_image_because_st_html_removes_inline_svg(year):
    """st.html sanitizes with the HTML profile only: an inline <svg> never reaches
    the page. The line must travel as an image, and that image must be the line."""
    for dark in (False, True):
        march = perf_calendar.month_row_html(month=year[2], dark=dark)
        assert "<svg" not in march and "<path" not in march
        assert _image_svg(march) == perf_calendar.spark_svg(month=year[2], dark=dark)[0]


def test_line_image_carries_its_own_colours(year):
    """An image does not see the page's CSS variables."""
    for dark, theme in ((False, "light"), (True, "dark")):
        svg = perf_calendar.spark_svg(month=year[2], dark=dark)[0]
        assert "var(" not in svg and "class=\"spark" not in svg
        assert f'stroke="{perf_calendar.GAIN_INK[theme]}"' in svg
        assert f'stroke="{perf_calendar.LINE_STRONG[theme]}"' in svg
        assert svg.count('vector-effect="non-scaling-stroke"') == svg.count("<path") == 3
    css = perf_calendar.styles_html()
    for colours in (perf_calendar.GAIN_INK, perf_calendar.LOSS_INK, perf_calendar.LINE_STRONG):
        assert colours["light"] in css and colours["dark"] in css


def test_month_line_links_the_daily_returns(year):
    """One point per day with a return, after a start at 0; the last is the month's return."""
    line = perf_calendar.month_line(month=year[2])
    values = [value for _, value in line]
    assert values[0] == 0.0
    # 3 Mar: 12 gained on 1,010 plus the 200 deposited that day.
    assert values[1:4] == pytest.approx([0.01, 1.01 * (1 + 12 / 1210) - 1, 1.01 * (1 + 12 / 1210) - 1])
    assert len(line) == 1 + 5                               # 2, 3, 4, 6 and 9 Mar; not the 5th
    assert values[-1] == pytest.approx(year[2]["summary"]["ret"])


def test_month_line_skips_days_without_a_close(year):
    """March has closes on the 2nd, 3rd, 4th, 6th and 9th. The 5th (no data) and
    the days after the 9th take no place: five days, evenly spaced, edge to edge."""
    xs = [x for x, _ in perf_calendar.month_line(month=year[2])]
    assert xs == pytest.approx([0, 0.2, 0.4, 0.6, 0.8, 1.0])


def test_day_labels_give_each_days_own_return(year):
    """One label per day on the line, at that day's point; the text is the day's
    return, not the running one the line's height shows."""
    labels = perf_calendar.day_labels(month=year[2])
    assert [item["date"] for item in labels] == [D(2026, 3, day) for day in (2, 3, 4, 6, 9)]
    # 2 Mar +1.00%, 3 Mar 12 on 1,210, 4 Mar flat, 6 Mar 1,222 -> 1,200, and 9 Mar
    # 1,200 -> 1,300 is the 100 deposited on Saturday the 7th: no gain.
    assert [item["text"] for item in labels] == ["+1.0", "+1.0", "0.0", "−1.8", "0.0"]
    assert [item["side"] for item in labels] == ["above", "above", "above", "below", "above"]
    # Left is a percent of the image's width, margin included; the last day is its right end.
    assert [item["left"] for item in labels] == pytest.approx(
        [(x * 100 + 3) / 106 * 100 for x in (0.2, 0.4, 0.6, 0.8, 1.0)])
    assert all(0 <= item["top"] <= perf_calendar.SPARK_HEIGHT_PX for item in labels)
    # The label sits on its point: same height as the line there, scaled to pixels.
    ys = _numbers(_path(_svg(year[2]), "line"))[1::2]
    assert [item["top"] for item in labels] == pytest.approx(
        [y / 40 * perf_calendar.SPARK_HEIGHT_PX for y in ys[1:]], abs=0.01)


def test_fmt_weekday_date():
    assert perf_calendar.fmt_weekday_date(date=D(2025, 11, 13)) == "Thu 13-11-2025"


@pytest.mark.parametrize("ret, text", [
    (0.0123, "+1.2"), (-0.0042, "−0.4"), (0.0, "0.0"), (0.0004, "0.0"), (-0.0004, "0.0"), (0.105, "+10.5"),
])
def test_fmt_day_label(ret, text):
    assert perf_calendar.fmt_day_label(ret=ret) == text


def test_month_row_writes_the_day_labels_over_the_line(year):
    march = perf_calendar.month_row_html(month=year[2], dark=False)
    assert march.count('<span class="lbl ') == 5
    assert '<span class="lbl below" data-date="Fri 06-03-2026" style="left:' in march
    assert ">−1.8</span>" in march
    # Each label carries its own day for the hover text.
    assert [part.split('"')[0] for part in march.split('data-date="')[1:]] == [
        "Mon 02-03-2026", "Tue 03-03-2026", "Wed 04-03-2026", "Fri 06-03-2026", "Mon 09-03-2026"]
    june = perf_calendar.month_row_html(month=year[5], dark=False)
    assert '<div class="lbls"></div>' in june                   # no days, no labels
    css = perf_calendar.styles_html()
    # The date shows on hover; the chart is over the row's button but only a label takes the pointer.
    assert ".perfyear .lbl:hover::after { content: attr(data-date);" in css
    assert "position: relative; z-index: 6; pointer-events: none; }" in css
    assert "pointer-events: auto; cursor: default; }" in css
    # Too narrow to keep the labels apart: they are not shown.
    assert (f"@container (max-width: {perf_calendar.LABELS_MIN_WIDTH_PX}px) "
            "{ .perfyear .lbl { display: none; } }") in css


def test_month_line_starts_where_the_data_starts(year):
    """February's only close is the series' first: no return, so no line."""
    assert perf_calendar.month_line(month=year[1]) == []
    assert perf_calendar.month_line(month=year[5]) == []


def test_line_is_drawn_inside_its_box(year):
    march = _svg(year[2])
    # A margin at each side: an image clips, and the end dot is wider than the line.
    assert 'viewBox="-3 0 106 40" preserveAspectRatio="none"' in march
    line = _path(march, "line")
    assert "nan" not in line and "inf" not in line
    numbers = _numbers(line)
    xs, ys = numbers[0::2], numbers[1::2]
    assert len(xs) == 6 and xs == sorted(xs) and (xs[0], xs[-1]) == (0.0, 100.0)
    assert all(0 <= x <= 100 for x in xs) and all(0 <= y <= 40 for y in ys)
    # The line starts on the zero rule and the dot sits on its last point.
    assert ys[0] == _numbers(_path(march, "zero"))[1]
    assert _numbers(_path(march, "end"))[:2] == [xs[-1], ys[-1]]


def test_line_colour_follows_the_return_not_the_money(year):
    """March ends at +0.17% on 0.00 of money: the line ends above zero, so it is a gain."""
    assert '<img class="spark gain"' in perf_calendar.month_row_html(month=year[2], dark=False)
    assert perf_calendar.spark_svg(month=year[2], dark=False)[1] == performance.GAIN


def _one_day_month(ret: float) -> dict:
    """A March 2026 row whose only result is Monday the 2nd."""
    closes = pd.DataFrame([{"date": D(2026, 2, 27), "nav": 1000.0},
                           {"date": D(2026, 3, 2), "nav": 1000.0 * (1 + ret)}], columns=["date", "nav"])
    return performance.period_months(
        daily=performance.daily(closes=closes, flows={}), details={}, fees={}, today=D(2026, 3, 2),
        months=[(2026, 3)])[0]


@pytest.mark.parametrize("ret, kind, colours", [
    (0.02, "spark gain", perf_calendar.GAIN_INK),
    (-0.02, "spark loss", perf_calendar.LOSS_INK),
    (0.0, "spark flat", perf_calendar.INK_2),
])
def test_line_class_and_colour_per_direction(ret, kind, colours):
    month = _one_day_month(ret)
    assert f'<img class="{kind}"' in perf_calendar.month_row_html(month=month, dark=False)
    assert f'stroke="{colours["light"]}" stroke-width="2"' in _svg(month)
    assert f'stroke="{colours["dark"]}" stroke-width="2"' in perf_calendar.spark_svg(month=month, dark=True)[0]


def test_flat_month_draws_a_level_line_on_the_zero_rule():
    """No spread to scale by: the line must not divide by zero."""
    svg = _svg(_one_day_month(0.0))
    assert _numbers(_path(svg, "line"))[1::2] == [20.0, 20.0]
    assert _numbers(_path(svg, "zero"))[1] == 20.0


def test_loss_line_ends_below_the_zero_rule():
    """SVG y grows downward."""
    svg = _svg(_one_day_month(-0.02))
    assert _numbers(_path(svg, "line"))[-1] > _numbers(_path(svg, "zero"))[1]


def test_month_row_shows_the_months_fees(year):
    march = perf_calendar.month_row_html(month=year[2], dark=False)
    assert '<div class="fees"><span>Fees</span><span>2.50</span></div>' in march
    june = perf_calendar.month_row_html(month=year[5], dark=False)
    assert '<div class="fees"><span>Fees</span><span>0.00</span></div>' in june


def test_month_row_without_results(year):
    june = perf_calendar.month_row_html(month=year[5], dark=True)
    assert '<div class="perfcal-root dark">' in june
    assert "no results" in june


def test_month_without_results_has_the_zero_rule_only(year):
    june = perf_calendar.month_row_html(month=year[5], dark=False)
    svg, kind, label = perf_calendar.spark_svg(month=year[5], dark=False)
    assert '<img class="spark"' in june and 'running return: no results"' in june
    assert 'class="zero"' in svg and 'class="line"' not in svg and 'class="end"' not in svg
    assert (kind, label) == ("", "no results")


def test_month_rows_are_all_the_same_height(year):
    """Every row holds one line box, and the stylesheet gives that box a fixed height."""
    assert {perf_calendar.month_row_html(month=month, dark=False).count("<img") for month in year} == {1}
    assert (f".perfyear .spark {{ display: block; width: 100%; height: {perf_calendar.SPARK_HEIGHT_PX}px;"
            in perf_calendar.styles_html())


def test_line_carries_no_tooltip_under_the_click_target(year):
    """The whole row is one button, so a tooltip on the line could never show."""
    march = perf_calendar.month_row_html(month=year[2], dark=False)
    assert "title=" not in march and "<title" not in _image_svg(march)


def test_rows_legend_explains_the_line():
    legend = perf_calendar.rows_legend_html(dark=True)
    assert legend.startswith('<div class="perfcal-root dark">')
    for text in ("Return since the month's start", "Month's start (0%)", "each day's own return",
                 "Days without a close are skipped", "own scale"):
        assert text in legend


def test_stylesheet_targets_the_tile_row_and_button_keys():
    css = perf_calendar.styles_html()
    for prefix in (perf_calendar.DAY_KEY_PREFIX, perf_calendar.DAY_OPEN_KEY_PREFIX,
                   perf_calendar.ROW_KEY_PREFIX, perf_calendar.OPEN_KEY_PREFIX):
        assert f"st-key-{prefix}" in css
    # A day container's selector must not also catch its own button's key.
    assert not perf_calendar.DAY_OPEN_KEY_PREFIX.startswith(perf_calendar.DAY_KEY_PREFIX)


# ---- colour -----------------------------------------------------------------

def _hex(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple, b: tuple) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("kind", [performance.GAIN, performance.LOSS])
def test_text_keeps_aa_contrast_on_every_tint_step(theme, kind):
    """WCAG 1.4.3: figures (ink) and the day number / money (secondary ink) need
    4.5:1 on the tint they sit on, including the strongest step."""
    surface = _hex(perf_calendar.SURFACE[theme])
    for alpha in perf_calendar.TINT_ALPHA[theme]:
        tinted = tuple(alpha * t + (1 - alpha) * s
                       for t, s in zip(perf_calendar.TINT_RGB[kind], surface))
        for ink in (perf_calendar.INK[theme], perf_calendar.INK_2[theme]):
            assert _contrast(_hex(ink), tinted) >= 4.5, (theme, kind, alpha, ink)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tint_steps_get_stronger(theme):
    alphas = perf_calendar.TINT_ALPHA[theme]
    assert list(alphas) == sorted(alphas) and len(set(alphas)) == 4
