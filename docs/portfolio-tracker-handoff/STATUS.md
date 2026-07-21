# Status

**As of 2026-07-21.** Check the git log before trusting this file.

## Shipped 2026-07-21 — layout, columns, axes, 3M/6M (latest)

Four owner-requested changes, each verified and deployed:

- **`968270f` layout** — stat tiles and the chart moved *below* Log a trade,
  Positions and Trade log. Pure markup move; verified rendered order, no
  duplicate ids, controls unaffected.
- **`62145aa` Unrealized column removed** from Positions. It was a position's
  contribution to the whole book, next to Return which is per-position — two
  percentages of different denominators read as redundant. (Its column summed
  exactly to the Total Return tile, verified before removal, so nothing is
  lost.) Dead `unreal` computation dropped from `replay()` too; `u()` stays for
  the Cash tile and the log's Value column. Positions is now Ticker / Current %
  / Avg cost / Price / Day / Return, all sortable.
- **`d48141b` chart axes** — gridlines, round-number tick labels on both axes
  (`niceStep`), and axis titles. See `ARCHITECTURE.md` § Performance chart.
- **`5acf81f` 3M and 6M periods** — daily and weekly candles only; hourly is
  disabled on them because Yahoo keeps ~60 days of 15m bars and a "3M" hourly
  chart would show 60 days under a 90-day label. Verified 3M = Apr 21→Jul 21
  (63 daily / 14 weekly), 6M = Jan 21→Jul 21 (125 / 27), zero network requests
  (prefetch covers them).

## Shipped 2026-07-21 — prefetch before reveal (see git log for hash)

The background warmer let the app appear before it finished, so an early click
could still catch cold data. `unlock()` now **awaits** the full warm behind the
gate (progress bar + "n of N" count), 6 workers, and reveals the app only when
every symbol × interval is cached. Owner explicitly accepted the longer initial
load twice.

Measured: full coverage at reveal (15/15 fetches, all three intervals + `^GSPC`);
after reveal every period button, candle size, portfolio switch and comparison
toggle runs in **0–4ms with zero network requests**. Warm server cache → ~25ms
unlock; simulated 300ms/request → ~935ms with the bar animating 0→40→80→100%
then hiding.

## Shipped 2026-07-21 — auto-refresh on tab return (see git log for hash)

A fresh page load always fetches live data, but a browser-restored or long-idle
tab wakes with yesterday's session — the one case that required pressing Refresh
by hand. Now `visibilitychange`/`pageshow(persisted)` trigger a **quiet**
`refreshAll(auto)` when the tab becomes visible and the last sync is >90s old
(matching the server cache's freshness window). Quick tab flips don't refetch;
the auto path skips the "Up to date" toast. Verified end to end by simulating a
visible tab return with an aged sync stamp: doc refetched, no toast, tables
intact; fresh-sync flips verified not to refetch. (Note for testing: pages in
the automation pane report `visibilityState: "hidden"`, so the event path needs
the `document.hidden` override trick in `TESTING.md`.)

## Shipped 2026-07-21 — price-dropout fix (see git log for hash)

Owner reported "sometimes no price for certain stocks, works on the next load" —
transient Yahoo throttling of Render's shared IP, made likelier by the prefetch
burst. Three layers, all verified under a simulated total Yahoo outage:
server retries across both Yahoo hosts with backoff + a 4-way cap on concurrent
Yahoo calls; expired cache entries served (`X-Stale: 1`) instead of erroring, so
any symbol seen once survives an outage; one client-side retry after 800ms.
See `ARCHITECTURE.md` § Prices.

## Shipped 2026-07-21 — background prefetch (see git log for hash)

Owner asked for everything to be instant after the initial load, accepting a
busier load in trade. `prefetchAll` now warms every portfolio's symbols at all
three candle intervals plus the benchmark in the background (3 workers), kicked
after unlock, Refresh, and every successful save. An in-flight promise map
(`_once`) guarantees the warmer and the visible chart never duplicate a request.

Measured: full warm-up = exactly one request per sym×interval (18 for 5 symbols +
^GSPC, 0 duplicates); after warm-up every interaction — candle size, period,
portfolio switch, comparison toggle — runs in 1–2ms with **zero network
requests**. See `ARCHITECTURE.md` § Prices and `TESTING.md` §11.

## Shipped 2026-07-21 — candles + speed (see git log for hash)

**Candlestick chart** (owner's request). The active book draws as candles —
open/high/low/close of the book's % return within each bucket — with selectable
candle sizes **1H / 1D / 1W**. 1H candles come from 15-minute samples, 1D from
hourly, 1W from daily; sizes the window can't honestly support are disabled with
the reason on hover (Yahoo keeps ~60d of 15m and ~400d of hourly history).
Comparison portfolios and the S&P stay lines through bucket closes. Backend:
`/etf-chart` gained an `interval=15m|60m|1d` parameter. See `ARCHITECTURE.md`
§ Performance chart and `DECISIONS.md` §23.

**Speed pass**, same push (all measured, `TESTING.md` §11):
- Server-side 120s cache on `/etf-chart`: ~350ms cold → ~4ms warm; second viewer
  and every toggle near-instant. gzip ~4× on payloads; `max-age=120`.
- Chart symbol batch + benchmark fetch in parallel; cached toggles 3–34ms.
- No more blank-to-spinner: the previous chart stays up, dimmed, while new data
  loads. Table sorts no longer rebuild the chart.
- Fixed en route: axis labels dropped the year (a 1Y span read "Jul 21 → Jul 21");
  day/week candle labels now show dates, hour candles show US-Eastern times.

Verified: bucketing unit-checked against hand-computed OHLC in all three modes;
last-candle close matches an independent book calc to 8 decimals; max candle high
equals max sample (nothing invented); availability clamping both directions;
overlays; cache/gzip benchmarks; gzipped JSON parses.

## Shipped 2026-07-21 — commit `19ca69f` (live)

- **Target % and Drift removed** from the positions table, with `setTarget` and the
  inline input. Reverses the earlier keep-targets choice — the owner changed their
  mind; both choices are recorded in `DECISIONS.md` §10. The `targets` map in stored
  documents remains as unread legacy data, so old books load unchanged.
- **Every remaining column is sortable** — click a header (arrow marks the active
  one), click again to flip. Text A→Z first, numbers largest-first, null values sink
  to the bottom either direction, CASH always pinned last. Default sort (largest
  current weight) moved from `replay()` into `render()`, leaving the engine
  presentation-free.

Verified: headers show no Target/Drift; Ticker and Return sorted both directions
correctly against the rendered cell values; CASH last in every ordering; no console
errors.

## Shipped 2026-07-21 — commit `41e8dd8` (live)

1. **Performance chart** — inline SVG, period buttons `1D/1W/1M/YTD/1Y/5Y`, a
   current-composition backtest of each book's % return, with a dashed **S&P 500**
   (`^GSPC`→`SPY`) benchmark and legend toggles to **compare portfolios**. See
   `ARCHITECTURE.md` § Performance chart for the (load-bearing) methodology.
2. **Owned holdings no longer show in the Trade log.** `opening: true` trades are
   filtered out of the log (which is now just logged buys/sells); they appear only in
   Positions. Each Positions row gained an `×` (`delHolding`) to remove a holding,
   since they're no longer deletable from the log. "Trades" stat counts non-opening
   trades. (This reverses the earlier "keep OPEN rows" choice — the owner changed
   their mind.)

Verified before and after deploy: chart draws the right number of lines; all six
period windows slice to the correct dates; S&P and compare toggles add/remove
lines; the chart's 1D endpoint matches an independent close-to-close calculation
to 6 decimals (`TESTING.md` §11); trade log hides opening seeds; `delHolding`
removes a position.

## Shipped 2026-07-21 — commit `ee64001` (live)

Committed (`tracker.html` only — `index.html`'s "All portfolios" work stayed
uncommitted on purpose) and pushed to `main`; Render auto-deployed and the live page
was confirmed serving it (branded light-theme gate). The three changes below:

`tracker.html` gained an addition to **"+ From what I own"**: an
**"Add to"** selector so already-owned holdings can be dropped into an *existing*
portfolio, not only a brand-new one. The entry-weight solver was generalised from a
hard-coded 100-unit base to the target book's current total (see `ARCHITECTURE.md`
§ solveOpening and `DECISIONS.md` §22). Backward compatible — the new-portfolio path
is unchanged.

Tested live against a stubbed backend: fresh-create still gives exact percentages;
adding NVDA at 10% into an existing AAPL/GOOG book lands NVDA at exactly 10.000% with
the rest rebalancing; adding an already-held ticker is blocked. Only `tracker.html`
changed (no backend change; the store already holds arbitrary JSON).

Also **rebranded to Christopher Edwards Financial Associates** in the same working
copy: a **light theme** — white background, navy (`#1c3765`) text and CTAs, brand
light-blue (`#6ca8c6`) accent — with the official CEFA wordmark on both the passcode
gate and the app header. The inlined logo is the **colored** variant
(`cefa-logo-cmk.svg`, navy + blue) so it reads on white; a base64 data URI in the
`--logo` CSS var. Green/red were darkened for contrast on white, and the spinner uses
`currentColor`. Page title and `theme-color` updated too. Verified live at each step —
gate, header, stat tiles, positions and log all render correctly, no console errors.
Purely presentational; no logic touched. File grew ~40KB → ~88KB from the inlined logo.

(An earlier navy-background version was built first, then flipped to this white-
background light theme at the owner's request. The light theme is the current state.)

And **paste-import hardening** so a real holdings list uploads cleanly (same commit):
a `money()` helper strips `$`/commas from number fields in
both importers, and "+ From what I own" no longer aborts the whole upload when a few
tickers lack a live quote — lines with a cost are added flat at cost and the symbols
warned, only price-and-cost-less lines are rejected. Verified with the owner's actual
50-line list (with `$` signs and two unpriceable mutual funds): all 50 imported, each
priced holding at exactly its stated %, cash the 10.79% remainder.

## Live right now

Deployed at commit `ee64001` on `main` — the **Christopher Edwards branded** version
(light theme, add-into-existing seeding, `$`-tolerant / resilient paste import).
Confirmed live on Render.

- `tracker.html` — trades are the source of truth; positions are derived
- `shared_store.py`, `/pf-shared` routes, `psycopg[binary]` dependency
- `CLAUDE.md` in the repo root, pointing future sessions at this folder

Confirmed working in production against the real Neon database: schema creation,
read, first insert, update, 409 conflict rejection, passcode enforcement, and the
absence of CORS headers on the API routes.

### Deployment history

| Commit | What |
|---|---|
| `5a67fc0` | First release — holdings-model (typed-in ticker + target % + buy price) |
| `dc31c59` | Trade log; positions derived; concurrency bug fix |
| `9a6f1cd` | "+ From what I own" — seed a portfolio you already hold, entry weights solved backwards from current percentages |
| `ee64001` | Seed into an *existing* portfolio (solver base = book's current total); Christopher Edwards Financial branding (light theme + logo); `$`-tolerant, missing-price-resilient paste import |
| `41e8dd8` | Performance chart (period buttons, S&P benchmark, portfolio comparison); opening seeds hidden from the trade log, holdings removed via Positions `×` |

## What the trade-log version added

A significant rewrite of the front end. The backend needed no changes, because the
store holds arbitrary JSON.

- A "Log a trade" form — buy/sell, ticker, size, price, timestamp
- Sells in three modes: % of book, % of position, close out
- Positions **derived** from the log instead of typed in
- Weighted-average cost basis across multiple buys of the same ticker
- Realized P&L per sell, and in the stat tiles
- A trade log table, newest first, with per-row warnings and CSV export
- Target % kept alongside, editable inline, with drift
- `migrate()` — converts legacy `holdings` documents into opening buy trades

It also **fixed a data-loss bug that had shipped in `5a67fc0`**: on a 409 conflict
the client adopted the server's document and then rolled back to its own stale copy
while keeping the new version number — so the next save would have silently
overwritten the other person. `save()` now returns `'ok' | 'conflict' | 'error'` and
`mutate()` rolls back only on `'error'`. See `ARCHITECTURE.md` § Concurrency.

**Migration on first load.** Any typed-in holdings from the previous version are
converted to opening buy trades automatically. Nothing is lost, but holdings that
had no date appear in the log as `Jan 1, 2000` — there was no real date to use.
Delete and re-log those if the dates matter.

## Open items

| Item | Owner | Notes |
|---|---|---|
| **Rotate `PF_PASSCODE`** | Patrick | The passcode was pasted into a chat transcript. It is also a bare personal name — weak for a page on a public URL. **Still unconfirmed** |
| Confirm `DATABASE_URL` was rotated | Patrick | The first Neon connection string was pasted into a chat too. A second Neon project was created; confirm the original was deleted or its password reset |
| `render.yaml` is stale | low priority | Declares a Python build; Render actually builds Docker. Misleading, harmless |
| Owner's uncommitted `index.html` work | Patrick | An "All portfolios" combined-account feature, unrelated to the tracker. Deliberately not committed |

## Known limitations

These are understood tradeoffs, not defects:

- **No user accounts.** Everyone with the passcode can read and edit everything,
  including deleting other people's portfolios. Right for a few colleagues, wrong
  for untrusted users.
- **Fixed-base percentages.** A 2% trade is the same size regardless of how much
  the book has grown. Chosen deliberately — see `DECISIONS.md` §8.
- **Average-cost basis, not tax lots.** Not suitable for tax reporting.
- **Delayed daily closes**, not real-time quotes.
- **Cold starts** on both Render and Neon free tiers.
- **Per-request database connections** — fine at this scale, first thing to change
  if it grows.
- **One shared document.** Fine at a few KB; every save rewrites the whole thing.
  Thousands of trades would want a real table per trade.

## Keeping this folder current

**It does not update itself, and it cannot.** There's no mechanism for a folder to
watch a conversation and rewrite itself. Anyone promising otherwise is describing
something that doesn't exist.

What actually keeps it accurate: `market-dashboard/CLAUDE.md` instructs any Claude
session working in that repo to update these files whenever the tracker changes.
That works as long as the work happens through a Claude session in that directory
and the instruction is honored — it is a convention, not an enforcement mechanism.

If you change the tracker by hand, update these files by hand. The most common way
this folder goes stale is someone editing `tracker.html` directly and never
touching `STATUS.md`.

**When reading this folder, trust the code over the docs.** Check `git log` in the
repo against the "as of" date at the top.
