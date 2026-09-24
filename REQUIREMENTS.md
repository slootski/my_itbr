# IBKR Dashboard — Requirements

> Spec for the next planning run. Read this first, then plan the build.

## Goal

Local **Python** app that connects to my **Interactive Brokers (IBKR)** account,
pulls my data, aggregates it, and shows **tables + graphs**.

- Form: **Streamlit** dashboard (local web UI, `localhost`).
- Account: start with **paper**, then live.
- Charts: **Plotly** (interactive). I want the graph types chosen for me.

## Data I want

- Portfolio + positions (holdings, qty, cost, market value, P&L).
- Account summary (net liq, cash, buying power, margin).
- Trade history (executions/fills, P&L by symbol).
- Historical price data (OHLC candlesticks) — *only if the chosen auth method supports it*.

## Connection — MUST resolve before building

I want to connect with **a key/token**, not by running the TWS/IB Gateway desktop
app. IBKR has no simple bearer API key. Real options (next run: present these and
let me pick, or pick Flex if I don't answer):

| Method | "Key"? | Pulls | Desktop app running? | Setup |
|--------|--------|-------|----------------------|-------|
| **Flex Web Service** | Yes — a token | positions, trades, cash, NAV (reports) | No | low-med. **No live/historical prices.** |
| **Web API (Client Portal) OAuth 1.0a** | consumer key + RSA-signed token | everything incl. market data | No | high (RSA signing) |
| **TWS API** (socket) | No | everything | Yes, always | low |

**Leaning: Flex Web Service** (token-based, matches "API key, pull my data").
Accept that it drops the live-price candlestick tab, OR pair Flex (account/trades)
with a free market-data source (e.g. yfinance) for the price charts.

### Flex Web Service notes (if chosen)
- Generate token: IBKR Account Management → Reporting → **Flex Web Service**.
- Define a **Flex Query** (which fields: positions, trades, cash report).
- Pull = HTTP GET with `token` + `queryId` → XML → parse to DataFrames.
- Token + queryId go in `.env` (gitignored), never in code.
- Read-only. Token expires (~1 year).

## Open decisions for next run
1. **Auth method** (Flex vs Web API OAuth vs TWS) — ask me.
2. If Flex: do we add **yfinance** (or similar) for price candlesticks, or drop that tab?
3. Persistence: SQLite snapshots for net-worth-over-time (IBKR gives no NAV history) — keep this idea.

## Constraints
- Python project, Streamlit + Plotly + pandas.
- Secrets only in `.env`; commit `.env.example`.
- Note: this machine has `PIP_USER` set globally — venv installs need `PIP_USER=0` prefix.
- Dir is not a git repo yet.
