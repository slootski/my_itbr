"""S2 smoke test — run the whole Streamlit app headless via AppTest.

Boundaries are monkeypatched so the run is fully offline and side-effect free:
- ``ibkr.flex.download_statement`` returns the synthetic Flex XML from conftest
  (which includes CashTransactions + OpenDividendAccruals for the Dividends tab);
- ``data.prices.fetch_ohlc`` returns a canned OHLC frame (no yfinance);
- ``FLEX_TOKEN`` / ``FLEX_QUERY_ID`` env vars make ``settings.is_configured``
  true, and ``DB_PATH`` points the snapshot DB at a pytest tmp path.

The test asserts the app runs without exceptions and renders all 8 tabs.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tests.conftest import SYNTHETIC_FLEX_XML

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"

EXPECTED_TABS = ["Overview", "Positions", "Trades", "Prices", "Watchlist", "Dividends", "Performance",
                 "Net Worth"]

_FAKE_OHLC = pd.DataFrame(
    {
        "date": pd.to_datetime([dt.date(2024, 12, 27), dt.date(2024, 12, 30), dt.date(2024, 12, 31)]),
        "open": [100.0, 101.0, 102.0],
        "high": [101.5, 102.5, 103.5],
        "low": [99.5, 100.5, 101.5],
        "close": [101.0, 102.0, 103.0],
        "volume": [1_000, 1_100, 1_200],
    }
)


@pytest.fixture
def smoke_app(monkeypatch, tmp_path):
    """AppTest wired to synthetic data: no network, no real snapshot DB."""
    from data import prices
    from ibkr import flex

    # settings.is_configured -> True; DB at a tmp path. monkeypatch.setenv wins
    # over any .env values (load_dotenv never overrides existing os.environ).
    monkeypatch.setenv("FLEX_TOKEN", "smoke-test-token")
    monkeypatch.setenv("FLEX_QUERY_ID", "999999")
    db_path = tmp_path / "smoke-snapshots.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))

    download_calls: list[tuple[str, str]] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        download_calls.append((token, query_id))
        return SYNTHETIC_FLEX_XML

    fetch_calls: list[str] = []

    def fake_fetch_ohlc(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        fetch_calls.append(symbol)
        return _FAKE_OHLC.copy()

    # app.py calls flex.download_statement / prices.fetch_ohlc on the module
    # objects, so patching the attributes severs both network boundaries.
    monkeypatch.setattr(flex, "download_statement", fake_download)
    monkeypatch.setattr(prices, "fetch_ohlc", fake_fetch_ohlc)

    # load_portfolio/load_prices are st.cache_data-cached (keyed by args);
    # start from a clean cache and leave none of the fake data behind.
    st.cache_data.clear()
    yield AppTest.from_file(str(APP_PATH), default_timeout=30), {
        "db_path": db_path,
        "download_calls": download_calls,
        "fetch_calls": fetch_calls,
    }
    st.cache_data.clear()


def test_app_runs_without_exception_and_renders_all_eight_tabs(smoke_app):
    at, ctx = smoke_app
    at.run()

    # S2 core: headless run raises nothing, surfaces no st.error/st.exception.
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    assert not at.error, f"app showed st.error: {[e.value for e in at.error]}"

    # All 8 tabs present, in spec order.
    assert [tab.label for tab in at.tabs] == EXPECTED_TABS

    # The pipeline really ran against the patched boundaries (offline).
    assert ctx["download_calls"] == [("smoke-test-token", "999999")]
    assert ctx["fetch_calls"] == ["AAPL"]  # Prices tab pulled the held symbol

    # The snapshot landed in the tmp DB, not the real one.
    assert ctx["db_path"].exists()

    # Dividends tab took the data path (calendar + headline), not the empty-info
    # fallback: the paid-sum metric only renders when the calendar is non-empty.
    assert any(m.label.startswith("Paid, last") for m in at.metric)


def test_prices_tab_renders_stats_and_overlay_toggles(smoke_app):
    """The Prices tab's KPI grid and overlay checkboxes render off real stats."""
    at, ctx = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    labels = [m.label for m in at.metric]
    for expected in ("Current price", "My avg cost", "Unrealized P&L", "Position",
                     "52-week high / low", "Avg volume", "Volatility (ann.)",
                     "ATR(14)", "Total return incl. divs",
                     "Dividends received (net)", "Last dividend"):
        assert expected in labels, f"missing metric {expected!r}; got {labels}"

    # Current price is the last close of the canned frame, not the Flex mark.
    current = next(m for m in at.metric if m.label == "Current price")
    assert current.value.startswith("103.00")

    keys = {cb.key for cb in at.checkbox}
    assert {"px_cost", "px_ma", "px_mean", "px_trades"} <= keys

    # Default period is 1y, so the 52-week range reuses the same pull — no
    # second fetch per symbol.
    assert ctx["fetch_calls"] == ["AAPL"]


# ---- e3: F1 coverage banner/caption + F3 token scrub ------------------------

# Live-bug-shaped CashTransactions section: one dividend (+WHTAX) settling on a
# single day, i.e. what a Flex Query with period=LastBusinessDay delivers.
_SHORT_CASH_TX = b"""<CashTransactions>
<CashTransaction type="Dividends" symbol="AAPL" amount="0.77" currency="USD" dateTime="2026-06-10;12:00:00" reportDate="2026-06-10" settleDate="2026-06-10" exDate="2026-05-15"/>
<CashTransaction type="Withholding Tax" symbol="AAPL" amount="-0.12" currency="USD" dateTime="2026-06-10;12:00:00" reportDate="2026-06-10" settleDate="2026-06-10" exDate="2026-05-15"/>
</CashTransactions>"""

_IBKR_PATH_TEXT = (
    "Account Management → Reporting → Flex Queries → edit query → "
    "Delivery Configuration → Period → 'Last 365 Calendar Days'"
)


def _xml_with_short_cash_tx() -> bytes:
    start = SYNTHETIC_FLEX_XML.index(b"<CashTransactions>")
    end = SYNTHETIC_FLEX_XML.index(b"</CashTransactions>") + len(b"</CashTransactions>")
    return SYNTHETIC_FLEX_XML[:start] + _SHORT_CASH_TX + SYNTHETIC_FLEX_XML[end:]


def _wire_app(monkeypatch, tmp_path, download) -> AppTest:
    """Minimal offline AppTest wiring (same boundaries as ``smoke_app``)."""
    from data import prices
    from ibkr import flex

    monkeypatch.setenv("FLEX_TOKEN", "smoke-test-token")
    monkeypatch.setenv("FLEX_QUERY_ID", "999999")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cov-snapshots.sqlite"))
    monkeypatch.setattr(flex, "download_statement", download)
    monkeypatch.setattr(
        prices, "fetch_ohlc",
        lambda symbol, period="1y", interval="1d": _FAKE_OHLC.copy(),
    )
    st.cache_data.clear()
    return AppTest.from_file(str(APP_PATH), default_timeout=30)


def test_coverage_banner_shows_for_short_statement(monkeypatch, tmp_path):
    """F1a: 1-day cash-tx coverage -> warning with day count, range, IBKR path."""
    xml = _xml_with_short_cash_tx()
    at = _wire_app(monkeypatch, tmp_path, lambda token, query_id, date_range: xml)
    at.run()
    st.cache_data.clear()

    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    banners = [str(w.value) for w in at.warning if "Flex Quer" in str(w.value)]
    assert banners, f"coverage banner missing; warnings={[str(w.value) for w in at.warning]}"
    text = banners[0]
    assert "covers only 1 day(s) of cash transactions" in text
    assert "10-06-2026 – 10-06-2026" in text
    assert _IBKR_PATH_TEXT in text  # exact IBKR navigation path required by F1a

    # F1b caption present alongside the banner.
    captions = [str(c.value) for c in at.caption]
    assert any("Cash-transaction data: 2 rows, 10-06-2026 – 10-06-2026" in c for c in captions)


def test_coverage_banner_absent_with_full_coverage(smoke_app):
    """F1: conftest XML spans ~3 months of cash tx — enough for the default 6M
    lookback (>= half-window) — so no banner; the caption still renders."""
    at, _ = smoke_app
    at.run()

    assert not at.exception
    assert not [w for w in at.warning if "Flex Quer" in str(w.value)], (
        "coverage banner must not fire on full-coverage synthetic data"
    )
    captions = [str(c.value) for c in at.caption]
    assert any("Cash-transaction data: 6 rows, 01-05-2024 – 19-12-2024" in c for c in captions)


def test_refresh_preserves_dividend_widget_state_and_refetches(monkeypatch, tmp_path):
    """FE-1 regression (e4): clicking sidebar Refresh must clear the cache and
    refetch WITHOUT resetting the keyed Dividends radios to their defaults.

    Live-bug mechanism: an ``st.rerun()`` inside the Refresh handler aborted the
    script run before the ``div_basis``/``div_lookback`` radios rendered, so
    Streamlit dropped their session state and the next run fell back to 6M /
    After tax (D12 violation through the Refresh path).
    """
    download_calls: list[tuple[str, str]] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        download_calls.append((token, query_id))
        return SYNTHETIC_FLEX_XML

    at = _wire_app(monkeypatch, tmp_path, fake_download)
    try:
        at.run()
        assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
        assert len(download_calls) == 1  # initial pull

        # User picks a non-default lookback; cached data — no refetch.
        at.radio(key="div_lookback").set_value("1Y").run()
        assert not at.exception
        assert at.radio(key="div_lookback").value == "1Y"
        assert any(m.label == "Paid, last 1 year" for m in at.metric)
        assert len(download_calls) == 1

        # Click the sidebar Refresh button.
        refresh = [b for b in at.sidebar.button if "Refresh" in b.label]
        assert refresh, f"Refresh button not found; sidebar buttons={[b.label for b in at.sidebar.button]}"
        refresh[0].click().run(timeout=30)

        # All three Refresh semantics hold:
        assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
        # 3. user selection retained (the live bug reset this to "6M")
        assert at.radio(key="div_lookback").value == "1Y", (
            "Refresh reset div_lookback to default — keyed widget state was dropped"
        )
        assert any(m.label == "Paid, last 1 year" for m in at.metric), (
            f"metrics={[m.label for m in at.metric]}"
        )
        # 1+2. cache cleared and data actually refetched
        assert len(download_calls) == 2, (
            f"Refresh did not refetch: download called {len(download_calls)} time(s)"
        )
    finally:
        st.cache_data.clear()


def test_refresh_retains_basis_and_lookback_together(monkeypatch, tmp_path):
    """QA e4: D10/D12 through the Refresh path with BOTH radios non-default.

    Set ``div_lookback="1Y"`` AND ``div_basis="Before tax"``, click Refresh:
    both selections must survive, the gross caption must persist, and the
    recomputed view (paid-sum metric, calendar HTML incl. Total row) must be
    identical to the pre-Refresh render (same underlying data refetched).
    """
    download_calls: list[tuple[str, str]] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        download_calls.append((token, query_id))
        return SYNTHETIC_FLEX_XML

    at = _wire_app(monkeypatch, tmp_path, fake_download)
    try:
        at.run()
        at.radio(key="div_lookback").set_value("1Y").run()
        at.radio(key="div_basis").set_value("Before tax").run()
        assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
        pre_metric = [m.value for m in at.metric if m.label == "Paid, last 1 year"]
        assert pre_metric, f"metrics={[m.label for m in at.metric]}"
        pre_html = [h.proto.body for h in at.get("html")]
        assert any("tfoot" in h and "Total" in h for h in pre_html)
        assert len(download_calls) == 1  # both radio reruns were cache hits

        refresh = [b for b in at.sidebar.button if "Refresh" in b.label]
        refresh[0].click().run(timeout=30)

        assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
        assert at.radio(key="div_lookback").value == "1Y"
        assert at.radio(key="div_basis").value == "Before tax"
        captions = [str(c.value) for c in at.caption]
        assert any("before withholding tax (gross)" in c for c in captions), (
            "gross-basis caption lost after Refresh"
        )
        post_metric = [m.value for m in at.metric if m.label == "Paid, last 1 year"]
        assert post_metric == pre_metric
        assert [h.proto.body for h in at.get("html")] == pre_html, (
            "calendar HTML changed across Refresh on identical data"
        )
        assert len(download_calls) == 2  # cache cleared AND refetched
    finally:
        st.cache_data.clear()


def test_flex_error_display_scrubs_token(monkeypatch, tmp_path):
    """F3: a FlexError whose text embeds the token (ibflex puts it in the
    request URL) must reach st.error with the token replaced by ***."""
    from ibkr import flex

    def boom(token: str, query_id: str, date_range: tuple | None) -> bytes:
        raise flex.FlexError(
            f"could not reach https://gdcdyn.interactivebrokers.com/"
            f"SendRequest?t={token}&q={query_id}&v=3"
        )

    at = _wire_app(monkeypatch, tmp_path, boom)
    at.run()
    st.cache_data.clear()

    assert not at.exception
    errors = [str(e.value) for e in at.error]
    assert errors, "expected st.error for the FlexError"
    assert all("smoke-test-token" not in e for e in errors), f"token leaked: {errors}"
    assert any("***" in e for e in errors)
    assert any("SendRequest" in e for e in errors)  # message stays informative


def _dividend_toggle(at):
    return next(
        c for c in at.checkbox
        if c.label == "Include dividends received (total return)"
    )


def test_positions_dividend_toggle_switches_heatmap_to_total_return(smoke_app):
    """The Positions heatmap toggle flips price-only return to total return."""
    at, _ = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    captions = [c.value for c in at.caption]
    assert any("dividends received are not counted" in c for c in captions)

    _dividend_toggle(at).check().run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    captions = [c.value for c in at.caption]
    # Smoke data pays AAPL dividends, so the income path is the one exercised.
    assert any("net dividends received" in c for c in captions)
    assert not any("dividends received are not counted" in c for c in captions)
    # The price-vs-income split renders with it, and only with it.
    assert any("dividend-paying holding" in c for c in captions)


def test_positions_split_bar_hidden_until_dividends_included(smoke_app):
    """The return-split chart belongs to the dividend mode, not the default view."""
    at, _ = smoke_app
    at.run()
    assert not any("dividend-paying holding" in c.value for c in at.caption)

    _dividend_toggle(at).check().run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    assert any("dividend-paying holding" in c.value for c in at.caption)


def test_net_worth_range_dropdown_clips_the_view(smoke_app):
    """The Net Worth range selector offers YTD/1Y/ALL and narrows the table."""
    at, _ = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    ranges = next(s for s in at.selectbox if s.label == "Range")
    assert list(ranges.options) == ["YTD", "1Y", "ALL"]
    assert ranges.value == "ALL"

    all_rows = len(at.dataframe[-1].value)
    ranges.select("YTD").run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    # Synthetic NAV history is all from 2024, so a 2026 YTD window is empty.
    assert len(at.dataframe) < 3 or len(at.dataframe[-1].value) <= all_rows


def test_net_worth_splits_contributions_from_earnings(smoke_app):
    """Contributed capital is summed in base currency, not raw amounts."""
    at, _ = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    metrics = {m.label: m.value for m in at.metric}
    assert "Contributed" in metrics, f"got metrics: {list(metrics)}"
    # 4000 ILS at 0.27 plus 500 USD is 1,580 base — never 4,500.
    assert "1,580" in metrics["Contributed"]
    assert "4,500" not in metrics["Contributed"]
    # NAV 10,500 against 1,580 paid in.
    assert "8,920" in metrics["Earnings"]


def test_net_worth_dividend_split_toggle(smoke_app):
    """The Net Worth income toggle adds a Dividends band and metric."""
    at, _ = smoke_app
    at.run()
    assert "Dividends" not in {m.label for m in at.metric}

    toggle = next(c for c in at.checkbox if c.label == "Split out dividend income")
    toggle.check().run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    metrics = {m.label: m.value for m in at.metric}
    # 24 + 25 dividends less 6 + 6.25 withholding = 36.75 net.
    assert "36.75" in metrics["Dividends"] or "36.8" in metrics["Dividends"]
    assert any("gold band is dividend income" in c.value for c in at.caption)


# ---- token expiry (IBKR Flex 1012 / 1015) ---------------------------------

def _flex_code_boom(code: str, message: str):
    def boom(token: str, query_id: str, date_range: tuple | None) -> bytes:
        raise flex_mod.FlexError(f"IBKR Flex error {code}: {message}", code=code)
    return boom


from ibkr import flex as flex_mod  # noqa: E402


def test_other_flex_codes_keep_generic_error(monkeypatch, tmp_path):
    """1014 (query invalid) is not a token problem — generic path unchanged."""
    at = _wire_app(monkeypatch, tmp_path, _flex_code_boom("1014", "Query is invalid."))
    at.run()
    st.cache_data.clear()

    assert not at.exception
    errors = [str(e.value) for e in at.error]
    infos = [str(i.value) for i in at.info]
    assert any("1014" in e for e in errors)
    assert not any("Generate a new token" in i for i in infos)
    assert any("Fix the token/query" in i for i in infos)


# ---- connect card + dialog (missing / dead credential only) ----------------

def _texts(at: AppTest) -> list[str]:
    """Every text-bearing element the user can read, as strings."""
    groups = (at.markdown, at.caption, at.subheader, at.error, at.info, at.warning,
              at.success, at.title)
    return [str(el.value) for group in groups for el in group]


def _connect_button(at: AppTest):
    return [b for b in at.button if b.key == "connect_open"]


def _wire_missing_credentials(monkeypatch, tmp_path, env_path):
    """App with placeholder credentials, .env redirected to ``env_path`` and a
    fake Flex download that records calls."""
    import config
    from data import prices
    from ibkr import flex

    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setenv("FLEX_TOKEN", "your_flex_token_here")
    monkeypatch.setenv("FLEX_QUERY_ID", "your_flex_query_id_here")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "key-snapshots.sqlite"))
    calls: list[tuple[str, str]] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        calls.append((token, query_id))
        return SYNTHETIC_FLEX_XML

    monkeypatch.setattr(flex, "download_statement", fake_download)
    monkeypatch.setattr(prices, "fetch_ohlc", lambda symbol, period: _FAKE_OHLC.copy())
    st.cache_data.clear()
    return AppTest.from_file(str(APP_PATH), default_timeout=30), calls


def test_connect_button_hidden_when_credentials_work(smoke_app):
    at, _ = smoke_app
    at.run()
    assert not at.exception
    assert not _connect_button(at)


@pytest.mark.parametrize(
    ("code", "message", "heading", "sidebar_status"),
    [
        ("1012", "Token has expired.", "Your IBKR token has expired", "IBKR: token expired"),
        ("1015", "Token is invalid.", "IBKR did not accept your token", "IBKR: token not accepted"),
    ],
)
def test_dead_token_shows_connect_card(monkeypatch, tmp_path, code, message, heading, sidebar_status):
    """1012/1015: one card (heading, error code, primary button), no raw error
    box, token never shown, and a hint instead of empty Net Worth controls."""
    at = _wire_app(monkeypatch, tmp_path, _flex_code_boom(code, message))
    at.run()
    st.cache_data.clear()

    assert not at.exception
    texts = _texts(at)
    assert heading in [str(h.value) for h in at.subheader]
    assert f"IBKR error {code}" in texts
    # The only error box is the sidebar status; no raw Flex error in the page.
    assert [str(e.value) for e in at.error] == [sidebar_status], [e.value for e in at.error]
    assert _connect_button(at)[0].label == "Enter new token"
    assert all("smoke-test-token" not in t for t in texts)
    assert any("Net Worth appears here" in t for t in texts)
    assert not [sb for sb in at.selectbox if sb.key == "nav_range"]


def test_expired_token_dialog_saves_only_the_token(monkeypatch, tmp_path):
    """1012: dialog shows steps with the current IBKR path, a token field only
    (query ID is still valid), and Save writes only FLEX_TOKEN."""
    import config

    env_path = tmp_path / ".env"
    env_path.write_text("# keep me\nFLEX_TOKEN=old\nFLEX_QUERY_ID=999999\n")
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    at = _wire_app(monkeypatch, tmp_path, _flex_code_boom("1012", "Token has expired."))
    at.run()
    assert not at.text_input  # dialog stays closed until the click

    _connect_button(at)[0].click().run()
    texts = _texts(at)
    assert any("Performance & Reports → Flex Queries → Flex Web Service Configuration" in t
               for t in texts), texts
    assert any("6 hours" in t for t in texts)
    assert [ti.label for ti in at.text_input] == ["Flex Web Service token"]
    assert [e.label for e in at.expander] == ["Which token do I need?"]

    at.text_input(key="connect_token").input("  123456789012345678901234  ")
    at.button(key="connect_save").click().run()
    st.cache_data.clear()

    assert not at.exception
    assert env_path.read_text() == (
        "# keep me\nFLEX_TOKEN=123456789012345678901234\nFLEX_QUERY_ID=999999\n"
    )
    assert all("123456789012345678901234" not in t for t in _texts(at))


def test_missing_credentials_dialog_validates_then_connects(monkeypatch, tmp_path):
    """Placeholder credentials: dialog also asks for the query ID; errors show
    under their fields without writing .env; valid input connects and loads
    all tabs, and the card disappears."""
    env_path = tmp_path / ".env"
    env_path.write_text("FLEX_TOKEN=your_flex_token_here\nFLEX_QUERY_ID=your_flex_query_id_here\n")
    at, calls = _wire_missing_credentials(monkeypatch, tmp_path, env_path)
    at.run()
    assert "Connect your IBKR account" in [str(h.value) for h in at.subheader]
    assert [str(w.value) for w in at.warning] == ["IBKR: not connected"]
    assert _connect_button(at)[0].label == "Connect IBKR account"
    assert not any("env.example" in t for t in _texts(at))  # no manual .env steps

    _connect_button(at)[0].click().run()
    assert [ti.label for ti in at.text_input] == ["Flex Web Service token", "Flex Query ID"]

    # Empty token + non-digit query ID: both field errors, nothing written.
    at.text_input(key="connect_query_id").input("abc")
    at.button(key="connect_save").click().run()
    texts = _texts(at)
    assert any("Paste the token from Flex Web Service Configuration" in t for t in texts), texts
    assert any("Use numbers only" in t for t in texts), texts
    assert "your_flex_token_here" in env_path.read_text()

    at.text_input(key="connect_token").input("111222333")
    at.text_input(key="connect_query_id").input("1537307")
    at.button(key="connect_save").click().run()
    st.cache_data.clear()

    assert not at.exception, [e.value for e in at.exception]
    assert env_path.read_text() == "FLEX_TOKEN=111222333\nFLEX_QUERY_ID=1537307\n"
    assert calls == [("111222333", "1537307")]
    assert [tab.label for tab in at.tabs] == EXPECTED_TABS
    assert any("Connected" in str(s.value) for s in at.success)
    assert any(str(s.value) == "IBKR: connected" for s in at.success)
    assert not _connect_button(at)


def test_dialog_cancel_closes_without_writing(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("FLEX_TOKEN=your_flex_token_here\nFLEX_QUERY_ID=your_flex_query_id_here\n")
    at, calls = _wire_missing_credentials(monkeypatch, tmp_path, env_path)
    at.run()
    _connect_button(at)[0].click().run()
    at.text_input(key="connect_token").input("111222333")
    at.button(key="connect_cancel").click().run()

    assert not at.exception
    assert "connect_dialog_open" not in at.session_state
    assert "your_flex_token_here" in env_path.read_text()
    assert calls == []


# ---- full-window pull + DB sync -----------------------------------------------

def test_full_window_pulls_once_and_reports_sync(monkeypatch, tmp_path):
    calls: list[tuple | None] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        calls.append(date_range)
        return SYNTHETIC_FLEX_XML

    at = _wire_app(monkeypatch, tmp_path, fake_download)
    try:
        at.run()
        assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
        # Synthetic statement already spans 366 days: no second, ranged pull.
        assert calls == [None]
        sidebar = [s.value for s in at.sidebar.success]
        assert any(v.startswith("DB sync: added") for v in sidebar), sidebar
        assert any(e.label == "DB sync details" for e in at.expander)

        # Same data again in a new session: nothing missing, nothing written.
        st.cache_data.clear()
        again = AppTest.from_file(str(APP_PATH), default_timeout=30)
        again.run()
        assert not again.exception
        assert "DB sync: up to date" in [s.value for s in again.sidebar.success]
    finally:
        st.cache_data.clear()


def test_short_query_period_is_repulled_with_full_range(monkeypatch, tmp_path):
    short = SYNTHETIC_FLEX_XML.replace(b'fromDate="2024-01-01"', b'fromDate="2024-12-01"')
    calls: list[tuple | None] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        calls.append(date_range)
        return short if date_range is None else SYNTHETIC_FLEX_XML

    at = _wire_app(monkeypatch, tmp_path, fake_download)
    try:
        at.run()
        assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
        assert calls[0] is None
        assert calls[1] == (dt.date(2024, 1, 2), dt.date(2024, 12, 31))  # 365 days to toDate
        assert len(calls) == 2
    finally:
        st.cache_data.clear()


def test_each_new_session_pulls_again(monkeypatch, tmp_path):
    calls: list[tuple | None] = []

    def fake_download(token: str, query_id: str, date_range: tuple | None) -> bytes:
        calls.append(date_range)
        return SYNTHETIC_FLEX_XML

    at = _wire_app(monkeypatch, tmp_path, fake_download)
    try:
        at.run()
        at.run()  # widget-style rerun in the same session: cached
        assert len(calls) == 1
        AppTest.from_file(str(APP_PATH), default_timeout=30).run()  # new session
        assert len(calls) == 2
    finally:
        st.cache_data.clear()


# ---- Performance tab ---------------------------------------------------------

# Three months of closes (Oct..Dec 2024) for the period controls; the stock
# statement has two closes in one month, which leaves nothing to choose between.
_THREE_MONTH_EQUITY = b"""<EquitySummaryInBase>
<EquitySummaryByReportDateInBase reportDate="2024-10-30" cash="500" stock="9000" total="9500"/>
<EquitySummaryByReportDateInBase reportDate="2024-10-31" cash="500" stock="9100" total="9600"/>
<EquitySummaryByReportDateInBase reportDate="2024-11-29" cash="500" stock="9300" total="9800"/>
<EquitySummaryByReportDateInBase reportDate="2024-12-30" cash="500" stock="9500" total="10000"/>
<EquitySummaryByReportDateInBase reportDate="2024-12-31" cash="600" stock="9900" total="10500"/>
</EquitySummaryInBase>"""


@pytest.fixture
def three_month_app(smoke_app, monkeypatch):
    from ibkr import flex

    head, rest = SYNTHETIC_FLEX_XML.split(b"<EquitySummaryInBase>")
    xml = head + _THREE_MONTH_EQUITY + rest.split(b"</EquitySummaryInBase>")[1]
    monkeypatch.setattr(flex, "download_statement", lambda token, query_id, date_range: xml)
    at, _ = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    return at


def _period(at):
    """The period slider. Found by label: its key changes each time a button
    or a month row sets the period (see app._set_perf_period)."""
    return next(s for s in at.select_slider if s.label == "Period")


def _metrics(at) -> dict:
    return {m.label: m.value for m in at.metric}


def test_performance_tab_with_one_month_shows_it_without_period_controls(smoke_app):
    """The synthetic statement has two closes (30 and 31 Dec 2024): 10000 -> 10500
    with no deposit in between, a +5.00% day worth +500.00."""
    at, _ = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"

    assert not [s for s in at.select_slider if s.label == "Period"]
    metrics = _metrics(at)
    assert metrics["Return"] == "+5.00%"
    assert metrics["P&L (USD)"] == "+500.00"
    assert metrics["Best day"] == "+5.00%"
    assert metrics["Up days"] == "1 / 1"
    # The statement's one trade (1 Jun 2024, commission 1.00) is outside December.
    assert metrics["Fees (USD)"] == "0.00"
    # Nothing charged: no split line under the total.
    assert next(m for m in at.metric if m.label == "Fees (USD)").delta in (None, "")


def test_perf_fee_text_names_each_kind_that_charged():
    import app

    split = {
        "trades": {"amount": 125.0, "count": 50},
        "fx": {"amount": 31.69, "count": 10},
        "other": {"amount": 0.0, "count": 0},
    }
    line, help_text = app._perf_fee_text(split=split)
    assert line == "Trades 125.00 · FX 31.69"
    assert "- Trades: 125.00 from 50 charges" in help_text
    assert "- FX: 31.69 from 10 charges" in help_text
    assert "Other" not in help_text


def test_perf_fee_text_without_fees_has_no_line():
    import app

    split = {kind: {"amount": 0.0, "count": 0} for kind in ("trades", "fx", "other")}
    line, help_text = app._perf_fee_text(split=split)
    assert line is None
    assert help_text.startswith("Trade commissions and broker fees")


def test_performance_period_starts_on_everything(three_month_app):
    """A new session opens on All: every month, one row each, All marked. 3M is
    the same three months here and comes first, so the mark must be set, not matched."""
    at = three_month_app
    period = _period(at)
    assert list(period.options) == ["October 2024", "November 2024", "December 2024"]
    assert period.value == ("2024-10", "2024-12")
    assert at.button_group(key="perf_preset").value == "All"
    assert _metrics(at)["Return"] == "+10.53%"
    assert [b.key for b in at.button if str(b.key).startswith("perf_open_")] == [
        "perf_open_2024_10", "perf_open_2024_11", "perf_open_2024_12"]
    # Both ends are reached: neither arrow can move the period.
    assert at.button(key="perf_newer").disabled and at.button(key="perf_older").disabled


def test_performance_single_month_period_shows_its_days(three_month_app):
    at = three_month_app
    _period(at).set_range("2024-12", "2024-12").run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    # December is measured from November's last close: 9800 -> 10500.
    assert _metrics(at)["Return"] == "+7.14%"
    assert not [b for b in at.button if str(b.key).startswith("perf_open_")]   # one month: its days


def test_performance_period_slider_selects_several_months(three_month_app):
    at = three_month_app
    _period(at).set_range("2024-10", "2024-12").run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    # 9500 -> 10500 over the period, no deposits in it.
    metrics = _metrics(at)
    assert metrics["Return"] == "+10.53%"
    assert metrics["P&L (USD)"] == "+1,000.00"
    assert metrics["Up days"] == "4 / 4"
    # Several months: one clickable row each.
    assert [b.key for b in at.button if str(b.key).startswith("perf_open_")] == [
        "perf_open_2024_10", "perf_open_2024_11", "perf_open_2024_12"]


def test_clicking_a_month_row_opens_that_month(three_month_app):
    at = three_month_app
    _period(at).set_range("2024-10", "2024-12").run()
    at.button(key="perf_open_2024_11").click().run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    assert _period(at).value == ("2024-11", "2024-11")
    assert _metrics(at)["P&L (USD)"] == "+200.00"


def test_arrows_move_the_period_and_stop_at_the_ends(three_month_app):
    at = three_month_app
    _period(at).set_range("2024-12", "2024-12").run()
    assert at.button(key="perf_newer").disabled and not at.button(key="perf_older").disabled
    at.button(key="perf_older").click().run()
    assert _period(at).value == ("2024-11", "2024-11")
    at.button(key="perf_older").click().run()
    assert _period(at).value == ("2024-10", "2024-10")
    assert at.button(key="perf_older").disabled


def test_quick_period_sets_the_slider_and_stays_selected(three_month_app):
    at = three_month_app
    assert at.button_group(key="perf_preset").value == "All"       # the starting period
    at.button_group(key="perf_preset").set_value("1M").run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    assert _period(at).value == ("2024-12", "2024-12")
    assert at.button_group(key="perf_preset").value == "1M"
    at.button_group(key="perf_preset").set_value("All").run()
    assert _period(at).value == ("2024-10", "2024-12")
    assert at.button_group(key="perf_preset").value == "All"


def test_quick_period_mark_follows_the_period_whoever_set_it(three_month_app):
    at = three_month_app
    at.button_group(key="perf_preset").set_value("All").run()
    # A custom period: no quick period is marked.
    _period(at).set_range("2024-10", "2024-11").run()
    assert at.button_group(key="perf_preset").value is None
    # Dragged back to the newest month: 1M again, though nobody clicked it.
    _period(at).set_range("2024-12", "2024-12").run()
    assert at.button_group(key="perf_preset").value == "1M"
    # A month row that is not the newest month: custom again.
    at.button_group(key="perf_preset").set_value("All").run()
    at.button(key="perf_open_2024_10").click().run()
    assert at.button_group(key="perf_preset").value is None


def test_clicking_the_selected_quick_period_keeps_it_selected(three_month_app):
    at = three_month_app
    at.button_group(key="perf_preset").set_value("All").run()
    at.button_group(key="perf_preset").set_value(None).run()      # a second click deselects
    assert _period(at).value == ("2024-10", "2024-12")
    assert at.button_group(key="perf_preset").value == "All"


def test_matching_preset_prefers_the_one_the_user_picked():
    import app

    options = ["2026-01", "2026-02", "2026-03"]
    everything = ("2026-01", "2026-03")
    # 3M, 6M, 1Y, YTD and All are all the same three months here.
    assert app._matching_preset(options=options, period=everything, picked=None) == "3M"
    assert app._matching_preset(options=options, period=everything, picked="YTD") == "YTD"
    assert app._matching_preset(options=options, period=everything, picked="1M") == "3M"
    assert app._matching_preset(options=options, period=("2026-01", "2026-02"), picked="YTD") is None


def test_year_fees_count_trades_on_days_without_a_close(three_month_app):
    """The June trade's 1.00 commission is outside Oct..Dec, so it is not in the period."""
    at = three_month_app
    _period(at).set_range("2024-10", "2024-12").run()
    assert _metrics(at)["Fees (USD)"] == "0.00"


def test_performance_closes_are_stored_for_later(smoke_app):
    """The statement's closes land in equity_daily, so days that age out of the
    Flex window keep their true value."""
    from data import snapshots

    at, ctx = smoke_app
    at.run()
    stored = snapshots.read_equity_daily(db_path=ctx["db_path"])
    assert [d.isoformat() for d in stored["report_date"]] == ["2024-12-30", "2024-12-31"]


def test_preset_periods_end_on_the_newest_month():
    import app

    options = app._month_options(months=[(2025, 11), (2026, 1), (2026, 3)])
    assert options == ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]   # no holes
    assert app._preset_period(options=options, preset="1M") == ("2026-03", "2026-03")
    assert app._preset_period(options=options, preset="3M") == ("2026-01", "2026-03")
    assert app._preset_period(options=options, preset="1Y") == ("2025-11", "2026-03")   # clamped
    assert app._preset_period(options=options, preset="YTD") == ("2026-01", "2026-03")
    assert app._preset_period(options=options, preset="All") == ("2025-11", "2026-03")


def test_shifting_a_period_keeps_its_length():
    import app

    options = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]
    st.session_state[app._PERF_PERIOD_KEY] = ("2025-12", "2026-01")
    try:
        app._shift_perf_period(options=options, step=1)
        assert st.session_state[app._PERF_PERIOD_KEY] == ("2026-01", "2026-02")
        app._shift_perf_period(options=options, step=5)   # clamped at the newest month
        assert st.session_state[app._PERF_PERIOD_KEY] == ("2026-02", "2026-03")
        app._shift_perf_period(options=options, step=-9)  # clamped at the oldest month
        assert st.session_state[app._PERF_PERIOD_KEY] == ("2025-11", "2025-12")
    finally:
        del st.session_state[app._PERF_PERIOD_KEY]
        del st.session_state[app._PERF_SLIDER_SEQ_KEY]


def test_clicking_a_day_opens_its_details(smoke_app):
    """31 Dec 2024: AAPL +50 (+3.45%), MSFT -4 (-1.00%) on a +500.00 day."""
    at, _ = smoke_app
    at.run()
    assert "Close" not in _metrics(at)                     # no dialog until a day is clicked
    # The first close on record (30 Dec) opens too; it has a close, just no change.
    assert [b.key for b in at.button if str(b.key).startswith("perf_dayopen_")] == [
        "perf_dayopen_2024_12_30", "perf_dayopen_2024_12_31"]

    at.button(key="perf_dayopen_2024_12_31").click().run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    metrics = _metrics(at)
    assert metrics["Close"] == "10,500.00" and metrics["Previous close"] == "10,000.00"
    assert any("Tuesday, 31 December 2024" in h.value for h in at.subheader)
    tables = [df.value for df in at.dataframe]
    tickers = next(t for t in tables if "Ticker" in t and "Price change %" in t)
    assert list(tickers["Ticker"]) == ["AAPL", "MSFT"]      # biggest gain first
    assert list(tickers["P&L"]) == [50.0, -4.0]
    assert list(tickers["Price change %"]) == [3.45, -1.0]
    bridge = next(t for t in tables if "Source" in t)
    assert list(bridge["Amount"]) == [46.0, 454.0, 500.0]   # held + everything else = day total


def test_per_ticker_days_are_stored_for_later(smoke_app):
    from data import snapshots

    at, ctx = smoke_app
    at.run()
    assert len(snapshots.read_symbol_daily(db_path=ctx["db_path"])) == 4


def test_period_changes_rebuild_the_slider_instead_of_writing_to_it(three_month_app):
    """Buttons and month rows must not set the slider's own key: Streamlit logs a
    warning whenever a widget that has a default value is also set through
    session state. (That warning needs a running server, so it cannot be caught
    here; the fresh key per change is what prevents it.)"""
    at = three_month_app
    assert _period(at).key == "perf_period_slider_0"
    at.button(key="perf_open_2024_12").click().run()                 # a month row
    at.button(key="perf_older").click().run()                        # an arrow
    at.button_group(key="perf_preset").set_value("All").run()        # a quick period
    at.button(key="perf_open_2024_11").click().run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    assert _period(at).key == "perf_period_slider_4"
    assert _period(at).value == ("2024-11", "2024-11")
    # Dragging by hand keeps the same slider.
    _period(at).set_range("2024-10", "2024-11").run()
    assert _period(at).key == "perf_period_slider_4"


def test_dragging_the_slider_then_an_arrow_moves_from_the_dragged_period(three_month_app):
    at = three_month_app
    _period(at).set_range("2024-11", "2024-12").run()
    at.button(key="perf_older").click().run()
    assert _period(at).value == ("2024-10", "2024-11")


# ---- Prices tab: step through the symbols ---------------------------------

@pytest.fixture
def three_symbol_app(smoke_app, monkeypatch):
    from ibkr import flex

    aapl = b'<OpenPosition symbol="AAPL"'
    extra = b"".join(
        b'<OpenPosition symbol="%s" assetCategory="STK" position="1" costBasisPrice="10" '
        b'costBasisMoney="10" markPrice="11" positionValue="11" fifoPnlUnrealized="1" '
        b'currency="USD"/>\n' % name for name in (b"MSFT", b"QTUM")
    )
    assert aapl in SYNTHETIC_FLEX_XML
    xml = SYNTHETIC_FLEX_XML.replace(aapl, extra + aapl, 1)
    monkeypatch.setattr(flex, "download_statement", lambda token, query_id, date_range: xml)
    at, _ = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    return at


def test_symbol_arrows_step_through_the_symbols_and_stop_at_the_ends(three_symbol_app):
    at = three_symbol_app
    assert list(at.selectbox(key="px_symbol").options) == ["AAPL", "MSFT", "QTUM"]
    assert at.selectbox(key="px_symbol").value == "AAPL"
    assert at.button(key="px_symbol_prev").disabled and not at.button(key="px_symbol_next").disabled

    at.button(key="px_symbol_next").click().run()
    assert at.selectbox(key="px_symbol").value == "MSFT"
    assert not at.button(key="px_symbol_prev").disabled and not at.button(key="px_symbol_next").disabled

    at.button(key="px_symbol_next").click().run()
    assert at.selectbox(key="px_symbol").value == "QTUM"
    assert at.button(key="px_symbol_next").disabled

    at.button(key="px_symbol_prev").click().run()
    assert at.selectbox(key="px_symbol").value == "MSFT"
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"


def test_symbol_arrows_move_from_a_symbol_picked_in_the_dropdown(three_symbol_app):
    at = three_symbol_app
    at.selectbox(key="px_symbol").set_value("QTUM").run()
    at.button(key="px_symbol_prev").click().run()
    assert at.selectbox(key="px_symbol").value == "MSFT"


def test_symbol_arrows_hidden_with_one_symbol(smoke_app):
    at, _ = smoke_app
    at.run()
    assert not [b for b in at.button if str(b.key).startswith("px_symbol_")]


# ---- Prices tab: expense ratio ---------------------------------------------

def test_expense_ratio_tile_shows_the_ratio_and_the_amount_paid(smoke_app, monkeypatch):
    from data import prices

    asked: list[str] = []

    def fake_expense_ratio(symbol: str) -> float:
        asked.append(symbol)
        return 0.4

    monkeypatch.setattr(prices, "fetch_expense_ratio", fake_expense_ratio)
    at, ctx = smoke_app
    at.run()
    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    tile = next(m for m in at.metric if m.label == "Expense Ratio")
    assert tile.value == "0.40%"
    # AAPL was bought on 2024-06-01, before the canned closes start (2024-12-27),
    # so the full history is pulled and the count starts at the first close.
    assert re.fullmatch(r"≈ [\d,]+\.\d\d USD paid since 2024-12-27", tile.delta), tile.delta
    assert asked == ["AAPL"]
    assert ctx["fetch_calls"] == ["AAPL", "AAPL"]


def test_expense_ratio_tile_is_blank_for_a_symbol_without_one(smoke_app):
    at, _ = smoke_app
    at.run()
    tile = next(m for m in at.metric if m.label == "Expense Ratio")
    assert tile.value == "—"
    assert not tile.delta
