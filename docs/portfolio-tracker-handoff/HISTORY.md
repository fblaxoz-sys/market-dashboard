# How this project got here

Narrative order, 2026-07-21. Useful mainly for understanding why things are shaped
the way they are, and what was already tried.

## The original ask

> "A very simple and easy to upload portfolio tracker that is shareable between
> people and updates with a refresh button… input stocks and etfs based on the
> percentage of the port as well as the price… keep track of the different
> percentages as prices change… once one person uploads stuff everyone else also
> sees what they did, not on individual files."

## What was found before building anything

**A Claude Artifact can't do it.** Available runtime capabilities were `downloads`
and `mcp` only — no shared state, and a CSP that blocks all external network
requests. Ruled out immediately.

**About 90% of it already existed.** The owner's live dashboard at
`market-dashboard-b592.onrender.com` had a "My Portfolios" tab already doing:
weights or shares with buy prices, a refresh-prices button, a "Current %" column
that recomputes as prices drift, bulk paste-import, export, multiple named
portfolios, and returns vs the S&P 500.

The only thing missing was the thing being asked for. `index.html:2042`:

```js
let portfolios = JSON.parse(localStorage.getItem('portfolios') || 'null') || [];
```

`localStorage` is per-browser. That single line **was** the "individual files"
problem. So the project was never "build a tracker" — it was "move that state to
the server," a much smaller and better-defined job.

## Choices the owner made

- Storage: **free hosted Postgres** (over a Render paid disk, a GitHub Gist, or an
  ephemeral file that loses data on every deploy)
- Access: **shared passcode** (over an open link, or public-read/passcode-write)
- Placement: **a brand-new standalone page** (over converting the existing tab or
  adding a second tab), to have something clean to hand to non-technical people

## Build order

1. `shared_store.py` — Postgres store with optimistic concurrency and an in-memory
   dev fallback.
2. `proxy_server.py` — added the file's first `do_POST`, plus `GET`/`POST
   /pf-shared`, a shared guard (configured → passcode → rate limit), and
   deliberately no CORS headers.
3. `requirements.txt` — added `psycopg[binary]`.
4. `tracker.html` — standalone page reusing the existing `/etf-chart` price
   endpoint.

Verified locally before deploying: the concurrency rejection, passcode enforcement
over real HTTP, absence of CORS headers, XSS escaping with hostile payloads, and
the drift arithmetic recomputed independently against the rendered numbers.

## Deployment, and two credential leaks

Setting it up ran into the same mistake twice.

1. The **Neon connection string** — including the database password — was pasted
   into the chat. Advice given: rotate it. The owner created a fresh Neon project
   instead, which achieves the same thing.
2. Later the **passcode** (`PF_PASSCODE`) was pasted into the chat as well. It was
   also a bare personal name, weak for a page sitting on a public URL. Rotation was
   requested; **as of this writing it is unconfirmed** — see `STATUS.md`.

Both were avoidable: neither value ever needed to pass through the conversation,
since both go directly from the provider into Render's dashboard.

The leaked passcode was used once, deliberately, to run the production verification
that couldn't otherwise be done — schema creation, read, insert, stale-write
rejection, update — then the test data was removed, leaving the document empty at
version 2. That verification is the only reason we know the SQL works against a
real Postgres, since there was no local database to test against.

## The trade log

Second phase. The ask: log a trade with portfolio, price, time; keep a reviewable
log; have the portfolio update itself.

This inverts the data model — trades become the source of truth and positions get
derived. It also fixes something the old model simply couldn't express: buying the
same ticker three times at three prices.

Two design questions went back to the owner:

- **Trade units.** Shares, dollars, or either was offered; the owner answered
  neither, saying *"percentages, i bought 2% apple at $"*. So trades stay in
  percentages, matching how they think about the book.
- **What 2% means.** Given a book that has grown 50%, is a later 2% buy the same
  size or bigger? Bigger (2% of the current book) was recommended as
  self-correcting. The owner chose **fixed base** — every 2% is the same size. That
  choice is load-bearing throughout the engine; see `DECISIONS.md` §8.

Sells got all three modes, and target % was kept alongside actual, both at the
owner's request.

## A bug found in the shipped code

While wiring the trade log, review of `mutate()`/`save()` turned up a real defect
**in the version already deployed**: on a 409 conflict the client adopted the
server's document, then rolled back to its own stale copy while retaining the new
version number. The next save would have passed the version check and silently
overwritten whoever it had just been told saved first.

That is precisely the failure the whole optimistic-concurrency design exists to
prevent, reintroduced by a careless rollback. Fixed by making `save()` return
`'ok' | 'conflict' | 'error'` so `mutate()` can tell "someone else won" apart from
"the request failed," and rolling back only on the latter. Tested with a real
simulated conflict, including a follow-up save proving the other person's trade
survives.

**Lesson worth keeping:** a boolean return type is what let this in. If you
refactor those two functions, re-run the conflict test in `TESTING.md`.

## Where it stands

The holdings-model version is live. The trade-log version is built, tested and
waiting on a decision to push, because deploying it changes the data model under
anyone currently using the page. See `STATUS.md`.
