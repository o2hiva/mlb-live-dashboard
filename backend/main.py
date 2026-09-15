import os
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models_db import Game, Prediction
import poller

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

Base.metadata.create_all(bind=engine)

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    _scheduler = poller.start_scheduler()
    yield
    if _scheduler:
        _scheduler.shutdown()


app = FastAPI(title="MLB Live Dashboard", lifespan=lifespan)

# Personal, single-user dashboard - CORS wide open for simplicity.
# Tighten this to your actual frontend origin once deployed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/games/today")
def games_today(game_date: str = None, db: Session = Depends(get_db)):
    """
    Games for one date - defaults to today. Pass ?game_date=2026-09-15
    to preview another day (e.g. tomorrow's slate, loaded ahead of time
    by the poller or by load_game_day.py).
    """
    target_date = game_date or date.today().isoformat()
    games = db.query(Game).filter(Game.game_date == target_date).all()
    out = []
    for g in games:
        latest_pred = (
            db.query(Prediction)
            .filter(Prediction.game_pk == g.game_pk)
            .order_by(Prediction.created_at.desc())
            .first()
        )
        out.append({
            "game_pk": g.game_pk,
            "game_date": g.game_date,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "status": g.status,
            "inning": g.inning,
            "inning_half": g.inning_half,
            "home_score": g.home_score,
            "away_score": g.away_score,
            "home_probable_pitcher": g.home_probable_pitcher,
            "away_probable_pitcher": g.away_probable_pitcher,
            "first_inning_run_probability": latest_pred.probability if latest_pred else None,
            "model_version": latest_pred.model_version if latest_pred else None,
        })
    return out


@app.get("/api/games/{game_pk}")
def game_detail(game_pk: int, db: Session = Depends(get_db)):
    g = db.get(Game, game_pk)
    if g is None:
        return {"error": "not found"}
    return {
        "game_pk": g.game_pk,
        "home_team": g.home_team,
        "away_team": g.away_team,
        "status": g.status,
        "home_score": g.home_score,
        "away_score": g.away_score,
        "inning_lines": [
            {"inning": l.inning, "half": l.half, "runs": l.runs} for l in g.inning_lines
        ],
        "predictions": [
            {"probability": p.probability, "market": p.market,
             "model_version": p.model_version, "created_at": p.created_at.isoformat()}
            for p in g.predictions
        ],
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/debug/inning-stats")
def debug_inning_stats(db: Session = Depends(get_db)):
    """
    Diagnostic: shows whether the background inning-stats backfill
    (inning_stats_sync.py) has actually populated real data yet. If
    team_inning_stat_rows/pitcher_inning_stat_rows are 0 or low, and/or
    last_synced_date is missing or way behind, that's why predictions
    are still falling back to the 47% placeholder.
    """
    from models_db import TeamInningStat, PitcherInningStat, SyncState

    team_row_count = db.query(TeamInningStat).filter_by(inning=1).count()
    pitcher_row_count = db.query(PitcherInningStat).filter_by(inning=1).count()
    sync_row = db.get(SyncState, "inning_stats_last_synced_date")

    sample_teams = db.query(TeamInningStat).filter_by(inning=1).limit(5).all()
    sample_pitchers = db.query(PitcherInningStat).filter_by(inning=1).limit(5).all()

    return {
        "team_inning_stat_rows": team_row_count,
        "pitcher_inning_stat_rows": pitcher_row_count,
        "last_synced_date": sync_row.value if sync_row else None,
        "sample_teams": [
            {"team": t.team_name, "games": t.games, "scored": t.scored} for t in sample_teams
        ],
        "sample_pitchers": [
            {"pitcher": p.pitcher_name, "starts": p.starts, "allowed": p.allowed} for p in sample_pitchers
        ],
    }


@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
