import os
import logging
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
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
_ensure_column("bet_tracker_settings", "kalshi_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "polymarket_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "novig_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "fanduel_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "draftkings_balance", "FLOAT DEFAULT 0.0")
_ensure_column("tracked_bets", "bet_type", "VARCHAR DEFAULT 'hits'")
_ensure_column("tracked_bets", "line", "FLOAT")
_ensure_column("batter_season_stats", "hr", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "bb", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "hr_allowed", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "bb_allowed", "INTEGER DEFAULT 0")

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
    games = (
        db.query(Game)
        .filter(Game.game_date == target_date)
        .order_by(Game.game_datetime_utc, Game.game_pk)
        .all()
    )
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
            inputs = hits_stats_sync.compute_batter_hits_inputs(
                r.batter_id, r.batting_order, opposing_pitcher_id, home_team=game.home_team
            )
            out.append({
                "batter_id": r.batter_id,
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


@app.get("/api/games/{game_pk}/hrr")
def game_hrr(game_pk: int, db: Session = Depends(get_db)):
    """
    Same shape as /api/games/{game_pk}/hits, but for the HRR (Hits+Runs+RBI)
    market: each batter gets (mean, sd) instead of (n_ab, p) since HRR is
    modeled as approximately Normal rather than binomial - the frontend
    computes "P(HRR >= line)" for any line instantly via a normal CDF,
    the same "compute once, adjust instantly client-side" pattern Hits uses.
    """
    from models_db import LineupBatter
    import hrr_stats_sync

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
            inputs = hrr_stats_sync.compute_hrr_inputs(
                r.batter_id, r.batting_order, opposing_pitcher_id,
                home_team=game.home_team, game_pk=game_pk, team_side=side,
            )
            out.append({
                "batter_id": r.batter_id,
                "batter_name": r.batter_name,
                "batting_order": r.batting_order,
                "mean": inputs["mean"] if inputs else None,
                "sd": inputs["sd"] if inputs else None,
                "hrr_index": inputs["batter_hrr_index"] if inputs else None,
            })
        return out

    return {
        "game_pk": game_pk,
        "home_lineup_confirmed": game.home_lineup_confirmed,
        "away_lineup_confirmed": game.away_lineup_confirmed,
        "away_opp_pitcher_name": game.home_probable_pitcher,
        "away_opp_pitcher_hrr_index": hrr_stats_sync.get_pitcher_hrr_index(game.home_probable_pitcher_id),
        "home_opp_pitcher_name": game.away_probable_pitcher,
        "home_opp_pitcher_hrr_index": hrr_stats_sync.get_pitcher_hrr_index(game.away_probable_pitcher_id),
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


@app.get("/api/bet-tracker/settings")
def get_bet_tracker_settings(db: Session = Depends(get_db)):
    """Bankroll (sum of platform balances) + Kelly % for the Bet Tracker
    tab - a single persistent row, same value from any device."""
    from models_db import BetTrackerSettings
    settings = db.get(BetTrackerSettings, 1)
    if settings is None:
        settings = BetTrackerSettings(id=1)
        db.add(settings)
        db.commit()
    return {
        "bankroll": settings.bankroll,
        "kelly_percent": settings.kelly_percent,
        "kalshi_balance": settings.kalshi_balance,
        "polymarket_balance": settings.polymarket_balance,
        "novig_balance": settings.novig_balance,
        "fanduel_balance": settings.fanduel_balance,
        "draftkings_balance": settings.draftkings_balance,
    }


class BetTrackerSettingsUpdate(BaseModel):
    kelly_percent: float | None = None
    kalshi_balance: float | None = None
    polymarket_balance: float | None = None
    novig_balance: float | None = None
    fanduel_balance: float | None = None
    draftkings_balance: float | None = None


@app.post("/api/bet-tracker/settings")
def update_bet_tracker_settings(update: BetTrackerSettingsUpdate, db: Session = Depends(get_db)):
    """
    Bankroll is never accepted directly from the client - it's always
    recomputed here as the sum of the five platform balances, so it
    can't drift out of sync with them (e.g. from a stale cached value
    on one device while another device updates a platform balance).
    """
    from models_db import BetTrackerSettings
    settings = db.get(BetTrackerSettings, 1)
    if settings is None:
        settings = BetTrackerSettings(id=1)
        db.add(settings)

    if update.kelly_percent is not None:
        settings.kelly_percent = update.kelly_percent
    if update.kalshi_balance is not None:
        settings.kalshi_balance = update.kalshi_balance
    if update.polymarket_balance is not None:
        settings.polymarket_balance = update.polymarket_balance
    if update.novig_balance is not None:
        settings.novig_balance = update.novig_balance
    if update.fanduel_balance is not None:
        settings.fanduel_balance = update.fanduel_balance
    if update.draftkings_balance is not None:
        settings.draftkings_balance = update.draftkings_balance

    settings.bankroll = (
        settings.kalshi_balance + settings.polymarket_balance + settings.novig_balance +
        settings.fanduel_balance + settings.draftkings_balance
    )
    db.commit()

    return {
        "bankroll": settings.bankroll,
        "kelly_percent": settings.kelly_percent,
        "kalshi_balance": settings.kalshi_balance,
        "polymarket_balance": settings.polymarket_balance,
        "novig_balance": settings.novig_balance,
        "fanduel_balance": settings.fanduel_balance,
        "draftkings_balance": settings.draftkings_balance,
    }


class TrackedBetCreate(BaseModel):
    game_pk: int
    bet_type: str = "hits"  # "hits", "first_inning_run", or "hrr"
    batter_id: int | None = None
    batter_name: str
    team_side: str | None = None
    batting_order: int | None = None
    hits_threshold: int | None = None
    line: float | None = None  # the O/U line at time of tracking - use this over hits_threshold for new bets
    yn: str
    model_probability: float | None = None
    market_probability: float | None = None
    wager: float | None = None
    potential_profit: float | None = None


@app.post("/api/bet-tracker/track")
def track_bet(bet: TrackedBetCreate, db: Session = Depends(get_db)):
    """
    Records a Hits bet snapshot when the 'Track' checkbox is checked -
    exactly what's on the row at that moment (market %, wager, potential
    profit, model probability), for a future end-of-day job to grade
    against what actually happened. Returns the new row's id, which the
    frontend holds onto so unchecking the box can delete the right row.
    """
    from models_db import TrackedBet
    row = TrackedBet(**bet.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@app.delete("/api/bet-tracker/track/{tracked_bet_id}")
def untrack_bet(tracked_bet_id: int, db: Session = Depends(get_db)):
    """Removes a tracked bet - called when the 'Track' checkbox is
    unchecked, or from the Remove button in the Bet Tracker tab's list."""
    from models_db import TrackedBet
    row = db.get(TrackedBet, tracked_bet_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return {"status": "removed"}


@app.get("/api/bet-tracker/tracked")
def list_tracked_bets(db: Session = Depends(get_db)):
    """
    All currently-tracked, not-yet-resolved bets, with just enough game
    context (matchup, date) to identify them at a glance in the Bet
    Tracker tab. Ordered most-recently-tracked first.
    """
    from models_db import TrackedBet
    rows = (
        db.query(TrackedBet)
        .filter_by(resolved=False)
        .order_by(TrackedBet.placed_at.desc())
        .all()
    )
    out = []
    for r in rows:
        game = db.get(Game, r.game_pk)
        out.append({
            "id": r.id,
            "bet_type": r.bet_type,
            "batter_name": r.batter_name,
            "team_side": r.team_side,
            "matchup": f"{game.away_team} @ {game.home_team}" if game else "Unknown matchup",
            "game_date": game.game_date if game else None,
            "hits_threshold": r.hits_threshold,
            "line": r.line if r.line is not None else r.hits_threshold,
            "yn": r.yn,
            "market_probability": r.market_probability,
            "wager": r.wager,
            "potential_profit": r.potential_profit,
            "placed_at": r.placed_at.isoformat(),
        })
    return out


@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
