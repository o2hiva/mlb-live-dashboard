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

## Run it locally

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then just open `frontend/index.html` directly in a browser (it defaults to
`http://localhost:8000` when running on localhost).

A `mlb_dashboard.db` SQLite file will appear in `backend/` — that's your database.

## Plug in your real model

Your existing first-inning pipeline (logistic regression baseline +
XGBoost/isotonic calibration) is the natural fit for `predictor.py`:

1. Export the trained model: `joblib.dump(model, "model.pkl")`, drop it in `backend/`.
2. Update `build_features()` in `predictor.py` to build the same feature vector
   your pipeline expects (starting pitcher stats, top-of-lineup metrics, park
   factor, weather, situational context).
3. Restart the backend — `predictor.py` auto-detects `model.pkl` and switches
   from the placeholder formula to your real model.

## Going live (hosting)

This needs a process that stays running (for the poller) plus a database that
persists — a static site alone can't do this. Good low-effort options:

- **Railway** or **Render**: push this repo, add a Postgres addon, set
  `DATABASE_URL` to the Postgres connection string it gives you, deploy the
  `backend/` folder as a web service (`uvicorn main:app --host 0.0.0.0 --port $PORT`).
  Host `frontend/index.html` as a static site on the same platform (or Netlify/
  Vercel), and update `API_BASE` in `index.html` to your backend's public URL.
- **Fly.io**: similar shape, with a Fly Postgres addon and a `fly.toml` for the backend.

Either way: since it's just for you, no auth is wired in — don't index it
publicly or put anything sensitive in it. Add a shared-secret header check in
`main.py` if you ever want a minimal gate.

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
