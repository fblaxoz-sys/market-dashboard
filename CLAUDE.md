# market-dashboard

This repo holds **two unrelated projects**. Know which one you're touching.

## 1. Shared Portfolio Tracker

Files: `tracker.html`, `shared_store.py`, plus the `/pf-shared` routes and `do_POST`
in `proxy_server.py`, plus `psycopg[binary]` in `requirements.txt`.

A multi-user portfolio tracker with a trade log, backed by Neon Postgres. One
shared document; everyone with the passcode sees the same book.

**Full documentation lives in `docs/portfolio-tracker-handoff/` in this repo**
(also reachable at `~/Desktop/portfolio-tracker-handoff`, a symlink to it — one
source of truth, no copy to go stale). Read
`ARCHITECTURE.md` and `DECISIONS.md` there before changing anything — several
choices look wrong until you know the reason, notably:

- Percentages are of a **fixed 100-unit base**, not the current book value. This is
  deliberate and was chosen over the recommended alternative. Don't "fix" it.
- `/pf-shared` deliberately sends **no CORS headers**, unlike every other endpoint
  in `proxy_server.py`. Don't add them for consistency.
- The store **fails closed**: `DATABASE_URL` set without `PF_PASSCODE` returns 503.
  Don't resolve a 503 by defaulting the passcode.
- `save()` returns `'ok' | 'conflict' | 'error'`, not a boolean. A boolean return
  is exactly how a silent data-loss bug got shipped once. If you refactor
  `save()`/`mutate()`, re-run the conflict test in the handoff folder's
  `TESTING.md` §9.

### Keep the handoff folder current

**Whenever you change the tracker, update `docs/portfolio-tracker-handoff/`
in the same session.** That folder is what gets handed to collaborators, and it is
only as accurate as the last person to touch it.

- behaviour or data-model change → `ARCHITECTURE.md`
- a choice made, especially one with a rejected alternative → `DECISIONS.md`
- shipped, pending, or newly-known limitation → `STATUS.md` (and its "as of" date)
- a new check worth re-running → `TESTING.md`
- env vars, deploy or local-dev changes → `SETUP.md`

Never write credentials into that folder — it's meant to be passed around, and
it is now **committed to this repo**, so anything written there is permanent and
visible to everyone with repo access. `DATABASE_URL` and `PF_PASSCODE` live only
in Render's environment settings.

Commit doc updates alongside the code change they describe, so the two never
drift apart in history.

## 2. Market Dashboard (older, unrelated)

`index.html` (~177KB), the ML nowcasting endpoints, scanners, backtests, `models/`,
`scripts/`, `tests/`. Deployed at the same Render service.

The tracker borrows exactly one thing from it: `/etf-chart?sym=` for prices.
Otherwise leave it alone — don't refactor it while working on the tracker.

**`index.html` usually carries uncommitted work in progress.** Stage files
explicitly; never `git add -A` in this repo.

## Deploying

`git push origin main` → Render auto-deploys. There is no staging environment;
pushing `main` publishes to a URL real people use.

Note `render.yaml` is **stale** — it declares a Python build, but the Render
dashboard has this service set to Docker, so `Dockerfile` is what actually runs.
Dependencies still come from `requirements.txt`.

The GitHub token in use cannot push `.github/workflows/*` (no `workflow` scope).
