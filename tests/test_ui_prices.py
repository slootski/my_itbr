"""UI-level tests for the Prices tab KPI formatting.

IBKR supports fractional shares, so the "Position" metric must not round a real
sub-share holding down to "0" — while whole lots must still read as plain
integers, with no trailing ".0000".
"""
from __future__ import annotations

import pytest

import app


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.2596, "0.2596"),      # fractional LLY-style holding — must not read "0"
        (120.0, "120"),          # whole lot stays an integer
        (120, "120"),            # int input, not just float
        (1234.5, "1,234.5"),     # thousands separator, no padding zeros
        (-3.25, "-3.25"),        # short position
    ],
)
def test_fmt_qty(value, expected):
    assert app._fmt_qty(value) == expected


@pytest.mark.parametrize("value", [None, float("nan")])
def test_fmt_qty_missing(value):
    assert app._fmt_qty(value) == "—"
