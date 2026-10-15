"""US stock market (NYSE) holidays: weekdays the exchange is closed.

IBKR's equity summary still carries a row on such a day, with NAV moved only by
interest accrual, so the performance calendar uses this list to tell a closed
market apart from a trading day. The rules are the exchange's regular ones;
one-off closures (a day of mourning, a storm) are not in it.
"""
from __future__ import annotations

import datetime as dt
import functools

from dateutil.easter import easter

_MON, _THU, _FRI, _SAT, _SUN = 0, 3, 4, 5, 6
# The exchange has closed for Juneteenth since 2022.
_JUNETEENTH_FROM = 2022


def _nth_weekday(*, year: int, month: int, weekday: int, n: int) -> dt.date:
    """The ``n``-th ``weekday`` of the month (``n`` from 1)."""
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(*, year: int, month: int, weekday: int) -> dt.date:
    """The last ``weekday`` of the month."""
    last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(*, date: dt.date) -> dt.date:
    """A fixed-date holiday on a Saturday is kept the Friday before, on a Sunday the Monday after."""
    if date.weekday() == _SAT:
        return date - dt.timedelta(days=1)
    if date.weekday() == _SUN:
        return date + dt.timedelta(days=1)
    return date


@functools.cache
def holidays(*, year: int) -> frozenset[dt.date]:
    """The weekdays of ``year`` the exchange is closed for a holiday."""
    days = {
        _nth_weekday(year=year, month=1, weekday=_MON, n=3),     # Martin Luther King Jr. Day
        _nth_weekday(year=year, month=2, weekday=_MON, n=3),     # Washington's Birthday
        easter(year) - dt.timedelta(days=2),                     # Good Friday
        _last_weekday(year=year, month=5, weekday=_MON),         # Memorial Day
        _observed(date=dt.date(year, 7, 4)),                     # Independence Day
        _nth_weekday(year=year, month=9, weekday=_MON, n=1),     # Labor Day
        _nth_weekday(year=year, month=11, weekday=_THU, n=4),    # Thanksgiving
        _observed(date=dt.date(year, 12, 25)),                   # Christmas
    }
    # New Year's Day on a Saturday is not made up on the Friday before: that
    # Friday is the last trading day of the old year.
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != _SAT:
        days.add(_observed(date=new_year))
    if year >= _JUNETEENTH_FROM:
        days.add(_observed(date=dt.date(year, 6, 19)))
    return frozenset(days)


def is_holiday(*, date: dt.date) -> bool:
    """True on a weekday the exchange is closed for a holiday."""
    return date in holidays(year=date.year)
