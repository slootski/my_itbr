"""Tests for ibkr.flex — error wrapping and transport retry, no network."""
from __future__ import annotations

import datetime as dt
import logging

import pytest
import requests

from ibkr import flex


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Collapse retry backoff so the retry tests stay fast."""
    monkeypatch.setattr(flex.time, "sleep", lambda _s: None)


def test_missing_credentials_raises():
    with pytest.raises(flex.FlexError):
        flex.download_statement(token="", query_id="", date_range=None)
    with pytest.raises(flex.FlexError):
        flex.download_statement(token="tok", query_id="", date_range=None)


def test_response_code_error_is_wrapped(monkeypatch):
    class _RCE(flex.ResponseCodeError):
        def __init__(self):
            self.code = "1003"
            self.message = "Invalid token"

    def _boom(*, token, query_id, date_range):
        raise _RCE()

    monkeypatch.setattr(flex, "_fetch", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert "1003" in str(exc.value)
    assert "Invalid token" in str(exc.value)


def test_generic_error_is_wrapped(monkeypatch):
    def _boom(*, token, query_id, date_range):
        raise RuntimeError("network down")

    monkeypatch.setattr(flex, "_fetch", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert "network down" in str(exc.value)


def test_success_returns_bytes(monkeypatch):
    monkeypatch.setattr(flex, "_fetch", lambda *, token, query_id, date_range: b"<xml/>")
    assert flex.download_statement(token="tok", query_id="qid", date_range=None) == b"<xml/>"


def _counting_download(errors: list[Exception | bytes]):
    """Return a fake download that yields `errors` in order, raising Exceptions."""
    calls: list[int] = []

    def _fake(*, token, query_id, date_range):
        calls.append(1)
        item = errors[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return _fake, calls


def test_connection_error_is_retried_then_succeeds(monkeypatch):
    fake, calls = _counting_download([
        requests.exceptions.ConnectionError("Failed to resolve 'gdcdyn...'"),
        b"<xml/>",
    ])
    monkeypatch.setattr(flex, "_fetch", fake)
    assert flex.download_statement(token="tok", query_id="qid", date_range=None) == b"<xml/>"
    assert len(calls) == 2


def test_timeout_is_retried(monkeypatch):
    fake, calls = _counting_download([
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.Timeout("timed out"),
        b"<xml/>",
    ])
    monkeypatch.setattr(flex, "_fetch", fake)
    assert flex.download_statement(token="tok", query_id="qid", date_range=None) == b"<xml/>"
    assert len(calls) == 3


def test_retries_are_bounded(monkeypatch):
    fake, calls = _counting_download(
        [requests.exceptions.ConnectionError("dns down")] * flex.MAX_ATTEMPTS
    )
    monkeypatch.setattr(flex, "_fetch", fake)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert len(calls) == flex.MAX_ATTEMPTS
    assert f"after {flex.MAX_ATTEMPTS} attempts" in str(exc.value)


def test_response_code_error_is_not_retried(monkeypatch):
    """IBKR answered — retrying would only burn requests toward the 1018 throttle."""
    class _RCE(flex.ResponseCodeError):
        def __init__(self):
            self.code = "1015"
            self.message = "Token is invalid."

    fake, calls = _counting_download([_RCE(), b"<xml/>"])
    monkeypatch.setattr(flex, "_fetch", fake)
    with pytest.raises(flex.FlexError):
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert len(calls) == 1


def test_non_transport_error_is_not_retried(monkeypatch):
    fake, calls = _counting_download([RuntimeError("malformed xml"), b"<xml/>"])
    monkeypatch.setattr(flex, "_fetch", fake)
    with pytest.raises(flex.FlexError):
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert len(calls) == 1


def test_response_code_error_carries_code(monkeypatch):
    class _RCE(flex.ResponseCodeError):
        def __init__(self):
            self.code = "1012"
            self.message = "Token has expired."

    def _boom(*, token, query_id, date_range):
        raise _RCE()

    monkeypatch.setattr(flex, "_fetch", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert exc.value.code == flex.TOKEN_EXPIRED
    assert exc.value.code in flex.TOKEN_ERROR_CODES


def test_non_response_errors_have_no_code(monkeypatch):
    def _boom(*, token, query_id, date_range):
        raise ValueError("garbage xml")

    monkeypatch.setattr(flex, "_fetch", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert exc.value.code is None


# ---- explicit date range (fd/td) -------------------------------------------

class _Resp:
    def __init__(self, content: bytes):
        self.content = content


_ACCESS_OK = (b'<FlexStatementResponse timestamp="27 September, 2026 10:00 AM EDT">'
              b'<Status>Success</Status><ReferenceCode>REF1</ReferenceCode>'
              b'<Url>https://ibkr/GetStatement</Url></FlexStatementResponse>')
_ACCESS_1003 = (b'<FlexStatementResponse timestamp="27 September, 2026 10:00 AM EDT">'
                b'<Status>Fail</Status><ErrorCode>1003</ErrorCode>'
                b'<ErrorMessage>Statement is not available.</ErrorMessage></FlexStatementResponse>')
_RANGE = (dt.date(2025, 9, 26), dt.date(2026, 9, 25))


def test_range_request_sends_fd_td_and_polls(monkeypatch):
    sent: list[tuple[str, dict]] = []

    def fake_get(url, params, headers, timeout):
        sent.append((url, params))
        return _Resp(_ACCESS_OK if "SendRequest" in url else b"<FlexQueryResponse/>")

    monkeypatch.setattr(flex.requests, "get", fake_get)
    out = flex.download_statement(token="tok", query_id="qid", date_range=_RANGE)
    assert out == b"<FlexQueryResponse/>"
    send_params = sent[0][1]
    assert send_params["fd"] == "20250926" and send_params["td"] == "20260925"
    assert send_params["q"] == "qid"
    assert sent[1] == ("https://ibkr/GetStatement", {"v": "3", "t": "tok", "q": "REF1"})


def test_query_period_request_omits_fd_td(monkeypatch):
    sent: list[dict] = []

    def fake_get(url, params, headers, timeout):
        sent.append(params)
        return _Resp(_ACCESS_OK if "SendRequest" in url else b"<FlexQueryResponse/>")

    monkeypatch.setattr(flex.requests, "get", fake_get)
    out = flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert out == b"<FlexQueryResponse/>"
    assert sent[0] == {"v": "3", "t": "tok", "q": "qid"}


# ---- host failover (gdcdyn <-> ndcdyn) -------------------------------------

_GDC = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_NDC = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_ACCESS_GDC = _ACCESS_OK.replace(b"https://ibkr/GetStatement", f"{_GDC}/GetStatement".encode())


def _fake_get_with_dead_host(dead_host: str, calls: list[str]):
    """requests.get that fails DNS for ``dead_host`` and answers on any other host."""
    def _fake(url, params, headers, timeout):
        calls.append(url)
        if dead_host in url:
            raise requests.exceptions.ConnectionError(f"Failed to resolve '{dead_host}'")
        return _Resp(_ACCESS_GDC if "SendRequest" in url else b"<FlexQueryResponse/>")
    return _fake


def test_statement_fails_over_from_default_host_to_new_host(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(flex.requests, "get",
                        _fake_get_with_dead_host("gdcdyn.interactivebrokers.com", calls))
    out = flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert out == b"<FlexQueryResponse/>"
    assert calls == [
        f"{_NDC}/SendRequest",
        f"{_GDC}/GetStatement",
        f"{_NDC}/GetStatement",
    ]


def test_send_request_fails_over_from_new_host_to_default_host(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(flex.requests, "get",
                        _fake_get_with_dead_host("ndcdyn.interactivebrokers.com", calls))
    out = flex.download_statement(token="tok", query_id="qid", date_range=_RANGE)
    assert out == b"<FlexQueryResponse/>"
    assert calls == [
        f"{_NDC}/SendRequest",
        f"{_GDC}/SendRequest",
        f"{_GDC}/GetStatement",
    ]


def test_all_hosts_down_uses_outer_retry_then_fails(monkeypatch):
    calls: list[str] = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        raise requests.exceptions.ConnectionError("dns down")

    monkeypatch.setattr(flex.requests, "get", fake_get)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert len(calls) == flex.MAX_ATTEMPTS * len(flex.FLEX_HOSTS)
    assert f"after {flex.MAX_ATTEMPTS} attempts" in str(exc.value)


def test_response_code_error_does_not_fail_over(monkeypatch):
    calls: list[str] = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        return _Resp(_ACCESS_1003)

    monkeypatch.setattr(flex.requests, "get", fake_get)
    with pytest.raises(flex.FlexError):
        flex.download_statement(token="tok", query_id="qid", date_range=None)
    assert calls == [f"{_NDC}/SendRequest"]


def test_failover_warning_scrubs_token(caplog, monkeypatch):
    token = "SECRET-TOKEN-123456"
    calls: list[str] = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError(f"GET {url}?t={params['t']} failed")
        return _Resp(_ACCESS_GDC if "SendRequest" in url else b"<FlexQueryResponse/>")

    monkeypatch.setattr(flex.requests, "get", fake_get)
    with caplog.at_level(logging.DEBUG, logger="myitbr.flex"):
        flex.download_statement(token=token, query_id="qid", date_range=None)
    joined = " ".join(r.getMessage() for r in caplog.records if r.name == "myitbr.flex")
    assert "unreachable" in joined
    assert token not in joined


def test_unknown_statement_host_is_tried_first_then_flex_hosts(monkeypatch):
    calls: list[str] = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        if url.startswith("https://ibkr/"):
            raise requests.exceptions.ConnectionError("dns down")
        return _Resp(_ACCESS_OK if "SendRequest" in url else b"<FlexQueryResponse/>")

    monkeypatch.setattr(flex.requests, "get", fake_get)
    assert flex.download_statement(token="tok", query_id="qid", date_range=None) == b"<FlexQueryResponse/>"
    assert calls == [f"{_NDC}/SendRequest", "https://ibkr/GetStatement",
                     "https://gdcdyn.interactivebrokers.com/GetStatement"]


def test_range_error_status_carries_code_and_is_not_retried(monkeypatch):
    calls: list[int] = []

    def fake_get(url, params, headers, timeout):
        calls.append(1)
        return _Resp(_ACCESS_1003)

    monkeypatch.setattr(flex.requests, "get", fake_get)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement(token="tok", query_id="qid", date_range=_RANGE)
    assert exc.value.code == "1003"
    assert len(calls) == 1


def test_range_polling_is_bounded(monkeypatch):
    busy = (b'<FlexStatementResponse timestamp="27 September, 2026 10:00 AM EDT">'
            b'<Status>Warn</Status><ErrorCode>1019</ErrorCode>'
            b'<ErrorMessage>Statement generation in progress.</ErrorMessage></FlexStatementResponse>')
    polls: list[int] = []

    def fake_submit(url, token, query):
        polls.append(1)
        return _Resp(busy)

    def fake_get(url, params, headers, timeout):
        if "SendRequest" in url:
            return _Resp(_ACCESS_OK)
        return fake_submit(url, params["t"], params["q"])

    monkeypatch.setattr(flex.requests, "get", fake_get)
    with pytest.raises(flex.FlexError):
        flex.download_statement(token="tok", query_id="qid", date_range=_RANGE)
    assert len(polls) == flex.MAX_POLLS


def test_range_failure_log_scrubs_token(caplog, monkeypatch):
    token = "SECRET-TOKEN-123456"

    def fake_get(url, params, headers, timeout):
        raise RuntimeError(f"GET https://ibkr/Send?t={params['t']}&fd={params['fd']} failed")

    monkeypatch.setattr(flex.requests, "get", fake_get)
    with caplog.at_level(logging.DEBUG, logger="myitbr.flex"):
        with pytest.raises(flex.FlexError):
            flex.download_statement(token=token, query_id="qid", date_range=_RANGE)
    joined = " ".join(r.getMessage() for r in caplog.records if r.name == "myitbr.flex")
    assert token not in joined
    assert "2025-09-26..2026-09-25" in joined
