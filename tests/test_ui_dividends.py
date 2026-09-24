"""QA frontend-pass tests (e1) — UI-level checks for the Dividends tab.

Two layers, matching the QA charter:

1. HTML rendering — drive ``app._calendar_html()`` with calendars built from
   synthetic DataFrames and a pinned ``today`` (deterministic), asserting on the
   exact markup the browser receives: "Mon YYYY" headers (D1), ``current``
   class + always-present current column (D2), ``<tfoot>`` Total row (D3),
   ``word`` cells carrying only "Declare"/"Pay" (D6), ``title=`` tooltips with
   dd-mm-yyyy dates / month-level projections (D9, accepted deviation 2), and
   net-vs-gross recompute of cells + Total (D10 recompute half).

2. AppTest widget scripting — run the real app offline (same boundary patching
   as tests/test_app_smoke.py) and script the ``div_basis`` / ``div_lookback``
   radios: labels, defaults, captions (D10 state half) and the independence of
   the two controls (D12). Through the app path ``today`` floats, so these
   tests assert widget state and captions, never cell amounts.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import app
from data import dividends
from tests.conftest import SYNTHETIC_FLEX_XML

# ---------------------------------------------------------------------------
# Layer 1 — HTML assertions on app._calendar_html (pinned today=2026-06-09)
# ---------------------------------------------------------------------------

TODAY = dt.date(2026, 6, 9)  # current month 2026-06


def _positions(*symbols: str) -> pd.DataFrame:
    return pd.DataFrame({"symbol": list(symbols)})


def _div_row(sym: str, amount: float, ex: dt.date, pay: dt.date, type_: str = "DIVIDEND") -> dict:
    return {
        "type": type_, "symbol": sym, "amount": amount,
        "settle_date": pay, "ex_date": ex, "datetime": None, "report_date": None,
    }


def _composite_calendar(net: bool = False) -> dividends.Calendar:
    """One calendar exercising every cell state at once (mirrors mock rows):

    - GRN: quarterly payer (paid 2025-09/12, 2026-03, ex 1 month before pay)
      -> red "Declare" 2026-08 + red "Pay" 2026-09 (split months, both future).
    - YEL: open accrual ex 12-05-2026, pay 06-06-2026 -> yellow in the CURRENT
      month, with a paid 2026-03 history cell (gross 100 / net 85 via WHTAX).
    """
    cash_tx = pd.DataFrame([
        _div_row("GRN", 50.0, dt.date(2025, 8, 20), dt.date(2025, 9, 15)),
        _div_row("GRN", 50.0, dt.date(2025, 11, 20), dt.date(2025, 12, 15)),
        _div_row("GRN", 50.0, dt.date(2026, 2, 20), dt.date(2026, 3, 15)),
        _div_row("YEL", 100.0, dt.date(2026, 3, 1), dt.date(2026, 3, 10)),
        _div_row("YEL", -15.0, dt.date(2026, 3, 1), dt.date(2026, 3, 10), type_="WHTAX"),
    ])
    accruals = pd.DataFrame([
        {"symbol": "YEL", "ex_date": dt.date(2026, 5, 12), "pay_date": dt.date(2026, 6, 6),
         "gross_amount": 40.0, "net_amount": 30.0, "quantity": 10, "tax": 10.0},
    ])
    return dividends.build_calendar(
        _positions("GRN", "YEL"), cash_tx, accruals, today=TODAY, net=net
    )


def _section(html_doc: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", html_doc, re.S)
    assert m, f"<{tag}> section missing from calendar HTML"
    return m.group(1)


def test_d1_month_headers_are_mon_yyyy():
    html_doc = app._calendar_html(_composite_calendar())
    head = _section(html_doc, "thead")
    labels = re.findall(r"<th[^>]*>([^<]*)</th>", head)
    assert labels[0] == "Ticker"
    month_labels = labels[1:]
    assert month_labels, "no month columns rendered"
    for lbl in month_labels:
        assert re.fullmatch(r"[A-Z][a-z]{2} \d{4}", lbl), f"bad month label: {lbl!r}"
    # chronological, and the YYYY-MM internal keys never leak into the header
    assert month_labels[0] == "Sep 2025" and month_labels[-1] == "Sep 2026"
    assert "2026-06" not in head


def test_d2_current_month_header_highlighted_exactly_once():
    html_doc = app._calendar_html(_composite_calendar())
    head = _section(html_doc, "thead")
    assert '<th class="current">Jun 2026</th>' in head
    assert head.count('class="current"') == 1  # only the current month stands out


def test_d2_current_month_column_rendered_even_when_empty():
    # History-only ticker: current month (2026-06) has no data for anyone,
    # yet the column must exist and carry the current marker in head and body.
    cash_tx = pd.DataFrame([_div_row("OLD", 5.0, dt.date(2026, 1, 5), dt.date(2026, 1, 15))])
    cal = dividends.build_calendar(_positions("OLD"), cash_tx, pd.DataFrame(), today=TODAY)
    html_doc = app._calendar_html(cal)
    assert '<th class="current">Jun 2026</th>' in html_doc
    assert '<td class="current"></td>' in _section(html_doc, "tbody")  # empty but marked
    assert '<td class="current"></td>' in _section(html_doc, "tfoot")  # Total empty too


def test_d3_total_footer_row():
    html_doc = app._calendar_html(_composite_calendar())
    foot = _section(html_doc, "tfoot")
    assert foot.startswith("<tr><th>Total</th>")
    cells = re.findall(r"<td[^>]*>([^<]*)</td>", foot)
    head_labels = re.findall(r"<th[^>]*>([^<]*)</th>", _section(html_doc, "thead"))[1:]
    by_month = dict(zip(head_labels, cells))
    assert by_month["Mar 2026"] == "150.00"   # paid 50 (GRN) + paid 100 (YEL gross)
    assert by_month["Jun 2026"] == "40.00"    # declared (yellow) counts in Total
    assert by_month["Aug 2026"] == ""         # red "Declare" month -> empty Total
    assert by_month["Sep 2026"] == ""         # red "Pay" month -> empty Total
    assert by_month["Apr 2026"] == ""         # no data -> empty, not 0.00


def test_d4_held_ticker_without_data_renders_placeholder_row():
    # NEW is held but has no dividend events: one grey full-width row, no
    # per-month cells, and it must not disturb the Total footer.
    cash_tx = pd.DataFrame([_div_row("GRN", 50.0, dt.date(2026, 2, 20), dt.date(2026, 3, 15))])
    cal = dividends.build_calendar(_positions("GRN", "NEW"), cash_tx, pd.DataFrame(), today=TODAY)
    assert cal.no_data == {"NEW"}
    html_doc = app._calendar_html(cal)
    body = _section(html_doc, "tbody")
    n_cols = len(cal.values.columns)
    assert (
        f'<tr><th>NEW</th><td class="nodata" colspan="{n_cols}">'
        "— no dividend data yet —</td></tr>" in body
    )
    assert body.count("<tr>") == 2  # GRN data row + NEW placeholder row


def test_d6_red_cells_render_words_only_never_amounts():
    for net in (False, True):  # identical in both tax modes
        html_doc = app._calendar_html(_composite_calendar(net=net))
        word_cells = re.findall(r'<td class="word(?: current)?"[^>]*>([^<]*)</td>', html_doc)
        assert sorted(word_cells) == ["Declare", "Pay"]
        for text in word_cells:
            assert not re.search(r"\d", text), f"red cell carries a number: {text!r}"


def test_d9_tooltips_title_attrs():
    html_doc = app._calendar_html(_composite_calendar())
    # Yellow declared cell: declaration + pay dates, dd-mm-yyyy, in title=.
    assert 'title="declaration: 12-05-2026\npay: 06-06-2026"' in html_doc
    # Red projected cells: estimated day from GRN's history (ex 20th, pay 15th),
    # marked as an estimate so it never reads as a confirmed date.
    assert 'title="projected declaration: ~20-08-2026 (estimated)">Declare</td>' in html_doc
    assert 'title="projected payment: ~15-09-2026 (estimated)">Pay</td>' in html_doc
    # Paid cells carry no tooltip (mock adds title only to declared/word cells).
    assert re.search(r'<td class="paid">50\.00</td>', html_doc)


def test_d9_yellow_current_month_cell_classes_and_title():
    # YEL's accrual pays in the current month: declared + current classes stack.
    html_doc = app._calendar_html(_composite_calendar())
    assert re.search(
        r'<td class="declared current" title="declaration: 12-05-2026\npay: 06-06-2026">40\.00</td>',
        html_doc,
    )


def test_d10_tax_mode_recomputes_cells_and_total_in_html():
    gross = app._calendar_html(_composite_calendar(net=False))
    net = app._calendar_html(_composite_calendar(net=True))
    # YEL paid cell: gross 100.00 -> net 85.00 (real WHTAX, accepted deviation 1)
    assert '<td class="paid">100.00</td>' in gross and "85.00" not in gross
    assert '<td class="paid">85.00</td>' in net and ">100.00</td>" not in net
    # Declared cell: accrual gross 40 -> net 30; Total row follows in both months.
    assert ">40.00</td>" in gross and ">30.00</td>" in net
    foot_gross = re.findall(r"<td[^>]*>([^<]*)</td>", _section(gross, "tfoot"))
    foot_net = re.findall(r"<td[^>]*>([^<]*)</td>", _section(net, "tfoot"))
    assert "150.00" in foot_gross and "40.00" in foot_gross
    # GRN has no WHTAX, so net applies the default 25%: 50 -> 37.50; Mar total
    # net = 37.50 + 85.00.
    assert '<td class="paid">37.50</td>' in net
    assert "122.50" in foot_net and "30.00" in foot_net
    # paid_sum headline recomputes too (full history: GRN 3x50/37.50 + YEL 100/85).
    assert _composite_calendar(net=False).paid_sum == 250.0
    assert _composite_calendar(net=True).paid_sum == 197.5


# ---------------------------------------------------------------------------
# Layer 2 — AppTest widget scripting (D10 state half, D12), fully offline
# ---------------------------------------------------------------------------

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
NET_CAPTION = "Amounts after tax (IBKR withholding per ticker; 25% where IBKR withheld nothing)."
GROSS_CAPTION = "Amounts before withholding tax (gross)."

_FAKE_OHLC = pd.DataFrame(
    {
        "date": pd.to_datetime([dt.date(2024, 12, 30), dt.date(2024, 12, 31)]),
        "open": [100.0, 101.0], "high": [102.0, 103.0],
        "low": [99.0, 100.0], "close": [101.0, 102.0], "volume": [1000, 1100],
    }
)


@pytest.fixture
def div_at(monkeypatch, tmp_path):
    """AppTest with the same offline boundaries as the S2 smoke fixture."""
    from data import prices
    from ibkr import flex

    monkeypatch.setenv("FLEX_TOKEN", "qa-ui-token")
    monkeypatch.setenv("FLEX_QUERY_ID", "424242")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "qa-ui.sqlite"))
    monkeypatch.setattr(flex, "download_statement", lambda t, q: SYNTHETIC_FLEX_XML)
    monkeypatch.setattr(prices, "fetch_ohlc", lambda *a, **k: _FAKE_OHLC.copy())
    st.cache_data.clear()
    yield AppTest.from_file(str(APP_PATH), default_timeout=30)
    st.cache_data.clear()


def _captions(at: AppTest) -> list[str]:
    return [c.value for c in at.caption]


def test_d10_defaults_labels_and_caption(div_at):
    at = div_at
    at.run()
    assert not at.exception and not at.error

    basis = at.radio(key="div_basis")
    lookback = at.radio(key="div_lookback")
    assert basis.value == "After tax"            # default = net
    assert list(basis.options) == ["After tax", "Before tax"]
    assert lookback.value == "6M"                # default lookback
    assert list(lookback.options) == ["3M", "6M", "1Y"]
    assert NET_CAPTION in _captions(at)          # caption states active view
    assert GROSS_CAPTION not in _captions(at)
    assert any(m.label == "Paid, last 6 months" for m in at.metric)

    # Toggle to gross: caption flips with the radio.
    at.radio(key="div_basis").set_value("Before tax").run()
    assert not at.exception and not at.error
    assert GROSS_CAPTION in _captions(at)
    assert NET_CAPTION not in _captions(at)


def test_d12_lookback_and_tax_mode_are_independent(div_at):
    at = div_at
    at.run()

    # Flip tax mode, then change lookback: tax choice must survive.
    at.radio(key="div_basis").set_value("Before tax").run()
    at.radio(key="div_lookback").set_value("3M").run()
    assert not at.exception and not at.error
    assert at.radio(key="div_basis").value == "Before tax"
    assert at.radio(key="div_lookback").value == "3M"
    assert GROSS_CAPTION in _captions(at)
    assert any(m.label == "Paid, last 3 months" for m in at.metric)

    # Now flip tax mode back: lookback must survive.
    at.radio(key="div_basis").set_value("After tax").run()
    assert not at.exception and not at.error
    assert at.radio(key="div_lookback").value == "3M"
    assert at.radio(key="div_basis").value == "After tax"
    assert NET_CAPTION in _captions(at)
    assert any(m.label == "Paid, last 3 months" for m in at.metric)
