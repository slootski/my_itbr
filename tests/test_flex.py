"""Tests for ibkr.flex — error wrapping and transport retry, no network."""
from __future__ import annotations

import pytest
import requests

from ibkr import flex


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Collapse retry backoff so the retry tests stay fast."""
    monkeypatch.setattr(flex.time, "sleep", lambda _s: None)


def test_missing_credentials_raises():
    with pytest.raises(flex.FlexError):
        flex.download_statement("", "")
    with pytest.raises(flex.FlexError):
        flex.download_statement("tok", "")


def test_response_code_error_is_wrapped(monkeypatch):
    class _RCE(flex.ResponseCodeError):
        def __init__(self):
            self.code = "1003"
            self.message = "Invalid token"

    def _boom(token, query_id):
        raise _RCE()

    monkeypatch.setattr(flex.flex_client, "download", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement("tok", "qid")
    assert "1003" in str(exc.value)
    assert "Invalid token" in str(exc.value)


def test_generic_error_is_wrapped(monkeypatch):
    def _boom(token, query_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(flex.flex_client, "download", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement("tok", "qid")
    assert "network down" in str(exc.value)


def test_success_returns_bytes(monkeypatch):
    monkeypatch.setattr(flex.flex_client, "download", lambda t, q: b"<xml/>")
    assert flex.download_statement("tok", "qid") == b"<xml/>"


def _counting_download(errors: list[Exception | bytes]):
    """Return a fake download that yields `errors` in order, raising Exceptions."""
    calls: list[int] = []

    def _fake(token, query_id):
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
    monkeypatch.setattr(flex.flex_client, "download", fake)
    assert flex.download_statement("tok", "qid") == b"<xml/>"
    assert len(calls) == 2


def test_timeout_is_retried(monkeypatch):
    fake, calls = _counting_download([
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.Timeout("timed out"),
        b"<xml/>",
    ])
    monkeypatch.setattr(flex.flex_client, "download", fake)
    assert flex.download_statement("tok", "qid") == b"<xml/>"
    assert len(calls) == 3


def test_retries_are_bounded(monkeypatch):
    fake, calls = _counting_download(
        [requests.exceptions.ConnectionError("dns down")] * flex.MAX_ATTEMPTS
    )
    monkeypatch.setattr(flex.flex_client, "download", fake)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement("tok", "qid")
    assert len(calls) == flex.MAX_ATTEMPTS
    assert f"after {flex.MAX_ATTEMPTS} attempts" in str(exc.value)


def test_response_code_error_is_not_retried(monkeypatch):
    """IBKR answered — retrying would only burn requests toward the 1018 throttle."""
    class _RCE(flex.ResponseCodeError):
        def __init__(self):
            self.code = "1015"
            self.message = "Token is invalid."

    fake, calls = _counting_download([_RCE(), b"<xml/>"])
    monkeypatch.setattr(flex.flex_client, "download", fake)
    with pytest.raises(flex.FlexError):
        flex.download_statement("tok", "qid")
    assert len(calls) == 1


def test_non_transport_error_is_not_retried(monkeypatch):
    fake, calls = _counting_download([RuntimeError("malformed xml"), b"<xml/>"])
    monkeypatch.setattr(flex.flex_client, "download", fake)
    with pytest.raises(flex.FlexError):
        flex.download_statement("tok", "qid")
    assert len(calls) == 1


def test_response_code_error_carries_code(monkeypatch):
    class _RCE(flex.ResponseCodeError):
        def __init__(self):
            self.code = "1012"
            self.message = "Token has expired."

    def _boom(token, query_id):
        raise _RCE()

    monkeypatch.setattr(flex.flex_client, "download", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement("tok", "qid")
    assert exc.value.code == flex.TOKEN_EXPIRED
    assert exc.value.code in flex.TOKEN_ERROR_CODES


def test_non_response_errors_have_no_code(monkeypatch):
    def _boom(token, query_id):
        raise ValueError("garbage xml")

    monkeypatch.setattr(flex.flex_client, "download", _boom)
    with pytest.raises(flex.FlexError) as exc:
        flex.download_statement("tok", "qid")
    assert exc.value.code is None
