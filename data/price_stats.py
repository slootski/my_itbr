"""Per-symbol price/position statistics for the Prices tab.

Combines three sources that the tab already has in hand:

  - the yfinance OHLC frame (:mod:`data.prices`) — market price, ranges,
    volatility, volume;
  - the parsed Flex open positions — what *I* paid (``cost_price``), how many
    shares, unrealized P&L;
  - the parsed Flex cash transactions — dividends actually received for this
    symbol, gross and net of withholding.

Everything is defensive: a missing column, an unheld symbol or an empty frame
yields ``None`` for the affected field rather than raising, so the tab renders
with whatever it can compute.
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from data import div_ledger

logger = logging.getLogger("myitbr.price_stats")

# Trading days per year, for annualizing the daily-return standard deviation.
_TRADING_DAYS = 252
# Wilder's default lookback for Average True Range.
_ATR_WINDOW = 14
# A fund accrues its expense ratio every calendar day, weekends included.
_DAYS_PER_YEAR = 365


def _f(value) -> float | None:
    """Coerce to float, mapping None/NaN/garbage to None."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def _to_date(value) -> dt.date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        ts = pd.to_datetime(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(ts) else ts.date()


def _pct(new: float | None, old: float | None) -> float | None:
    """Percent change from ``old`` to ``new`` (None when undefined)."""
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def moving_average(ohlc: pd.DataFrame, window: int) -> pd.Series | None:
    """Rolling mean of close over ``window`` bars.

    Returns None when the frame is too short for a single full window, so the
    caller can skip the overlay instead of drawing an all-NaN line.
    """
    if ohlc is None or ohlc.empty or "close" not in ohlc or len(ohlc) < window:
        return None
    return ohlc["close"].rolling(window).mean()


def _atr(ohlc: pd.DataFrame, window: int = _ATR_WINDOW) -> float | None:
    """Average True Range over the last ``window`` bars."""
    needed = {"high", "low", "close"}
    if ohlc is None or ohlc.empty or not needed.issubset(ohlc.columns) or len(ohlc) < window + 1:
        return None
    high, low, prev_close = ohlc["high"], ohlc["low"], ohlc["close"].shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return _f(true_range.rolling(window).mean().iloc[-1])


def dividends_received(cash_tx: pd.DataFrame, symbol: str) -> dict:
    """Everything IBKR has paid me on ``symbol``, from the shared dividend ledger.

    ``gross`` sums DIVIDEND + PAYMENTINLIEU; ``net`` is the cash that landed
    (withholding folded in). ``last_amount``/``last_date`` describe the most
    recent payment, and ``ttm_gross`` covers the trailing 12 months (the basis
    for the yield-on-price figure). Same figures as the Dividends tab.
    """
    ledger, _ = div_ledger.events(cash_tx=cash_tx)
    if not symbol:
        ledger = ledger.iloc[0:0]
    return div_ledger.for_symbol(
        ledger=ledger, symbol=symbol, ttm_start=dt.date.today() - dt.timedelta(days=365),
    )


def expense_cost_per_year(*, expense_ratio_pct: float | None,
                          market_value: float | None) -> float | None:
    """What a fund's expense ratio takes from a holding in a year, at today's value.

    The fund deducts it from its own assets, so it is already inside the price
    and never shows as a charge on the account. None when either input is missing.
    """
    if expense_ratio_pct is None or market_value is None:
        return None
    return abs(market_value) * expense_ratio_pct / 100.0


def _signed_trades(*, trades: pd.DataFrame | None, symbol: str) -> pd.DataFrame:
    """One row per trade of ``symbol``: ``day`` and ``signed`` quantity (sells negative)."""
    empty = pd.DataFrame({"day": pd.Series(dtype="datetime64[ns]"), "signed": pd.Series(dtype=float)})
    needed = {"symbol", "trade_date", "side", "quantity"}
    if trades is None or trades.empty or not needed.issubset(trades.columns):
        return empty
    rows = trades[trades["symbol"].astype(str) == str(symbol)]
    quantity = pd.to_numeric(rows["quantity"], errors="coerce").abs()
    sells = rows["side"].astype(str).str.upper().str.startswith("S")
    out = pd.DataFrame({
        "day": pd.to_datetime(rows["trade_date"], errors="coerce").dt.normalize(),
        "signed": quantity.where(~sells, -quantity),
    }).dropna()
    return out.sort_values("day") if not out.empty else empty


def first_trade_date(*, trades: pd.DataFrame | None, symbol: str) -> dt.date | None:
    """Day of the earliest known trade of ``symbol`` (None when there is none)."""
    moves = _signed_trades(trades=trades, symbol=symbol)
    return None if moves.empty else moves["day"].min().date()


def expense_paid(
    *, closes: pd.DataFrame | None, trades: pd.DataFrame | None, symbol: str,
    quantity: float | None, expense_ratio_pct: float | None, today: dt.date,
) -> dict:
    """Estimate of what a fund's expense ratio has taken from my holding so far.

    The fund never bills it: it is deducted from the fund's assets every day, so
    no statement carries the amount. It is rebuilt here as, for each calendar day
    held, shares x close x ratio / 365. Shares on a day are today's count minus
    every later trade, so the figure stays right when the oldest trades are missing.

    ``amount`` is in the price currency and ``since`` is the first day counted:
    the first known trade, or the first close when ``closes`` starts later. Both
    are None when an input is missing. Today's ratio is applied to every day.
    """
    out = {"amount": None, "since": None}
    if (expense_ratio_pct is None or quantity is None or closes is None or closes.empty
            or not {"date", "close"}.issubset(closes.columns)):
        return out
    moves = _signed_trades(trades=trades, symbol=symbol)
    if moves.empty:
        return out
    days = pd.to_datetime(closes["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    close = pd.Series(closes["close"].to_numpy(), index=days).dropna()
    close = close[close.index.notna() & ~close.index.duplicated(keep="last")].sort_index()
    if close.empty:
        return out
    start, end = max(moves["day"].min(), close.index.min()), pd.Timestamp(today)
    if start > end:
        return out
    calendar = pd.date_range(start, end, freq="D")
    # Weekends and holidays accrue at the last close before them.
    price = close.reindex(calendar, method="ffill")
    traded = moves.groupby("day")["signed"].sum().reindex(calendar, fill_value=0.0)
    bought_later = traded[::-1].cumsum()[::-1].shift(-1, fill_value=0.0)
    held = (quantity - bought_later).clip(lower=0.0)
    amount = float((held * price).sum()) * expense_ratio_pct / 100.0 / _DAYS_PER_YEAR
    out.update({"amount": amount, "since": start.date()})
    return out


def _position_row(positions: pd.DataFrame, symbol: str) -> pd.Series | None:
    if positions is None or positions.empty or "symbol" not in positions:
        return None
    rows = positions[positions["symbol"].astype(str) == str(symbol)]
    return None if rows.empty else rows.iloc[0]


def summarize(
    ohlc: pd.DataFrame,
    symbol: str,
    positions: pd.DataFrame | None = None,
    cash_tx: pd.DataFrame | None = None,
    year_ohlc: pd.DataFrame | None = None,
    *,
    live_price: float | None,
    live_previous_close: float | None,
) -> dict:
    """Build the full statistics dict the Prices tab renders.

    ``ohlc`` is the frame for the *selected* period; ``year_ohlc`` (optional) is
    a 1-year frame used only for the 52-week range, which must not move when the
    user narrows the period selector.

    ``live_price`` (with ``live_previous_close``) replaces the last close as the
    current price: the day change, live P&L, market value, distance from the
    period high and both yields then follow the delayed intraday quote. None
    keeps the last close.
    """
    stats: dict = {"symbol": symbol}
    has_prices = ohlc is not None and not ohlc.empty and "close" in ohlc

    # ---- market price -----------------------------------------------------
    if has_prices:
        close = ohlc["close"].dropna()
        current = _f(close.iloc[-1]) if not close.empty else None
        prev = _f(close.iloc[-2]) if len(close) > 1 else None
        first = _f(close.iloc[0]) if not close.empty else None
        stats.update({
            "current": current,
            "prev_close": prev,
            "day_change": None if current is None or prev is None else current - prev,
            "day_change_pct": _pct(current, prev),
            "as_of": _to_date(ohlc["date"].iloc[-1]) if "date" in ohlc else None,
            "period_mean": _f(close.mean()),
            "period_high": _f(ohlc["high"].max()) if "high" in ohlc else _f(close.max()),
            "period_low": _f(ohlc["low"].min()) if "low" in ohlc else _f(close.min()),
            "period_return_pct": _pct(current, first),
            "bars": len(ohlc),
        })
        stats["off_period_high_pct"] = _pct(stats["current"], stats["period_high"])
        stats["avg_volume"] = _f(ohlc["volume"].mean()) if "volume" in ohlc else None

        # Annualized stdev of daily returns, plus ATR as an absolute-dollar
        # counterpart (volatility in the units of the price axis).
        returns = close.pct_change().dropna()
        stats["volatility_pct"] = (
            _f(returns.std() * (_TRADING_DAYS ** 0.5) * 100.0) if len(returns) > 1 else None
        )
        stats["atr"] = _atr(ohlc)
    else:
        stats.update({
            "current": None, "prev_close": None, "day_change": None,
            "day_change_pct": None, "as_of": None, "period_mean": None,
            "period_high": None, "period_low": None, "period_return_pct": None,
            "off_period_high_pct": None, "avg_volume": None,
            "volatility_pct": None, "atr": None, "bars": 0,
        })

    if live_price is not None:
        stats.update({
            "current": live_price,
            "prev_close": live_previous_close,
            "day_change": None if live_previous_close is None else live_price - live_previous_close,
            "day_change_pct": _pct(live_price, live_previous_close),
            "off_period_high_pct": _pct(live_price, stats["period_high"]),
        })

    # ---- 52-week range (independent of the selected period) ---------------
    year = year_ohlc if year_ohlc is not None and not year_ohlc.empty else ohlc
    if year is not None and not year.empty and "close" in year:
        stats["high_52w"] = _f(year["high"].max()) if "high" in year else _f(year["close"].max())
        stats["low_52w"] = _f(year["low"].min()) if "low" in year else _f(year["close"].min())
    else:
        stats["high_52w"] = stats["low_52w"] = None

    # ---- my position ------------------------------------------------------
    row = _position_row(positions, symbol)
    if row is not None:
        qty = _f(row.get("quantity"))
        avg_cost = _f(row.get("cost_price"))
        stats.update({
            "quantity": qty,
            "avg_cost": avg_cost,
            "cost_basis": _f(row.get("cost_basis")),
            "mark_price": _f(row.get("mark_price")),
            "market_value": _f(row.get("market_value")),
            "unrealized_pnl": _f(row.get("unrealized_pnl")),
            "currency": row.get("currency"),
        })
        # Prefer live P&L off the yfinance close; fall back to the Flex figure,
        # which is stale as of the statement date.
        if stats["current"] is not None and avg_cost is not None and qty is not None:
            stats["unrealized_live"] = (stats["current"] - avg_cost) * qty
            stats["market_value_live"] = stats["current"] * qty
        else:
            stats["unrealized_live"] = stats["unrealized_pnl"]
            stats["market_value_live"] = stats["market_value"]
        stats["vs_cost_pct"] = _pct(stats["current"], avg_cost)
    else:
        stats.update({
            "quantity": None, "avg_cost": None, "cost_basis": None,
            "mark_price": None, "market_value": None, "unrealized_pnl": None,
            "currency": None, "unrealized_live": None, "market_value_live": None,
            "vs_cost_pct": None,
        })

    # ---- dividends --------------------------------------------------------
    div = dividends_received(cash_tx, symbol)
    stats["dividends"] = div
    qty = stats.get("quantity")
    ttm, current = div.get("ttm_gross"), stats.get("current")
    # Yield on current price: TTM dividend income per share / price. Needs the
    # share count to get back to a per-share figure from the cash amounts.
    if ttm and qty and current:
        stats["yield_pct"] = (ttm / qty) / current * 100.0
    else:
        stats["yield_pct"] = None
    # Yield on *my* cost — what the income actually earns on what I paid.
    avg_cost = stats.get("avg_cost")
    if ttm and qty and avg_cost:
        stats["yield_on_cost_pct"] = (ttm / qty) / avg_cost * 100.0
    else:
        stats["yield_on_cost_pct"] = None

    # Total return = price P&L + every dividend ever received on the symbol.
    pnl, div_net = stats.get("unrealized_live"), div.get("net")
    if pnl is not None or div_net is not None:
        stats["total_return"] = (pnl or 0.0) + (div_net or 0.0)
    else:
        stats["total_return"] = None
    basis = stats.get("cost_basis")
    stats["total_return_pct"] = (
        stats["total_return"] / abs(basis) * 100.0
        if stats["total_return"] is not None and basis else None
    )

    return stats
