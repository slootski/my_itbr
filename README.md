# IBKR Dashboard

Local Streamlit dashboard for an Interactive Brokers account. Pulls portfolio,
trades and account data via the **Flex Web Service** (token-based, read-only, no
TWS/IB Gateway desktop app needed), pairs it with **yfinance** for price
candlesticks and **Polygon.io** for declared-dividend amounts Flex hasn't booked
yet, and stores **NAV snapshots in SQLite** to chart net worth over time.

## Stack

Python · Streamlit · Plotly · pandas · ibflex · yfinance · Polygon.io

## Setup

This machine has `PIP_USER` set globally, so venv installs need a `PIP_USER=0`
prefix.

```bash
python3 -m venv .venv
source .venv/bin/activate
PIP_USER=0 pip install -r requirements.txt
```

### IBKR Flex credentials

1. IBKR **Account Management → Reporting → Flex Web Service** → generate a **token**.
2. Define a **Flex Query** including these sections:
   - Open Positions
   - Trades (executions)
   - Cash Report
   - Cash Transactions (drives the Dividends tab's paid cells)
   - Open Dividend Accruals (drives the Dividends tab's declared cells)
   - (NAV is read from the statement's equity summary / cash report)
   Note its **Query ID**.
3. Copy the example env file and fill in the values:

```bash
cp .env.example .env
# edit .env -> FLEX_TOKEN, FLEX_QUERY_ID
```

Start with a **paper** account, then switch the token to live.

### Polygon API key (optional)

`POLYGON_API_KEY` enables real **declared-dividend amounts** on the Dividends tab
for held tickers Flex has no accrual for yet (free key at
[polygon.io](https://polygon.io)). Without it the calendar falls back to yfinance
ex/pay dates only (red "Declare"/"Pay", no amount) — nothing else changes.

```bash
# edit .env -> POLYGON_API_KEY=...
```

## Run

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`. Use the sidebar **Refresh** button to re-pull
from IBKR (results are cached between reruns). Each successful pull records a NAV
snapshot for the Net Worth tab.

## Tabs

- **Overview** — account summary (net liq, cash, gross exposure, leverage). Flex
  exposes no Reg-T buying power / margin requirement; exposure & leverage are
  derived from positions.
- **Positions** — holdings table, allocation treemap, unrealized P&L bars.
- **Trades** — fills table, realized P&L by symbol, cumulative P&L line.
- **Prices** — candlestick + volume (yfinance) for a held symbol, plus a KPI grid
  (current price and day change, my average cost and unrealized P&L, period and
  52-week ranges, volatility/ATR/average volume, and total dividends received for
  that symbol with yield on price and on cost, and a fund's expense ratio with
  what it takes from the holding per year). Optional chart overlays — average
  cost line, MA20/MA50, period mean, buy/sell trade markers — each toggleable.
- **Watchlist** — named lists of stocks (any yfinance ticker, held or not), each with an
  optional **Buy below** and **Sell above** price. A table of the list shows the delayed intraday price, the
  day change and how far the price is from each target; picking a row (its circle, or ▲ ▼)
  shows the Prices-style tiles and chart with the target lines. Stocks can be added,
  moved between lists and removed. While a price is at or below its Buy below price, or at
  or above its Sell above price, an amber alert box shows at the top of Overview. Each alert can be opened in the
  Watchlist, snoozed for an hour or hidden until tomorrow; while one is shown, a bell at the
  top right leads back to Overview. Prices
  are fetched when the app opens; **Update schedule** can re-fetch every N minutes, for a
  number of hours or with no end time (only while the page is open), and **Update now**
  fetches at once. Lists and targets live in the SQLite store, and every change refreshes
  the day's backup. The tab also works when IBKR is not connected.
- **Dividends** — per-ticker / per-month dividend projection calendar (see below).
- **Performance** — daily performance calendar (see below).
- **Net Worth** — NAV line over time from local SQLite snapshots.

## Local archive — surviving the rolling Flex window

The Flex Query delivers a **rolling window** (`Last365CalendarDays`), so trades and
cash transactions drop off the back as they age, and the Flex Web Service cannot
reach back for them afterwards. Open positions and cost basis are unaffected —
they are point-in-time as of the statement's `toDate` — but realized P&L, the
buy/sell chart markers and the whole dividend history would lose their tail once
the account is older than the window.

So each refresh folds the statement into append-only local tables
(`trades_archive`, `cash_tx_archive` in `db/snapshots.sqlite`) and the tabs read
the **union**: current window plus everything ever seen. Re-pulling is idempotent.
Rows are keyed by IBKR's own `tradeID` / `transactionID` when the Flex Query
includes those columns, falling back to a content hash when it does not (weaker:
two partial fills of one order at the same price and second would collapse).

The same refresh backfills `nav_snapshots` from the statement's
`EquitySummaryInBase` section, which carries one row **per day** across the whole
window — far denser than the one-per-refresh snapshots, so the Net Worth line
fills in every day the dashboard was never opened. Existing snapshot rows are
never overwritten.

`db/snapshots.sqlite` therefore becomes the only copy of history older than the
Flex window. Archiving is best-effort: any store failure degrades to the statement
window and logs, rather than breaking a tab. Implementation: `data/history.py`,
tests: `tests/test_history.py`.

### Backups

Each refresh drops a dated copy in `BACKUP_DIR` (default `db/backups/`), keeping
the newest `BACKUP_KEEP` (default 14) days and pruning the rest. One per day — a second
refresh the same day reuses the existing copy.

Watchlists and targets are typed by hand and cannot be pulled from IBKR again, so
every watchlist change rewrites the day's file. The copy taken before the day's first
IBKR write is kept beside it as `snapshots-<day>-presync.sqlite` and is pruned with
its day.

Copies are made with `VACUUM INTO`, not a file copy, so a backup taken while
Streamlit holds the database open is always internally consistent, never a torn
mid-write image. Each new copy is opened and `PRAGMA integrity_check`-ed **before**
any old one is pruned, so a failed backup can never be the reason the previous
good ones disappeared. The sidebar shows the count and latest date.

Backups on the same disk as the original only protect against accidental deletion
and corruption. To also survive losing the machine, point `BACKUP_DIR` at a synced
or external volume:

```bash
# .env
BACKUP_DIR=~/Library/Mobile Documents/com~apple~CloudDocs/ibkr-backups
```

Restoring is a file copy — every dated file is a complete, standalone database:

```bash
cp db/backups/snapshots-2026-08-11.sqlite db/snapshots.sqlite
```

Implementation: `data/backup.py`, tests: `tests/test_backup.py`.

## Dividend calendar — how it works

Rows are held tickers, then tickers you sold that paid (marked **sold**: past
payments only, never projected). A sold ticker is listed only when the months
shown include a dividend or a trade of it, so one sold before the period does
not appear in it. Columns are months ("Mon YYYY"); the
current-month column is highlighted and always present.

**One definition of dividend income.** `data/div_ledger.py` is the only code
that turns cash transactions into dividend amounts. The calendar, the Net Worth
dividend band, the Prices tab and the Positions heatmap all read from it, so
they always show the same totals. Amounts are in base currency. After tax is
the cash that landed: the dividend minus IBKR's withholding. When IBKR withheld
nothing, after tax equals the gross amount; no tax rate is assumed.

Cell **color encodes status, not value**:

- **Green (paid)** — dividend actually received (Cash Transactions). Past months
  are paid-only: history is green or empty, never projected.
- **Yellow (declared)** — open accrual: amount known, not yet paid. Shown in the
  **payment** month, current month or later only (a stale accrual never rewrites
  history; received cash beats a lagging accrual in the current month).
- **Red "Declare"** — estimated upcoming declaration month (not yet declared).
- **Red "Pay"** — estimated upcoming payment month. The amount is unknown until
  declared, so red cells show only the word — never a number.

Projection rules: each ticker's red cells are estimated from its own historical
payment cadence (needs ≥ 2 paid months) and its typical ex-date→payment gap.
A ticker is projected only when neither an open accrual nor an external declared
dividend (below) already covers a payment in the current month or later, and only
out to its **next** payment month — nothing beyond (per-ticker forward horizon).
Declaration and payment in different months → both red cells; same month → a
single "Declare" cell.

### External backfill — declared dividends Flex hasn't booked

Flex only knows dividends paid into the account or booked as an open accrual. A
held ticker whose next dividend is publicly declared but not yet in Flex (e.g.
COPX, semi-annual, just announced) would otherwise show only a red projection.
For **any** such ticker — both no-data rows and would-be red-projection rows —
two external sources fill the gap (consulted only for tickers lacking a Flex
accrual; Flex data is never overridden):

- **Polygon.io (`POLYGON_API_KEY` set)** — the real declared distribution:
  ex/pay/declaration dates + per-share amount. Renders a **yellow declared** cell
  in the payment month and suppresses that ticker's red projection. Value is
  shown **only when exact for the active tax basis**: Polygon reports gross, so
  *Before tax* shows the cash figure (per-share × shares held) while *After tax*
  shows a dated, **valueless** yellow cell (em-dash) — never a fabricated net.
  Short / zero holdings are skipped (no honest cash figure).
- **yfinance (fallback)** — ex/pay dates only, for tickers still otherwise empty;
  renders red "Declare"/"Pay" with no amount.

Controls:

- **After tax / Before tax** — recomputes every displayed amount, the Total row
  and the headline sum. Net uses the **real per-ticker withholding** from IBKR
  (WHTAX folded into the dividend's month), not a flat rate.
- **Lookback 3M / 6M / 1Y** — clips how much *history* is shown (forward
  projection always stays) and scopes the headline **"Paid, last …"** figure:
  the sum of **paid (green) dividends only** over the trailing window ending at
  and including the current month. Lookback and tax mode are independent.
- **Total row** — per-month sum of the displayed paid + declared amounts (red
  cells contribute nothing).
- **Tooltips** — hover a yellow cell for `declaration: dd-mm-yyyy` / `pay:
  dd-mm-yyyy` (ex-date is the declaration proxy; Flex carries no declaration
  date). Red cells show month-level estimates.

The reference mockup for this behavior lives at
`~/Downloads/dividend-projection-calendar.html`; data layer:
`data/dividends.py` (`build_calendar`), external sources: `data/div_provider.py`
(Polygon) and `data/div_external.py` (yfinance), rendering: `tab_dividends` in
`app.py`, tests: `tests/test_dividends.py` + `tests/test_div_provider.py`.

## Performance calendar — how each day is measured

One cell per trading day (Mon–Fri columns plus a **Week** total; weekends are
not drawn). The large figure is the day's return, the amount under it is the
profit or loss in base currency.

A **Period** slider with two handles picks the months, `◀ ▶` slide the period
by a month, and quick periods (1M, 3M, 6M, 1Y, YTD, All) set it in one click.
One month shows its days; several months show one row per month, with a
line of the running return day by day (linked daily returns, from 0% at the
month's start, each row on its own scale) and the month's return, P&L and
fees, and clicking a row opens that month. The line has one point per day with
a close; days without one are skipped. The number at each point is that day's
own return in percent (not shown when the chart is too narrow for them).

Hovering a day shows its numbers and the **best and worst ticker** of the day,
ranked by price change. **Clicking a day** opens its details: P&L per ticker as
a bar chart and a table, how much of the day came from shares already held
versus everything else, the day's cash events and its trades. The metrics above
(return, P&L, best and worst day, up days, fees) always cover the chosen period.

- A day is measured from the previous end-of-day close to its own close:
  `pnl = nav - prev_nav - flow`, `return = nav / (prev_nav + flow) - 1`.
- `flow` is deposits and withdrawals, dated by IBKR's **report date** (the day
  NAV moves), so adding money never looks like a gain. Dividends and interest
  are part of the return.
- **Fees** (a metric, a row in the day's hover panel and a column in the daily
  table) are trade commissions plus fee-type cash transactions. They are
  already inside the profit or loss; the figure is a breakdown, not a deduction.
- Week, month and year returns link the daily returns (time-weighted); they are
  not a sum of percents. This is why a period can show a money gain and a
  negative return, or the reverse, when deposits changed the account's size.
- Direction is shown three ways (tint hue, arrow, sign) and size by four fixed
  tint steps (0.25%, 0.75%, 1.5%), so the calendar does not rely on colour.
- **No data**: a weekday with no close; the next day's figure covers both.
  **Pending**: no close yet. A ring marks a deposit or withdrawal day.

Closes come from the statement's `EquitySummaryInBase` and are stored in the
insert-only table `equity_daily`, so days older than the Flex window keep their
true value. `nav_snapshots` is not used for this: its live-pull rows hold the
latest figure under the pull date, which is not that day's close.

Per-ticker figures come from the statement's `PriorPeriodPositions` (closing
price and mark-to-market P&L per symbol per day) and are stored in the
insert-only table `symbol_daily` for the same reason. That P&L covers shares
held coming into the day; shares traded that day are in "everything else".

Implementation: `data/performance.py` (calculation), `charts/perf_calendar.py`
(markup), `tab_performance` in `app.py`; tests: `tests/test_performance.py`,
`tests/test_perf_calendar.py`.

## Notes

- `.env` is gitignored; never commit secrets. Ship `.env.example`.
- Flex token is read-only and expires after at most one year (IBKR Flex error
  1012). The app detects 1012/1015 and shows step-by-step regeneration
  guidance: Account Management → Reporting → Flex Web Service → Generate a
  new token → paste into `.env` as `FLEX_TOKEN` → Refresh data. Local NAV
  history stays available meanwhile.
- After a failed IBKR download the app does not ask IBKR again by itself: press
  **Refresh data** (or save a new token) to retry. The Watchlist and Net Worth tabs
  keep working meanwhile.
- Flex carries no live/historical prices; the Prices tab uses yfinance instead.
- Polygon is optional and only queried for the tickers that need it (cached per
  refresh); on the free tier a rate limit just degrades to the yfinance fallback.
