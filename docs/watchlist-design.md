# Watchlist screen — design

Status: approved by the user on 2026-10-12 and implemented the same day. The review ended
in round 5 with four approvals. Section 10 holds the final review's minor findings;
where it differs from an earlier section, section 10 wins. The last changelog entries
record what the implementation changed.

## 1. Decisions

Answered by the user on 2026-10-12 unless marked "designer".

| # | Topic | Decision |
|---|-------|----------|
| D1 | Layout | A table of the chosen list; selecting a row shows the Prices-style tiles and chart for that stock below the table. |
| D2 | Membership | A stock is in exactly one list. Move takes it out of the old list. |
| D3 | Tickers | Any yfinance ticker, held or not (stocks, ETFs, indices, crypto, FX). Checked when added; an unknown one is refused. |
| D4 | Targets | Per stock: one buy target and one sell target, both optional. No note. |
| D5 | Notification | On Overview. Buy: shown while price <= buy target. Sell: shown while price >= sell target. No dismiss. |
| D6 | Trigger price | The delayed intraday price from yfinance, not the daily close. |
| D7 | Shown price | On Watchlist the table, the notification and the "Current price" tile show the same delayed intraday price. The chart stays daily candles. The Prices tab is unchanged. |
| D8 | Fetch schedule | Default: only when the app is opened. The user can switch to "every N minutes", for X hours or always. |
| D9 | Auto update | While a schedule is on, the whole page redraws every N minutes with no click, so table, tiles and alerts always agree. Nothing redraws while a dialog is open. |
| D10 | Schedule is saved | It survives closing and opening the app until its end time; "always" stays on. An ended schedule falls back to "only when opened". |
| D11 | Delete list | A confirmation shows how many stocks the list holds; on confirm the list, its stocks and their targets are deleted. |
| D12 | Unheld stock | Position tiles are hidden. |
| D13 | Schedule control | At the top of the Watchlist tab, with an **Update now** button that fetches watchlist prices only. |
| D14 | Opening list | A new session opens on the list that holds the first alert, with that stock selected. With no alert: the first list by name. The list dropdown shows each list's alert count. |
| D15 | Distance | A positive distance left to the target, and "reached" when the target is hit. |
| D16 | Alert box | Text only, no button that switches tab. |
| D17 | Backups | Every watchlist change refreshes today's backup file. |
| D18 | IBKR not connected | Watchlist and the alert box still work. "Shares held" and the position tiles are empty. |
| D19 | Ticker spelling | `BRK.B`, `BRK B` and `BRK-B` are one stock: stored in the yfinance spelling, matched to the IBKR position. |
| D20 | Old defaults | `price_stats.summarize`, `figures.candlestick` and `backup.run` keep their existing default parameters; removing them is a separate task. New arguments and new functions have no defaults. |
| D21 | After move or remove (designer) | No stock is selected (changed 2026-10-13 by D33; before: the first row). |
| D22 | Sorting (designer) | A user sort does not change which stock the detail shows. |
| D23 | List names (designer) | Compared after Unicode NFC + trim + casefold: `Épargne` and `épargne` are the same name. |
| D24 | "For X hours" (designer) | Elapsed hours, stored in UTC. A daylight-saving change does not shorten or lengthen it. |
| D25 | Restore of an old backup (designer) | Brings back everything as it was, including the saved schedule. |
| D26 | DB busy on save (designer) | SQLite waits up to 5 seconds, then the dialog stays open with a message. |
| D27 | Row click | The row click stays the control. Its logic is unit-tested as a pure function; the click itself is checked by hand. If the browser proof (5.5) fails, work stops and the user is asked. No silent fallback. |
| D28 | Missing price on Overview | Named only for a stock that has a target. Others show `no data` on the Watchlist tab only. |
| D29 | Pre-sync backup | The copy taken before an IBKR write is kept under a second name (`snapshots-<day>-presync.sqlite`) and removed with its day. The day's normal file follows the watchlist changes. |
| D30 | IBKR failure | After a failed IBKR pull the app remembers the failure. Only "Refresh data" or a newly saved token tries IBKR again; other clicks and timed redraws do not, so they stay fast. Watchlist prices keep updating. This changes the whole app: today every click retries IBKR. (Answered in round 3; replaces the round-2 wording.) |
| D32 | Picking a stock (answered during implementation, after the browser spike) | Streamlit selects a table row only by the small circle at the row's left edge, not by a click elsewhere in the row. The circle stays, and ▲ ▼ buttons next to "Edit / move / remove" step through the list, as on Prices. The caption says so. |
| D31 | Pre-sync file without its day file (designer) | Deleted when its day is no longer among the kept days. Kept days = the newest `keep` distinct days over main and pre-sync files together. |
| D33 | Default selection (user, 2026-10-13) | A list opens with no stock selected: no chart, no tiles, **Edit / move / remove** disabled. A row click, ▲ ▼, the ticker search, adding a stock or the first alert of a new session (D14) selects one. The selected row's circle is expected to clear it (not checked in the browser). The button is primary while a stock is selected. |

Facts that limit the design (checked in this repo and by the reviewers' experiments in
rounds 1 and 2):

- Streamlit 1.58.0. `st.tabs` renders every tab on every run. Two identical keyless
  `st.plotly_chart` or `st.selectbox` calls raise `StreamlitDuplicateElementId`;
  `st.metric` has no element ID.
- A fragment's body runs on every full run and on each of its timer runs. A timer run
  executes only the fragment, with the arguments of the last full run. `st.rerun()` called
  in a timer run is a full rerun and keeps other widgets' state. `st.rerun()` called from a
  fragment body *during a full run* aborts that run, and keyed widgets not yet rendered
  lose their state. The browser clears timers on every full run, so `run_every=None`
  stops a timer. A fragment that renders nothing works.
- A dialog closed with X or Esc sends no rerun unless it has `on_dismiss`; with it, the
  close is a full run. A dialog called inside `with tab:` still renders as a modal.
- In a browser a click inside a dialog is a fragment run (old dialog arguments). Under
  `AppTest` it is a full run (dialog rebuilt). Deleting a dialog's widget keys, or a
  dialog body calling `st.rerun()` after a button, breaks the next `AppTest.run()`;
  `on_click` callbacks that assign empty values to the keys do not.
- A session-state key cannot be assigned after the widget with that key is instantiated in
  the same run; it can be assigned before, and in an `on_click` / `on_change` callback.
  Once a widget's key is in session state Streamlit ignores the widget's `value=`
  argument, so a keyed field shows whatever was last assigned to its key.
  `st.number_input` without `value=` defaults to 0.0 and an emptied field reads back as
  0.0; with `value=None` an emptied field reads back as `None`, and an assigned key still
  decides what is shown. After a dialog closes Streamlit drops its widget keys.
- A selectbox whose options are labels that change between runs loses its pick; with
  `options=ids` and `format_func` it keeps it. `on_change` runs before the script body.
- In `st.dataframe`, sorting uses the underlying value. A pandas Styler's text is shown on
  a number cell only when that column's `column_config` has no `format`; Styler cell
  colours and `column_config` header `help` work together with `on_select`.
- `st.dataframe(selection_mode="single-row-required")` starts with row 0 selected and
  never reports an empty selection for a non-empty table; since D33 (2026-10-13) the
  table uses `single-row`, which starts empty and lets the user clear the mark. The selection is the row's
  position in the frame as passed. A user sort keeps the same row selected (the browser
  tracks the original row index). It can be set by writing
  `st.session_state[key] = {"selection": {"rows": [n]}}` before the call, every run, also
  with `on_select="rerun"` and a pandas Styler; the key is missing before the first render.
- `AppTest` runs `app.py` in a fresh namespace on every run, so a patch on a name defined
  in `app.py` has no effect; patches on imported modules (`prices`, `watchlist`, `config`)
  do. `AppTest` cannot click a dataframe row, fire `on_dismiss`, or run fragment timers.
  Dialog widgets are found at the root of the tree, not inside the tab.
- yfinance 0.2.66: `fast_info["last_price"]` costs one request per symbol (0.4-0.9 s);
  `fast_info["regular_market_previous_close"]` shares that request and is the regular
  close. `fast_info` raises the same `KeyError` for an unknown ticker and for no network.
  `Ticker.history(period="5d", raise_errors=True)` raises `YFPricesMissingError` for an
  unknown ticker and a connection error when offline. yfinance is safe to call from
  worker threads.
- `backup.run` is called only from `load_portfolio`, before an IBKR write, and writes
  nothing when today's file exists. `VACUUM INTO` fails when the target exists and inside
  an open transaction. On the real DB size (0.5 MB) it takes about 5 ms.
- `sqlite3.connect()` on a missing path creates an empty file; a `file:...?mode=ro` URI
  does not. `db/` is not in git, so on a fresh install the directory may be missing.
- `load_portfolio` is cached, but `st.cache_data` stores no exception: after a failed IBKR
  pull every run calls IBKR again.
- `config.load_env_file` copies the real `.env` credentials over the environment at import.
- The app has no login. "On log in" means "when the app page is opened".
- Auto update only happens while the page is open in a browser.

## 2. User flows

Clicks counted from the Watchlist tab; typing not counted.

**F1. Create a list** (2 clicks): **＋ New list** → type the name → **Create**. The new
list is selected and shown empty.

**F2. Rename a list** (2 clicks): **Rename** → change the name → **Save**.

**F3. Delete a list** (2 clicks): **Delete** → the dialog says `Delete list "Quantum"? It
holds 3 stocks: QTUM, IONQ, RGTI. Their buy and sell targets are deleted too. This cannot
be undone.` → **Delete list**. The first remaining list becomes selected. An empty list
shows `Delete list "Quantum"? It is empty.` More than 10 stocks: the first 10 and
`and 4 more`.

**F4. Add a stock** (2 clicks): **＋ Add stock** → type the ticker and, if wanted, the
targets → **Add**. The app checks the ticker. Known: the dialog closes, the row is
selected. **Add and add another** saves and keeps the dialog open with empty fields and
the line `Added QTUM at 152.40 USD.` A refused ticker keeps the dialog open with the
message under the Ticker field (section 6).

**F5. Remove a stock** (4 clicks): select the row → **Edit / move / remove** →
**Remove from watchlist** → **Remove IONQ and its targets**.

**F6. Move a stock** (3 clicks + 1 pick): select the row → **Edit / move / remove** → pick
the other list in "List" → **Save**. The row leaves this list; no stock is
selected (D21). A toast says `Moved IONQ to ETFs.`, or `Moved IONQ to ETFs and saved its
targets.` when targets changed in the same save.

**F7. Set, change or clear a target** (3 clicks): select the row →
**Edit / move / remove** → type the targets (empty = no target) → **Save**. A toast says
`Saved targets for IONQ.`

**F8. See a notification** (0 clicks): open the app. Overview is the first tab; the box
"Watchlist alerts" is at its top. The Watchlist tab is already on the alert's list with
that stock selected (D14).

**F9. Set the update schedule** (2 clicks): **Update schedule** → choose → **Save**.
Saving "For 4 hours" again starts 4 hours from now; the dialog says so.

**F10. Fetch prices now** (1 click): **Update now**.

## 3. Wireframes

### 3.1 No lists yet

```
Watchlist
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ You have no watchlists yet. Create one to start watching stocks.   │
└──────────────────────────────────────────────────────────────────────┘
[ ＋ New list ]
```

### 3.2 Empty list (stocks exist in another list)

```
List [ Quantum                 ▾]  [＋ New list] [Rename] [Delete]
Prices fetched 10:02 · updates: only when I open the app      [Update now] [Update schedule]
Alerts in other lists: Metals (1)

┌──────────────────────────────────────────────────────────────────────┐
│ ℹ "Quantum" is empty. Add a stock to see its price and set targets.  │
└──────────────────────────────────────────────────────────────────────┘
[ ＋ Add stock ]
```

The schedule line and its two buttons are shown only when at least one stock is watched in
any list. The "Alerts in other lists" line is shown only when another list has an alert.

### 3.3 Filled list, a row selected

```
List [ Quantum (1 alert)       ▾]  [＋ New list] [Rename] [Delete]
Prices fetched 10:07 · updates: every 1 min until 14:02 · works only while this page is open
                                                            [Update now] [Update schedule]
Alerts in other lists: Metals (1)

[＋ Add stock]  [Edit / move / remove: QTUM]

  Symbol  Alert                Price       Day     Buy target  Above buy  Sell target  Below sell  Shares held
  IONQ    Buy target reached    41.10 USD  -2.10%   42.00      reached    —            —           —
▸ QTUM                         152.40 USD  +0.99%  150.00      1.60%      175.00       12.91%      1.78
  RGTI                         no data     —         9.00      —          —            —           —
Click a row to see its chart and figures, and to edit it.

──────────────────────────────────────────────────────────────────────────────
QTUM                                                         Period [ 1y ▾]
(KPI tiles and chart; see 3.4)
[x] Target lines  [x] Avg cost line  [x] MA 20/50  [ ] Period mean  [x] My trades
(candlestick chart with a green dashed line "Buy 150.00" and a red dashed line "Sell 175.00")
Caption: "Current price (delayed)" on this tab is the yfinance quote, which may be delayed
(about 15 minutes on most exchanges), so P&L here can differ from the Prices tab, which
uses the last close. The chart is daily candles.
```

Under the table a caption says `Click a row to see its chart and figures, and to edit it.`
With IBKR not connected a second caption says `IBKR not connected: positions not shown.`

The selected stock in this wireframe is held (QTUM), so all toggles are shown. The
"Target lines" toggle is hidden for a stock with no target, like "Avg cost line" for an
unheld stock.

List dropdown labels: `Quantum`, `Quantum (1 alert)`, `Quantum (2 alerts)`.

Messages above the table, when present, are in this order: the database line (section 4),
the fetch-failure line (3.7), the one-time target line (3.5).

Columns:
- **Alert**: `Buy target reached` (green cell) when price <= buy target,
  `Sell target reached` (red cell) when price >= sell target, else empty. Second column.
- **Price**: delayed intraday price and its currency, or `no data`.
- **Day**: change from the previous regular close, in %.
- **Above buy** (header tooltip "How far the price is above your buy target"):
  `(price - buy target) / buy target` when positive, `reached` when zero or negative.
- **Below sell** (tooltip "How far the price is below your sell target"):
  `(sell target - price) / sell target` when positive, `reached` when zero or negative.
- **Shares held**: share count from Positions, or `—`.
- The frame holds **numbers** in every numeric column (price, day, targets, distances with
  0.0 for reached, shares; missing = NaN). The texts `reached`, `no data`, `—`, the `%`
  sign and the currency come from display formatting (a pandas Styler, one
  `Styler.format(subset=...)` per row for the per-row currency), so sorting is numeric.
  `column_config` supplies only the header `help` texts, never a `format`.
- Row order: rows with an alert first, then by symbol. The user can sort by any column;
  the same stock stays selected and shown (D22).
- At most one row is selected. With no row selected (D33) the table is shown without the
  detail below it, **Edit / move / remove** is disabled and plain, ▲ is disabled and ▼
  selects the first row. With a row selected the button is `Edit / move / remove: SYMBOL`
  in the primary style.

Schedule line texts:
- `Prices fetched 10:02 · updates: only when I open the app`
- `Prices fetched 10:07 · updates: every 1 min until 14:02 · works only while this page is open`
- `Prices fetched 23:50 · updates: every 1 min until 13 Oct 02:02 · works only …` (end not today)
- `Prices fetched 10:07 · updates: every 4 min, no end time · works only …`

Times are local time; "fetched" is when the app asked yfinance, and carries the date when
it is not today (`Prices fetched 11 Oct 22:40`). A third part, `· works only while this
page is open`, is added while a schedule is on.

### 3.4 Detail tiles

Exact rows, up to four tiles per row (the last row may be shorter), left to right. Labels
are those of the Prices tab, including the `({period})` suffix on "Period mean",
"Period high / low" and "Period return". The order differs from the Prices tab on purpose
(targets first); `_render_price_detail` takes the layout from `key_prefix`.

Held stock:
1. Current price (delayed) · Buy target · Sell target · Position
2. My avg cost · Unrealized P&L · Total return incl. divs · Period return
3. Period mean · Period high / low · 52-week high / low · Avg volume
4. Volatility (ann.) · ATR(14) · Expense Ratio · Yield (TTM) / on cost
5. Dividends received (net) · Dividends received (gross) · Last dividend

Unheld stock:
1. Current price (delayed) · Buy target · Sell target · Period return
2. Period mean · Period high / low · 52-week high / low · Avg volume
3. Volatility (ann.) · ATR(14) · Expense Ratio

The Prices tab keeps its own four rows unchanged. The table below says which tiles exist
for which stock.

| Tile | Held stock | Unheld stock |
|------|-----------|--------------|
| Current price (delayed) and day change | yes | yes |
| Buy target, caption `price 1.60% above target` or `reached` | yes | yes |
| Sell target, caption `price 12.91% below target` or `reached` | yes | yes |
| Period mean, Period high / low, 52-week high / low, Period return | yes | yes |
| Avg volume, Volatility, ATR(14), Expense Ratio | yes | yes |
| My avg cost, Unrealized P&L, Position, Total return incl. divs | yes | hidden |
| Dividends received (net, gross), Last dividend, Yield (TTM) / on cost | yes | hidden |

- Target captions are neutral: no colour, no arrow. A stock with no target shows `—` and
  no caption.
- On Prices the first tile stays `Current price`, and the caption sentence "current price
  is the last close" is printed only there.
- For an unheld stock the Expense Ratio tile shows the ratio only, and the
  "Avg cost line" and "My trades" toggles are hidden.
- On Prices the toggles keep their labels "Avg cost line" and "Buy/Sell markers"; on
  Watchlist the second is "My trades", so it is not confused with "Target lines".

### 3.5 Dialogs

Dialog titles are built per call (`st.dialog(f"Edit {symbol}", ...)`), so the stock or the
list is always named in the dialog itself.

```
┌ New list ───────────────────┐   ┌ Rename list "Quantum" ──────┐
│ List name [______________]  │   │ List name [Quantum_______]  │
│            [Cancel][Create] │   │              [Cancel][Save] │
└─────────────────────────────┘   └─────────────────────────────┘

┌ Delete list "Quantum" ───────────────────────────────────┐
│ Delete list "Quantum"? It holds 3 stocks: QTUM, IONQ,     │
│ RGTI. Their buy and sell targets are deleted too.         │
│ This cannot be undone.                                    │
│                                  [Cancel] [Delete list]   │
└───────────────────────────────────────────────────────────┘

┌ Add stock to "Quantum" ───────────────────┐  ┌ Edit IONQ ──────────────────────────┐
│ Ticker      [________]                    │  │ IONQ · 41.10 USD now                │
│ Buy target  [________]                    │  │ List              [ Quantum     ▾]  │
│ Sell target [________]                    │  │ Buy target (USD)  [42.00____]       │
│ [Cancel] [Add and add another] [Add]      │  │ Sell target (USD) [_________]       │
└───────────────────────────────────────────┘  │                     [Cancel][Save]  │
                                               │ ─────────────────────────────────── │
                                               │ [Remove from watchlist]             │
                                               └─────────────────────────────────────┘
   after one click on Remove, the last part of the dialog becomes:
                                               │ ─────────────────────────────────── │
                                               │ ⚠ This deletes IONQ and its targets │
                                               │   from "Quantum".                   │
                                               │        [Remove IONQ and its targets]│
                                               └─────────────────────────────────────┘

┌ Update schedule ────────────────────────────┐
│ Now: every 1 min until 14:02.               │
│ ( ) Only when I open the app                │
│ (•) Every [ 1 ] minute(s)                   │
│      (•) For [ 4 ] hour(s) ( ) No end time  │
│ Save starts a new 4 hours from now.         │
│ Works only while this page is open.         │
│ With 12 watched stocks this is about 720    │
│ yfinance requests an hour.                  │
│                             [Cancel][Save]  │
└─────────────────────────────────────────────┘
```

- Edit: the target fields are labelled with the quote's currency (`USD`, `GBp`); with no
  quote the labels have no currency. In Add (no quote yet) both target fields have the
  help text `In the quote's currency. London stocks quote in pence.`
- Every target field is an `st.number_input` created with `value=None` and the placeholder
  `Leave empty for no target`, so it can be emptied and then reads as "no target".
- Rename, Edit and Update schedule open with the current values in their fields; New
  list and Add open empty.
- Remove: the armed button has a different label, sits under a warning line and is
  right-aligned, while the first button was left-aligned, so a double click cannot remove.
  Closing the dialog in any way disarms it.
- Update schedule: "Minutes" (1-60), "For / No end time" and "Hours" (1-24) are disabled while
  "Only when I open the app" is chosen; "Hours" is disabled while "No end time" is chosen. The
  "Now:" line shows the schedule in force. "Save starts a new N hours from now" is shown
  for "For X hours". The requests line is shown for "Every N minutes": `stocks × 60 / N`.
- After a stock is added with a target, or after a **Save** in Edit that changed that
  target (not after a move only): when the buy target is at or above the current price
  the page shows once, above the table,
  `QTUM: the price is already at or below your buy target.` For a sell target at or below
  the price: `QTUM: the price is already at or above your sell target.`

### 3.6 Overview notification

At the top of Overview, above "Account", in `st.container(border=True)`. Each alert line
starts with a coloured badge (`:green-badge[BUY]`, `:red-badge[SELL]`). The box is not
rendered when there is nothing to say.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Watchlist alerts (2)                                                    │
│ [BUY]  IONQ  41.10 USD is at or below your buy target 42.00 · Quantum   │
│ [SELL] COPX  61.30 USD is at or above your sell target 60.00 · Metals   │
│ No price for: RGTI. Its target was not checked.                         │
│ Fetched 10:07. Quotes may be delayed (about 15 minutes on most          │
│ exchanges). See the Watchlist tab.                                      │
└─────────────────────────────────────────────────────────────────────────┘
```

- One line per stock and side; buy lines first, then by symbol.
- `No price for:` names only stocks that have a target and no price (D28); for several:
  `No price for: RGTI, IONQ. Their targets were not checked.` "Fetched"
  carries the date when it is not today.
- Every watchlist message in the app uses `st.info`, `st.warning` or plain text, never
  `st.error` (existing tests assert the exact list of `st.error` messages).
- With no alert line but a `No price for:` line, the title is `Watchlist prices`.
- If at least one target exists and every watched quote failed, the box is titled
  `Watchlist prices` and has one line: `Watchlist prices could not be fetched. Targets
  were not checked. Open the Watchlist tab and click Update now.`
- With IBKR not connected, the box is shown above the connect card or the error text
  (D18); see 3.8.

### 3.7 yfinance failure

- One stock's quote fails: `no data` in Price, `—` in Day and both distance cells, no
  alert. In the detail the "Current price (delayed)" and target captions show `—`; the
  chart and the other tiles are still drawn when the daily candles load. Only when the
  candles also fail does "No price data for RGTI" replace the chart.
- All fail: the same per row, plus above the table:
  `Prices could not be fetched (yfinance). Click **Update now** to try again.`
- A failed quote stays `no data` until the next scheduled fetch or **Update now**.
- Add-stock check cannot reach yfinance: the dialog says
  `Could not check "RGTI" (yfinance did not answer). Try again.` and adds nothing.

### 3.8 IBKR not connected or failed (D18, D30)

```
┌ Watchlist alerts (1) ───────────────────────────────────────────────┐
│ [BUY]  IONQ  41.10 USD is at or below your buy target 42.00 · Quantum│
└──────────────────────────────────────────────────────────────────────┘
(connect card for a missing or rejected token, or the error text for another failure)
IBKR is not asked again automatically. Click **Refresh data** in the sidebar to retry.

[ Watchlist ] [ Net Worth ]
IBKR not connected: positions not shown.
(the Watchlist tab as in 3.1-3.3)
```

- The retry sentence is shown under the connect card and under the error text, in every
  failed state. It replaces today's `Fix the token/query in .env, then click **Refresh
  data**.` for a non-token failure, which becomes
  `Fix the token/query in .env if needed. IBKR is not asked again automatically. Click
  **Refresh data** in the sidebar to retry.`
- The stored failure message is scrubbed of the token before it is stored.

## 4. Data model

New module `data/watchlist.py`, same SQLite file as the rest (`settings.db_path`). No
existing table is changed and no name clashes with the six existing tables, so the user's
database needs no migration. `data/sync.py` names only its own tables; the sync report is
not affected.

```sql
CREATE TABLE IF NOT EXISTS watchlists (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL CHECK (name = trim(name) AND length(name) BETWEEN 1 AND 40),
    name_key TEXT NOT NULL UNIQUE,        -- NFC + casefold of name, written by Python (D23)
    created  TEXT NOT NULL                -- UTC ISO timestamp with offset
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    symbol      TEXT PRIMARY KEY NOT NULL
                CHECK (symbol = upper(trim(symbol)) AND length(symbol) BETWEEN 1 AND 20),
    match_key   TEXT NOT NULL UNIQUE      -- symbol with " " and "." as "-", written by Python (D19)
                CHECK (match_key = replace(replace(symbol, '.', '-'), ' ', '-')),
    list_id     INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    buy_target  REAL CHECK (buy_target  IS NULL OR (typeof(buy_target)  IN ('real','integer')
                                                    AND buy_target  > 0 AND buy_target  < 1e308)),
    sell_target REAL CHECK (sell_target IS NULL OR (typeof(sell_target) IN ('real','integer')
                                                    AND sell_target > 0 AND sell_target < 1e308)),
    added       TEXT NOT NULL,            -- UTC ISO timestamp with offset
    CHECK (buy_target IS NULL OR sell_target IS NULL OR buy_target < sell_target)
);
CREATE INDEX IF NOT EXISTS watchlist_items_list ON watchlist_items(list_id);

CREATE TABLE IF NOT EXISTS watchlist_refresh (
    id               INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    interval_minutes INTEGER CHECK (interval_minutes IS NULL OR
                                    (typeof(interval_minutes) = 'integer' AND interval_minutes BETWEEN 1 AND 60)),
    until            TEXT,                -- UTC ISO timestamp with offset; NULL with an interval = always
    CHECK (until IS NULL OR interval_minutes IS NOT NULL)
);
```

Rules:

- **The CHECKs are a second guard, not the validation.** Python validates first
  (`clean_name`, `clean_symbol`, `validate_targets`); the CHECKs do not catch everything
  raw SQL could write (for example a name of non-breaking spaces only).
- **`AUTOINCREMENT` is deliberate.** Without it SQLite reuses the id of a deleted list, so
  a stale `list_id` held by another browser tab would write into a different list.
- **`init_db` is the only place that creates anything.** It creates the DB's parent
  directory, the file and the tables, and is called once per run from `main()` before
  either early return (D18, fresh install). If it fails the watchlist is disabled for the
  run with a message; no other tab is affected.
- **Reads never write.** A read first checks `Path.exists()` and then opens
  `Path(db_path).resolve().as_uri() + "?mode=ro"` with `uri=True` and
  `timeout=_BUSY_SECONDS`, so it cannot create a file or a directory. The URI is built
  with `as_uri()`, never by joining strings: a path containing `?`, `#` or `%` would
  otherwise open or create another file. A missing file gives empty results. Missing
  tables are told apart from an error by a query on `sqlite_master` first: missing =
  empty results without the error flag; any `sqlite3.Error` = empty results with it.
- **Order in a run:** `init_db` (a read-write connection) runs before the first read, so
  a journal left by a crash is recovered before the read-only connections open.
- **Connections.** Writes use `sqlite3.connect(path, timeout=_BUSY_SECONDS)` with
  `_BUSY_SECONDS = 5`, default (legacy) transaction mode. Every connection is wrapped in
  `contextlib.closing`. `PRAGMA foreign_keys = ON` is run first, outside any transaction;
  a unit test reads it back as 1. Journal mode stays as it is (no WAL).
- **Every write is one transaction** (`with conn:` inside `closing`). A failed write
  leaves the tables as they were. `delete_list` deletes the list's items and then the list
  in one transaction. `update_item` is one statement:
  `UPDATE watchlist_items SET list_id = ?, buy_target = ?, sell_target = ? WHERE symbol = ?`.
  `INSERT OR REPLACE` is never used on `watchlists`. Datetimes are bound as
  `value.isoformat()`, never as `datetime` objects.
- **Values.** Targets are cast with `float(...)` after `validate_targets`, so numpy and
  other number types are stored as reals. `validate_targets` refuses `NaN`, infinity, zero
  and negatives (SQLite would store `NaN` as NULL and silently clear the target).
- **Errors.** Every failure a user can cause becomes a `WatchlistError` whose text is
  shown in the dialog:

  | Cause | Detected by | Text |
  |-------|-------------|------|
  | Duplicate list name | `sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"` on `watchlists` | `A list named "Quantum" already exists.` |
  | Duplicate stock (same symbol or same `match_key`) | `SQLITE_CONSTRAINT_PRIMARYKEY` or `_UNIQUE` on `watchlist_items`; the other list's name is then read | section 6 texts |
  | List is gone | `SQLITE_CONSTRAINT_FOREIGNKEY`, or `rowcount == 0` on a list write | `That list no longer exists.` |
  | Stock is gone | `rowcount == 0` on an item write | `QTUM is no longer on the watchlist.` |
  | DB busy, locked or read-only | `OperationalError` with `SQLITE_BUSY`, `SQLITE_LOCKED` or `SQLITE_READONLY*` | `The database is busy or cannot be written. Try again.` (D26) |
  | Anything else (`_CHECK`, `_NOTNULL`, other `sqlite3.Error`) | fallthrough | `The watchlist could not be saved.` plus a log line with the real error |

  The cause is chosen from `exc.sqlite_errorname` and from which function raised it
  (each write function touches one table), never by parsing the message. `SQLITE_BUSY`
  and `SQLITE_READONLY` are matched by prefix (`SQLITE_READONLY_DIRECTORY`, …). A plain
  duplicate symbol raises on `match_key` (`_UNIQUE`); `_PRIMARYKEY` fires only when the
  spellings differ in a way `match_key` keeps apart, so both map to the same texts.

  Read functions catch `sqlite3.Error`, log it and return empty results plus an `error`
  flag; the tab shows one line.
- **Names.** `clean_name` = `unicodedata.normalize("NFC", name).strip()` (Python `strip`
  removes every Unicode space); `name_key` = cleaned `.casefold()`. Renaming a list to its
  own name in another letter case is allowed.
- **Time.** Every timestamp is timezone-aware UTC, stored as ISO text with offset; Python
  compares, SQL never does. `read_refresh` treats an `until` that cannot be parsed or has
  no offset as "only when opened".
- **`watchlist_refresh`.** No row, or `interval_minutes IS NULL` = only when the app is
  opened. A row whose `until` is in the past is read as that too and is not deleted on
  read. `write_refresh` is an upsert on `id = 1`.
- **`read_items`** uses `LEFT JOIN` to `watchlists`, so an item whose list is missing is
  still returned and can be moved or removed.
- **Later changes.** A new column is added the way `history._ADDED_COLUMNS` / `_migrate`
  does it; a table rebuild uses `PRAGMA user_version` (0 in this design).

**Backups (D17, D29).** `data/backup.py` changes:

```python
def refresh(*, db_path: Path | str, backup_dir: Path | str, keep: int, today: str) -> Path | None
```

- `refresh` writes today's file `snapshots-<day>.sqlite` even when it exists.
- `run` (called before an IBKR write, as today) additionally writes
  `snapshots-<day>-presync.sqlite` when that file does not exist yet, so the state before
  the day's first IBKR write is kept apart from the file `refresh` replaces (D29). **This
  step depends only on the pre-sync file and runs before the "today's main file exists"
  check**, so it also happens on a day whose main file was already created by `refresh`
  (watchlist edit first, IBKR write later). `run`'s return value is unchanged: the main
  file's path when it wrote one, else `None`. The pre-sync step and the main-file step
  each have their own `try`, so a failed pre-sync copy does not stop the main copy.
- `today` is the same local calendar date for both functions. `app.py` passes
  `today=watchlist.utc_now().astimezone(watchlist.local_tz()).date().isoformat()` to
  `refresh` and, in `load_portfolio`, to `run` (its `today` default stays, D20; the caller
  now passes it), so both files carry the same date, also under a patched clock.
- Both use one shared helper: `VACUUM INTO` a temporary file with a unique name in the
  backup directory, on a new connection (never inside a transaction), verify it,
  `os.replace` it over the target, and remove the temporary file in `finally`. A module
  lock serialises them. Connections are closed with `contextlib.closing`.
- The pre-sync name does not match `_NAME_RE`, so `existing()`, the sidebar count and
  `keep` count days, not files. `_prune` deletes every pre-sync file whose day is not
  among the kept days, which covers a pre-sync file left without its main file (D31).
  Kept days are the newest `keep` distinct days over both name patterns.
- Temporary files are named `<target name>.<random hex>.part`; they match neither
  pattern. Inside the lock, after the "DB file exists" guard and only when the backup
  directory exists, both functions remove every `*.part` file there (including the old
  fixed `snapshots-<day>.sqlite.part`) before writing, and remove their own in `finally`
  with `unlink(missing_ok=True)`. Nothing creates the backup directory when there is no
  DB file. `backup._verify` opens the copy with the same `as_uri()` rule.
- Guards kept: no DB file → nothing is written; any failure is caught, logged at warning
  level and returns `None`. Neither function raises. A failed `refresh` is not shown to
  the user; the log line is the record.
- `app.py` calls `refresh` after every successful watchlist write, including the
  schedule. Measured cost: about 5 ms.

## 5. Module plan

Every new function is keyword-only and has no default parameter values.

### 5.1 New: `data/watchlist.py`

```python
class WatchlistError(Exception):
    """A rule was broken or the write failed; str(exc) is the text shown to the user."""

def utc_now() -> dt.datetime                               # aware UTC; the one clock, patched in tests
def local_tz() -> dt.tzinfo                                # the machine's zone, for display; patched in tests

def init_db(*, db_path: Path | str) -> None
def read_lists(*, db_path: Path | str) -> dict             # {"lists": DataFrame(id, name, count), "error": bool}
def read_items(*, db_path: Path | str) -> dict             # {"items": DataFrame(symbol, list_id, list_name, buy_target, sell_target, added), "error": bool}
def create_list(*, db_path: Path | str, name: str, now: dt.datetime) -> int
def rename_list(*, db_path: Path | str, list_id: int, name: str) -> None
def delete_list(*, db_path: Path | str, list_id: int) -> int          # stocks deleted
def add_item(*, db_path: Path | str, list_id: int, symbol: str,
             buy_target: float | None, sell_target: float | None, now: dt.datetime) -> None
def update_item(*, db_path: Path | str, symbol: str, list_id: int,
                buy_target: float | None, sell_target: float | None) -> None
def remove_item(*, db_path: Path | str, symbol: str) -> None
def read_refresh(*, db_path: Path | str) -> dict           # {"interval_minutes": int | None, "until": dt.datetime | None}
def write_refresh(*, db_path: Path | str, interval_minutes: int | None, until: dt.datetime | None) -> None
```

Pure helpers (no DB, no network, no clock):

```python
def clean_symbol(*, text: str) -> str                      # trim, upper case, inner spaces -> "-"; raises WatchlistError when empty, > 20 or bad characters
def match_key(*, symbol: str) -> str                       # upper; " " and "." -> "-"
def clean_name(*, name: str) -> str                        # NFC + strip; raises WatchlistError when empty or > 40
def name_key(*, name: str) -> str
def validate_targets(*, buy_target: float | None, sell_target: float | None) -> None
def distance_left(*, price: float | None, target: float | None, side: str) -> float | None   # side "buy" | "sell"; 0.0 = reached
def alert_side(*, price: float | None, buy_target: float | None, sell_target: float | None) -> str | None
def table(*, items: pd.DataFrame, quotes: dict, positions: pd.DataFrame) -> pd.DataFrame      # numeric frame of 3.3, alert rows first
def alerts(*, items: pd.DataFrame, quotes: dict) -> dict   # {"lines": [...], "missing": [symbols with a target and no price], "all_failed": bool, "by_list": {list_id: count}}
def first_alert(*, items: pd.DataFrame, quotes: dict) -> dict | None   # {"list_id", "symbol"} for D14
def resolve_selection(*, stored_rows: list[int] | None, rows_seen: list[int] | None,
                      order_seen: list[str], order: list[str], symbol: str | None) -> str | None
def is_active(*, setting: dict, now: dt.datetime) -> bool
def refresh_seconds(*, setting: dict, now: dt.datetime) -> int | None
def quote_bucket(*, setting: dict, now: dt.datetime, session: str, nonce: int) -> str
def tick_should_rerun(*, setting: dict, now: dt.datetime, session: str, nonce: int,
                      bucket_seen: str, dialog_open: bool) -> bool
def refresh_label(*, setting: dict, now: dt.datetime, tz: dt.tzinfo) -> str
def requests_per_hour(*, stocks: int, interval_minutes: int) -> int
def dialog_state(*, kind: str, payload: dict) -> dict      # {wl_dlg_* key: start value} for that dialog
def dialog_open(*, keys: set[str]) -> bool                 # any of wl_dialog / connect / perf-day flags present
def fetched_label(*, fetched: dt.datetime, now: dt.datetime, tz: dt.tzinfo) -> str   # "10:07" or "11 Oct 22:40"
```

- `table` matches "Shares held" by `match_key` on both the watched symbol and the position
  symbol, so IBKR's `BRK B` matches the stored `BRK-B`.
- `resolve_selection` is rule 3 of "Selection" in 5.4: a stored selection that differs from
  `rows_seen` is a click, read with the *previous* row order `order_seen`; otherwise the
  current `symbol` is kept if it is in `order`; otherwise `None` (D33); `None` for an
  empty list. An empty `stored_rows` after a marked row is the user clearing the mark:
  `None`. `stored_rows` is `None` before the first render.
- Functions that take `now` refuse a naive datetime.
- `dialog_state` returns a value for **every** field of the dialog, including fields that
  are disabled in the current mode (Streamlit drops a closed dialog's keys). It gives
  each field its start value: empty (`""` / `None`)
  for New list and Add; the current name for Rename; the current list id and targets for
  Edit; the current mode, minutes and hours for Update schedule.

### 5.2 Changed: `data/prices.py`

```python
def fetch_quote(*, symbol: str) -> dict | None
def fetch_quotes(*, symbols: tuple[str, ...], workers: int) -> dict[str, dict | None]
def resolve_symbol(*, text: str) -> dict
```

- `fetch_quote`: `{"price", "previous_close", "currency"}` from `fast_info["last_price"]`,
  `fast_info["regular_market_previous_close"]`, `fast_info["currency"]`. `None` when the
  lookup fails or the price is missing, `NaN`, zero or negative. `previous_close` is
  `None` when missing or not positive. Never raises.
- `fetch_quotes`: `fetch_quote` per symbol in a `ThreadPoolExecutor` with `workers` threads
  (the caller passes `_QUOTE_WORKERS = 6`). The result has every input symbol as a key, in
  input order, whatever order the threads finish in. No `st.*` call inside threads.
- `resolve_symbol`: `{"status": "ok" | "unknown" | "unreachable", "symbol": str | None}`.
  Candidates in order: `clean_symbol(text)`, then the same with `.` replaced by `-`
  (`BRK.B` → `BRK-B`, while `7203.T` and `VOD.L` resolve as typed). For each:
  `yf.Ticker(c).history(period="5d", raise_errors=True)`.

  | Outcome | Status |
  |---------|--------|
  | A non-empty frame | `ok`, with that candidate |
  | `YFPricesMissingError`, or an empty frame, for every candidate | `unknown` |
  | Any other exception (connection, timeout, HTTP, rate limit) | `unreachable`, stop |

  An invalid ticker (`clean_symbol` raises) is refused before any yfinance call.

### 5.3 Changed: `data/price_stats.py`, `charts/figures.py`

- `price_stats.summarize` gets two new keyword-only arguments, `live_price: float | None`
  and `live_previous_close: float | None`. When `live_price` is given it replaces the last
  close as `current` (day change, live P&L, market value, distance from the period high,
  both yields); `None` keeps today's behaviour. Call sites: `app.py` (1),
  `tests/test_price_stats.py` (6).
- `figures.candlestick` gets `buy_target: float | None` and `sell_target: float | None`
  (keyword-only), each a dashed horizontal line when given. Call sites: `app.py` (1),
  `tests/test_figures.py` (11).
- Existing defaults in both stay (D20).

### 5.4 Changed: `app.py`

**Shared detail.** The part of `tab_prices` after the symbol picker becomes:

```python
def _render_price_detail(
    *, symbol: str, period: str, positions: pd.DataFrame, cash_tx: pd.DataFrame,
    trades: pd.DataFrame, key_prefix: str, quote: dict | None,
    buy_target: float | None, sell_target: float | None, show_position: bool,
) -> None
```

- `tab_prices` keeps its symbol picker, arrows and keyless "Period" selectbox, and calls it
  with `key_prefix="px"`, `quote=None`, no targets, `show_position=True`.
- Widget keys are `f"{key_prefix}_cost"`, `_ma`, `_mean`, `_trades`, `_targets`; the chart
  gets `key=f"{key_prefix}_chart"`. Existing Prices keys are unchanged, so the existing
  Prices tests are unchanged. Watchlist's period selectbox has `key="wl_period"`.

**New functions.**

```python
@st.cache_data(show_spinner=False, max_entries=8)
def load_quotes(symbols: tuple[str, ...], bucket: str) -> dict    # {"quotes": {symbol: quote | None}, "fetched": aware UTC}
def _watchlist_state() -> dict                 # lists, items, setting, quotes, fetched, bucket, error; built once per run
def _save_watchlist(*, action: Callable[[], object]) -> str | None   # runs a write, refreshes the backup, returns the error text
def _render_watchlist_alerts(*, state: dict) -> None
def tab_watchlist(*, state: dict, positions: pd.DataFrame, cash_tx: pd.DataFrame, trades: pd.DataFrame) -> None
def _watchlist_tick() -> None                  # the timer fragment
def _open_watchlist_dialog(*, kind: str, payload: dict) -> None
def _close_watchlist_dialog() -> None
def _watchlist_dialog(*, payload: dict) -> None    # opens the dialog named in the payload
```

`load_quotes` takes positional arguments like the existing cached loaders. The spinner
text is not in the cache decorator (it would be inserted at the page top on every timed
fetch); `_watchlist_state` wraps the call in `st.spinner("Fetching watchlist prices…")`
only for a session's first fetch.

**Clock.** `app.py` never reads the clock for the watchlist; it calls
`watchlist.utc_now()` and `watchlist.local_tz()` through the module, never through
`from data.watchlist import utc_now` (that would freeze the binding for tests that
`import app`). Tests patch those two attributes on the
imported module (a name defined in `app.py` cannot be patched under `AppTest`).

**One state per run.** `main()` calls `watchlist.init_db` and `_watchlist_state()` once,
after `render_sidebar()`, before the tabs and before either early return, and passes the result to the alert box and
to the tab. The DB is read once and quotes are requested once per run. `load_quotes` gets
the sorted tuple of *all* watched symbols and is not called when nothing is watched.

**Fetch schedule.** `load_quotes` is cached on `(symbols, bucket)`:

| Setting | Bucket | Effect |
|---------|--------|--------|
| Only when opened, or an ended schedule | `f"{session}|{nonce}"` | One fetch per browser session. |
| Every N minutes, active | `f"{nonce}|{floor(epoch_seconds / (N * 60))}"` | One fetch per N-minute window, shared by every session. |

`nonce` is `st.session_state["wl_nonce"]` (starts at 0); **Update now** adds 1 in its
`on_click`. The sidebar "Refresh data" clears `st.cache_data` and so also fetches again.
Adding or removing a stock changes `symbols` and so fetches again. **Update now** calls
neither IBKR nor the OHLC loader, also after a failed IBKR pull (D30).

**Auto update (D9).** In `main()`, right after `_watchlist_state()` and before the first
early return, in this order:

```python
st.session_state["wl_bucket_seen"] = state["bucket"]       # also when nothing is watched
st.session_state["wl_tick_armed"] = False
st.fragment(_watchlist_tick, run_every=watchlist.refresh_seconds(setting=..., now=...))()
```

`_watchlist_tick` renders nothing:
1. If `wl_tick_armed` is `False` (this is the fragment's run inside a full run): set it to
   `True` and return. It never reruns during a full run.
2. Otherwise (a timer run): read the setting and call `watchlist.tick_should_rerun(...)`.
   When it returns `True`, call `st.rerun()` (whole app).

`tick_should_rerun` is `True` when no dialog is open and the bucket for `now` differs from
`bucket_seen`. So the page redraws once per window; when the schedule ends the bucket
changes form, one full run happens, `run_every` becomes `None` and the timer stops.

"A dialog is open" = any of `wl_dialog`, `_CONNECT_OPEN`, `_PERF_DAY_KEY` is in session
state. A stale flag must not block auto update for the session, so:
- `tab_watchlist` deletes `wl_dialog` at each place where it returns without reaching the
  dialog call (the watchlist could not be read; `init_db` failed) and when the flag's
  `kind` is not one of the six known kinds. It is never deleted at the top of `main()`,
  so it cannot fight the `on_click` that set it in the same run.
- `tab_performance` deletes `_PERF_DAY_KEY` where it finds no matching day row today
  (`app.py` around line 1359) and at its early return for too little data; `main()`
  deletes it in both early-return paths, where that tab is not rendered.
- The test "is a dialog open" is the pure `watchlist.dialog_open(*, keys: set[str]) -> bool`
  over the session-state key names, so it can be unit-tested.

**IBKR failure (D30).** When `load_portfolio` raises `FlexError`, `main()` stores
`{"code", "message"}` in `st.session_state["flex_failed"]`. While that key is present,
`main()` does not call `load_portfolio`; it renders the stored failure (connect card or
error text, as today). The key is deleted in exactly three places: the sidebar
"Refresh data" button, a successful save in the connect dialog, and the not-configured
path. Exact places: inside `if st.sidebar.button("🔄 Refresh data")` in `render_sidebar`
(the first statement of `main()`, so before the skip); after `save_env_values` and before
`st.rerun()` in `connect_dialog`; and in the not-configured branch of `main()`. The
message is stored already scrubbed of the token. So row clicks, list picks, **Update
now** and timed redraws never wait for IBKR after a failure. No existing test needs
editing for this (checked in round 4 on a scratch copy: 485 passed with the rule applied).

**Dialogs.** One dialog at a time, driven by a flag.
- A button's `on_click` calls `_open_watchlist_dialog(kind=..., payload=...)`, which sets
  `st.session_state["wl_dialog"] = {"kind": ..., **payload}`. The payload holds everything
  the dialog shows (list id and name, the item's symbol, targets, list, quote, the other
  lists). **It also assigns `watchlist.dialog_state(kind=..., payload=...)` to the
  `wl_dlg_*` widget keys**, so each field opens with its start value. No field passes a
  start value in the dialog body (Streamlit would ignore it once the key exists, and Edit
  or Rename would open blank). The one exception is fixed, not a start value: target
  fields pass `value=None`, which only makes an empty field possible (without it an
  emptied target reads back as 0.0 and could never be cleared); the assigned key still
  decides what is shown. The dialog is built from the payload and those
  keys only, so it behaves the same whether
  the click inside it is a fragment run (browser) or a full run (`AppTest`), and when the
  row was changed in another browser tab.
- `tab_watchlist` calls `_watchlist_dialog(payload=...)` while the flag is set. The title
  is built per call: `st.dialog(title, on_dismiss=_close_watchlist_dialog)(body)(...)`.
- Every dialog body starts with `if "wl_dialog" not in st.session_state: st.rerun()`,
  which closes it in a browser after a callback cleared the flag.
- **Save, Create, Delete list, Add, Add and add another, Remove (confirm) and Cancel are
  `on_click` callbacks.** A callback reads the dialog's widgets from session state
  (`wl_dlg_*` keys), does the write through `_save_watchlist`, and then either:
  - on success: sets `wl_list` / `wl_symbol` and the one-time message or toast text, and
    calls `_close_watchlist_dialog()`; "Add and add another" instead keeps the flag, sets
    the "Added …" line and assigns the Add dialog's empty `dialog_state` to the fields;
  - on a `WatchlistError`: stores the text in `st.session_state["wl_dlg_error"]` and keeps
    the flag, so the dialog stays open and shows it.
- `_close_watchlist_dialog` removes `wl_dialog`, `wl_dlg_error` and the "remove armed"
  flag. It never deletes or changes the `wl_dlg_*` widget keys; the next open assigns
  them. Also the `on_dismiss` callback.
- The Edit dialog's "List" selectbox has `options=<list ids>` and a `format_func` that
  gives the name.
- A toast is requested by a callback as text in `st.session_state["wl_toast"]` and shown
  with `st.toast` in `main()` on the next full run, before the early returns, which then
  removes the key.
- Remove is two keyed buttons: `wl_dlg_remove` (its `on_click` sets
  `st.session_state["wl_dlg_remove_armed"]`) and `wl_dlg_remove_confirm`, rendered in a
  different place (3.5).

**Selection.** `st.session_state["wl_list"]` (list id) and `st.session_state["wl_symbol"]`
are the truth. Neither is a widget key.

1. New session (`wl_list` not set): `first_alert(...)` gives the list and symbol (D14);
   with no alert, the first list and no stock (D33).
2. `wl_list` not in the DB any more: the first list. (F3 and changes in another tab.)
3. The list selectbox has `key="wl_list_select"`, `options=<list ids>` and a
   `format_func` giving `name` or `name (N alerts)`, so a changing label does not reset
   the pick. Before it is created each run, `st.session_state["wl_list_select"] = wl_list`;
   its `on_change` (which runs before the body) copies the pick into `wl_list`.
4. The table is `st.dataframe(styler, key=f"wl_table_{list_id}", on_select="rerun",
   selection_mode=["single-row", "single-cell"])`. Before it is created each run:
   `wl_symbol = watchlist.resolve_selection(stored_rows=<the key's rows or None>,
   rows_seen=wl_rows_seen, order_seen=wl_order_seen, order=<symbols of this run>,
   symbol=wl_symbol)`; then the row index of `wl_symbol` (or no row) is written to the
   table's key and to `wl_rows_seen`, this run's order to `wl_order_seen`, and the table's
   key to `wl_table_seen`. `wl_rows_seen` is compared only when `wl_table_seen` is this
   table, so another list's rows are not read as a click.
5. A user sort keeps the same original row selected, so the stored selection does not
   change and the detail keeps its stock (D22).

**Tab wiring.** Connected: `Overview, Positions, Trades, Prices, Watchlist, Dividends,
Performance, Net Worth`. `tab_overview` gets the state and calls
`_render_watchlist_alerts` first.

**IBKR not connected (D18).** In both early-return paths of `main()` the alert box is
rendered above the connect card, and the bare `render_net_worth_or_hint()` call is replaced
by `st.tabs(["Watchlist", "Net Worth"])`, whose Net Worth tab still calls
`render_net_worth_or_hint()`. `tab_watchlist` then gets empty `positions`, `cash_tx` and
`trades` frames. The sidebar's "Database: …" text is rendered before `init_db` and is left
as it is: on a fresh install it says "empty" on the first run and "ready" from the second.

### 5.5 To prove in a browser before the rest is built

Built first, as a spike inside the real tab, each with a screenshot:

1. Row click selects that stock; the highlight follows `wl_symbol` after add, move, remove
   and after rows reorder; after a sort the detail and the highlight stay on the stock.
2. After a timer-driven full rerun the user stays on the same tab at the same scroll
   position; an open dropdown at that moment does not break.
3. New list and Add open empty, also after "Add and add another"; Rename, Edit and
   Update schedule open with the current values, also right after another dialog closed.
4. The empty timer fragment leaves no visible gap.
5. The Styler texts (`reached`, `no data`) display while sorting stays numeric.

**If any of these fails, work stops and the user is asked (D27).** There is no automatic
fallback. (An option to offer then: a "Stock" dropdown with ▲ ▼ arrows above the detail
instead of the row click, with keys `wl_symbol_prev` / `wl_symbol_next`.)

## 6. Edge cases

| Case | Behaviour |
|------|-----------|
| Ticker already in this list | Refused: `QTUM is already in "Quantum".` |
| Ticker already in another list | Refused: `QTUM is already in "ETFs". Open that list and use Edit / move / remove to move it here.` |
| Same stock in another spelling (`BRK.B` when `BRK-B` is watched) | Refused with the same text, naming `BRK-B`. |
| Unknown ticker | Refused: `"QTUMM" was not found on yfinance. Check the ticker.` |
| yfinance not reachable on Add | Refused: `Could not check "RGTI" (yfinance did not answer). Try again.` |
| Lower case, outer spaces | Cleaned, then checked. |
| `BRK.B`, `BRK B` | Stored as `BRK-B`; "Shares held" matches IBKR's `BRK B`. |
| `7203.T`, `VOD.L`, `^GSPC`, `BTC-USD`, `EURUSD=X` | Accepted as typed when yfinance knows them (D3). "Shares held" is `—` unless the IBKR symbol has the same `match_key`. |
| Empty ticker | `Type a ticker.` No yfinance call. |
| Ticker longer than 20 characters, or with a character outside `A-Z 0-9 . - ^ =` | `That is not a valid ticker. Use letters, digits and . - ^ =` No yfinance call. |
| Duplicate list name (any case or accent form) | `A list named "Quantum" already exists.` |
| Rename to the same name in another letter case | Allowed. |
| Empty or blank list name | `Type a name.` |
| List name longer than 40 characters | `Use 40 characters or fewer.` |
| List name with quotes, `%`, `;`, emoji | Stored and shown as typed (parameterised SQL). |
| Target zero, negative, `NaN` or infinite | `A target must be a number above 0.` |
| Buy target >= sell target | `The buy target must be below the sell target.` |
| Target field empty | Stored as no target. |
| Buy target at or above the current price | Allowed; the one-time line in 3.5; the alert shows at once. |
| Price missing, zero or negative | `no data`; no alert; other rows work. |
| Previous close missing or zero | Day shows `—`; price and alerts work. |
| Quote in pence (`GBp`) or another currency | Targets have no currency and are compared with the quote as it comes; the currency is shown next to the price and in the Edit field labels. |
| Delisted after it was added | Stays with `no data`; named on Overview only if it has a target (D28); the user removes it. |
| Watched stock is later bought | "Shares held" shows the count; the detail shows the position tiles. |
| Held stock is later sold | "Shares held" `—`; position tiles hidden; the stock stays watched. |
| Last list deleted | State 3.1. |
| Selected stock removed or moved | No stock is selected (D21); an empty list shows 3.2. |
| Selected list deleted in another browser tab | The first list is selected; no error. |
| Rename, delete or add on a list deleted in another tab | The dialog shows `That list no longer exists.`; closing it shows the current lists. |
| Edit of a stock removed in another tab | The dialog shows `QTUM is no longer on the watchlist.` |
| Move to a list deleted in another tab | The dialog shows `That list no longer exists.` |
| Two tabs save the same stock | Last save wins. |
| Two tabs create the same list name | The second is refused with the duplicate-name text. |
| DB locked for more than 5 seconds, or read-only, on save | The dialog stays open: `The database is busy or cannot be written. Try again.` (D26) |
| DB file, its directory or the watchlist tables missing | `init_db` creates them at the start of the run; a read before that returns an empty watchlist. |
| DB cannot be read | `The watchlist could not be read from the database.`; other tabs work. |
| Schedule ends while the page is open | One redraw at the first tick after the end; the line shows "only when I open the app"; the timer stops. |
| App closed, opened after the end time | Only when opened. |
| App closed, opened before the end time, or "always" | Still on (D10). |
| Daylight-saving change during "for X hours" | Still X elapsed hours (D24); the shown local end time shifts by the hour. |
| End time on another day | Shown with the date. |
| Timer tick while a dialog is open | No redraw; it happens at the first tick after the dialog closes. |
| The IBKR pull failed | No click and no timed redraw calls IBKR again; the connect card stays; watchlist prices update. "Refresh data" or a newly saved token retries (D30). |
| `init_db` fails (directory cannot be created, file not writable) | A warning line on the Watchlist tab; no alert box; every other tab works. |
| An item whose list row is missing | Still listed by `read_items` with an empty list name; it can be moved to an existing list or removed (a targets-only save is refused with `That list no longer exists.`). |
| `7203-T` typed while `7203.T` is watched | Refused as already watched (same `match_key`); the duplicate lookup is by `match_key`. Accepted limit of D19. |
| `DB_PATH` contains `?`, `#` or `%` | Reads open that exact file (URI built with `as_uri()`). |
| A quote fails in "only when opened" mode | `no data` until **Update now** or "Refresh data". |
| Many stocks and a 1-minute schedule | The dialog shows the requests per hour; no hard limit. |
| Market closed | The quote is the last traded price; alerts keep showing while true (D5). |
| Nothing watched | No yfinance call, no alert box, no schedule line. |
| New alert appears mid-session, in another list | The list is not switched; "Alerts in other lists" names it; Overview shows it. |
| An old backup is restored | Lists, targets and the schedule are as they were then (D25). |

## 7. Test plan

### 7.1 Test isolation and seams

Today `tests/test_app_smoke.py` run alone has one failing test: `config` is first imported
inside `AppTest.run()`, and its import copies the real `.env` credentials over the values
the fixture set. Every new smoke test would inherit that. So `tests/conftest.py` gets:

- `import config` at module top, so the import happens before any fixture sets the
  environment (verified in review: the file then passes alone).
- Autouse fixtures for every test:
  - `DB_PATH` → `tmp_path` (as `_isolate_backups` does for `BACKUP_DIR`);
  - `config.ENV_PATH` → a file in `tmp_path`, and `FLEX_TOKEN` / `FLEX_QUERY_ID` set to
    `""` unless the test sets them (an empty value keeps `is_configured` false; any other
    made-up string would make the app call `flex.download_statement`). Existing tests keep
    their meaning because their own `setenv` runs later and wins;
  - `st.cache_data.clear()` before and after each test (the schedule bucket has no session
    part, so equal patched times would otherwise share a cache entry between tests);
  - stubs for `prices.fetch_quote`, `prices.fetch_quotes`, `prices.resolve_symbol` and
    `prices.fetch_ohlc`.
- Tests of those four functions keep a reference to the real one taken at import, as
  `tests/test_prices.py` already does for `fetch_expense_ratio`. The four existing
  `fetch_ohlc` tests in `tests/test_prices.py` **must** be edited to call that reference
  (three of them would otherwise pass against the stub by accident).
- **Time:** tests patch `watchlist.utc_now` and `watchlist.local_tz`. Every smoke test
  that shows a time, a schedule or counts fetches patches both.
- **No direct call of an `app.py` function that reads `settings`** (`import app` builds
  `settings` before fixtures run, so it would point at the real DB). Logic that needs a
  direct test is a pure function in `data/watchlist.py`: `tick_should_rerun`,
  `resolve_selection`, `dialog_state`, `quote_bucket`, `refresh_label`.
- The "existing DB" used in migration tests is built in `tmp_path` with `snapshots` and
  `history`; the real `db/snapshots.sqlite` is never read or copied.
- `AppTest` lookups: the Watchlist tab is found by its label, not by index; dialog widgets
  are found by key at the root.
- Done criterion: the full suite passes **and each test file passes when run alone**.

### 7.2 Unit: `tests/test_watchlist.py` (new)

- Lists: create; duplicate refused for another case, another accent form (NFC/NFD) and
  outer spaces; blank, NBSP-only and 41-character names refused; rename; rename to an
  existing name refused; rename to own name in another case allowed; delete empty; delete
  with stocks removes its items and only its items; names with quotes, `%`, `;`, emoji
  round-trip; `read_lists` counts and order; rename and delete of a missing list.
- Items: add; duplicate refused in the same list and across lists; duplicate by
  `match_key` (`BRK.B` vs `BRK-B`) refused, also by raw SQL (the UNIQUE column); add to a
  missing list; update targets; move; move and targets in one call; remove; update and
  remove of a missing symbol; move to a missing list; two saves of one stock, last wins.
  Each refusal is a `WatchlistError` with the exact text of section 4 / 6.
- Targets: zero, negative, `NaN`, `inf`, buy >= sell refused; `None` clears; `numpy`
  float and int inputs are stored as reals; raw-SQL writes of text, `inf`, a lower-case
  symbol and `interval_minutes = 1.5` are refused by the CHECKs.
- Error mapping: each row of the error table in section 4, including an unexpected
  `sqlite3.Error` giving the generic text.
- Connection: `PRAGMA foreign_keys` reads 1; a raw-SQL list delete cascades; no
  `ResourceWarning` after each public function (checked in the test with
  `warnings.catch_warnings(record=True)` and `gc.collect()`).
- `init_db`: on a DB with the six current tables it adds `watchlists`,
  `watchlist_items` and `watchlist_refresh` and changes no existing row count (the test
  asserts those three exist, not the exact table set, because `AUTOINCREMENT` adds
  `sqlite_sequence`); twice changes nothing; creates a missing parent directory.
- Reads: on a missing file create no file and no directory and return empty; on a DB
  without the tables return empty; on a read-only file with tables work.
- Failed write: a write that raises midway leaves the tables as they were.
- Locked DB, `_BUSY_SECONDS` patched to 0.1: with another connection in `BEGIN IMMEDIATE`
  every write raises the "busy" error; with `BEGIN EXCLUSIVE` reads (which also use
  `_BUSY_SECONDS`) return empty with the error flag. Read-only file: a write raises the "busy or cannot be written" error.
- `clean_symbol`, `match_key`: `" qtum "`, `brk.b`, `BRK B`, `7203.T`, `^GSPC`, `BTC-USD`,
  empty, 21 characters, bad characters.
- `distance_left`, `alert_side`: above, equal, below for both sides; missing price;
  missing target.
- `table`: numeric dtypes; held, unheld, held by another spelling; missing quote; Day is
  NaN when the previous close is missing; alert rows first, then symbol; 0.0 at equality;
  a `GBp` quote is compared as it comes.
- `alerts`: buy only, sell only, both lists, none; `missing` holds only stocks with a
  target (D28); `all_failed`; `by_list` counts; order. `first_alert`: none, one, buy
  before sell.
- `resolve_selection`: first render (`stored_rows=None`); no change keeps the symbol; a
  click picks the clicked row; a click read with the previous order after a reorder; the
  symbol gone from the list gives `None`; a cleared mark gives `None`; an empty list gives `None`; a sort
  (stored rows unchanged) keeps the symbol (D22).
- Schedule: `is_active`, `refresh_seconds`, `quote_bucket`, `refresh_label`,
  `requests_per_hour`, `tick_should_rerun` for: only when opened; timed and running;
  exactly at the end time; ended; always (still on after any time, D10); the window edge
  (one second before, at, one second after); the bucket holds the session only when not
  active; the nonce changes the bucket; a label whose end is on another day; a 4-hour
  schedule across a daylight-saving change in `Europe/Berlin` and `America/New_York` is
  4 elapsed hours (these two zones need the system zone data, present on macOS and
  Linux; `tzdata` is added to the dev requirements for other systems); a naive `now` is
  refused; `tick_should_rerun` is `False` for the same
  bucket, `True` for a new one, `False` while a dialog is open, `True` once when the
  schedule has ended.
- Refresh setting: write then read on a new connection keeps the aware `until`; `until`
  without an interval is refused; an unparsable or naive stored `until` reads as "only
  when opened"; saving "for 4 hours" twice sets `until` from the second `now` (F9).
- `dialog_open`: each of the three flags alone, none, an unrelated key.
- `fetched_label`: today gives the time; another day gives date and time.
- `dialog_state`: every field of each dialog has a key, also disabled ones; empty values
  for New list and Add; the payload's name for Rename; list
  id and targets for Edit; mode, minutes and hours for Update schedule.
- Reads with `?`, `#` and `%41` in the DB path (built in `tmp_path`) open that file and
  create no other; `read_items` returns an item whose list row was deleted with raw SQL and
  foreign keys off.

### 7.3 Unit: changed modules

- `tests/test_prices.py`: `fetch_quote` for normal, `KeyError`, other exception, empty
  symbol, `NaN`, zero and negative price, previous close missing and zero; `fetch_quotes`
  returns every key in input order when the threads finish in reverse order (forced with
  `threading.Event` and `wait(timeout=...)`, `workers` >= number of symbols, no `sleep`) and one failing symbol does not break the rest;
  `resolve_symbol` for ok, `YFPricesMissingError`, empty frame, connection error,
  `BRK.B` → `BRK-B` on the second candidate, `7203.T` on the first, no second call after
  `unreachable`, and no call at all for an empty or invalid ticker.
- `tests/test_price_stats.py`: `summarize` with `live_price` changes current, day change,
  live P&L, market value, yields; with `None` every existing expectation is unchanged.
- `tests/test_figures.py`: `candlestick` with the buy line, the sell line, both, neither.
- `tests/test_backup.py`: `refresh` overwrites today's file with the new content; `run`
  writes the pre-sync file once and does not overwrite it; **`refresh` then `run` on the
  same day still writes the pre-sync file**; `refresh` leaves the pre-sync file alone;
  pruning a day removes both files; a pre-sync file without its main file is pruned when
  its day is outside the kept days; a temporary file left by a crashed run is removed;
  `status` counts days; a backup directory with `#` in its name verifies. No existing
  test's meaning changes; `test_unverifiable_copy_is_not_left_behind` is updated to the
  new temporary-file suffix and also asserts no pre-sync file is left, and
  `test_prunes_to_keep_newest` also asserts the pre-sync files, and
  `test_stale_part_file_does_not_block_a_backup` asserts the planted old `.part` file is
  removed; `test_missing_source_db_is_a_noop` still holds (no directory is created); a
  failed pre-sync copy does not stop the main copy; a failed `VACUUM`
  leaves the old file and no temporary file; a missing DB writes nothing; neither function
  raises; a DB restored from an older backup reads back the older lists and schedule (D25).

### 7.4 Smoke: `tests/test_app_smoke.py` (`AppTest`)

- Tabs: eight, Watchlist fifth. `EXPECTED_TABS`, the test name
  `test_app_runs_without_exception_and_renders_all_seven_tabs` and the module docstring
  ("7 tabs") are updated.
- No lists (3.1); empty list (3.2); last list deleted returns to 3.1; nothing watched
  shows no schedule line and makes no quote call.
- F1, F2, F3 (with stocks, empty, cancel deletes nothing, more than 10 stocks text).
  Each flow continues with another action after the dialog closes, to prove the next run
  works. Rename opens with the current name, also when opened twice in a row and right
  after another dialog closed.
- F4: ok; empty ticker; invalid ticker (no `resolve_symbol` call); unknown; unreachable;
  duplicate in this list; duplicate in another list; `brk.b` stored as `BRK-B`;
  **Add and add another** keeps the dialog, shows its line and empties the fields.
- F5: one click does not remove; the second does; Cancel disarms.
- F6 and D21; F7 set, change, clear (an emptied field is saved as no target); each refusal shows its text and saves nothing; the
  one-time "already at or below" line and its sell twin; the Edit labels carry the quote
  currency (`GBp`). **Edit opens with the saved targets and list: Edit, Save, Edit again
  shows the saved values, and a second Save without typing changes nothing.** The three
  toast texts of F6 / F7.
- Selection: tests set `at.session_state["wl_symbol"]`, rerun, and check that it is kept
  and the detail matches: the exact tile rows of 3.4 for a held and for an unheld stock,
  the hidden "Avg cost line" and "My trades" toggles for an unheld stock, target tiles
  and captions, "Current price (delayed)" equals the stubbed quote; with a failed quote
  and working candles the chart is still drawn.
- Table: the frame's values for alert, distances and `reached`; Day empty when the
  previous close is missing; the all-failed line (3.7); one failing stock leaves the rest.
- D14: a new session with an alert in the second list opens on that list and stock; the
  dropdown labels show counts; "Alerts in other lists" names the other list; an alert that
  appears on a later run does not switch the list.
- F8: no box with no alert; buy line; sell line; order; `No price for:` only for a stock
  with a target; the `Watchlist prices` title; the all-failed line; no button inside the
  box (D16); no `st.error` from the watchlist.
- F9: saving a schedule changes the line; the dialog's disabled fields per mode; the
  "Now:" and "Save starts" lines; the requests-per-hour line for 12 stocks at 1 minute;
  the dialog opens with the saved mode, minutes and hours; a second `AppTest` on the same DB reads the schedule
  (restart), for a timed one and for "always"; with `watchlist.utc_now` patched past the
  end time the line says "only when I open the app".
- F10 / D13: **Update now** fetches quotes again and leaves `download_calls` (IBKR) and
  `fetch_calls` (OHLC) unchanged.
- D8: a plain rerun does not fetch again; a new session does; "Refresh data" does; quotes
  are fetched once for the alert box and the table in one run.
- D17 / D29: a watchlist write refreshes the backup file in the tmp backup directory and
  leaves the pre-sync file; both carry the patched local date.
- D18: with no credentials, and with a failing pull, Watchlist and the alert box render
  and "Shares held" is empty; the Watchlist tab is found by label.
- D30 (with a counting failing `download_statement` passed to the app fixture): after a
  failed pull, a plain rerun, a change of `wl_symbol` and of `wl_list_select` (set in
  session state; `AppTest` cannot click a row) and **Update now** make no further IBKR
  call while quotes are still fetched; "Refresh data" makes one; the stored message has
  no token in it; the retry sentence of 3.8 is shown. In a separate test, a saved token
  makes one call (asserted on the same `.click().run()`; the existing connect dialog
  cannot be run again after its save). In another, emptying `FLEX_TOKEN` and rerunning
  removes the stored failure with no IBKR call, and restoring it makes one.
- Fresh install: an `AppTest` run with `DB_PATH` under a directory that does not exist
  renders the Watchlist tab (3.1). With `watchlist.init_db` patched to raise, the tab
  shows its warning line, there is no alert box and every other tab renders.
- A stale `_PERF_DAY_KEY` set in session state for a day with no row is gone after one
  run.
- "Fetched" carries the date: with `watchlist.utc_now` patched to the next day and no new
  fetch, the schedule line and the alert box show the date.
- A stock with no target has no "Target lines" toggle; the IBKR-not-connected caption.
- Two tabs (two `AppTest` instances on one DB): selected list deleted elsewhere; edit of
  a stock removed elsewhere; rename, delete and add on a list deleted elsewhere; move to a
  list deleted elsewhere; the same list name created twice.
- DB errors: with `watchlist.read_items` patched to report an error the tab shows its
  line and the other tabs render; with a write patched to raise the busy error the dialog
  stays open with the text.
- Prices tab regression: its existing tests pass unchanged (same keys, same fetch count),
  including for a stock that is both held and watched with no targets (two equal charts
  in one run).

### 7.5 Manual checks in the browser (not automatable), each with a screenshot

1. The five spike items of 5.5.
2. With "every 1 minute": the table, the tiles and the Overview box change by themselves.
3. Text typed in an open dialog survives a tick; the redraw comes after the dialog closes.
4. Closing each of the six dialogs with X and with Esc, then reopening: New list and Add
   are empty; Rename, Edit and Update schedule show the current values; Remove is disarmed.
5. The schedule ends while the page is open: the line changes and the redraws stop.
6. Restart the app before and after the end time, and with "always".
7. Two browser tabs: delete a list in one, use it in the other.
8. Market closed: prices and alerts still show.
9. Real yfinance: add `QTUM`, an unknown ticker, `BRK.B`, `^GSPC`; then with the network
   off, Add shows "could not check" and the table shows `no data`.
10. With a schedule on and a wrong token: no IBKR request per minute and none on row
    clicks (app log); "Refresh data" makes one.
11. Restore yesterday's backup file over the DB: lists, targets and schedule are
    yesterday's.
12. One screenshot of each state: 3.1; 3.2; 3.3 with an alert row, a `no data` row and
    "Alerts in other lists"; detail for a held and for an unheld stock; each of the six
    dialogs; Remove armed; a refused ticker; the Overview box with a buy and a sell line;
    the `Watchlist prices` box; IBKR not connected with Watchlist shown (3.8), for a
    rejected token and for another failure.
13. A fresh install (no `db/` directory): the app starts and the Watchlist tab works.
14. The README lists the Watchlist tab and what it does.
15. In Edit, empty a target field with the keyboard and Save: the target is cleared
    (`AppTest` cannot show that a browser lets the field be emptied).

## 8. Open questions

None.

## 10. Final-review notes (binding; they override earlier sections where they differ)

All minor findings of round 5. None changes a decision.

Screen and wording (UX):
1. **Number precision.** One rule for the table, tiles, alert lines and target fields:
   2 decimals for a value >= 1; 4 significant digits for a value below 1 (FX, small
   coins). Target fields set `format` and `step` to match the quote's size.
2. `IBKR not connected: positions not shown.` is shown at the top of the Watchlist tab in
   every state (3.1, 3.2, 3.3), not under the table.
3. 3.8 texts by state:
   - no token set, or token rejected (connect card): `Save a token above to connect.`
   - another failure (error text): `Fix the token/query in .env if needed. IBKR is not
     asked again automatically. Click **Refresh data** in the sidebar to retry.`
   The retry sentence is shown only while `flex_failed` is set.
4. A stock with no quote: `Added RGTI (no price yet).` in Add, and `RGTI · no price` as
   the first line of Edit.
5. Database trouble on the Watchlist tab: the tab shows only the line
   (`The watchlist could not be read from the database.` or, when `init_db` failed,
   `The watchlist could not be set up in the database.`), with no 3.1 info and no
   **＋ New list**. Overview then shows one line in the box:
   `Watchlist could not be read. Targets were not checked.`
   In 3.3 "the database line" refers to these texts.
6. `Alerts in other lists: Metals (1 alert)`, the same form as the dropdown.
7. Section 6: the mode is called "only when I open the app" everywhere. Added row: a sell
   target at or below the current price is allowed, shows the one-time sell line of 3.5,
   and the alert shows at once.

Streamlit:
8. The "Target lines" and "Avg cost line" / "My trades" toggles are rendered
   `disabled=True` instead of hidden when they do not apply, so their state is kept.
9. `dialog_state` for Update schedule with no schedule saved: minutes 1, hours 4. With a
   timed schedule: the saved minutes, and hours 4 (hours are not stored, only the end).
10. `_watchlist_dialog` is called only when neither `_CONNECT_OPEN` nor `_PERF_DAY_KEY` is
    set; otherwise `wl_dialog` is deleted (two dialogs in one run raise).
11. The alert box is rendered into an `st.container()` slot that always exists, and every
    `st.tabs` call has a `key`, so the box appearing or disappearing cannot reset the
    active tab. The not-connected state is added to spike item 2 of 5.5.
12. `_render_price_detail` gets an explicit `layout: str` argument (`"prices"` or
    `"watchlist"`) for tile rows, labels and caption; `key_prefix` is for keys only.
13. Edit of an item whose list is missing: `dialog_state` gives `None` for "List", the
    field shows empty, and **Save** is refused with `Pick a list.` until one is picked.
    (Replaces "a targets-only save is refused" in section 6.)

Data:
14. Backup cleanup removes only `snapshots-*.part` files, never other `*.part` files (the
    backup directory may be a shared or synced folder). A test covers an unrelated
    `.part` file.
15. Add order: `clean_symbol` → duplicate lookup by `match_key` in `read_items` → only
    then `resolve_symbol`. So `7203-T` against a watched `7203.T` says "already in", with
    no yfinance call.
16. The `backup.run` call is in `_sync_and_read` (not `load_portfolio`). That function
    keeps its own `dt.datetime.now()` for the sync and uses the watchlist clock only for
    the backup day; they are the same day except under a patched clock in tests.
17. The backup lock is taken once, in `run` and in `refresh`; the shared helper does not
    take it.
18. `watchlist.local_tz()` returns a `zoneinfo.ZoneInfo` zone (not a fixed offset), so the
    shown end time is right across a daylight-saving change.

Tests:
19. The `ResourceWarning` check sets `warnings.simplefilter("always", ResourceWarning)`
    inside the `catch_warnings` block; the same check is added for `backup.run` and
    `backup.refresh`.
20. A list change in a smoke test is made with `at.selectbox(key="wl_list_select")`'s
    `set_value(...)` or by setting `wl_list`, never by setting `wl_list_select` in session
    state (rule 3 overwrites it).
21. 7.2 Items adds: an item whose list is missing can be moved, can be removed, and a
    save with no list picked is refused.
22. The 3.8 sentences are asserted in the connect-card path and in the error-text path.
23. F4 adds: `7203-T` while `7203.T` is watched is refused with no `resolve_symbol` call.
24. The cache bucket's `session` is `app.session_key()` (the uuid kept in session state),
    not Streamlit's context session id, which is constant under `AppTest`.
25. With IBKR not connected and a healthy DB the watchlist emits no `st.warning` (an
    existing test asserts `at.warning == ["IBKR: not connected"]`); its caption of note 2
    is an `st.caption`.
26. `load_quotes` and every other new function is called with keyword arguments. A test
    walks the AST of the new and changed modules and fails when a new function, or a new
    parameter of `summarize`, `candlestick`, `run`, has a default value.

## 9. Review changelog

### Round 1 — 2026-10-12 — UX reject, Streamlit reject, QA reject, Data reject

User answers recorded as D13-D20; designer decisions D21-D26.

Fixed (blocker and major): auto update redesigned as one timer fragment asking for a full
redraw; ticker check through `history(raise_errors=True)`; quote cost, parallel fetch and
the regular previous close; chart and period keys against duplicate element IDs; selection
rules; flag-driven dialogs with `on_dismiss`; the wrong backup fact and backup after every
write; reads that wrote; DB error mapping; aware UTC time; test isolation; a test for
every edge case; ticker spellings and kinds; `NaN` and non-positive values; distance
columns; alert findability; the look of the alert box; manual checks instead of "not
covered".

Fixed (minor): wording of fetch time, table alert text, schedule control; **Update now**;
click counts; "Edit / move / remove"; no "nothing selected" state; toggle labels;
duplicate-in-other-list text; "Add and add another"; "Current price (delayed)"; CHECKs;
`AUTOINCREMENT` reason; pragma test; `contextlib.closing`; migration path; `LEFT JOIN`.

Not changed, with reason: Remove and Move stay inside one dialog (two more buttons would
crowd the header); the Add dialog does not show the price before saving (the check runs on
Add); no hard limit on stocks × interval (the user chose free intervals, D8).

### Round 2 — 2026-10-12 — UX reject (2 major), Streamlit reject (3), QA reject (6), Data reject (1)

All four confirmed the round-1 fixes are in the body. User answers recorded as D27-D30.

Fixed (major):
- **Edit dialog did not name the stock; double click could remove** (UX): titles built per
  call, first body line names the stock, the armed button has another label and place
  (3.5, 5.4).
- **Alert in another list invisible on the Watchlist tab** (UX): "Alerts in other lists"
  line (3.2, 3.3).
- **Timer could rerun during a full run and reset other widgets** (Streamlit): the
  `wl_tick_armed` rule and the fixed order in `main()` (5.4).
- **Dialog close broke the next `AppTest` run; `wl_list` clashed with a widget key**
  (Streamlit): all dialog actions are `on_click` callbacks, keys get empty values instead
  of being deleted, `wl_list` is not a widget key (5.4).
- **A failed IBKR pull would be retried every minute** (Streamlit): D30, `flex_failed`
  and `wl_timed_run` (5.4).
- **`app._now` cannot be patched under `AppTest`** (QA): the clock moved to
  `watchlist.utc_now` / `local_tz` (5.1, 7.1).
- **Direct test of the timer could not observe a rerun and would open the real DB** (QA):
  pure `tick_should_rerun`; rule against direct calls of `app.py` functions (5.1, 7.1).
- **`-W error::ResourceWarning` test was vacuous** (QA): in-test warning capture (7.2).
- **Row-click rule untested** (QA): pure `resolve_selection` with unit tests (5.1, 7.2).
- **Edge cases without a test** (QA): added for empty ticker, no yfinance call on an
  invalid ticker, pence quotes, two saves, read-only DB, mid-session alert, Day with no
  previous close, delete/add/move on a deleted list, no schedule line, D10 "always", D13,
  D25, disabled fields, F9 restart (7.2-7.5).
- **Dialog behaviour differed between browser and `AppTest`** (QA): the dialog is built
  from its payload only (5.4).
- **Three functions could not name the deleted list** (Data): the text is
  `That list no longer exists.` everywhere (4, 6).

Fixed (minor): numeric table frame with display formatting; `No price for:` only with a
target and the `Watchlist prices` title (D28); "Buy target reached" wording; tile captions
and row order; "Now:" and "Save starts" lines; schedule line in 3.2; toasts; next step in
the all-failed line; currency in Edit labels; "Shares held"; the sort fact corrected;
stale dialog flag cleared; spinner only on the first fetch; spike list extended; pre-sync
backup copy (D29); `init_db` creates the directory and runs before early returns; read-only
URI for reads; error mapping by `sqlite_errorname`; narrow `OperationalError` mapping;
`float()` cast; stronger CHECKs; tolerant `read_refresh`; `match_key` UNIQUE column; shared
backup helper with unique temp name and lock; lock test with IMMEDIATE and EXCLUSIVE; tab
lookup by label; time-zone seam; autouse cache clear and `.env` isolation; the four
`fetch_ohlc` test edits; ordered `fetch_quotes`; enumerated manual states.

Not changed, with reason: none.

### Round 3 — 2026-10-12 — UX approve, Streamlit reject (1 major), QA reject (1), Data reject (2)

All four confirmed the round-2 fixes are in the body. User answer recorded as the new D30;
designer decision D31; `backup.run` added to D20.

Fixed (major):
- **Edit, Rename and Update schedule would open blank after an earlier dialog close, so
  Save could clear targets** (Streamlit, QA; same finding): the open callback assigns each
  field's start value to its key through the pure `dialog_state`; the body passes no
  `value=`; close no longer touches the keys (5.1, 5.4, 3.5, 5.5, 7.2, 7.4, 7.5).
- **No pre-sync copy when a watchlist edit came before the IBKR write** (Data): the
  pre-sync step in `run` runs before the main-file check; test for that order (4, 7.3).
- **Read-only DB path unsafe for paths with `?`, `#`, `%`** (Data): `as_uri()` for reads
  and for `backup._verify`; tests (4, 6, 7.2, 7.3).

Fixed (minor): IBKR retry rule simplified by the user's answer (no `wl_timed_run` flag);
Add-field currency help; exact tile rows; combined move-and-targets toast; F6 click count;
sell-target line text; order of messages above the table; date on "fetched"; "Click a row"
caption; chart kept when only the quote fails; "Its target was not checked"; placeholder;
ticker rule in the message; delay wording; "minute(s)", "no end time", "works only while
this page is open"; D21 "first row" defined; selectbox options as ids with `format_func`;
Styler and `column_config` rule; `_watchlist_state()` after the sidebar; stale
`_PERF_DAY_KEY`; toast shown in the full run; stale-flag rule made concrete; error mapping
per function and by prefix; `match_key` CHECK; read timeout; hot-journal order; missing
tables against errors; orphan and crashed temporary backup files; one local date for both
backup files; `isoformat()` binding; placeholder credentials are `""`; the four
`fetch_ohlc` edits are required; module-form clock calls only; counting failing pull in
the D30 test; `threading.Event`; tests for the requests line, D16, `init_db` failure,
orphan item, toasts, hidden toggles, fresh install, README; Net Worth tab still calls
`render_net_worth_or_hint`; no `st.error` from the watchlist.

Not changed, with reason:
- The sidebar "Database: …" text stays as it is (Data minor): it is rendered before
  `init_db`; on a fresh install it says "empty" for one run. Cosmetic, and changing the
  order would move the sidebar's code for no user benefit.
- Consequences noted by UX and accepted: with the row click as the only control (D27)
  stepping through N stocks takes N row clicks; a page reload is a new session and opens
  on the alert's list again (D14).

### Round 4 — 2026-10-12 — UX approve, Data approve, QA approve, Streamlit reject (1 major)

All four confirmed the round-3 fixes are in the body. No questions for the user.

Fixed (major):
- **A target could not be cleared** (Streamlit): a number field without `value=` reads back
  0.0 when emptied. Target fields now pass `value=None`; the assigned key still sets what
  is shown (facts, 3.5, 5.4, 7.4, 7.5 item 15).

Fixed (minor): wireframe 3.3 now selects a held stock so it agrees with 3.4; "works only
while this page is open" in the wireframe and line texts; one wording "only when I open the
app"; singular and plural alert labels; hidden "Target lines" for a stock with no target;
tile labels with `({period})` and the stated reason for the different order; one-time
target line only when the target changed; plural "No price for"; caption wording; new
wireframe 3.8 with the retry sentence for every failed IBKR state and the
"positions not shown" caption; token scrubbed before the failure is stored; the three
exact places that clear `flex_failed`; "no existing test needs editing" for D30; D30 tests
split so the saved-token step is last, plus the not-configured case; `today=` passed to
`backup.run` as well; "kept days" defined (D31); temporary-file name, cleanup inside the
lock and after the guards; separate `try` per backup step; silent failed `refresh` stated;
orphan item can be moved or removed only; `7203-T` against `7203.T`; `dialog_state` returns
every field; `_PERF_DAY_KEY` cleared at all early returns; pure `dialog_open` and
`fetched_label` with tests; `%41` path test; `threading.Event` with timeout and enough
workers; zone data note; toast shown in `main()`.

Not changed, with reason: none.

### Round 5 — 2026-10-12 — UX approve, Streamlit approve, QA approve, Data approve

All four confirmed the round-4 fixes are in the body. No major finding, no question for the
user. The loop ended here. The 30 minor findings of this round are recorded as the 26
binding notes of section 10 (duplicates merged).

### Implementation — 2026-10-12 — browser spike (5.5)

Run in Chrome against a scratch copy of the database.

- Item 1 (row click): **partly failed.** A row is selected only by its circle at the left
  edge. The user was asked (D27) and chose the circle plus ▲ ▼ buttons (D32). The highlight
  follows the stock after add, edit, an alert reordering the rows, and a user sort.
- Item 2 (timed redraw): passed. With "every 1 minute" the fetch time moved by itself; the
  tab, the scroll position and the selection stayed.
- Item 3 (dialog fields): passed. Add opens empty and is emptied by "Add and add another";
  Edit opens with the saved list and targets; an emptied target is saved as no target.
- Item 4 (no gap from the timer fragment): passed.
- Item 5 (table text): passed with one change. Streamlit draws its own text in a missing
  cell whatever the Styler gives, so the table sets `placeholder="—"` and a missing price
  shows `—`, not `no data`. `reached`, the currency and numeric sorting work as designed.

### Implementation — 2026-10-12 — what differs from the design

- Picking a stock: circle plus ▲ ▼ (D32, asked).
- A missing price cell shows `—`, not `no data` (Streamlit's placeholder; not asked, cosmetic).
- The app-level tests are in `tests/test_watchlist_app.py`, not in `tests/test_app_smoke.py`.
- `tzdata` was not added to the requirements: the zone tests use the system zone data,
  which macOS and Linux have.
- Section 7.5 (manual browser checks) was done only in part; the implementation report
  lists which checks were run.
- Later the same day, on the user's request: a click anywhere in a stock's row selects it.
  The table now also has Streamlit's `single-cell` selection; a selected cell is read as a
  click on its row and then cleared, so the row mark moves. This corrects the spike's
  finding for item 1: only the plain row mode is limited to the circle. Checked in the
  browser. The circle and ▲ ▼ still work.
- ▲ ▼ buttons next to the List dropdown step through the lists (hidden with one list).

### Change — 2026-10-13 — no stock selected by default (D33, on the user's request)

- D33: a list opens with no stock selected. A stock becomes selected by a row click, ▲ ▼,
  the ticker search, adding it, or the first alert of a new session (D14, kept). Clicking
  the selected row's circle again is expected to clear the selection.
- With no stock selected: no chart and no tiles; **Edit / move / remove** is disabled and
  plain; ▼ selects the first row. With a stock selected the button is primary.
- D21 changed: after a move or a remove no stock is selected.
- The table's mode is `single-row` instead of `single-row-required`.
- Not checked in the browser with `single-row` (tests set the table's state by hand):
  the selected row's circle sends an empty selection; a cell click on an unmarked row
  marks it; a user sort keeps the mark (D22).
- Later the same day, on the user's request: an empty list also shows **Edit / move /
  remove**, disabled and plain, next to **＋ Add stock**.

### Change — 2026-10-13 — list menu and quiet update buttons (on the user's request)

Replaces the five boxed buttons in 3.3 (the user chose it from three mock-ups):

```
List [ Quantum (1 alert)             ▾] [▲] [▼]              [ List actions ▾ ]
Prices fetched 10:02 · updates: only when I open the app   ↻ Update now  ◷ Update schedule
```

- **List actions** is a popover with **New list**, **Rename <list>**, **Delete <list>**
  (F1-F3 gain one click). Opening a dialog closes it.
- **Update now** and **Update schedule** are borderless text buttons at the right of the
  status line.
- The state with no lists (3.1) keeps its single **＋ New list** button.
- Not checked in the browser.
