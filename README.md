# MLB Live Dashboard (starter)

A personal, no-login dashboard that tracks MLB games live and shows a
first-inning run-probability prediction per game, updated in near-real-time.

## How it fits together

```
MLB Stats API  --poller.py (every ~15s during live games)-->  database
                                                                   |
                                                        FastAPI (main.py)
                                                                   |
                                                    frontend/index.html
                                                (polls every 10s, no login)
```

- `backend/mlb_client.py` — talks to MLB's free public Stats API (schedule + live feed).
- `backend/poller.py` — background job, runs inside the same process as the API,
  keeps the database in sync with today's games.
- `backend/predictor.py` — where your trained model plugs in. Ships with a
  placeholder flat probability so the app runs before the real model is wired up.
- `backend/main.py` — FastAPI app + REST endpoints the frontend reads from.
- `frontend/index.html` — single-page dashboard, plain JS, no build step.
  Lives inside `backend/frontend/` so it deploys together with the backend
  as one unit (important for platforms like Railway where you point at the
  `backend` folder as the deploy root).

## Run it locally

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open your browser to `http://localhost:8000` — the backend now serves
the dashboard page itself, so there's just the one URL.

A `mlb_dashboard.db` SQLite file will appear in `backend/` — that's your database.

## The prediction model

This runs your actual `core.py` model - `inning_scoring_probability()` -
a log5 sabermetric formula, not a trained ML model. It combines:

- Each team's real season-to-date rate of scoring in the 1st inning
- The OPPOSING starting pitcher's real season-to-date rate of allowing
  a run in the 1st

...via log5, with the exact validated shrinkage constants from your
`core.py` (`INNING_LEAGUE_RATES`, `INNING_TEAM_SHRINKAGE_K`,
`INNING_PITCHER_SHRINKAGE_K`). `predictor.py` ported these verbatim.

The real counts it needs (team games/scored, pitcher starts/allowed)
come from `inning_stats_sync.py` - a live-dashboard port of your
`fetch_inning_scoring_stats.py`, reading the same MLB Stats API
schedule+linescore data, but writing to this app's own database on a
recurring schedule instead of an Excel workbook you run by hand.

**First run:** it backfills from 2026-07-16 (your fetch script's own
default start date) through yesterday - about a two-month backfill,
one API call per day, running in the background so it doesn't block
app startup. After that, it only fetches new days.

**Extending to innings 2-3:** add the inning number to
`TRACKED_INNINGS` in `inning_stats_sync.py` - the pitcher-level model
already supports it (`MAX_SUPPORTED_INNING = 3`). Innings 4+ would need
a second sync path + formula (`inning_scoring_probability_team_level`
in your `core.py`) since no specific reliever is knowable pre-game -
that isn't ported here yet.

## Loading a specific day ahead of time

The poller now automatically loads today PLUS the next `SYNC_DAYS_AHEAD`
days (2, by default) every 60 seconds - so tomorrow's games and probable
pitchers show up in the dashboard before game day, and keep refreshing
right up to first pitch (catching any late pitcher swap, since MLB
doesn't always lock in a starter days out).

To force-load a specific date immediately instead of waiting for the
poller's next cycle - useful right after deploying, or to check a day's
slate on demand:

```bash
cd backend
python load_game_day.py --date 2026-09-15
```

Use the date picker at the top of the dashboard page to view that day's
games once loaded (defaults to today).

## Going live (hosting) — Railway walkthrough

The backend now serves the dashboard page itself (one URL, no separate
frontend host needed), which makes this straightforward on Railway's free
tier. This needs a process that stays running (for the poller) plus a
database that persists — a static site alone can't do this.

1. **Put the code on GitHub.** If you don't already have a repo: create one
   at github.com (new repository, e.g. `mlb-live-dashboard`), then from the
   `mlb-live-dashboard` folder:
   ```
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/mlb-live-dashboard.git
   git push -u origin main
   ```
2. **Create a Railway account** at railway.app (sign in with GitHub is easiest).
3. **New Project → Deploy from GitHub repo** → pick your `mlb-live-dashboard` repo.
4. Railway will try to build the whole repo; tell it the backend lives in a
   subfolder: in the service's **Settings → Root Directory**, set it to `backend`.
5. **Add a database**: in the project, click **+ New → Database → PostgreSQL**.
   Railway creates it and exposes a `DATABASE_URL` — click on your web
   service, go to **Variables**, and add a reference to that Postgres
   `DATABASE_URL` (Railway's UI lets you pick it from a dropdown rather than
   typing it in).
6. Railway auto-detects the `Procfile` in `backend/` and uses
   `uvicorn main:app --host 0.0.0.0 --port $PORT` as the start command — no
   extra config needed.
7. Once it deploys, Railway gives you a public URL like
   `mlb-live-dashboard-production.up.railway.app`. Open that on your phone,
   laptop, anywhere — it's the same dashboard, always on.

Since it's just for you, no login is wired in — don't share the link
publicly. If you want a minimal gate, add a shared-secret check in `main.py`
later.

**Alternative:** Render.com works almost identically (New → Web Service from
GitHub repo, root directory `backend`, add a Postgres instance, same start
command) if you'd rather use that instead of Railway.

## Etiquette / limitations

- MLB's Stats API is free and widely used by hobby projects, but it's not an
  officially documented/licensed public API — keep polling gentle (the
  defaults here are 15s during live games, 60s otherwise) and this is for
  personal use, not redistribution.
- The MLB API calls in `mlb_client.py` are written against the documented
  schema but untested from this sandbox (its network is locked to package
  registries) — sanity-check the JSON shape against a live response once you
  run this somewhere with normal internet access, in case MLB has tweaked a
  field name since this was written.

## Extending beyond first-inning runs

`models_db.Prediction` already has a `market` column — add more prediction
types (full-game total runs, moneyline, strikeout props, etc.) by writing more
`predict_*` functions in `predictor.py` and calling them from `poller.py`.
This also sets up cleanly to extend into NFL/NCAA later, alongside the
shrinkage-based prop model you've been building for those sports — same
schedule → poll → predict → serve shape, different data source per league.
