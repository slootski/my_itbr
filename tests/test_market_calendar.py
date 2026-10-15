"""Tests for data.market_calendar — NYSE holidays."""
from __future__ import annotations

import datetime as dt

import pytest

from data import market_calendar

D = dt.date


@pytest.mark.parametrize("year, expected", [
    # The exchange's published lists.
    (2025, [D(2025, 1, 1), D(2025, 1, 20), D(2025, 2, 17), D(2025, 4, 18), D(2025, 5, 26), D(2025, 6, 19),
            D(2025, 7, 4), D(2025, 9, 1), D(2025, 11, 27), D(2025, 12, 25)]),
    (2026, [D(2026, 1, 1), D(2026, 1, 19), D(2026, 2, 16), D(2026, 4, 3), D(2026, 5, 25), D(2026, 6, 19),
            D(2026, 7, 3), D(2026, 9, 7), D(2026, 11, 26), D(2026, 12, 25)]),
    (2027, [D(2027, 1, 1), D(2027, 1, 18), D(2027, 2, 15), D(2027, 3, 26), D(2027, 5, 31), D(2027, 6, 18),
            D(2027, 7, 5), D(2027, 9, 6), D(2027, 11, 25), D(2027, 12, 24)]),
])
def test_holidays_match_the_exchange_calendar(year, expected):
    assert sorted(market_calendar.holidays(year=year)) == expected


def test_new_year_on_a_saturday_is_not_made_up_on_the_friday_before():
    """1 Jan 2022 was a Saturday; Fri 31 Dec 2021 was a trading day."""
    assert not market_calendar.is_holiday(date=D(2021, 12, 31))
    assert D(2021, 12, 31) not in market_calendar.holidays(year=2022)


def test_juneteenth_only_from_2022():
    assert not market_calendar.is_holiday(date=D(2021, 6, 18))
    assert market_calendar.is_holiday(date=D(2022, 6, 20))           # 19 Jun 2022 was a Sunday


def test_every_holiday_is_a_weekday():
    for year in range(2020, 2031):
        assert all(day.weekday() < 5 for day in market_calendar.holidays(year=year))


def test_an_ordinary_trading_day_is_no_holiday():
    assert not market_calendar.is_holiday(date=D(2026, 9, 8))
