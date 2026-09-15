from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models_db import Game, Prediction
import poller

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
def games_today(db: Session = Depends(get_db)):
    games = db.query(Game).all()
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
