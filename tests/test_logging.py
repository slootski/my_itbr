"""Tests for runtime logging (e3 B2): setup idempotence, per-stage records,
and the no-token-in-logs guarantee. No network."""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import pytest

import logsetup
from data import dividends
from ibkr import flex, parser


def _cash_tx() -> pd.DataFrame:
    return pd.DataFrame([
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(2026, 3, 15), "ex_date": dt.date(2026, 3, 1),
         "datetime": None, "report_date": None},
        {"type": "DIVIDEND", "symbol": "ABC", "amount": 100.0,
         "settle_date": dt.date(2026, 6, 15), "ex_date": dt.date(2026, 6, 1),
         "datetime": None, "report_date": None},
    ])


# ---- setup_logging --------------------------------------------------------

def test_setup_logging_idempotent():
    """Repeated setup (Streamlit rerun) must not grow the handler list."""
    logger = logsetup.setup_logging()
    count_after_first = len(logger.handlers)
    assert count_after_first >= 1  # at least the stderr handler attached

    for _ in range(3):
        again = logsetup.setup_logging()
        assert again is logger
        assert len(logger.handlers) == count_after_first


def test_setup_logging_creates_log_dir_and_file():
    logsetup.setup_logging()
    file_handlers = [
        h for h in logging.getLogger("myitbr").handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers, "rotating file handler missing"
    assert logsetup.LOG_FILE.parent.is_dir()


# ---- per-stage records ----------------------------------------------------

def test_build_calendar_emits_records(caplog):
    with caplog.at_level(logging.INFO, logger="myitbr.dividends"):
        dividends.build_calendar(
            pd.DataFrame({"symbol": ["ABC"]}), _cash_tx(), pd.DataFrame(),
            today=dt.date(2026, 6, 11), lookback_months=12,
        )
    records = [r for r in caplog.records if r.name == "myitbr.dividends"]
    assert records, "build_calendar emitted no log records"
    msg = " ".join(r.getMessage() for r in records)
    assert "build_calendar" in msg
    assert "paid_sum" in msg


def test_build_calendar_empty_path_emits_record(caplog):
    with caplog.at_level(logging.INFO, logger="myitbr.dividends"):
        cal = dividends.build_calendar(
            pd.DataFrame({"symbol": ["ABC"]}), pd.DataFrame(), pd.DataFrame(),
        )
    assert cal.empty
    assert any(
        r.name == "myitbr.dividends" and "no events" in r.getMessage()
        for r in caplog.records
    )


def test_parser_emits_section_records(caplog, flex_xml):
    with caplog.at_level(logging.INFO, logger="myitbr.parser"):
        stmt = parser.parse_statement(flex_xml)
        parser.cash_transactions_df(stmt)
        parser.dividend_accruals_df(stmt)
        parser.trades_df(stmt)
        parser.positions_df(stmt)
    messages = [r.getMessage() for r in caplog.records if r.name == "myitbr.parser"]
    joined = " ".join(messages)
    assert "parse_statement" in joined  # statement period/from/to summary
    assert "cash_transactions: 6 rows" in joined
    assert "dividend_accruals: 1 rows" in joined
    assert "trades: 1 rows" in joined
    assert "positions: 1 rows" in joined
    assert "2024-05-01" in joined  # min settle date surfaced for coverage


def test_flex_logs_query_id_never_token(caplog, monkeypatch):
    token = "SECRET-TOKEN-123456"
    monkeypatch.setattr(flex, "_fetch", lambda *, token, query_id, date_range: b"<xml/>")
    with caplog.at_level(logging.DEBUG, logger="myitbr.flex"):
        flex.download_statement(token=token, query_id="qid42", date_range=None)
    records = [r for r in caplog.records if r.name == "myitbr.flex"]
    assert records, "download_statement emitted no log records"
    joined = " ".join(r.getMessage() for r in records)
    assert "qid42" in joined
    assert "6 bytes" in joined  # payload size logged (len of b"<xml/>")
    assert token not in joined


def test_flex_failure_log_scrubs_token(caplog, monkeypatch):
    token = "SECRET-TOKEN-123456"

    def _boom(*, token, query_id, date_range):
        raise RuntimeError(f"GET https://ibkr/Send?t={token}&q=qid42 timed out")

    monkeypatch.setattr(flex, "_fetch", _boom)
    with caplog.at_level(logging.DEBUG, logger="myitbr.flex"):
        with pytest.raises(flex.FlexError):
            flex.download_statement(token=token, query_id="qid42", date_range=None)
    joined = " ".join(
        r.getMessage() for r in caplog.records if r.name == "myitbr.flex"
    )
    assert "failed" in joined
    assert token not in joined
    assert "***TOKEN***" in joined
