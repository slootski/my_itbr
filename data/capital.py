"""Splitting net liquidation into money paid in versus money earned.

A NAV line answers "how much is in there", not "how much of it is mine". An
account that grows purely because it is fed monthly looks identical to one that
grows because its holdings did well. Subtracting cumulative net contributions
from NAV separates the two.

Contributions come from the ``DEPOSITWITHDRAW`` cash transactions. They are
summed in the account's **base** currency (``amount_base``), never in the
transaction's own: funding a USD account from an ILS bank posts an ILS amount,
and adding that to a USD dividend overstates contributions by roughly the
exchange rate.
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

logger = logging.getLogger("myitbr.capital")

# IBKR posts both directions of external cash movement under this one type; a
# withdrawal is simply a negative amount, so a plain sum is net contributions.
DEPOSIT_TYPE = "DEPOSITWITHDRAW"

# Dividend income, net of the withholding IBKR posts as a separate negative row
# — the same definition the Dividends tab and the Positions heatmap use, so the
# three never disagree about what "dividends received" means.
DIVIDEND_TYPES = frozenset({"DIVIDEND", "PAYMENTINLIEU"})
WHTAX_TYPE = "WHTAX"

_DATE_COLS = ("settle_date", "datetime", "report_date")


def _event_date(row) -> dt.date | None:
    """Settlement date preferred, matching how the rest of the app dates cash."""
    for col in _DATE_COLS:
        value = row.get(col)
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        try:
            stamp = pd.to_datetime(value)
        except (ValueError, TypeError):
            continue
        if not pd.isna(stamp):
            return stamp.date()
    return None


def _base_amount(row) -> float | None:
    """The row's value in base currency, or None when it cannot be trusted.

    Rows archived before the currency columns existed carry no ``amount_base``.
    Falling back to the raw ``amount`` there would silently mix currencies —
    the exact bug this module exists to avoid — so those rows are dropped and
    reported instead.
    """
    value = row.get("amount_base")
    if value is None or pd.isna(value):
        return None
    return float(value)


def contributions(cash_tx: pd.DataFrame) -> pd.DataFrame:
    """Per-day net external cash movement, in base currency.

    Returns a frame of ``date`` / ``amount`` / ``cumulative``, oldest first.
    Empty when the archive holds no usable deposit rows.
    """
    empty = pd.DataFrame(columns=["date", "amount", "cumulative"])
    if cash_tx is None or cash_tx.empty or "type" not in cash_tx:
        return empty

    rows = cash_tx[cash_tx["type"].astype(str) == DEPOSIT_TYPE]
    if rows.empty:
        return empty

    records, unpriced = [], 0
    for _, row in rows.iterrows():
        amount = _base_amount(row)
        date = _event_date(row)
        if amount is None:
            unpriced += 1
            continue
        if date is None:
            continue
        records.append({"date": date, "amount": amount})

    if unpriced:
        # Loud, because the number on screen is wrong-by-omission until the next
        # refresh backfills the currency columns onto these rows.
        logger.warning(
            "capital: %d deposit row(s) have no base-currency amount and were "
            "excluded; refresh to backfill currency data",
            unpriced,
        )
    if not records:
        return empty

    df = pd.DataFrame(records).groupby("date", as_index=False)["amount"].sum()
    df = df.sort_values("date").reset_index(drop=True)
    df["cumulative"] = df["amount"].cumsum()
    return df


def dividend_income(cash_tx: pd.DataFrame) -> pd.DataFrame:
    """Per-day net dividend income, in base currency, oldest first.

    Withholding tax is folded in, so this is cash that actually landed. Same
    base-currency rule as ``contributions``: a dividend paid in a non-base
    currency cannot be added to one paid in base without its rate.
    """
    empty = pd.DataFrame(columns=["date", "amount", "cumulative"])
    if cash_tx is None or cash_tx.empty or "type" not in cash_tx:
        return empty

    kinds = cash_tx["type"].astype(str)
    rows = cash_tx[kinds.isin(DIVIDEND_TYPES) | (kinds == WHTAX_TYPE)]
    if rows.empty:
        return empty

    records = []
    for _, row in rows.iterrows():
        amount, date = _base_amount(row), _event_date(row)
        if amount is None or date is None:
            continue
        records.append({"date": date, "amount": amount})
    if not records:
        return empty

    df = pd.DataFrame(records).groupby("date", as_index=False)["amount"].sum()
    df = df.sort_values("date").reset_index(drop=True)
    df["cumulative"] = df["amount"].cumsum()
    return df


def unpriced_deposits(cash_tx: pd.DataFrame) -> int:
    """How many deposit rows lack a base-currency amount (pre-migration rows)."""
    if cash_tx is None or cash_tx.empty or "type" not in cash_tx:
        return 0
    rows = cash_tx[cash_tx["type"].astype(str) == DEPOSIT_TYPE]
    if rows.empty or "amount_base" not in rows:
        return len(rows)
    return int(rows["amount_base"].isna().sum())


def composition(nav_history: pd.DataFrame, cash_tx: pd.DataFrame) -> pd.DataFrame:
    """NAV per day split into contributed capital and earnings.

    ``earnings`` is NAV minus everything paid in as of that day — trading gains,
    dividends and interest together, net of fees. It goes negative when the
    account is worth less than what was put into it, which is a real state and
    is left as-is rather than clipped. ``dividends`` is the cumulative income
    portion *of* that figure, not an extra on top.

    The cumulative contribution is carried forward across days with no deposit,
    so the series is defined on every NAV date, not only on funding days.
    """
    cols = ["date", "net_liq", "contributed", "dividends", "earnings"]
    if nav_history is None or nav_history.empty:
        return pd.DataFrame(columns=cols)

    date_col = "snapshot_date" if "snapshot_date" in nav_history else "date"
    if date_col not in nav_history or "net_liq" not in nav_history:
        return pd.DataFrame(columns=cols)

    nav = nav_history[[date_col, "net_liq"]].copy()
    nav["date"] = pd.to_datetime(nav[date_col], errors="coerce").dt.date
    nav = nav.dropna(subset=["date"]).sort_values("date")
    nav["net_liq"] = pd.to_numeric(nav["net_liq"], errors="coerce")
    nav = nav.dropna(subset=["net_liq"])
    if nav.empty:
        return pd.DataFrame(columns=cols)

    contrib = contributions(cash_tx)
    if contrib.empty:
        return pd.DataFrame(columns=cols)

    # As-of join: each NAV day takes the latest cumulative contribution at or
    # before it. merge_asof needs sorted datetime keys on both sides.
    left = nav.assign(_k=pd.to_datetime(nav["date"]))
    right = contrib.assign(_k=pd.to_datetime(contrib["date"]))[["_k", "cumulative"]]
    merged = pd.merge_asof(left.sort_values("_k"), right.sort_values("_k"), on="_k")

    merged["contributed"] = merged["cumulative"].fillna(0.0)
    merged["earnings"] = merged["net_liq"] - merged["contributed"]

    # Dividends are a slice of earnings, not an addition to them: the cash is
    # already inside net_liq. Carried as its own column so the chart can show
    # how much of the growth was income rather than price movement.
    income = dividend_income(cash_tx)
    if income.empty:
        merged["dividends"] = 0.0
    else:
        right_inc = income.assign(_k=pd.to_datetime(income["date"]))[["_k", "cumulative"]]
        right_inc = right_inc.rename(columns={"cumulative": "dividends"})
        merged = pd.merge_asof(
            merged.sort_values("_k"), right_inc.sort_values("_k"), on="_k"
        )
        merged["dividends"] = merged["dividends"].fillna(0.0)
    return merged[cols].reset_index(drop=True)


# ---- range filtering ------------------------------------------------------

# Labels are what the user picks; the value is the lookback in days, or None for
# everything. YTD is special-cased since its length depends on today's date.
RANGES = ("YTD", "1Y", "ALL")
DEFAULT_RANGE = "ALL"


def range_start(label: str, today: dt.date | None = None) -> dt.date | None:
    """First date included by a range label, or None when unbounded."""
    today = today or dt.date.today()
    if label == "YTD":
        return dt.date(today.year, 1, 1)
    if label == "1Y":
        return today - dt.timedelta(days=365)
    return None


def clip(df: pd.DataFrame, label: str, today: dt.date | None = None) -> pd.DataFrame:
    """Rows of ``df`` (a ``date`` column) falling inside the named range."""
    start = range_start(label, today)
    if start is None or df is None or df.empty or "date" not in df:
        return df
    return df[df["date"] >= start].reset_index(drop=True)
