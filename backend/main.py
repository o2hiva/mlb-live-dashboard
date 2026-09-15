import os
import logging
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models_db import Game, Prediction
import poller

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

log = logging.getLogger("main")

Base.metadata.create_all(bind=engine)


def _ensure_column(table: str, column: str, sql_type: str):
    """
    create_all() only creates brand-new tables - it never adds columns
    to a table that already exists. Since this app's schema keeps
    growing (game_datetime_utc, home_team_id, etc. were all added after
    the "games" table already existed on a live deploy), this checks
    for a missing column and adds it if needed. Safe to call every
    startup: does nothing once the column is already there.
    """
    try:
        inspector = inspect(engine)
        if table not in inspector.get_table_names():
            return  # create_all() will make the whole table fresh, column included
        existing_columns = {c["name"] for c in inspector.get_columns(table)}
        if column in existing_columns:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        log.info("Added missing column %s.%s", table, column)
    except Exception:
        log.exception("Failed to ensure column %s.%s exists", table, column)


_ensure_column("games", "game_datetime_utc", "VARCHAR")
_ensure_column("games", "home_lineup_confirmed", "BOOLEAN DEFAULT FALSE")
_ensure_column("games", "away_lineup_confirmed", "BOOLEAN DEFAULT FALSE")

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
            "game_datetime_utc": g.game_datetime_utc,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "status": g.status,
            "inning": g.inning,
            "inning_half": g.inning_half,
            "home_score": g.home_score,
            "away_score": g.away_score,
            "home_probable_pitcher": g.home_probable_pitcher,
            "away_probable_pitcher": g.away_probable_pitcher,
            "home_lineup_confirmed": g.home_lineup_confirmed,
            "away_lineup_confirmed": g.away_lineup_confirmed,
            "first_inning_run_yes_probability": latest_pred.probability if latest_pred else None,
            "first_inning_run_no_probability": (1 - latest_pred.probability) if latest_pred else None,
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


@app.get("/api/admin/refresh-inning-stats")
def manual_refresh_inning_stats(db: Session = Depends(get_db)):
    """
    Manually triggers the end-of-day inning-stats sync right now, instead
    of waiting for its scheduled 09:00 UTC run. Safe to hit any time -
    it only processes days it hasn't already synced, so running this
    right after the automatic daily run (or repeatedly) just confirms
    you're already up to date rather than double-counting anything.

    Runs synchronously and returns the resulting counts, so visiting
    this URL in a browser both triggers the refresh AND shows you the
    result in one step.
    """
    import inning_stats_sync
    from models_db import TeamInningStat, PitcherInningStat, SyncState

    inning_stats_sync.refresh_inning_stats()

    team_row_count = db.query(TeamInningStat).filter_by(inning=1).count()
    pitcher_row_count = db.query(PitcherInningStat).filter_by(inning=1).count()
    sync_row = db.get(SyncState, "inning_stats_last_synced_date")

    return {
        "status": "refresh complete",
        "team_inning_stat_rows": team_row_count,
        "pitcher_inning_stat_rows": pitcher_row_count,
        "last_synced_date": sync_row.value if sync_row else None,
    }


@app.get("/api/games/{game_pk}/hits")
def game_hits(game_pk: int, db: Session = Depends(get_db)):
    """
    Confirmed batters (if any) for both sides of a game, each with
    (n_ab, p, hit_index) - enough for the frontend to compute "at least
    H hits" for any H instantly, client-side, without another request
    per threshold change. Also includes each side's opposing starting
    pitcher's own hit index (their hits-allowed rate vs. league
    average). A side with no confirmed lineup yet returns an empty list
    for that side - the frontend shows "Not Confirmed" rather than a
    batter list in that case.
    """
    from models_db import LineupBatter
    import hits_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    def batters_for_side(side: str, opposing_pitcher_id):
        rows = (
            db.query(LineupBatter)
            .filter_by(game_pk=game_pk, team_side=side)
            .order_by(LineupBatter.batting_order)
            .all()
        )
        out = []
        for r in rows:
            inputs = hits_stats_sync.compute_batter_hits_inputs(r.batter_id, r.batting_order, opposing_pitcher_id)
            out.append({
                "batter_name": r.batter_name,
                "batting_order": r.batting_order,
                "n_ab": inputs["n_ab"] if inputs else None,
                "p": inputs["p"] if inputs else None,
                "hit_index": inputs["hit_index"] if inputs else None,
            })
        return out

    return {
        "game_pk": game_pk,
        "home_lineup_confirmed": game.home_lineup_confirmed,
        "away_lineup_confirmed": game.away_lineup_confirmed,
        # Away batters face the HOME team's probable pitcher, and vice versa.
        "away_opp_pitcher_name": game.home_probable_pitcher,
        "away_opp_pitcher_hit_index": hits_stats_sync.get_pitcher_hit_index(game.home_probable_pitcher_id),
        "home_opp_pitcher_name": game.away_probable_pitcher,
        "home_opp_pitcher_hit_index": hits_stats_sync.get_pitcher_hit_index(game.away_probable_pitcher_id),
        "home_batters": batters_for_side("home", game.away_probable_pitcher_id),
        "away_batters": batters_for_side("away", game.home_probable_pitcher_id),
    }


@app.get("/api/debug/boxscore/{game_pk}")
def debug_boxscore(game_pk: int):
    """
    Diagnostic: shows the raw boxscore data for one game against the
    now-corrected parsing logic (ported from a previously-working
    script, fill_lineups.py, rather than guessed) - each player's
    battingOrder string and whether it's read as a confirmed starter.
    """
    import mlb_client

    boxscore = mlb_client.get_boxscore(game_pk)

    def summarize_side(side: str):
        team_box = boxscore.get("teams", {}).get(side, {})
        players = team_box.get("players", {})
        player_orders = [
            {"key": k, "battingOrder": v.get("battingOrder"),
             "name": v.get("person", {}).get("fullName")}
            for k, v in players.items()
        ]
        confirmed, batters = mlb_client.extract_boxscore_lineup(boxscore, side)
        return {
            "players_dict_size": len(players),
            "player_batting_orders": player_orders,
            "extracted_confirmed": confirmed,
            "extracted_batters": batters,
        }

    return {
        "game_pk": game_pk,
        "home": summarize_side("home"),
        "away": summarize_side("away"),
    }


@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
