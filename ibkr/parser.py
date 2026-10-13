"""Parse Flex statement XML into pandas DataFrames.

Uses ``ibflex.parser`` for the heavy lifting (typed parse of the XML), then
flattens the sections we care about into DataFrames. Field access is defensive:
a given Flex Query may or may not include a section, so missing sections yield
empty DataFrames rather than errors.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import io
import logging
import xml.etree.ElementTree as ET
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

import pandas as pd
from ibflex import parser as flex_parser
import ibflex.Types as _ibflex_types

logger = logging.getLogger("myitbr.parser")


# ---- low-level helpers ----------------------------------------------------

def _span(df: pd.DataFrame, col: str) -> str:
    """``min..max`` of a date-ish column for log lines; "-" when nothing there.

    Defensive on purpose: a logging helper must never break the parse.
    """
    try:
        if df.empty or col not in df.columns:
            return "-"
        values = df[col].dropna()
        if values.empty:
            return "-"
        return f"{values.min()}..{values.max()}"
    except Exception:  # noqa: BLE001
        return "?"

def _coerce(value: Any) -> Any:
    """Make ibflex values friendly for pandas/plotly.

    Decimals -> float, Enums -> their name, leave dates/None/str as-is.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.name
    return value


def _records_to_df(records: Iterable[Any] | None, fields: list[str]) -> pd.DataFrame:
    """Build a DataFrame from ibflex records, pulling only ``fields``.

    Each record is a typed ibflex object exposing the field names as attributes.
    Missing attributes become None so the column always exists.
    """
    rows = []
    for rec in records or ():
        rows.append({f: _coerce(getattr(rec, f, None)) for f in fields})
    return pd.DataFrame(rows, columns=fields)


# ---- top level ------------------------------------------------------------

def _strip_unknown_statement_children(xml_bytes: bytes) -> bytes:
    """Remove <FlexStatement> child elements that ibflex's FlexStatement
    dataclass does not model, so one unsupported section does not abort the
    whole parse. Returns serialized XML bytes.

    ibflex parsing is all-or-nothing: a single child element it cannot map to a
    field of ``FlexStatement`` raises FlexParserError. The installed ibflex 1.1
    does not model every IBKR Flex section (e.g. MutualFundDividendDetails), so
    we use the dataclass fields as an allowlist to drop any unknown section.
    """
    allowed = {f.name for f in dataclasses.fields(_ibflex_types.FlexStatement)}
    root = ET.fromstring(xml_bytes)
    for stmt in root.iter("FlexStatement"):
        for child in list(stmt):
            if child.tag not in allowed:
                stmt.remove(child)
    return ET.tostring(root, encoding="utf-8")


def parse_statement(xml: bytes | str):
    """Parse raw Flex XML and return the first FlexStatement.

    Returns None when the response contains no statements.

    ``ibflex.parser.parse`` reads from a file path or file-like object, so raw
    XML bytes/str are wrapped in an in-memory buffer. Raw XML is cleaned first
    to drop sections ibflex cannot model (see ``_strip_unknown_statement_children``).
    """
    if isinstance(xml, (bytes, bytearray)):
        source = io.BytesIO(_strip_unknown_statement_children(bytes(xml)))
    elif isinstance(xml, str):
        source = io.BytesIO(_strip_unknown_statement_children(xml.encode("utf-8")))
    else:
        source = xml  # already a path or file-like
    response = flex_parser.parse(source)
    statements = getattr(response, "FlexStatements", None) or ()
    stmt = statements[0] if statements else None
    try:
        if stmt is None:
            logger.warning("parse_statement: response contained no FlexStatement")
        else:
            logger.info(
                "parse_statement: period=%s from=%s to=%s | sections: "
                "CashTransactions=%d OpenDividendAccruals=%d Trades=%d OpenPositions=%d",
                getattr(stmt, "period", None),
                getattr(stmt, "fromDate", None),
                getattr(stmt, "toDate", None),
                len(getattr(stmt, "CashTransactions", None) or ()),
                len(getattr(stmt, "OpenDividendAccruals", None) or ()),
                len(getattr(stmt, "Trades", None) or ()),
                len(getattr(stmt, "OpenPositions", None) or ()),
            )
    except Exception:  # noqa: BLE001 — logging must never break the parse
        pass
    return stmt


def account_info(stmt) -> dict:
    info = getattr(stmt, "AccountInformation", None)
    if info is None:
        return {}
    return {
        "account_id": _coerce(getattr(info, "accountId", None)),
        "name": _coerce(getattr(info, "name", None)),
        "currency": _coerce(getattr(info, "currency", None)),
        "account_type": _coerce(getattr(info, "accountType", None)),
    }


def positions_df(stmt) -> pd.DataFrame:
    fields = [
        "symbol", "assetCategory", "position", "costBasisPrice", "costBasisMoney",
        "markPrice", "positionValue", "fifoPnlUnrealized", "currency",
    ]
    df = _records_to_df(getattr(stmt, "OpenPositions", None), fields)
    df = df.rename(columns={
        "assetCategory": "asset_class",
        "position": "quantity",
        "costBasisPrice": "cost_price",
        "costBasisMoney": "cost_basis",
        "markPrice": "mark_price",
        "positionValue": "market_value",
        "fifoPnlUnrealized": "unrealized_pnl",
    })
    logger.info("positions: %d rows", len(df))
    return df


def trades_df(stmt) -> pd.DataFrame:
    # tradeID/transactionID are opt-in columns of the Flex Query, so they may
    # come back None. They are carried anyway because data/history.py uses them
    # as the archive's primary key when present — see that module for why the
    # content-hash fallback is second choice.
    fields = [
        "symbol", "assetCategory", "tradeDate", "dateTime", "buySell", "quantity",
        "tradePrice", "proceeds", "ibCommission", "fifoPnlRealized", "currency",
        "tradeID", "transactionID",
    ]
    df = _records_to_df(getattr(stmt, "Trades", None), fields)
    df = df.rename(columns={
        "assetCategory": "asset_class",
        "tradeDate": "trade_date",
        "dateTime": "datetime",
        "buySell": "side",
        "tradePrice": "price",
        "ibCommission": "commission",
        "fifoPnlRealized": "realized_pnl",
        "tradeID": "trade_id",
        "transactionID": "transaction_id",
    })
    logger.info("trades: %d rows (trade_date %s)", len(df), _span(df, "trade_date"))
    return df


def cash_df(stmt) -> pd.DataFrame:
    fields = ["currency", "endingCash", "endingSettledCash"]
    df = _records_to_df(getattr(stmt, "CashReport", None), fields)
    return df.rename(columns={
        "endingCash": "ending_cash",
        "endingSettledCash": "ending_settled_cash",
    })


def cash_transactions_df(stmt) -> pd.DataFrame:
    """Cash transactions — dividends, payments-in-lieu, and withholding tax.

    Drives the *paid* (green) cells of the dividend calendar. The ``type`` column
    is the coerced enum name, e.g. ``DIVIDEND``, ``PAYMENTINLIEU``, ``WHTAX``.
    Requires the Flex Query to include the Cash Transactions section; absent it,
    the returned DataFrame is empty.
    """
    fields = [
        "type", "symbol", "amount", "currency", "fxRateToBase",
        "dateTime", "reportDate", "settleDate", "exDate",
        "transactionID", "actionID",
    ]
    df = _records_to_df(getattr(stmt, "CashTransactions", None), fields)
    df = df.rename(columns={
        "dateTime": "datetime",
        "reportDate": "report_date",
        "settleDate": "settle_date",
        "exDate": "ex_date",
        "transactionID": "transaction_id",
        "actionID": "action_id",
        "fxRateToBase": "fx_rate",
    })
    # ``amount`` is in the transaction's own currency, so a funding deposit made
    # in ILS and a dividend paid in USD cannot be added together. Carry a
    # base-currency figure alongside it; anything summing across rows must use
    # this one. Missing/unparseable rate means the row is already in base.
    if not df.empty:
        rate = pd.to_numeric(df.get("fx_rate"), errors="coerce").fillna(1.0)
        rate = rate.where(rate > 0, 1.0)
        df["amount_base"] = pd.to_numeric(df["amount"], errors="coerce") * rate
    logger.info(
        "cash_transactions: %d rows (settle_date %s, report_date %s, ex_date %s)",
        len(df), _span(df, "settle_date"), _span(df, "report_date"), _span(df, "ex_date"),
    )
    return df


def dividend_accruals_df(stmt) -> pd.DataFrame:
    """Open dividend accruals — declared but not yet paid.

    Drives the *declared* (yellow) cells of the dividend calendar. Requires the
    Flex Query to include the Open Dividend Accruals section.
    """
    fields = [
        "symbol", "exDate", "payDate", "grossAmount", "netAmount", "quantity", "tax",
    ]
    df = _records_to_df(getattr(stmt, "OpenDividendAccruals", None), fields)
    df = df.rename(columns={
        "exDate": "ex_date",
        "payDate": "pay_date",
        "grossAmount": "gross_amount",
        "netAmount": "net_amount",
    })
    logger.info(
        "dividend_accruals: %d rows (ex_date %s, pay_date %s)",
        len(df), _span(df, "ex_date"), _span(df, "pay_date"),
    )
    return df


def equity_summary_df(stmt) -> pd.DataFrame:
    """Daily equity summary in base currency (drives the NAV reading)."""
    fields = ["reportDate", "cash", "stock", "total"]
    df = _records_to_df(getattr(stmt, "EquitySummaryInBase", None), fields)
    df = df.rename(columns={"reportDate": "report_date", "total": "net_liq"})
    if not df.empty and "report_date" in df:
        df = df.sort_values("report_date").reset_index(drop=True)
    return df


SYMBOL_DAILY_COLS = ["date", "symbol", "description", "price", "mtm_pnl", "currency"]


def symbol_daily_df(stmt) -> pd.DataFrame:
    """Per day and symbol: the closing price and the day's mark-to-market P&L.

    From the statement's ``PriorPeriodPositions`` section. ``mtm_pnl`` is IBKR's
    ``priorMtmPnl``: the gain or loss on the shares held coming into that day,
    in the position's currency. It leaves out shares bought or sold that day.
    A security IBKR lists under a temporary symbol during a corporate action is
    filed under its underlying symbol, so a day has one row per symbol
    (P&L summed, the last price kept).
    """
    fields = ["date", "symbol", "underlyingSymbol", "description", "price", "priorMtmPnl", "currency"]
    df = _records_to_df(getattr(stmt, "PriorPeriodPositions", None), fields)
    if df.empty:
        return pd.DataFrame(columns=SYMBOL_DAILY_COLS)
    df["symbol"] = df["underlyingSymbol"].where(df["underlyingSymbol"].astype(bool), df["symbol"])
    df = df.rename(columns={"priorMtmPnl": "mtm_pnl"}).dropna(subset=["date", "symbol"])
    df = (df.groupby(["date", "symbol"], as_index=False)
          .agg(description=("description", "last"), price=("price", "last"),
               mtm_pnl=("mtm_pnl", "sum"), currency=("currency", "last")))
    df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
    logger.info("symbol_daily: %d rows (date %s)", len(df), _span(df, "date"))
    return df[SYMBOL_DAILY_COLS]


def latest_nav(stmt) -> dict:
    """Best-effort current net liquidation + cash in base currency.

    Prefers the most recent EquitySummaryInBase row; falls back to summing the
    CashReport ending cash when no equity summary is present.
    """
    base_ccy = account_info(stmt).get("currency")
    eq = equity_summary_df(stmt)
    if not eq.empty:
        last = eq.iloc[-1]
        return {
            "net_liq": float(last.get("net_liq")) if pd.notna(last.get("net_liq")) else None,
            "cash": float(last.get("cash")) if pd.notna(last.get("cash")) else None,
            "currency": base_ccy,
            "as_of": last.get("report_date"),
        }

    cash = cash_df(stmt)
    total_cash = float(cash["ending_cash"].dropna().sum()) if not cash.empty else None
    return {
        "net_liq": None,
        "cash": total_cash,
        "currency": base_ccy,
        "as_of": dt.date.today(),
    }
