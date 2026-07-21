# Architecture

## Shape of the thing

```
Browser (tracker.html)
   │  passcode in X-PF-Pass header
   │  GET  /pf-shared          → read the shared document
   │  POST /pf-shared          → write it, with a version check
   │  GET  /etf-chart?sym=XXX  → daily OHLC prices (pre-existing endpoint)
   ▼
proxy_server.py  (stdlib ThreadingHTTPServer, no framework)
   │
   ▼
shared_store.py  →  Neon Postgres, one row, one JSON document
```

No build step, no framework, no bundler. `tracker.html` is a single self-contained
file — inline CSS, inline JS, no external requests. Editing it and pushing is the
whole deployment story for the front end.

---

## The shared document

Everything lives in **one JSON document**, one row in Postgres:

```sql
CREATE TABLE shared_pf (
    id         TEXT PRIMARY KEY,       -- always 'default'
    doc        JSONB       NOT NULL,
    version    INTEGER     NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT        NOT NULL DEFAULT ''
);
```

The table creates itself on first use (`_ensure_schema`). No migrations to run.

Document shape:

```jsonc
{
  "portfolios": [
    {
      "name": "Main",
      "trades": [
        { "id":"m3k2a", "ts":"2026-03-14T09:30", "side":"buy",
          "sym":"AAPL", "mode":"pct", "amt":2, "price":182.50 }
      ],
      "targets": { "AAPL": 15 }        // legacy — no longer read or written (see DECISIONS §10)
    }
  ]
}
```

A trade:

| Field | Meaning |
|---|---|
| `id` | Unique string, generated client-side. Used for deletes and warning lookups |
| `ts` | `datetime-local` string, e.g. `2026-03-14T09:30`. Sorted as text — ISO format makes that correct |
| `side` | `buy` or `sell` |
| `sym` | Ticker, uppercased, restricted to `A-Z 0-9 . - ^` |
| `mode` | `pct` (% of book), `frac` (% of the position), `all` (close out). Buys are always `pct` |
| `amt` | The number for that mode. Ignored when `mode === 'all'` |
| `price` | Your fill price |

**Positions are never stored.** They're derived by replaying the log every render.
That's what makes correcting a mistyped trade correct the whole portfolio.

---

## The replay engine (`replay()` in tracker.html)

This is the core. Read it before touching anything numeric.

The book is **100 units, all cash at the start.** Percentages are of that fixed
base — *not* of the book's current value. See `DECISIONS.md` §8; this was a
deliberate choice by the owner and it is load-bearing.

```
cash = 100, realized = 0, pos = {}

for each trade, sorted by ts (ties broken by id):

  BUY:
    units   = amt                      # 2% buy spends exactly 2 units, always
    shares += units / price
    cost   += units
    cash   -= units

  SELL:
    mode 'all'  → shares = everything held
    mode 'frac' → shares = held * (amt / 100)
    mode 'pct'  → shares = amt / price
    shares = min(shares, held)                       # capped, warning emitted
    avg       = cost / held                          # weighted average cost
    realized += shares * (price - avg)
    cost     -= shares * avg                         # avg per-share is unchanged
    shares   -= shares
    cash     += shares * price
```

Then, valuing at the latest close:

```
value_i    = shares_i * price_i
total      = Σ value_i + cash
current%_i = value_i / total * 100
total return = total / 100 - 1
```

(Target %/drift used to be computed here and shown as a column pair; both were
removed from the UI on 2026-07-21 at the owner's request. The `targets` map still
exists in stored documents — harmless legacy data, no longer read or written.)

Worked example, verified against the implementation:

| Step | Result |
|---|---|
| Buy 10 units @ $100 | 0.1 shares, cost 10, cash 90 |
| Buy 10 units @ $200 | 0.15 shares, cost 20, **avg $133.33**, cash 80 |
| Sell 50% of position @ $300 | 0.075 shares out, **realized 12.5**, cash 102.5, avg still $133.33 |
| Close out @ $300 | cash 125, realized 25, **total return +25%** |

Note that average cost does **not** move when you sell — only when you buy. That's
standard weighted-average cost basis and it's intentional.

### Seeding a portfolio you already own (`solveOpening()`)

"**+ From what I own**" seeds holdings that already exist. You state what each
holding is worth *today* as a share of the book, plus optionally what you paid.
The modal's **"Add to"** selector chooses the destination: a brand-new portfolio,
or one you already have.

You cannot just log those percentages as buys. In the fixed-base model a position's
current weight is its entry weight grown by `current / cost` — so a holding that has
doubled needs a *smaller* entry weight to sit at the same current percentage. The
entry weights have to be solved backwards.

With `pᵢ` the target share (as a fraction), `gᵢ = current price / your cost`,
`P = Σpᵢ`, and `base` the book's current total value:

```
V  = base / (1 − P + Σ(pᵢ / gᵢ))    resulting book value, in units
wᵢ = pᵢ · V / gᵢ                    entry weight for holding i
```

Derivation: we need `wᵢgᵢ = pᵢV` for every holding, and `V = Σ(wᵢgᵢ) + cash` where
`cash = base − Σwᵢ`. Substituting gives `V = VP + base − V·Σ(pᵢ/gᵢ)`, which solves
for `V` directly. The denominator is always positive (`P ≤ 1`, all `gᵢ > 0`), so
there's no divide-by-zero case.

**`base` generalises the original hard-coded `100`.** For a fresh portfolio the book
is 100 units of cash, so `base = 100` and this is exactly the original formula. When
adding into a portfolio that already holds things, `base` is that book's *current
total* (`replay(trades).total`, computed after its holdings are priced). The new
holding then lands at exactly its stated `pᵢ` of the **combined** book; it is funded
out of cash, and — if it carries a baked-in gain (`gᵢ ≠ 1`) — the existing holdings
dilute proportionally as the total grows. Verified live: adding NVDA at 10% (cost
150, price 300) into a book of AAPL 50% / GOOG 20% / 30% cash gives NVDA exactly
10.000%, AAPL 47.5%, GOOG 19%, cash 23.5%.

Adding a ticker the target portfolio **already holds** is blocked at the modal: the
existing shares blend into the replay and the stated percentage can no longer be hit
exactly, so the user is told to log it as an ordinary Buy instead.

**Pasting a real holdings list.** The number fields are parsed through `money()`,
which strips `$` and thousands commas, so a pasted `TICKER, percent, $1,262.42` reads
correctly (both this modal and the "Paste list" importer use it). And a large paste
no longer fails wholesale when a few tickers have no live quote — mutual-fund tickers
(TRAIX, WEFIX…) or a rate-limited batch: any line that has a **cost** is added
anyway, valued flat at that cost (`g = 1`) until a quote appears, and the affected
symbols are named in a warning. Only a line with **neither** a price nor a cost is
rejected. Verified with a real 50-line list (with `$` signs and two unpriceable
funds): all 50 imported, every priced holding landed at exactly its stated %.

Omitting the cost sets `gᵢ = 1`, which collapses to `wᵢ = pᵢ` — that holding simply
starts at today's price with no gain.

Worked example (verified live): asking for AAPL 20%, MSFT 15%, VOO 35%, NVDA 10%
with costs of $145.30 / $380 / none / $95.20 produced entry weights of
10.78 / 17.20 / 42.25 / 5.63 — and current percentages of exactly 20 / 15 / 35 / 10
with 20% cash.

The resulting trades are flagged `opening: true`. They do not appear in the trade
log (it shows only logged buys/sells); the holding is visible in Positions and
removable there via its `×` (`delHolding`). Seeding no longer writes `targets` —
the target/drift feature was removed from the UI on 2026-07-21.

### Edge cases handled

- **Overselling** — capped to what's held, `⚠` shown on the log row.
- **Selling something you don't hold** — blocked at the form with a message.
- **Negative cash** — allowed (you may have logged buys exceeding 100%), but a
  warning banner appears. It is not treated as an error because a partially-logged
  history can legitimately pass through that state.
- **Bad price / bad size** — trade skipped in the replay, `⚠` on the row, rest of
  the book still computes.
- **Missing price data** — position falls back to cost basis so the book still
  totals; the ticker is named in a warning.

---

## Concurrency — the part most likely to be broken by a careless edit

Multiple people share one document. Last-write-wins would silently destroy work,
so writes are **optimistically concurrent**:

1. Client loads, receives `version: N`.
2. Client saves, sending `version: N` alongside the document.
3. Server runs `UPDATE ... WHERE id = 'default' AND version = N`.
4. Zero rows updated → someone else already saved → **409**, with the winning
   document in the response body.
5. Client adopts the server's version and tells the user.

First write is `INSERT ... ON CONFLICT (id) DO NOTHING RETURNING ...`, only when
the client also believed the document was new (`version 0`). No rows returned
means someone beat it there → also a 409.

### The trap

On a 409 the client has **already replaced its document with the server's**. It
must NOT then roll back to a local backup — doing so shows stale data while
holding the *new* version number, so the next save would sail through the version
check and silently overwrite the other person.

This bug was written and shipped once, then found and fixed. `save()` therefore
returns `'ok' | 'conflict' | 'error'` and `mutate()` rolls back **only on
`'error'`**. If you refactor these two functions, re-run the conflict test in
`TESTING.md` — a boolean return type is exactly how the bug got in.

---

## HTTP API

Both routes live in `proxy_server.py` on the `Handler` class. `do_POST` did not
exist before this project; the file was `do_GET`-only.

### `GET /pf-shared`

```jsonc
200 → { "doc": {...}, "version": 5, "updated_at": 1784640906.5,
        "updated_by": "Patrick", "persistent": true }
401 → { "error": "Wrong or missing passcode." }
503 → { "error": "Server misconfigured: PF_PASSCODE is not set..." }
```

`persistent: false` means the server is running without `DATABASE_URL` on the
in-memory fallback — the page shows an amber "not saving permanently" banner.

### `POST /pf-shared`

```jsonc
body → { "doc": {...}, "version": 5, "who": "Patrick" }

200 → same shape as GET, with the new version
409 → { ...winning document..., "error": "Someone else saved changes first..." }
400 → malformed JSON, or doc without a "portfolios" array
413 → body missing or over 2MB
429 → rate limited
```

### Deliberate properties

- **No `Access-Control-Allow-Origin`** on these two routes — unlike every other
  endpoint in the file, which sets `*`. The page is same-origin, so it doesn't
  need CORS, and omitting it stops any other website from reading the holdings out
  of a logged-in visitor's browser. **Do not "fix" this for consistency.**
- **Passcode in the `X-PF-Pass` header**, never a query parameter. URLs land in
  server logs, browser history and `Referer` headers.
- **`Cache-Control: no-store`** so holdings don't sit in an intermediary cache.
- **Payload capped at 2MB.** Real documents are a few KB.
- **Rate limited** per IP, reusing the file's existing `_rate_ok` helper.

---

## Auth

One shared passcode in the `PF_PASSCODE` env var, compared with
`hmac.compare_digest`. There are no user accounts — everyone with the passcode has
full read and write over every portfolio in the document.

**Fail-closed:** if `DATABASE_URL` is set but `PF_PASSCODE` is not, `configured()`
returns false and both routes return 503 rather than serving. A production
deployment therefore cannot come up unprotected by accident. Do not "fix" a 503 by
defaulting the passcode to something.

Local development with neither variable set runs the in-memory store and accepts
any passcode — that's the `not DATABASE_URL` branch in `check_passcode`.

---

## Prices

Reuses the repo's existing endpoint rather than adding new market-data code:

```
GET /etf-chart?sym=AAPL&interval=1d      (interval optional, default 1d)
→ { "sym":"AAPL", "interval":"1d",
    "ohlc":[["2026-07-20",322.1,326.9,321.4,326.59], ...],
    "divs":[["2026-05-09",0.25], ...] }
```

Intervals (added for the candle chart): `1d` → ~5y of daily bars keyed by date,
with dividends; `60m` → ~400d of hourly bars keyed `YYYY-MM-DDTHH:MM` **UTC**;
`15m` → ~60d. Those ranges are Yahoo's hard caps on intraday history. Anything
else is a 400. Quotes (stat tiles, day change) read the last two daily closes.

Performance (all measured):

- **Server-side cache**, `_CHART_CACHE`, TTL 120s per `(sym, interval)` — a warm
  hit serves in ~4ms vs ~350ms against Yahoo, so the second viewer's page and
  every toggle are near-instant. Entry count bounded at 300 (oldest half evicted)
  for the 512MB instance. `Cache-Control: public, max-age=120` lets the browser
  skip the request entirely on quick revisits.
- **Transient-failure armor** (Yahoo intermittently 429s/5xxes bursts from shared
  datacenter IPs like Render's — the "no price for X, fine next load" symptom):
  `_yahoo_ohlc` retries 3× across both query hosts (`query1`/`query2`) with
  backoff, concurrent Yahoo calls are capped at 4 (`_YAHOO_SEM`) so a prefetch
  burst can't trip the throttle, and if Yahoo still fails the handler serves the
  **expired cache entry** (`X-Stale: 1`, `no-store`) rather than a 500 — expired
  entries are never deleted, only evicted by the size bound, so once any viewer
  has seen a symbol an outage is invisible. The client (`fetchChartJson`) retries
  once more after 800ms. Verified with a simulated total outage: cached symbol →
  200 stale after 3 server attempts; never-seen symbol → clean 500.
- **gzip** when the client accepts it (~4× smaller: an hourly payload drops
  ~107KB → ~26KB; `fetch` decompresses transparently).
- Client fetches run through `Promise.allSettled` (one bad ticker degrades that
  row, not the page) and the chart's symbol batch and benchmark resolve in
  parallel. **Refresh** clears the client caches and reloads; the server cache
  absorbs the cost of the refetch.
- **Prefetch-before-reveal** (`prefetchAll`): `unlock()` **awaits** a full warm —
  every portfolio's symbols at all three intervals plus the benchmark, 6 workers
  — behind the gate, showing a progress bar (`#gate-progress`), and only then
  reveals the app. The deliberate trade, made twice by the owner: a longer
  initial load so that **no click ever waits on the network**. Measured after
  reveal: every period button, candle size, portfolio switch and comparison
  toggle completes in 0–4ms with **zero** new requests. Worker count is 6
  against the server's own 4-way Yahoo cap, so the client is never the
  bottleneck; with a warm server cache the whole warm-up is ~25ms, and at a
  simulated 300ms/request it's ~935ms with the bar animating 0→100%.
  `prefetchSoon()` still runs the same routine in the background after Refresh
  and after every successful save, to warm tickers an edit introduced.
  `_once()` keeps one promise per in-flight URL so the warmer and the visible
  chart never duplicate a request. Prefetch failures are silent by design — a
  symbol with no history warns when actually viewed, not from the warmer.

---

## Front-end structure

Everything is in `tracker.html`. Rough map, top to bottom:

| Section | What it does |
|---|---|
| `<style>` | **Christopher Edwards Financial brand theme** — a **light theme**: white background, navy (`#1c3765`) text and CTAs, brand light-blue accent (`#6ca8c6`), via CSS variables in `:root`. The official CEFA wordmark (the **colored** `cefa-logo-cmk.svg`, navy + blue) is inlined once as a base64 SVG in the `--logo` variable and shown on the gate and header (`.logo` / `.logo-hd` / `.logo-gate`). Note: the spinner uses `currentColor` so it's visible on both white and navy buttons. Started life as `index.html`'s dark purple theme; no longer tracks it |
| gate markup | Passcode screen |
| app markup | Header, banners, portfolio picker, trade form, positions table, trade log, then stat tiles + chart (reordered 2026-07-21 so the things you act on come first) |
| import modal | Paste-a-list bulk entry |
| state | `doc`, `version`, `active`, `prices`, `series`, `persistent`, `chart` |
| `api/adopt/load/save/mutate` | Server round-trips and optimistic-concurrency handling |
| `migrate()` | Converts legacy `holdings` documents into trades |
| `fetchPrice/loadPrices/refreshAll` | Price fetching (`fetchPrice` also caches full `[date,close]` history into `series`) |
| `replay()` | **The engine** |
| `render()` | Rebuilds stat tiles, positions table, trade log; calls `renderChart()` |
| `renderChart/svgCandleChart/bucketCandles/bookValueOnAxis/alignSeries/periodStartIdx/niceStep` | **Performance chart** (see below) |
| `logTrade/delTrade/delHolding` | Trade entry, per-trade + per-holding delete |
| `posSort/sortPos/posCmp` | Positions-table sorting (click a header; CASH stays pinned last) |
| `newPf/renamePf/delPf/pickPf` | Portfolio management |
| `doImport/exportTrades` | Bulk paste-in, CSV export |
| `unlock/lock` | Passcode gate |

### Trade log vs. Positions (changed 2026-07-21)

`opening: true` trades are **hidden from the Trade log** — the log is buys and sells
the user logs, not seeded/owned holdings. Owned holdings appear only in **Positions**,
and each Positions row has an `×` (`delHolding(sym)`) that deletes every entry for that
ticker, since they're no longer removable from the log. The "Trades" stat counts
non-opening trades only.

### Performance chart — candles

Pure inline SVG (`svgCandleChart`) — no library, no external request. The **active
book draws as candlesticks**: each candle is the open/high/low/close of the book's
% return within that bucket. Two load-bearing choices:

1. **Current-composition backtest** — the book's *current* shares (from `replay`)
   re-priced at every past sample (`bookValueOnAxis` × `alignSeries`); cash flat.
   Opening/owned positions carry a synthetic "now" timestamp, so a replay by trade
   date would show the book empty until the seed date. This is the only honest view
   and the UI says so.
2. **A candle's range must come from samples finer than the candle.** 1H candles
   are built from 15-minute closes (4/candle, chunked per trading day), 1D from
   hourly (grouped by date), 1W from daily (grouped by ISO week — `isoWeekKey`,
   `bucketCandles`). Building a book's daily candle from each stock's own daily
   high/low would overstate the range — different stocks peak at different moments
   — so we never do. This is why candle sizes enable/disable per window
   (`CANDLE_OK`): Yahoo keeps only ~60 days of 15m bars and ~400 days of hourly,
   so 5Y offers weekly only, and **3M/6M offer daily and weekly but not hourly**
   — otherwise a "3M" window would quietly show 60 days of data. `renderChart`
   clamps an invalid combination to the first valid size.

Periods: `1D / 1W / 1M / 3M / 6M / YTD / 1Y / 5Y` (`CHART_PERIODS`,
`periodStartIdx`).

Values are normalised to 0% at the window start (`normVals`). Comparison
portfolios and the `^GSPC`→`SPY` benchmark (`ensureSpx`) draw as **lines through
each bucket's close** at candle centers — multiple candle series on one chart is
unreadable. Each candle carries an SVG `<title>` tooltip with its OHLC. Timestamps
are stored UTC; hour candles label in US-Eastern (`fmtBucket`), day/week candles
label as dates with the year. Holdings with no history at the chosen granularity
are dropped from the drawing and named in a warning banner. `chartSeq` guards
against a slow async redraw landing after a newer one; while a fetch is in flight
the previous chart stays up, dimmed, instead of blanking to a spinner.

**Axes** (`svgCandleChart`): both are labelled. The side carries % return ticks
at round numbers chosen by `niceStep()` (1/2/2.5/5/10 × a power of ten) with
faint gridlines and an emphasised zero line — it scales from 0.2% steps on a
quiet single day to 25% steps across five years. The bottom carries time ticks
with gridlines, the count derived from plot width (~108px/label, 2–6 ticks) so
labels never collide; end labels anchor inward. Axis titles are a rotated
`RETURN %` and a bottom title naming the current unit (`xAxisTitle()` — "TIME
(ET) · HOURLY CANDLES" / "DATE · DAILY CANDLES" / "WEEK · WEEKLY CANDLES").

Note when testing the rotated title: `getBBox()` ignores transforms and will
report it as clipped. Use `getBoundingClientRect()`.

Data: `/etf-chart?sym=X&interval=15m|60m|1d` (see § Prices). Client caches:
`series` (daily closes, shared with quotes) and `intraday['15m'|'60m']`. A sort
click re-renders tables only (`render({skipChart:true})`).

### Security note on rendering

Holdings and portfolio names are written by **other users** and rendered with
`innerHTML`. Every interpolated value goes through `esc()`. Ticker input is
additionally restricted to `[A-Z0-9.\-^]`. This was tested with hostile payloads
in portfolio names and editor names — see `TESTING.md`. If you add a column,
escape it.

### Local storage

Only two keys, both conveniences, neither authoritative:

- `pf_pass` — remembered passcode so you don't retype it
- `pf_who` — your display name, shown as "last saved by"

All real state is server-side. This is the whole point of the project: the
predecessor kept portfolios in `localStorage`, which is why everyone saw a
different book.
