# Setup, deploy and local development

## Environment variables

Set in the Render dashboard → service `market-dashboard` → **Environment**.

| Key | Required | What it is |
|---|---|---|
| `DATABASE_URL` | yes, in production | Neon Postgres connection string, `postgresql://…?sslmode=require` |
| `PF_PASSCODE` | yes, whenever `DATABASE_URL` is set | The shared passcode |

Other variables on that service (`BREVO_API_KEY`, `DIGEST_TO`, `DIGEST_TOKEN`,
`GMAIL_APP_PASSWORD`, `GMAIL_USER`) belong to the older dashboard project. Leave
them alone.

Behaviour by combination:

| `DATABASE_URL` | `PF_PASSCODE` | Result |
|---|---|---|
| set | set | Normal production |
| set | **missing** | **503 on both routes** — fail-closed, by design |
| missing | set | Runs in-memory, passcode still enforced, amber banner in UI |
| missing | missing | Local dev: in-memory, any passcode accepted |

The table is created automatically on first use. There is nothing to run by hand.

## Provisioning a database from scratch

1. Create a project at [neon.tech](https://neon.tech), free tier.
2. **Region: AWS `us-west-2` (Oregon)** — the Render service is GCP `us-west1`
   (Oregon). Cross-country adds a few hundred ms to every save, because each
   request opens a fresh connection with a TLS handshake. Neon cannot change a
   project's region after creation; you'd have to recreate it.
3. Turn **Neon Auth off**. It installs a user-accounts/sessions system this project
   doesn't use — auth here is one shared passcode checked by the app.
4. Copy the connection string into Render as `DATABASE_URL`.

To rotate the database password: Neon → **Branches** → `main` → **Roles &
Databases** → `neondb_owner` → ⋯ → **Reset password**, then update `DATABASE_URL`
in Render. If you can't find that screen, deleting and recreating the project also
works and is often faster — the schema rebuilds itself, though existing portfolio
data is lost.

To rotate the passcode: change `PF_PASSCODE` in Render and save. Redeploys
automatically; the old passcode stops working immediately.

## Deploying

```bash
cd ~/Desktop/market-dashboard
git add tracker.html shared_store.py proxy_server.py requirements.txt
git commit -m "..."
git push origin main          # Render auto-deploys from main
```

**Stage files explicitly.** The repo has unrelated uncommitted work in
`index.html`. Never `git add -A` here.

Deploys take roughly 1–5 minutes. Verify:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  https://market-dashboard-b592.onrender.com/tracker.html      # expect 200
curl -s https://market-dashboard-b592.onrender.com/pf-shared   # expect 401
```

A **401** means both env vars are set correctly. A **503** means `PF_PASSCODE` is
missing. A **404** means the deploy hasn't landed yet.

### Build gotcha

`render.yaml` declares `env: python` with a pip build command — **this is stale and
not what runs.** The Render dashboard has the service set to **Docker**, so
`Dockerfile` is the real build:

```dockerfile
FROM python:3.11-slim
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "proxy_server.py"]
```

Dependencies still come from `requirements.txt`, so adding one works either way —
but don't trust `render.yaml` when reasoning about the build.

## Running locally

There is a launch config at `~/Desktop/inflation/.claude/launch.json`:

- **`dashboard`** — port 8765, no env vars → in-memory store, any passcode works
- **`tracker-secure`** — port 8766, `PF_PASSCODE=testpass123` → exercises the real
  auth path without a database

Or plainly:

```bash
cd ~/Desktop/market-dashboard
python3 proxy_server.py                                   # port 8765
PORT=8766 PF_PASSCODE=testpass123 python3 proxy_server.py # with auth
```

Then open `http://localhost:8765/tracker.html`.

The heavy ML imports in `proxy_server.py` are lazy, so the server starts without
`statsmodels`/`sklearn`/`pandas` installed. You only need those to exercise the
nowcast endpoints, which the tracker doesn't touch.

`psycopg` is only imported when `DATABASE_URL` is set. To test against a real
database locally:

```bash
DATABASE_URL='postgresql://…' PF_PASSCODE=test \
  uv run --with 'psycopg[binary]>=3.1' python proxy_server.py
```

## Seeding local data

```bash
curl -s -X POST localhost:8765/pf-shared -H 'Content-Type: application/json' -d '{
  "doc": {"portfolios": [{"name":"Main","targets":{"AAPL":15},"trades":[
     {"id":"a1","ts":"2026-01-02T09:30","side":"buy","sym":"AAPL","mode":"pct","amt":15,"price":180}
  ]}]},
  "version": 0, "who": "seed"
}'
```

Add `-H 'X-PF-Pass: testpass123'` against the `tracker-secure` instance.

## Repo layout — what belongs to this project

```
market-dashboard/
├── tracker.html      ← this project
├── shared_store.py   ← this project
├── proxy_server.py   ← shared; this project added /pf-shared + do_POST
├── requirements.txt  ← shared; this project added psycopg[binary]
├── Dockerfile        ← shared, unmodified
├── render.yaml       ← shared, stale (see above)
├── index.html        ← OTHER PROJECT (~177KB market dashboard)
├── models/           ← OTHER PROJECT, untracked
├── scripts/ tests/   ← OTHER PROJECT
└── ...               ← OTHER PROJECT (ML nowcasts, scanners, backtests)
```

## Operational notes

- **Render free tier sleeps when idle.** First request after a quiet period takes
  ~30 seconds. Not a bug; it looks like a hang to a new user, so warn them.
  Everything else measures fast (warm TTFB ~0.2–0.6s, page 37KB gzipped, chart
  interactions 1–4ms), so this cold start is *the* load-time bottleneck. The fix
  is the **Render Starter plan (~$7/mo)** — upgrade the existing service in place
  (dashboard → market-dashboard → "Upgrade your instance"), which keeps the URL,
  the database, and every env var. Do **not** change region in that flow: the
  Neon DB is in Oregon beside the server, so an East-coast server would add
  ~300ms to every save unless the DB moves too (a separate, bigger migration with
  a new URL). A free alternative is an external uptime pinger (UptimeRobot /
  cron-job.org) hitting `/healthz` every 5 min, but it's a workaround with
  free-tier caps that breaks silently if it lapses.
- **Neon free tier idles too**, adding a second or two to the first query.
- There is a keep-alive ping hitting `/healthz`, which is deliberately cheap and
  doesn't touch the database.
- The GitHub token in use **cannot push `.github/workflows/*`** (no `workflow`
  scope). Those must go through the GitHub web UI.

## Importing a portfolio from a brokerage export

`tools/portfolio_import.py` converts an .xlsx/.csv statement into the paste block
for **"+ From what I own"**:

```bash
python3 tools/portfolio_import.py statement.xlsx
python3 tools/portfolio_import.py statement.csv --sheet "Positions"
```

Output is `TICKER, percent-now, avg-cost` lines plus a summary and warnings.
Needs `openpyxl` for .xlsx (`uv run --with openpyxl python3 tools/...` if absent).

What it handles, because brokerage exports reliably contain all of it:

- junk rows above the header and disclaimers below it; the header is found by
  scanning for a Symbol column next to something numeric
- fuzzy column names across brokers (`Mkt Val` / `Market Value` /
  `Current Market Value`; `Avg Cost Per Share` / `Cost Basis` / `Total Cost`)
- `"$1,234.56"`, `"(123.45)"`, `"12.3%"` strings
- cost basis given as a **total** → divided by quantity for per-share
- the same ticker in **multiple lots** → merged, cost value-weighted
- money-market funds and cash lines → folded into cash and **excluded** from the
  output, because the tracker derives cash as whatever the weights leave short
  of 100%
- **totals rows** → ignored (one slipping through inflates the account and skews
  every percentage — this was a real bug caught in testing)
- broker ticker spellings Yahoo rejects (`BRK.B`/`BRKB` → `BRK-B`, see
  `TICKER_ALIASES`)

**The cross-check that matters:** when the file has its own "% of account"
column, every computed percentage is compared against it, and any gap over
0.5pt prints `DO NOT PASTE until this is resolved`. That is the guard against
the one dangerous failure — a missed row producing output that looks fine and
is wrong. Verified by deleting a cash row from a fixture and confirming it trips.

Missing cost basis is not an error: that holding starts flat at today's price.
