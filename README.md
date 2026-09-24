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
  that symbol with yield on price and on cost). Optional chart overlays — average
  cost line, MA20/MA50, period mean, buy/sell trade markers — each toggleable.
- **Dividends** — per-ticker / per-month dividend projection calendar (see below).
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
the newest `BACKUP_KEEP` (default 14) and pruning the rest. One per day — a second
refresh the same day reuses the existing copy.

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

Rows are held tickers, columns are months ("Mon YYYY"), the current-month column
is highlighted and always present. Cell **color encodes status, not value**:

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

## Notes

- `.env` is gitignored; never commit secrets. Ship `.env.example`.
- Flex token is read-only and expires after at most one year (IBKR Flex error
  1012). The app detects 1012/1015 and shows step-by-step regeneration
  guidance: Account Management → Reporting → Flex Web Service → Generate a
  new token → paste into `.env` as `FLEX_TOKEN` → Refresh data. Local NAV
  history stays available meanwhile.
- Flex carries no live/historical prices; the Prices tab uses yfinance instead.
- Polygon is optional and only queried for the tickers that need it (cached per
  refresh); on the free tier a rate limit just degrades to the yfinance fallback.
