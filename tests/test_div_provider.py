"""Tests for data.div_provider — Polygon declared-dividend lookup, no network."""
from __future__ import annotations

import datetime as dt

from data import div_cache, div_provider


class _FakeResp:
    def __init__(self, payload=None, status=200, headers=None, boom=False):
        self._payload = {"results": payload or []}
        self.status_code = status
        self.headers = headers or {}
        self._boom = boom

    def raise_for_status(self):
        if self._boom:
            raise RuntimeError("http 500")

    def json(self):
        return self._payload


def _patch(monkeypatch, payload=None, boom=False, status=200):
    monkeypatch.setattr(
        div_provider.requests, "get",
        lambda *a, **k: _FakeResp(payload, status=status, boom=boom),
    )


def _patch_seq(monkeypatch, responses):
    """Patch requests.get to return each response in turn (for retry tests)."""
    it = iter(responses)
    monkeypatch.setattr(div_provider.requests, "get", lambda *a, **k: next(it))
    monkeypatch.setattr(div_provider.time, "sleep", lambda *_: None)


_TODAY = dt.date(2026, 6, 12)


def test_no_key_or_symbol_returns_empty(monkeypatch):
    # Never even hits the network without a key.
    _patch(monkeypatch, boom=True)
    assert div_provider.fetch_declared("COPX", "", today=_TODAY) == {}
    assert div_provider.fetch_declared("", "k", today=_TODAY) == {}


def test_picks_soonest_upcoming_with_amount(monkeypatch):
    _patch(monkeypatch, payload=[
        {"ex_dividend_date": "2026-12-29", "pay_date": "2027-01-06",
         "declaration_date": "2026-07-01", "cash_amount": 0.30, "currency": "USD"},
        {"ex_dividend_date": "2026-06-29", "pay_date": "2026-07-07",
         "declaration_date": "2026-01-20", "cash_amount": 0.258, "currency": "USD"},
    ])
    out = div_provider.fetch_declared("COPX", "k", today=_TODAY)
    assert out["ex_date"] == dt.date(2026, 6, 29)
    assert out["pay_date"] == dt.date(2026, 7, 7)
    assert out["decl_date"] == dt.date(2026, 1, 20)
    assert out["amount"] == 0.258
    assert out["currency"] == "USD"


def test_skips_past_and_amountless_records(monkeypatch):
    _patch(monkeypatch, payload=[
        {"ex_dividend_date": "2026-01-10", "cash_amount": 0.5},   # past
        {"ex_dividend_date": "2026-08-01", "cash_amount": None},  # no amount
    ])
    assert div_provider.fetch_declared("COPX", "k", today=_TODAY) == {}


def test_network_error_degrades_to_empty(monkeypatch):
    _patch(monkeypatch, boom=True)
    assert div_provider.fetch_declared("COPX", "k", today=_TODAY) == {}


def test_retries_429_then_succeeds(monkeypatch):
    rec = [{"ex_dividend_date": "2026-06-29", "cash_amount": 0.258, "currency": "USD"}]
    _patch_seq(monkeypatch, [
        _FakeResp(status=429, headers={"Retry-After": "1"}),
        _FakeResp(payload=rec),
    ])
    status, data = div_provider._fetch_raw("COPX", "k", _TODAY)
    assert status == "ok"
    assert data["amount"] == 0.258


def test_persistent_429_is_unavailable(monkeypatch):
    _patch_seq(monkeypatch, [_FakeResp(status=429) for _ in range(5)])
    status, data = div_provider._fetch_raw("COPX", "k", _TODAY)
    assert status == "unavailable"
    assert data == {}
    # Public helper still degrades to {} for back-compat.
    _patch_seq(monkeypatch, [_FakeResp(status=429) for _ in range(5)])
    assert div_provider.fetch_declared("COPX", "k", today=_TODAY) == {}


# ---- fetch_declared_many (cached batch) -----------------------------------

def _record():
    return {"ex_date": dt.date(2026, 6, 29), "pay_date": dt.date(2026, 7, 7),
            "decl_date": dt.date(2026, 1, 20), "amount": 0.258, "currency": "USD"}


def test_many_uses_fresh_cache_without_network(tmp_path, monkeypatch):
    db = tmp_path / "c.sqlite"
    div_cache.put(db, "COPX", _record(), _TODAY)

    def _boom(*_a, **_k):
        raise AssertionError("should not hit the network on a fresh cache hit")
    monkeypatch.setattr(div_provider, "_fetch_raw", _boom)

    out = div_provider.fetch_declared_many(["COPX"], "k", str(db), today=_TODAY)
    assert out["COPX"]["amount"] == 0.258
    assert out["COPX"]["ex_date"] == dt.date(2026, 6, 29)  # date round-trips


def test_many_unavailable_falls_back_to_stale_cache(tmp_path, monkeypatch):
    db = tmp_path / "c.sqlite"
    div_cache.put(db, "COPX", _record(), dt.date(2026, 6, 1))  # stale (older day)
    monkeypatch.setattr(div_provider, "_fetch_raw", lambda *_a, **_k: ("unavailable", {}))

    out = div_provider.fetch_declared_many(["COPX"], "k", str(db), today=_TODAY)
    assert out["COPX"]["amount"] == 0.258  # served stale, not dropped to red


def test_many_caches_ok_empty_and_skips_refetch(tmp_path, monkeypatch):
    db = tmp_path / "c.sqlite"
    calls = {"n": 0}

    def _fetch(*_a, **_k):
        calls["n"] += 1
        return "ok", {}  # genuine "no upcoming dividend"
    monkeypatch.setattr(div_provider, "_fetch_raw", _fetch)

    out1 = div_provider.fetch_declared_many(["PLTR"], "k", str(db), today=_TODAY)
    assert out1 == {}                       # empty omitted from result
    out2 = div_provider.fetch_declared_many(["PLTR"], "k", str(db), today=_TODAY)
    assert out2 == {}
    assert calls["n"] == 1                  # second call served from the empty marker


def test_many_no_key_returns_empty(tmp_path):
    out = div_provider.fetch_declared_many(["COPX"], "", str(tmp_path / "c.sqlite"))
    assert out == {}
