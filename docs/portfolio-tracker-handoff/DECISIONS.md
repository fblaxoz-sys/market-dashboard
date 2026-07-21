# Decisions and their reasons

Each entry: what was chosen, what was rejected, and why. Several of these look
wrong at a glance. Read the reason before changing one.

---

## 1. Not a Claude Artifact

**Rejected.** The obvious first idea — publish a shareable artifact page — cannot
work. The available runtime capabilities were `downloads` and `mcp` only: there is
no shared-state capability, and artifacts run under a CSP that blocks all external
network requests. So an artifact could neither hold shared data nor fetch prices.

## 2. Built on the existing Render service, not something new

The owner already had a deployed Python service with a market-data proxy. That's
exactly the infrastructure a shared tracker needs: a public URL, a backend that can
hold state, and existing price endpoints. Standing up a second service would have
duplicated all of it.

## 3. A standalone page, not the existing "My Portfolios" tab

`index.html` already had a portfolio tab doing weights, drift, refresh and import —
but on `localStorage`, so every person saw their own copy. That single line was the
entire problem.

Three options were offered: convert that tab, add a second "shared" tab, or build a
standalone page. **The owner chose standalone**, to have something clean to hand to
non-technical people. The old tab was left untouched and still works, privately,
per-browser.

## 4. Postgres (Neon free tier) for storage

Rejected alternatives:

- **A JSON file on disk** — Render's free tier has an ephemeral filesystem. Data is
  wiped on every deploy and every idle spin-down. This silently loses everything.
- **Render persistent disk** — works, but requires leaving the free tier (~$7/mo).
- **GitHub Gist as a store** — free and no new infra, but needs a token with `gist`
  scope and is rate-limited and awkward.

Neon's free tier persists properly, costs nothing at this data size (a few KB), and
needs only one env var. Chosen by the owner.

## 5. A shared passcode, not an open link

The dashboard URL is public and unauthenticated. Rejected: link-only access (anyone
who finds the URL sees real financial holdings) and public-read/passcode-write
(the holdings are the sensitive part, so read is what needs protecting).

There are deliberately **no user accounts** — everyone with the passcode has full
read/write over every portfolio. That's the right complexity for a handful of
colleagues sharing a book, and wrong if this ever grows to untrusted users.

## 6. Passcode in a header, never the URL

Query strings end up in server logs, browser history and `Referer` headers. A
`?pass=` parameter would leak the credential into all three.

## 7. Optimistic concurrency, not last-write-wins

Two people editing one document is the normal case here, not an edge case.
Last-write-wins silently destroys whoever saved first — the single worst failure
mode for a shared tracker, because nobody notices.

A version check costs one integer and turns silent data loss into a visible "someone
else saved first, here's their version." See the trap described in
`ARCHITECTURE.md` — this has already been got wrong once.

## 8. Fixed-base percentages — **owner's choice, against the recommendation**

The question: you buy 2% Apple. A year later the book is up 50%. You buy 2% more.
Is the second purchase the same size, or 50% bigger?

- **Recommended:** 2% of the book *as it stands*, so the second buy deploys more
  money. Self-correcting as the account grows. Would have required historical price
  lookups at each trade date to compute the book's value at that moment — feasible,
  since `/etf-chart` returns 5 years of daily closes.
- **Chosen by the owner:** 2% of a **fixed 100-unit base**. Every 2% is the same
  size whenever it happens.

The tradeoff, recorded so it isn't rediscovered as a bug: as the account grows, a
"2%" trade stays the same absolute size, so later trades become proportionally
smaller bets without the user noticing. The owner was told this and chose it anyway
for predictability. **This is not a bug. Don't "fix" it without asking.**

Switching later means replaying the log against historical book values — the trade
data is sufficient to do it, no information is lost by having chosen this.

## 9. Trades are the source of truth; positions are derived

Originally holdings were typed in directly as `{ticker, target%, buy price}`. That
cannot represent buying the same ticker three times at three prices.

With a trade log, positions are replayed from the log every render. Consequences:
weighted-average cost basis is correct; correcting a mistyped trade corrects the
whole portfolio; and deleting a trade is a legitimate edit rather than a
reconciliation problem.

## 10. Target % — kept at first, then removed (2026-07-21)

Originally the owner chose to keep a target % per ticker as a plan, shown against
actual with the drift. **Later the same day they reversed it**: the Target % and
Drift columns were removed from the positions table and `setTarget` deleted, in the
same change that made every remaining column sortable.

The `targets` map still exists in stored documents but is no longer read or
written — harmless legacy data. If drift ever comes back, the storage shape is
already there.

## 11. Sells support three modes

Owner's choice, all three: % of book, % of the position ("half my Apple"), and
close-it-out. Closing out exactly would otherwise require the user to do arithmetic.

## 12. Realized P&L against weighted-average cost

Not FIFO, not specific-lot. Average cost is the simplest defensible method and
matches what most brokerages show by default. Selling does not change the average
cost per share — only buys do.

This is **not** tax-lot accounting and shouldn't be presented as such.

## 13. `psycopg[binary]`, not plain `psycopg`

The Docker image is `python:3.11-slim`, which has no Postgres client libraries. The
`[binary]` extra ships prebuilt wheels with libpq bundled, so no `libpq-dev`
install step and no compile. Don't drop the `[binary]`.

## 14. A separate `shared_store.py`

`proxy_server.py` is already ~130KB. Storage went in its own module to avoid
growing it further, and because storage is the piece most likely to be swapped
(different database, connection pooling) without touching HTTP handling.

## 15. Per-request database connections

Every request opens a fresh connection. At a handful of users this is fine and it
avoids stale-connection handling entirely.

It is the first thing to change if usage grows — connection setup includes a TLS
handshake, several round trips. Same-region placement (both us-west) keeps that
cheap. If you add pooling, `psycopg_pool` is the natural choice.

## 16. Reuse `/etf-chart` rather than write new price code

The repo already had a working, rate-limited Yahoo proxy. Adding a second path to
the same data would mean two things to maintain and two ways to get throttled.

## 17. Fail closed when misconfigured

If `DATABASE_URL` is set but `PF_PASSCODE` is missing, the endpoints return 503
instead of serving. The alternative — defaulting to no passcode — would put real
holdings on a public URL because someone forgot an env var. A visible 503 is
strictly better than silent exposure.

## 18. No CORS headers on `/pf-shared`

Every other endpoint in `proxy_server.py` sets `Access-Control-Allow-Origin: *`.
These two deliberately don't. Same-origin requests don't need it, and its absence
prevents a malicious page from reading the holdings using a visitor's session.
Consistency is not a good enough reason to add it.

## 19. Escape everything rendered

Portfolio names, tickers and editor names come from other users. Every interpolated
value passes through `esc()`, tickers are additionally character-restricted, and
this was verified with hostile payloads. Any new column needs the same treatment.

## 20. Seeding an existing portfolio solves backwards, rather than logging the stated percentages

"+ From what I own" takes *current* weights, not entry weights. Logging the stated
percentages directly as buys would be wrong: a holding that has doubled since you
bought it would land at roughly twice the percentage you asked for.

So entry weights are back-solved in closed form (`ARCHITECTURE.md` § solveOpening).
Verified: asking for 20/15/35/10 with real cost bases produces exactly 20/15/35/10.

Cost basis is **optional per line**. Omitted, that holding starts flat at today's
price. This matters because people often know their allocation but not every
historical fill price, and demanding cost for every row would make the feature
unusable for them.

Stated percentages are also copied into `targets`, so the allocation you seeded from
becomes the plan you drift against. That's a guess about intent, but a defensible
one — you can clear any target by emptying its box.

These trades carry `opening: true` and render as **OPEN**, not **BUY**. They're a
reconstruction of a position, not a trade that happened on that date, and the log
shouldn't claim otherwise.

## 21. Pushed straight to `main`

Not a general policy — this repo deploys by pushing `main` and Render auto-deploys.
A feature branch wouldn't deploy, which is the point of the push.

Note the repo carries **unrelated uncommitted work** in `index.html` (an "All
portfolios" combined-account feature). It was deliberately excluded from the
tracker commits. Stage files explicitly; never `git add -A` here.

## 22. "From what I own" can add into an existing portfolio, not only a new one

Originally the feature always *created* a new portfolio. The owner wanted to drop
already-owned holdings into a portfolio that already exists too.

The seeding math (`solveOpening`) assumed a fresh 100-unit book. Rather than special-
case the two paths, the `100` was generalised to a `base` argument: for a new
portfolio `base = 100`; for an existing one `base = replay(trades).total`, that
book's current value. The identical closed form then lands the new holding at
exactly its stated percentage of the **combined** book — verified live and with a
standalone replay. Backward compatible: the fresh path is unchanged because
`base` defaults to 100.

**Rejected: honouring the stated % against just the free cash, or against the
pre-existing total.** Both let the new holding miss its target once the book is
revalued. Solving against the *resulting* total is the only choice that makes "add
NVDA at 10%" mean 10% of what you'll actually have.

**Blocked: adding a ticker the target portfolio already holds.** The existing shares
merge into the replay by symbol, so the stated percentage can't be hit exactly. The
modal refuses it and points the user at a normal Buy — better than silently landing
at the wrong weight. Percentages of the new holdings must still sum to ≤ 100%, as
before (keeps the denominator positive); the *combined* book may exceed 100%, which
simply drives cash negative with the existing warning.

## 23. Portfolio candles come from finer-grained samples, never from per-stock OHLC

The chart draws the book as candlesticks (owner's request, 2026-07-21). A candle's
high/low must come from actual samples of the book's value inside the bucket: 1H
candles from 15-minute closes, 1D from hourly, 1W from daily.

The tempting shortcut — building the book's daily candle by weighting each stock's
own daily high and low — was rejected as dishonest: different stocks peak at
different moments, so the summed extremes describe a book state that never existed
and overstate every candle's range.

Consequence: candle sizes are gated by the window (`CANDLE_OK`), because Yahoo
keeps only ~60 days of 15-minute bars and ~400 days of hourly. A 5-year window
offers weekly candles only. This is a data-availability fact, not a bug; the
disabled chips say so on hover. The active book draws as candles; comparisons and
the S&P benchmark stay lines through bucket closes — overlapping candle series are
unreadable.

## 24. Price responses are cached server-side for 120 seconds

Every tracker session requests the same symbols, and every viewer repeats them.
`/etf-chart` responses now sit in an in-process cache for 120s (measured: ~350ms
cold vs ~4ms warm), are gzipped when accepted (~4×), and carry
`Cache-Control: max-age=120`. 120s staleness is acceptable for delayed daily/hourly
closes on a page with a Refresh button; anything longer would make "Refresh" a lie.
The cache is per-process and bounded (300 entries) — a free-tier instance restart
simply starts cold.
