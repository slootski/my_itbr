"""Shared fixtures for the test suite.

The synthetic Flex XML mirrors a real statement closely enough to exercise every
parser path (account, equity summary, positions, trades, cash, cash transactions,
open dividend accruals) without touching the network or needing IBKR credentials.

Dividend sections (for the app smoke test and parser tests):
- CashTransactions: two AAPL quarterly dividends (Sep/Dec 2024) each with a
  matching Withholding Tax row keyed by the same exDate, so both the gross and
  net (WHTAX-folded) calendar paths are exercised.
- Two Deposits & Withdrawals rows, one in ILS (fxRateToBase 0.27) and one in
  USD, so the contributed-capital split is exercised against the currency
  mixing it exists to prevent: 4000 ILS + 500 USD is 1580 base, not 4500.
- PriorPeriodPositions: AAPL and MSFT on the two equity days, for the per-ticker
  view of a day (31 Dec: AAPL +3.45%, +50; MSFT -1.00%, -4).
- OpenDividendAccruals: one AAPL accrual (pay 2025-01-16). Relative to any
  "today" after Jan 2025 it is stale, so it must neither rewrite history (D4)
  nor suppress the red Declare/Pay projection (BUG-10) — the quarterly cadence
  still projects forward from the current month, keeping the Dividends tab
  calendar non-empty no matter when the suite runs.
"""
from __future__ import annotations

import pandas as pd
import pytest
import streamlit as st

# Imported here, before any fixture sets the environment: config copies the real
# .env credentials over os.environ at import, and a first import inside
# AppTest.run() would overwrite what a test just set.
import config

SYNTHETIC_FLEX_XML = b"""<?xml version="1.0"?>
<FlexQueryResponse queryName="t" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U123" fromDate="2024-01-01" toDate="2024-12-31" period="" whenGenerated="2024-12-31;00:00:00">
<AccountInformation accountId="U123" name="Test" currency="USD" accountType="Individual"/>
<EquitySummaryInBase>
<EquitySummaryByReportDateInBase reportDate="2024-12-30" cash="500" stock="9500" total="10000"/>
<EquitySummaryByReportDateInBase reportDate="2024-12-31" cash="600" stock="9900" total="10500"/>
</EquitySummaryInBase>
<OpenPositions>
<OpenPosition symbol="AAPL" assetCategory="STK" position="10" costBasisPrice="100" costBasisMoney="1000" markPrice="150" positionValue="1500" fifoPnlUnrealized="500" currency="USD"/>
</OpenPositions>
<Trades>
<Trade symbol="AAPL" assetCategory="STK" tradeDate="2024-06-01" dateTime="2024-06-01;10:00:00" buySell="BUY" quantity="10" tradePrice="100" proceeds="-1000" ibCommission="-1" fifoPnlRealized="0" currency="USD"/>
</Trades>
<CashTransactions>
<CashTransaction type="Deposits &amp; Withdrawals" amount="4000" currency="ILS" fxRateToBase="0.27" dateTime="2024-05-01;12:00:00" reportDate="2024-05-01" settleDate="2024-05-01"/>
<CashTransaction type="Deposits &amp; Withdrawals" amount="500" currency="USD" fxRateToBase="1" dateTime="2024-07-01;12:00:00" reportDate="2024-07-01" settleDate="2024-07-01"/>
<CashTransaction type="Dividends" symbol="AAPL" amount="24" currency="USD" dateTime="2024-09-20;12:00:00" reportDate="2024-09-20" settleDate="2024-09-20" exDate="2024-08-15"/>
<CashTransaction type="Withholding Tax" symbol="AAPL" amount="-6" currency="USD" dateTime="2024-09-20;12:00:00" reportDate="2024-09-20" settleDate="2024-09-20" exDate="2024-08-15"/>
<CashTransaction type="Dividends" symbol="AAPL" amount="25" currency="USD" dateTime="2024-12-19;12:00:00" reportDate="2024-12-19" settleDate="2024-12-19" exDate="2024-11-14"/>
<CashTransaction type="Withholding Tax" symbol="AAPL" amount="-6.25" currency="USD" dateTime="2024-12-19;12:00:00" reportDate="2024-12-19" settleDate="2024-12-19" exDate="2024-11-14"/>
</CashTransactions>
<OpenDividendAccruals>
<OpenDividendAccrual symbol="AAPL" exDate="2024-12-27" payDate="2025-01-16" grossAmount="26" netAmount="19.5" quantity="10" tax="6.5"/>
</OpenDividendAccruals>
<PriorPeriodPositions>
<PriorPeriodPosition symbol="AAPL" underlyingSymbol="AAPL" description="APPLE INC" assetCategory="STK" currency="USD" date="2024-12-30" price="145" priorMtmPnl="-20"/>
<PriorPeriodPosition symbol="MSFT" underlyingSymbol="MSFT" description="MICROSOFT CORP" assetCategory="STK" currency="USD" date="2024-12-30" price="400" priorMtmPnl="5"/>
<PriorPeriodPosition symbol="AAPL" underlyingSymbol="AAPL" description="APPLE INC" assetCategory="STK" currency="USD" date="2024-12-31" price="150" priorMtmPnl="50"/>
<PriorPeriodPosition symbol="MSFT" underlyingSymbol="MSFT" description="MICROSOFT CORP" assetCategory="STK" currency="USD" date="2024-12-31" price="396" priorMtmPnl="-4"/>
</PriorPeriodPositions>
<CashReport>
<CashReportCurrency currency="USD" endingCash="600" endingSettledCash="600"/>
</CashReport>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


@pytest.fixture(autouse=True)
def _no_polygon_network(monkeypatch):
    """Disable the Polygon declared-dividend lookup for every test.

    The real key may be present in ``.env`` (loaded into os.environ at import),
    and app-path tests would otherwise hit the network and rate-limit. Clearing
    the env var routes the app down the no-key branch. Provider/cache unit tests
    pass keys as explicit arguments, so they are unaffected.
    """
    monkeypatch.setenv("POLYGON_API_KEY", "")


@pytest.fixture(autouse=True)
def _no_expense_ratio_network(monkeypatch):
    """Stub the yfinance expense-ratio lookup for every test.

    The Prices tab asks for it on every run, so any app-path test would otherwise
    hit the network. Tests that want a ratio patch ``prices.fetch_expense_ratio``
    again; tests of the lookup itself keep a reference to the real function.
    """
    from data import prices

    monkeypatch.setattr(prices, "fetch_expense_ratio", lambda symbol: None)


@pytest.fixture(autouse=True)
def _isolate_store_and_credentials(monkeypatch, tmp_path_factory):
    """Keep every test away from the developer's database, .env and credentials.

    ``DB_PATH`` and ``config.ENV_PATH`` point at tmp paths, and the Flex
    credentials are empty unless a test sets them (empty keeps the app on its
    not-connected path; any made-up value would make it call IBKR).
    """
    home = tmp_path_factory.mktemp("store")
    monkeypatch.setenv("DB_PATH", str(home / "isolated.sqlite"))
    monkeypatch.setattr(config, "ENV_PATH", home / ".env")
    monkeypatch.setenv("FLEX_TOKEN", "")
    monkeypatch.setenv("FLEX_QUERY_ID", "")


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    """st.cache_data is process-wide; a schedule's cache key has no session part,
    so equal patched times would share an entry between tests."""
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture(autouse=True)
def _no_yfinance_network(monkeypatch):
    """Stub every yfinance entry point the app reaches.

    Tests that need prices or quotes patch these again; tests of the functions
    themselves keep a reference to the real one taken at import.
    """
    from data import prices, watchlist

    monkeypatch.setattr(prices, "fetch_ohlc", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(prices, "fetch_quote", lambda symbol: None)
    monkeypatch.setattr(prices, "fetch_quotes",
                        lambda symbols, workers: {symbol: None for symbol in symbols})
    monkeypatch.setattr(
        prices, "resolve_symbol",
        lambda text: {"status": "ok", "symbol": watchlist.clean_symbol(text=text)},
    )


@pytest.fixture(autouse=True)
def _isolate_backups(monkeypatch, tmp_path_factory):
    """Redirect BACKUP_DIR at a tmp path for every test.

    App-path tests reload ``config.load_settings()``, which resolves BACKUP_DIR
    from the environment — so without this a test run writes a *synthetic-data*
    backup into the developer's real ``db/backups/``, where it both pollutes the
    rotation and (being dated today) blocks that day's genuine backup. Autouse
    rather than per-test: the failure mode is a future test forgetting, and it
    fails silently in the developer's own data.
    """
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path_factory.mktemp("backups")))


@pytest.fixture
def flex_xml() -> bytes:
    return SYNTHETIC_FLEX_XML


@pytest.fixture
def stmt(flex_xml):
    from ibkr import parser
    s = parser.parse_statement(flex_xml)
    assert s is not None
    return s


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "snap.sqlite"
