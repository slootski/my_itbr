"""The one place dividend income is defined and calculated.

Every screen that shows dividends received — the Dividends calendar, the Net
Worth dividend band, the Prices tab and the Positions heatmap — reads from here,
so they cannot disagree about the total.

Definitions:

- **Income rows** are cash transactions of type ``DIVIDEND`` or
  ``PAYMENTINLIEU`` (gross, positive). **Tax rows** are ``WHTAX`` (withholding,
  negative; a refund is positive).
- **Amounts are in the account's base currency** (``amount_base``), so a
  dividend paid in another currency is never added raw. A frame without that
  column at all (hand-built frames) uses ``amount``. A row whose ``amount_base``
  is empty is excluded and counted in ``unpriced``, never guessed.
- **After tax is the cash that actually landed**: gross plus IBKR's withholding.
  When IBKR withheld nothing, after tax equals gross. No rate is assumed.
- **One event per payment**: income rows sharing symbol, ex-date and pay date are
  one payment. Tax joins its payment by (symbol, ex-date), because IBKR may post
  the withholding on a different day or month than the dividend it belongs to.
  Tax with no matching payment stays as its own event on its own date, so it is
  never dropped.
- **Pay date** is the settle date, then the transaction datetime, then the report
  date. Rows with none of them are excluded.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

INCOME_TYPES = frozenset({"DIVIDEND", "PAYMENTINLIEU"})
TAX_TYPE = "WHTAX"

EVENT_COLS = ["symbol", "ex_date", "pay_date", "gross", "tax", "net", "payments"]


def _to_date(*, value) -> dt.date | None:
    # NaT is a datetime instance, so it must be caught before the isinstance checks.
    if value is None or value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        stamp = pd.to_datetime(value, errors="coerce")
    except (ValueError, TypeError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def pay_date(*, row) -> dt.date | None:
    """Settle date, then datetime, then report date."""
    return (
        _to_date(value=row.get("settle_date"))
        or _to_date(value=row.get("datetime"))
        or _to_date(value=row.get("report_date"))
    )


def _value(*, row, has_base: bool) -> float | None:
    raw = row.get("amount_base") if has_base else row.get("amount")
    if raw is None or pd.isna(raw):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _symbol(*, value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def events(*, cash_tx: pd.DataFrame | None) -> tuple[pd.DataFrame, int]:
    """All dividend payments, oldest first, plus the count of unpriced rows.

    Columns: ``symbol``, ``ex_date``, ``pay_date``, ``gross``, ``tax`` (<= 0 for
    withholding), ``net`` (= gross + tax), ``payments`` (income rows folded in).
    """
    empty = pd.DataFrame(columns=EVENT_COLS)
    if cash_tx is None or cash_tx.empty or "type" not in cash_tx:
        return empty, 0

    has_base = "amount_base" in cash_tx
    kinds = cash_tx["type"].astype(str)
    rows = cash_tx[kinds.isin(INCOME_TYPES) | (kinds == TAX_TYPE)]
    unpriced = 0
    payments: dict[tuple, dict] = {}
    # (symbol, ex_date) -> key of the latest payment carrying that ex-date.
    by_ex: dict[tuple, tuple] = {}
    taxes: list[tuple[str, dt.date | None, dt.date | None, float]] = []

    for row in rows.to_dict("records"):
        sym = _symbol(value=row.get("symbol"))
        if sym is None:
            continue
        amount = _value(row=row, has_base=has_base)
        if amount is None:
            unpriced += 1
            continue
        ex = _to_date(value=row.get("ex_date"))
        paid = pay_date(row=row)
        if str(row.get("type")) == TAX_TYPE:
            taxes.append((sym, ex, paid, amount))
            continue
        if paid is None:
            continue
        key = (sym, ex, paid)
        event = payments.setdefault(key, {"symbol": sym, "ex_date": ex, "pay_date": paid,
                                          "gross": 0.0, "tax": 0.0, "payments": 0})
        event["gross"] += amount
        event["payments"] += 1
        latest = by_ex.get((sym, ex))
        if latest is None or paid > latest[2]:
            by_ex[(sym, ex)] = key

    for sym, ex, paid, amount in taxes:
        key = by_ex.get((sym, ex))
        if key is None:
            if paid is None:
                continue
            key = (sym, ex, paid, "tax-only")
            payments.setdefault(key, {"symbol": sym, "ex_date": ex, "pay_date": paid,
                                      "gross": 0.0, "tax": 0.0, "payments": 0})
        payments[key]["tax"] += amount

    if not payments:
        return empty, unpriced
    df = pd.DataFrame(list(payments.values()))
    df["net"] = df["gross"] + df["tax"]
    df = df.sort_values(["pay_date", "symbol"]).reset_index(drop=True)
    return df[EVENT_COLS], unpriced


def _ym(*, date: dt.date) -> str:
    return f"{date.year:04d}-{date.month:02d}"


def by_symbol_month(*, ledger: pd.DataFrame) -> dict[str, dict[str, dict]]:
    """symbol -> {"YYYY-MM": {"gross", "net"}}, bucketed by pay month."""
    out: dict[str, dict[str, dict]] = {}
    for event in ledger.to_dict("records"):
        cell = out.setdefault(event["symbol"], {}).setdefault(
            _ym(date=event["pay_date"]), {"gross": 0.0, "net": 0.0})
        cell["gross"] += event["gross"]
        cell["net"] += event["net"]
    return out


def by_day(*, ledger: pd.DataFrame, net: bool) -> pd.DataFrame:
    """Per pay date income, oldest first: ``date``, ``amount``, ``cumulative``."""
    if ledger.empty:
        return pd.DataFrame(columns=["date", "amount", "cumulative"])
    column = "net" if net else "gross"
    df = (ledger.groupby("pay_date", as_index=False)[column].sum()
          .rename(columns={"pay_date": "date", column: "amount"}))
    df = df.sort_values("date").reset_index(drop=True)
    df["cumulative"] = df["amount"].cumsum()
    return df


def for_symbol(*, ledger: pd.DataFrame, symbol: str, ttm_start: dt.date) -> dict:
    """Totals for one symbol: gross, net, payment count, first/last payment, TTM gross.

    ``ttm_start`` is the first day counted in ``ttm_gross`` (the yield basis).
    Values are None when the symbol has no dividend events.
    """
    out = {"gross": None, "net": None, "count": 0, "last_amount": None,
           "last_date": None, "ttm_gross": None, "first_date": None}
    rows = ledger[ledger["symbol"] == str(symbol)] if not ledger.empty else ledger
    if rows.empty:
        return out
    paid = rows[rows["payments"] > 0]
    ttm = float(paid.loc[paid["pay_date"] >= ttm_start, "gross"].sum())
    out.update({
        "gross": float(rows["gross"].sum()),
        "net": float(rows["net"].sum()),
        "count": int(rows["payments"].sum()),
        "ttm_gross": ttm or None,
    })
    if not paid.empty:
        last = paid.iloc[-1]
        out.update({"last_amount": float(last["gross"]), "last_date": last["pay_date"],
                    "first_date": paid.iloc[0]["pay_date"]})
    return out


def total(*, ledger: pd.DataFrame, net: bool, start: dt.date | None, end: dt.date | None) -> float:
    """Sum over pay dates in [start, end]; None on a side means unbounded."""
    if ledger.empty:
        return 0.0
    mask = pd.Series(True, index=ledger.index)
    if start is not None:
        mask &= ledger["pay_date"] >= start
    if end is not None:
        mask &= ledger["pay_date"] <= end
    return float(ledger.loc[mask, "net" if net else "gross"].sum())
