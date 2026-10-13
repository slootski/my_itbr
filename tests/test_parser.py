"""Tests for ibkr.parser — XML -> DataFrames + NAV reading."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum

import pytest

from ibkr import parser


class _Color(Enum):
    RED = 1


def test_coerce():
    assert parser._coerce(Decimal("1.5")) == 1.5
    assert isinstance(parser._coerce(Decimal("1.5")), float)
    assert parser._coerce(_Color.RED) == "RED"
    assert parser._coerce(None) is None
    assert parser._coerce("x") == "x"


def test_parse_statement_no_statements_returns_none():
    xml = (b'<FlexQueryResponse queryName="t" type="AF">'
           b'<FlexStatements count="0"></FlexStatements></FlexQueryResponse>')
    assert parser.parse_statement(xml) is None


def test_parse_statement_strips_unknown_section(flex_xml):
    # Inject a section ibflex 1.1 cannot model as a child of <FlexStatement>.
    bogus = (b"<MutualFundDividendDetails><MutualFundDividendDetail/>"
             b"</MutualFundDividendDetails></FlexStatement>")
    polluted = flex_xml.replace(b"</FlexStatement>", bogus)
    assert parser.parse_statement(polluted) is not None


def test_account_info(stmt):
    assert parser.account_info(stmt) == {
        "account_id": "U123",
        "name": "Test",
        "currency": "USD",
        "account_type": "Individual",
    }


def test_latest_nav_prefers_last_equity_row(stmt):
    nav = parser.latest_nav(stmt)
    assert nav["net_liq"] == 10500.0
    assert nav["cash"] == 600.0
    assert nav["currency"] == "USD"
    assert nav["as_of"] == dt.date(2024, 12, 31)


def test_positions_df_columns_renamed(stmt):
    df = parser.positions_df(stmt)
    assert len(df) == 1
    for col in ["symbol", "asset_class", "quantity", "cost_price", "cost_basis",
                "mark_price", "market_value", "unrealized_pnl", "currency"]:
        assert col in df.columns
    row = df.iloc[0]
    assert row["symbol"] == "AAPL"
    assert row["market_value"] == 1500.0
    assert row["unrealized_pnl"] == 500.0


def test_trades_df_columns_renamed(stmt):
    df = parser.trades_df(stmt)
    assert len(df) == 1
    for col in ["symbol", "asset_class", "trade_date", "datetime", "side",
                "quantity", "price", "proceeds", "commission", "realized_pnl", "currency"]:
        assert col in df.columns
    assert df.iloc[0]["side"] == "BUY"


def test_cash_df(stmt):
    df = parser.cash_df(stmt)
    assert len(df) == 1
    assert df.iloc[0]["ending_cash"] == 600.0


def test_equity_summary_sorted(stmt):
    df = parser.equity_summary_df(stmt)
    assert len(df) == 2
    assert "net_liq" in df.columns
    # sorted ascending by report_date -> last row is the most recent total
    assert df.iloc[-1]["net_liq"] == 10500.0
    assert list(df["report_date"]) == sorted(df["report_date"])


def test_cash_transactions_carry_currency_and_base_amount(flex_xml):
    stmt = parser.parse_statement(flex_xml)
    df = parser.cash_transactions_df(stmt)
    deposits = df[df["type"].astype(str) == "DEPOSITWITHDRAW"]
    assert set(deposits["currency"]) == {"ILS", "USD"}
    # 4000 ILS at 0.27 is 1080 base — not 4000. Summing the raw amounts across
    # currencies is the bug amount_base exists to prevent.
    by_ccy = dict(zip(deposits["currency"], deposits["amount_base"]))
    assert by_ccy["ILS"] == pytest.approx(1080.0)
    assert by_ccy["USD"] == pytest.approx(500.0)
    assert deposits["amount"].sum() == pytest.approx(4500.0)
    assert deposits["amount_base"].sum() == pytest.approx(1580.0)


def test_cash_transactions_missing_fx_rate_treated_as_base(flex_xml):
    stmt = parser.parse_statement(flex_xml)
    df = parser.cash_transactions_df(stmt)
    divs = df[df["type"].astype(str) == "DIVIDEND"]
    # The dividend rows carry no fxRateToBase; they are already in base.
    assert list(divs["amount_base"]) == pytest.approx(list(divs["amount"]))


def test_symbol_daily_one_row_per_day_and_symbol(stmt):
    import datetime as dt

    df = parser.symbol_daily_df(stmt)
    assert list(df.columns) == ["date", "symbol", "description", "price", "mtm_pnl", "currency"]
    assert len(df) == 4
    last = df[(df["date"] == dt.date(2024, 12, 31)) & (df["symbol"] == "AAPL")].iloc[0]
    assert (last["price"], last["mtm_pnl"], last["description"]) == (150.0, 50.0, "APPLE INC")


def test_symbol_daily_files_a_temporary_symbol_under_its_underlying():
    """During a corporate action IBKR lists the old line under a timestamped
    symbol; the day must still show one XOM row with both amounts."""
    from tests.conftest import SYNTHETIC_FLEX_XML

    extra = (b'<PriorPeriodPosition symbol="20260701222712XOM" underlyingSymbol="XOM" description="OLD" '
             b'assetCategory="STK" currency="USD" date="2024-12-31" price="100" priorMtmPnl="1.5"/>'
             b'<PriorPeriodPosition symbol="XOM" underlyingSymbol="XOM" description="NEW" '
             b'assetCategory="STK" currency="USD" date="2024-12-31" price="101" priorMtmPnl="2.5"/>')
    xml = SYNTHETIC_FLEX_XML.replace(b"</PriorPeriodPositions>", extra + b"</PriorPeriodPositions>")
    df = parser.symbol_daily_df(parser.parse_statement(xml))
    xom = df[df["symbol"] == "XOM"]
    assert len(xom) == 1 and xom.iloc[0]["mtm_pnl"] == 4.0


def test_symbol_daily_without_the_section_is_an_empty_typed_frame():
    from tests.conftest import SYNTHETIC_FLEX_XML

    head, rest = SYNTHETIC_FLEX_XML.split(b"<PriorPeriodPositions>")
    xml = head + rest.split(b"</PriorPeriodPositions>")[1]
    df = parser.symbol_daily_df(parser.parse_statement(xml))
    assert df.empty and list(df.columns) == parser.SYMBOL_DAILY_COLS
