# Testing

No test framework. Everything below was run by hand and can be re-run. If you
change the engine or the concurrency handling, re-run the relevant section.

---

## 1. Storage and concurrency (HTTP, no database needed)

Against a local in-memory instance (`python3 proxy_server.py`, port 8765):

```bash
# first write succeeds
curl -s -X POST localhost:8765/pf-shared -H 'Content-Type: application/json' \
  -d '{"doc":{"portfolios":[{"name":"Main","trades":[],"targets":{}}]},"version":0,"who":"Alice"}'
# → version 1

# a second writer still holding version 0 must be REJECTED
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8765/pf-shared \
  -H 'Content-Type: application/json' \
  -d '{"doc":{"portfolios":[{"name":"Bobs","trades":[],"targets":{}}]},"version":0,"who":"Bob"}'
# → 409, body contains Alice's document

# retrying at the correct version succeeds
# → version 2
```

**Expected:** 409 on the stale write, and the response body carries the winning
document so the client can display it.

## 2. Passcode enforcement (real HTTP)

Against an instance started with `PF_PASSCODE=testpass123`:

| Request | Expected |
|---|---|
| `GET /pf-shared` no header | 401 |
| `GET /pf-shared` wrong passcode | 401 |
| `GET /pf-shared` correct passcode | 200 |
| `POST /pf-shared` no header | 401 (writes blocked too, not just reads) |
| `GET` with `Origin: https://evil.example` | **no** `Access-Control-*` headers |

That last row matters — every other endpoint in the file sets
`Access-Control-Allow-Origin: *`. These deliberately don't.

## 3. Fail-closed configuration

```bash
DATABASE_URL='postgres://fake' python3 -c "
import shared_store as s
print(s.configured())         # → (False, 'Server misconfigured: PF_PASSCODE...')
print(s.check_passcode('x'))  # → False
"
```

**Expected:** with a database configured but no passcode, the store refuses
everything rather than serving holdings unprotected.

## 4. Driver behaviour and database-down handling

```bash
DATABASE_URL='postgresql://u:p@127.0.0.1:59999/nope?sslmode=require' \
  uv run --with 'psycopg[binary]>=3.1' python -c "
import shared_store as s
try: s.load()
except s.StoreError as e: print('clean StoreError:', e)
"
```

**Expected:** a clean `StoreError`, not a raw traceback — an unreachable database
should surface as a readable message, not a 500 with a stack trace.

## 5. XSS (multi-user content)

Seed a portfolio whose **name** is `<img src=x onerror="window.__XSS=1">pwn` and
whose **editor name** is `<script>window.__XSS2=1</script>`, then load the page and
check:

```js
window.__XSS === 1        // → false
window.__XSS2 === 1       // → false
document.querySelectorAll('#pf-pick img, #saved-note script').length  // → 0
document.querySelector('#pf-pick option').textContent
// → the payload rendered as literal text
```

**Expected:** payloads render as inert text. Re-run if you add any column that
interpolates user-supplied values.

## 6. Replay engine arithmetic

In the page console, with a stubbed price (`prices['TEST'] = {cur:300, prev:300}`):

| Trades | Expected |
|---|---|
| buy 10 @ $100, buy 10 @ $200 | 0.15 shares, avg cost **$133.3333**, cash 80 |
| + sell 50% of position @ $300 | 0.075 shares left, avg **still $133.3333**, cash 102.5, realized **12.5** |
| + close out @ $300 | 0 positions, cash **125**, realized **25**, total return **+25%** |
| buy 10 @ $100, then sell 90% of book @ $100 | capped to holdings, cash back to 100, warning `more than held — capped` |

Average cost must **not** move on a sell. If it does, the cost-basis maths is wrong.

## 7. Drift arithmetic

Cross-checked against an independent calculation rather than trusting the UI:

```
AAPL: 15 units @ $180, now $326.59  → 15 × (326.59/180) = 27.2158
MSFT: 10 units @ $400, now $402.29  → 10 × (402.29/400) = 10.0572
cash 75                              → total 112.273

AAPL current %  = 27.2158 / 112.273 = 24.24%   drift +9.24
MSFT current %  = 10.0572 / 112.273 =  8.96%   drift −1.04
portfolio return = 112.273/100 − 1  = +12.27%
```

The rendered table matched to the decimal.

## 8. Legacy migration

Seed an old-shape document (`holdings`, no `trades`) and load the page:

```json
{"portfolios":[{"name":"Main","holdings":[
  {"sym":"AAPL","target":15,"buy":180},
  {"sym":"MSFT","target":10,"buy":400,"date":"2025-03-14"}]}]}
```

**Expected:** two opening buy trades with matching amounts and prices; MSFT keeps
its date, AAPL falls back to `2000-01-01`; `targets` populated as
`{AAPL:15, MSFT:10}`; the `holdings` key removed; positions and cash (75)
reconstructed correctly.

## 9. Conflict handling in the client — **the important one**

This is where a live bug was found. Re-run it after touching `save()` or
`mutate()`.

1. Load the page, note its version (say 3).
2. From another client, save a change → server goes to version 4.
3. In the page, make an edit and let it save.

**Expected:**

```
afterVersion:        4              (adopted the server's)
afterPortfolioName:  the other person's
myStaleTradeGone:    true           (our edit is dropped, not silently applied)
bossTradePresent:    true           (their work survives)
banner:              "Someone else saved changes first..."
```

4. Then make another edit and save.

**Expected:** version 5, containing **both** the other person's trade and yours —
proving the retry builds on their version rather than clobbering it.

**Failure signature of the old bug:** after step 3 the page shows *its own* stale
document while holding version 4, and step 4 succeeds in wiping the other person's
trade.

## 10. Seeding an existing portfolio (`solveOpening`)

The round-trip that matters: solved entry weights, replayed through the engine, must
reproduce the percentages that were asked for. The solver and the replay engine are
independent code paths, so agreement between them is a real check.

Create via "+ From what I own" with:

```
AAPL, 20, 145.30
MSFT, 15, 380.00
VOO, 35
NVDA, 10, 95.20
```

**Expected:** current percentages of exactly 20 / 15 / 35 / 10 and cash 20%, with
entry weights visibly *different* from them (about 10.78 / 17.20 / 42.25 / 5.63 at
mid-2026 prices). VOO, given no cost, shows a 0.00% return. All four trades carry
`opening: true` and render as **OPEN**.

Then trade on top of it:

| Action | Expected |
|---|---|
| Buy 5% more AAPL at today's price | AAPL 20% → 24.14%; MSFT and VOO **unchanged** (a cash↔position swap doesn't move the book's total); avg cost rises |
| Sell half the NVDA | NVDA 10% → exactly 5%; realized P&L booked; cash up by the proceeds |

Rejections — none of these may create a portfolio:

| Input | Expected message |
|---|---|
| Weights summing over 100 | "Those add up to 115.00% — more than the whole portfolio." |
| No name | "Give the portfolio a name." |
| An unparseable line | "Couldn't read 1 line(s): …" |
| A ticker with no price data | "No price data for: … Check the ticker, or remove that line." |

Confirm `doc.portfolios.length` is unchanged after each failure.

## 11. Performance chart (candles)

**Bucketing unit check** — synthetic data, hand-verifiable, no network:

```js
const ts = ['2026-07-20T13:30','2026-07-20T13:45','2026-07-20T14:00','2026-07-20T14:15',
            '2026-07-20T14:30','2026-07-20T14:45','2026-07-21T13:30','2026-07-21T13:45'];
bucketCandles(ts, [0,2,-1,3, 1,4, 5,2], '1H')
// → three candles: {o0 h3 l-1 c3}, {o1 h4 l1 c4}, {o5 h5 l2 c2}
//   (chunks of four 15m samples, new chunk at the day boundary)
bucketCandles(ts, [0,2,-1,3, 1,4, 5,2], '1D')
// → {o0 h4 l-1 c4}, {o5 h5 l2 c2}
bucketCandles(['2026-07-13','2026-07-14','2026-07-17','2026-07-20','2026-07-21'],
              [1,3,0,5,4], '1W')
// → {o1 h3 l0 c0} (ISO week of 7/13), {o5 h5 l4 c4}
```

**Availability clamping** — the candle chips must track the window:

| Window | Enabled sizes | Auto-clamp |
|---|---|---|
| 1D | 1H only | switching to 1D window forces 1H candles |
| 1W / 1M | 1H, 1D, 1W | — |
| 3M / 6M | 1D, 1W | jumping from 1M+1H to 3M clamps to 1D |
| 1Y / YTD | 1D, 1W | — |
| 5Y | 1W only | switching to 5Y forces 1W candles |

Counts observed 2026-07-21: 1Y+1D → 252 candles; 1M+1H → 145; 5Y+1W → 262;
3M+1D → 63 (Apr 21→Jul 21), 3M+1W → 14; 6M+1D → 125 (Jan 21→Jul 21),
6M+1W → 27.

**Axes**: both must be labelled. Side ticks land on round numbers and rescale
per window (verified 0.2% steps on 1D, 25% steps on 5Y); bottom ticks must not
overlap — measure with `getBBox()` on the x-label texts and confirm no pair
overlaps horizontally. The rotated `RETURN %` title needs
`getBoundingClientRect()` to check, not `getBBox()` (which ignores the
transform and falsely reports it clipped).

**Numeric integrity** (console, on the 1D window with 1H candles): the last
candle's close must equal an independent book calc from the raw 15m bars, and the
max candle high must equal the max sample — a candle must never show a value no
sample had. Verified exact to 8 decimals:

```js
// expected: (book at last 15m sample / book at window's first sample − 1) × 100
// then compare with candles[candles.length-1].c, and
// Math.max(...vals) === Math.max(...candles.map(c => c.h))
```

**Backend intervals** (`curl`): `interval=15m` → ~1550 bars ≈ 60 days;
`interval=60m` → 2000 bars ≈ 14 months; no interval → daily + divs, unchanged;
`interval=5m` → 400. Warm repeat of the same URL returns in single-digit ms
(server cache); with `Accept-Encoding: gzip` the hourly payload is ~4× smaller
and still parses (`--compressed`).

**Smoothness**: a positions-table sort must NOT rebuild the chart (mark the SVG
node, sort, confirm the same node), and toggling to an already-cached
period/candle combination must not blank the chart to a spinner.

**Prefetch before reveal** — the app must not appear until everything is warm:

```js
// after unlock resolves, coverage must already be complete
const all = new Set(); doc.portfolios.forEach(p => p.trades.forEach(t => all.add(t.sym)));
['1d','60m','15m'].map(a => [...all].filter(s => getBars(a, s)).length === all.size)
// → [true, true, true], and getBars(a, spxSym) truthy for all three
```

Then **every** period button, candle size, portfolio switch and comparison
toggle must run with **zero** new `/etf-chart` requests. Measured 2026-07-21:
0–4ms each, 0 requests.

To see the progress bar (a warm server cache finishes in ~25ms, too fast to
render), stub latency first:

```js
const rf = window.fetch;
window.fetch = async (...a) => { if (String(a[0]).includes('/etf-chart'))
  await new Promise(r => setTimeout(r, 300)); return rf(...a); };
prices={}; series={}; intraday={'15m':{},'60m':{}}; spxSym=null;
const p = pass; lock(); document.getElementById('pass').value = p; await unlock();
window.fetch = rf;
// → bar animates 0→40%→80%→100% with "Loading market data… n of N", then hides
```

**Background prefetch** (the same routine after Refresh/save):

```js
// coverage: every symbol in every portfolio at every interval, plus benchmark
[...allSyms].filter(s => getBars('15m', s)).length   // === allSyms.size, same for 60m/1d
// dedupe: no /etf-chart URL fetched twice
const urls = performance.getEntriesByType('resource').map(e => e.name)
  .filter(u => u.includes('/etf-chart'));
urls.length === new Set(urls).size                    // → true
```

Then every interaction — candle sizes, periods, portfolio switch, comparison
toggle — must complete with **zero new network requests**. Measured 2026-07-21:
1–2ms each, 18 total requests to fully warm 5 symbols + ^GSPC, 0 duplicates.

**Auto-refresh on tab return** (console; note the automation pane reports
`visibilityState:"hidden"`, so the real getter must be overridden to simulate a
visible tab):

```js
Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
lastSync = Date.now() - 100000;                     // stale
document.dispatchEvent(new Event('visibilitychange'));
// → /pf-shared refetched, lastSync updated, NO "Up to date" toast
delete document.hidden;
```

A dispatch with `lastSync` fresh (<90s) must NOT refetch. A hidden document must
never auto-refresh (the guard's whole point).

Also confirm the log/positions split: a portfolio seeded via "+ From what I own"
with extra trades on top shows only the real BUY/SELL rows in the trade log, the
Trades stat counts only those, and every Positions row has an `×` (`delHolding`)
that removes the whole holding — seed and trades — after a confirm.

## 12. Production smoke test

After any deploy:

```bash
B=https://market-dashboard-b592.onrender.com
curl -s -o /dev/null -w "%{http_code}\n" $B/tracker.html   # 200
curl -s $B/pf-shared                                        # 401 (NOT 503)
curl -s -o /dev/null -w "%{http_code}\n" $B/                # 200, dashboard intact
curl -s -o /dev/null -w "%{http_code}\n" "$B/etf-chart?sym=SPY"  # 200, prices intact
```

**401 not 503** is the signal that both env vars are set. 503 means `PF_PASSCODE`
is missing. Allow ~30s for a cold start on the free tier.

A full production check of the database path additionally requires the passcode:
`GET /pf-shared` with the correct `X-PF-Pass` returning `"persistent": true`
confirms `DATABASE_URL` is live and the SQL runs. Without the passcode you can
verify the door is locked but not what's behind it.
