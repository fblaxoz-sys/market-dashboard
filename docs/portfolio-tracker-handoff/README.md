# Shared Portfolio Tracker — handoff package

A multi-user portfolio tracker with a trade log. One shared book: whoever has the
passcode sees the same holdings, and a trade logged by one person is already there
when the next person opens the link. You log trades; positions, weights, cash and
P&L are computed from them.

- **Live:** https://market-dashboard-b592.onrender.com/tracker.html
- **Code:** GitHub `fblaxoz-sys/market-dashboard` (private — you'll need access)
- **Host:** Render (free tier), service `market-dashboard`, region GCP `us-west1`
- **Database:** Neon Postgres (free tier), region AWS `us-west-2`
- **Owner:** Patrick Osborn
- **Built:** 2026-07-21

---

## If you are an AI assistant picking this up

Read in this order:

| File | What's in it |
|---|---|
| `ARCHITECTURE.md` | How it works — data model, the percentage math, the replay engine, the API |
| `DECISIONS.md` | *Why* it works that way, including rejected alternatives. **Read before changing anything** — several choices look wrong until you know the reason |
| `SETUP.md` | Env vars, deploying, running locally, how to test |
| `STATUS.md` | What's done, what's pending, known issues and gotchas |
| `HISTORY.md` | How the project got here, in order, including things that were tried and abandoned |
| `TESTING.md` | Every check that was run, with expected results, so you can re-run them |

## Where the code lives

The tracker is **three files** inside a much larger, older repo:

```
market-dashboard/
├── tracker.html        ← the entire front end, self-contained, no build step
├── shared_store.py     ← Postgres-backed shared state
├── proxy_server.py     ← pre-existing server; tracker added /pf-shared routes
├── requirements.txt    ← tracker added psycopg[binary]
├── Dockerfile          ← what Render actually builds from
├── index.html          ← UNRELATED older project (market dashboard, ~177KB)
└── ...                 ← UNRELATED (ML nowcasting, backtests, scanners)
```

**Do not refactor the rest of that repo.** `index.html`, the ML endpoints, the
scanners and the nowcast models are a separate project with their own history.
The tracker only borrows one endpoint from it (`/etf-chart`, for prices).

## Where this folder lives

`docs/portfolio-tracker-handoff/` **inside the `market-dashboard` repo**, so it
travels with the code — clone the repo and you have both. On Patrick's Mac,
`~/Desktop/portfolio-tracker-handoff` is a symlink to it, so there is exactly one
copy and no second one to go stale.

## Credentials

This package contains **no secrets**, deliberately — it's committed to the repo
and meant to be passed around. Two values live only in Render's environment
settings:

- `DATABASE_URL` — Neon Postgres connection string
- `PF_PASSCODE` — the shared passcode that gates the tracker

Patrick has both and can supply them directly. Don't commit them and don't write
them into this folder.

## Keeping this folder current

It does not update itself — see "Keeping this current" at the bottom of `STATUS.md`
for the mechanism and its honest limits.
