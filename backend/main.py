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
import mlb_client

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
_ensure_column("games", "abstract_status", "VARCHAR DEFAULT 'Preview'")
_ensure_column("bet_tracker_settings", "kalshi_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "polymarket_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "novig_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "fanduel_balance", "FLOAT DEFAULT 0.0")
_ensure_column("bet_tracker_settings", "draftkings_balance", "FLOAT DEFAULT 0.0")
_ensure_column("tracked_bets", "bet_type", "VARCHAR DEFAULT 'hits'")
_ensure_column("tracked_bets", "line", "FLOAT")
_ensure_column("tracked_bets", "actual_value", "FLOAT")
_ensure_column("batter_season_stats", "hr", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "bb", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "strikeouts", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "plate_appearances", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "hr_allowed", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "bb_allowed", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "runs_allowed", "INTEGER DEFAULT 0")
_ensure_column("batter_platoon_splits", "bat_side", "VARCHAR")
_ensure_column("batter_platoon_splits", "bat_side_updated_at", "TIMESTAMP")

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
    target_date = game_date or mlb_client.mlb_today().isoformat()
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
    Manually triggers JUST the 1st-inning stats sync - kept for backward
    compatibility (bookmarked from before the full end-of-day routine
    existed). For everything (1st-inning stats + Hits/HRR player stats +
    league rates), use /api/admin/run-daily-updates instead.
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


@app.get("/api/admin/run-daily-updates")
def manual_run_daily_updates():
    """
    Manually triggers the FULL end-of-day routine right now, instead of
    waiting for its scheduled run (see poller.py's start_scheduler -
    same time as the 1st-inning sync used to run alone, now folded
    together): force-refreshes real season stats for every batter and
    pitcher who played yesterday, refreshes the league-average rates
    those formulas depend on, and re-syncs 1st-inning stats too - the
    live-dashboard equivalent of running run_daily_updates.py by hand.

    Safe to run any time, and safe to run more than once - every step
    is idempotent (force-refreshing already-current data just confirms
    it's current, doesn't double-count or corrupt anything).

    Runs synchronously and returns a summary, so visiting this URL in a
    browser both triggers it AND shows you the result in one step.
    """
    import end_of_day
    return end_of_day.run_end_of_day_update()


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
    import platoon_stats_sync

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
                "bat_side": platoon_stats_sync.get_batter_hand(r.batter_id, r.batter_name),
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
        "away_opp_pitcher_hand": platoon_stats_sync.get_pitcher_hand(game.home_probable_pitcher_id, game.home_probable_pitcher or ""),
        "away_opp_pitcher_hit_index": hits_stats_sync.get_pitcher_hit_index(game.home_probable_pitcher_id),
        "home_opp_pitcher_name": game.away_probable_pitcher,
        "home_opp_pitcher_hand": platoon_stats_sync.get_pitcher_hand(game.away_probable_pitcher_id, game.away_probable_pitcher or ""),
        "home_opp_pitcher_hit_index": hits_stats_sync.get_pitcher_hit_index(game.away_probable_pitcher_id),
        "home_batters": batters_for_side("home", game.away_probable_pitcher_id),
        "away_batters": batters_for_side("away", game.home_probable_pitcher_id),
    }


@app.get("/api/games/{game_pk}/hrr")
def game_hrr(game_pk: int, db: Session = Depends(get_db)):
    """
    Same shape as /api/games/{game_pk}/hits, but for the HRR (Hits+Runs+RBI)
    market: each batter gets Negative Binomial parameters (r, p) instead
    of (n_ab, p) - the frontend computes "P(HRR >= line)" for any line
    instantly via a Negative Binomial survival function, the same
    "compute once, adjust instantly client-side" pattern Hits uses.
    """
    from models_db import LineupBatter
    import hrr_stats_sync
    import hits_stats_sync
    import platoon_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    # Computed ONCE per request, not once per batter - each of these can
    # be a real MLB API round-trip on a cold cache (dozens of calls), so
    # doing this per-batter instead (9-18 times) risks the whole request
    # timing out. See hrr_stats_sync's docstrings for the same note.
    la_b9 = hits_stats_sync.get_league_average_hit_rate()
    hrr_rates = hrr_stats_sync.get_league_hrr_rates()

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
                la_b9=la_b9, hrr_rates=hrr_rates,
            )
            out.append({
                "batter_id": r.batter_id,
                "batter_name": r.batter_name,
                "batting_order": r.batting_order,
                "bat_side": platoon_stats_sync.get_batter_hand(r.batter_id, r.batter_name),
                "r": inputs["r"] if inputs else None,
                "p": inputs["p"] if inputs else None,
                "hrr_index": inputs["batter_hrr_index"] if inputs else None,
            })
        return out

    return {
        "game_pk": game_pk,
        "home_lineup_confirmed": game.home_lineup_confirmed,
        "away_lineup_confirmed": game.away_lineup_confirmed,
        "away_opp_pitcher_name": game.home_probable_pitcher,
        "away_opp_pitcher_hand": platoon_stats_sync.get_pitcher_hand(game.home_probable_pitcher_id, game.home_probable_pitcher or ""),
        "away_opp_pitcher_hrr_index": hrr_stats_sync.get_pitcher_hrr_index(game.home_probable_pitcher_id, la_b9=la_b9, hrr_rates=hrr_rates),
        "home_opp_pitcher_name": game.away_probable_pitcher,
        "home_opp_pitcher_hand": platoon_stats_sync.get_pitcher_hand(game.away_probable_pitcher_id, game.away_probable_pitcher or ""),
        "home_opp_pitcher_hrr_index": hrr_stats_sync.get_pitcher_hrr_index(game.away_probable_pitcher_id, la_b9=la_b9, hrr_rates=hrr_rates),
        "home_batters": batters_for_side("home", game.away_probable_pitcher_id),
        "away_batters": batters_for_side("away", game.home_probable_pitcher_id),
    }


@app.get("/api/games/{game_pk}/hr")
def game_hr(game_pk: int, db: Session = Depends(get_db)):
    """
    Same shape as /api/games/{game_pk}/hits, but for HR ("at least 1
    home run" - always a fixed threshold, no adjustable line, matching
    core.py's own hr_probability_for_row: Player model's HR formulas
    never read an adjustable threshold cell the way Hits does).
    """
    from models_db import LineupBatter
    import hr_stats_sync
    import hrr_stats_sync
    import platoon_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    # Computed ONCE per request, not once per batter - see hrr_stats_sync's
    # docstrings for why (a cold cache is otherwise a real timeout risk).
    la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]

    def batters_for_side(side: str, opposing_pitcher_id):
        # Same "once per side, not once per batter" reasoning - all 9
        # batters on a side share the same opposing pitcher's hand.
        pitcher_hand = platoon_stats_sync.get_pitcher_hand(opposing_pitcher_id, "") if opposing_pitcher_id else None
        rows = (
            db.query(LineupBatter)
            .filter_by(game_pk=game_pk, team_side=side)
            .order_by(LineupBatter.batting_order)
            .all()
        )
        out = []
        for r in rows:
            inputs = hr_stats_sync.compute_hr_inputs(
                r.batter_id, r.batting_order, opposing_pitcher_id,
                home_team=game.home_team, la_hr_rate=la_hr_rate, pitcher_hand=pitcher_hand,
            )
            out.append({
                "batter_id": r.batter_id,
                "batter_name": r.batter_name,
                "batting_order": r.batting_order,
                "bat_side": platoon_stats_sync.get_batter_hand(r.batter_id, r.batter_name),
                "n_ab": inputs["n_ab"] if inputs else None,
                "p": inputs["p"] if inputs else None,
                "hr_index": inputs["hr_index"] if inputs else None,
                "used_platoon": inputs["used_platoon"] if inputs else False,
            })
        return out

    return {
        "game_pk": game_pk,
        "home_lineup_confirmed": game.home_lineup_confirmed,
        "away_lineup_confirmed": game.away_lineup_confirmed,
        "away_opp_pitcher_name": game.home_probable_pitcher,
        "away_opp_pitcher_hand": platoon_stats_sync.get_pitcher_hand(game.home_probable_pitcher_id, game.home_probable_pitcher or ""),
        "away_opp_pitcher_hr_index": hr_stats_sync.get_pitcher_hr_index(game.home_probable_pitcher_id, la_hr_rate=la_hr_rate),
        "home_opp_pitcher_name": game.away_probable_pitcher,
        "home_opp_pitcher_hand": platoon_stats_sync.get_pitcher_hand(game.away_probable_pitcher_id, game.away_probable_pitcher or ""),
        "home_opp_pitcher_hr_index": hr_stats_sync.get_pitcher_hr_index(game.away_probable_pitcher_id, la_hr_rate=la_hr_rate),
        "home_batters": batters_for_side("home", game.away_probable_pitcher_id),
        "away_batters": batters_for_side("away", game.home_probable_pitcher_id),
    }


@app.get("/api/games/{game_pk}/pitcher-k")
def game_pitcher_k(game_pk: int, db: Session = Depends(get_db)):
    """
    Pitcher K prop - structurally different from Hits/HRR/HR: only 2
    rows (the two starting pitchers), not one per batter, since a
    strikeout total is a per-PITCHER stat. Each pitcher's probability
    depends on the OPPOSING lineup being confirmed (the genuinely
    lineup-specific batter blend - see pitcher_k_sync.py), not their
    own team's.
    """
    import pitcher_k_sync
    import platoon_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    la_b13 = pitcher_k_sync.get_league_k_rate()

    def pitcher_row(pitcher_id, pitcher_name, batting_team_side):
        inputs = pitcher_k_sync.compute_pitcher_k_inputs(pitcher_id, game_pk, batting_team_side, la_b13=la_b13) \
            if pitcher_id else None
        return {
            "pitcher_id": pitcher_id,
            "pitcher_name": pitcher_name,
            "pitcher_hand": platoon_stats_sync.get_pitcher_hand(pitcher_id, pitcher_name or "") if pitcher_id else None,
            "mean": inputs["mean"] if inputs else None,
            "sd": inputs["sd"] if inputs else None,
            "pitcher_k_index": inputs["pitcher_k_index"] if inputs else None,
            "opposing_lineup_k_index": inputs["opposing_lineup_k_index"] if inputs else None,
        }

    return {
        "game_pk": game_pk,
        "home_lineup_confirmed": game.home_lineup_confirmed,
        "away_lineup_confirmed": game.away_lineup_confirmed,
        # The home pitcher faces the AWAY lineup, and vice versa.
        "home_pitcher": pitcher_row(game.home_probable_pitcher_id, game.home_probable_pitcher, "away"),
        "away_pitcher": pitcher_row(game.away_probable_pitcher_id, game.away_probable_pitcher, "home"),
    }


@app.get("/api/games/{game_pk}/pitcher-hits-allowed")
def game_pitcher_hits_allowed(game_pk: int, db: Session = Depends(get_db)):
    """
    Pitcher Hits Allowed prop - same 2-row structure as Pitcher K (one
    per starting pitcher, not one per batter), since this is also a
    per-PITCHER stat. Needs no new sync trigger of its own - reuses
    PitcherHitsStat/PitcherKStat/BatterSeasonStat, all already synced
    at lineup confirmation for other props. See
    pitcher_hits_allowed_sync.py for the full formula.
    """
    import pitcher_hits_allowed_sync
    import hits_stats_sync
    import platoon_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    la_b9 = hits_stats_sync.get_league_average_hit_rate()

    def pitcher_row(pitcher_id, pitcher_name, batting_team_side):
        inputs = pitcher_hits_allowed_sync.compute_pitcher_hits_allowed_inputs(pitcher_id, game_pk, batting_team_side, la_b9=la_b9) \
            if pitcher_id else None
        return {
            "pitcher_id": pitcher_id,
            "pitcher_name": pitcher_name,
            "pitcher_hand": platoon_stats_sync.get_pitcher_hand(pitcher_id, pitcher_name or "") if pitcher_id else None,
            "mean": inputs["mean"] if inputs else None,
            "pitcher_hits_allowed_index": inputs["pitcher_hits_allowed_index"] if inputs else None,
            "opposing_lineup_hit_index": inputs["opposing_lineup_hit_index"] if inputs else None,
        }

    return {
        "game_pk": game_pk,
        "home_lineup_confirmed": game.home_lineup_confirmed,
        "away_lineup_confirmed": game.away_lineup_confirmed,
        "home_pitcher": pitcher_row(game.home_probable_pitcher_id, game.home_probable_pitcher, "away"),
        "away_pitcher": pitcher_row(game.away_probable_pitcher_id, game.away_probable_pitcher, "home"),
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
    Every tracked bet, both still-pending and already-graded (see
    bet_grading.py), with just enough game context (matchup, date) to
    identify them at a glance in the Bet Tracker tab. Most-recently-
    tracked first. Resolved bets include their actual outcome/result so
    a win/loss doesn't just silently vanish from this list once graded.
    """
    from models_db import TrackedBet
    rows = (
        db.query(TrackedBet)
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
            "resolved": r.resolved,
            "actual_value": r.actual_value,
            "result": r.result,
        })
    return out


@app.get("/api/admin/grade-bets")
def manual_grade_bets():
    """
    Manually grades every tracked bet whose game has finished, right
    now, instead of waiting for the scheduled end-of-day run. Safe to
    hit any time and any number of times - already-resolved bets are
    never re-graded, and a bet whose game isn't final yet (or whose
    per-game stats aren't available yet) is simply left pending.
    """
    import bet_grading
    return bet_grading.grade_pending_bets()


@app.get("/api/debug/pitcher-k-inputs/{pitcher_id}")
def debug_pitcher_k_inputs(pitcher_id: int, game_pk: int, batting_team_side: str, db: Session = Depends(get_db)):
    """
    Diagnostic: shows every raw value feeding into a Pitcher K
    probability - the stored season stats, the derived med_ip/n, the
    league rate, and the final mean/sd - so an implausible result (e.g.
    a probability rounding to 100%) can be traced to its actual cause
    (a genuine small-sample data quirk vs. a real bug) instead of
    guessed at. batting_team_side: the side this pitcher FACES (e.g.
    "away" if he's the home starter).
    """
    from models_db import PitcherKStat
    import pitcher_k_sync

    pitcher = db.get(PitcherKStat, pitcher_id)
    if pitcher is None:
        return {"error": f"No PitcherKStat row for pitcher_id {pitcher_id} - stats haven't synced for them yet"}

    la_b13 = pitcher_k_sync.get_league_k_rate()
    team_factor, batters_with_data = pitcher_k_sync._lineup_k_factor(db, game_pk, batting_team_side, la_b13)

    raw = {
        "pitcher_id": pitcher_id,
        "pitcher_name": pitcher.pitcher_name,
        "strikeouts": pitcher.strikeouts,
        "batters_faced": pitcher.batters_faced,
        "games_started": pitcher.games_started,
        "outs": pitcher.outs,
        "meets_min_batters_faced_50": pitcher.batters_faced >= pitcher_k_sync.MIN_PITCHER_BATTERS_FACED,
    }

    med_ip = (pitcher.outs / 3) / pitcher.games_started if pitcher.games_started > 0 else 5
    n = med_ip * 4.3

    derived = {
        "la_b13_league_k_rate": la_b13,
        "med_ip_derived": med_ip,
        "n_batters_faced_per_start": n,
        "opposing_lineup_team_factor": team_factor,
        "opposing_lineup_batters_with_data": batters_with_data,
        "meets_min_lineup_batters_5": batters_with_data >= pitcher_k_sync.MIN_LINEUP_BATTERS_WITH_DATA,
    }

    inputs = pitcher_k_sync.compute_pitcher_k_inputs(pitcher_id, game_pk, batting_team_side, la_b13=la_b13)

    return {"raw_stats": raw, "derived_values": derived, "final_result": inputs}


@app.get("/api/debug/hrr-inputs/{game_pk}")
def debug_hrr_inputs(game_pk: int, db: Session = Depends(get_db)):
    """
    Diagnostic: shows the RAW values feeding into compute_hrr_inputs'
    gating condition for every confirmed batter in a game, plus the
    three league rates - so an "N/A" can be traced to the exact failing
    condition (insufficient batter AB, insufficient pitcher outs, a
    zero league rate, or a missing pitcher row entirely) instead of
    guessed at.
    """
    from models_db import LineupBatter, BatterSeasonStat, PitcherHitsStat
    import hits_stats_sync
    import hrr_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    la_b9 = hits_stats_sync.get_league_average_hit_rate()
    hrr_rates = hrr_stats_sync.get_league_hrr_rates()

    def side_debug(side: str, opposing_pitcher_id):
        pitcher = db.get(PitcherHitsStat, opposing_pitcher_id) if opposing_pitcher_id else None
        rows = db.query(LineupBatter).filter_by(game_pk=game_pk, team_side=side).order_by(LineupBatter.batting_order).all()
        batters_debug = []
        for r in rows:
            batter = db.get(BatterSeasonStat, r.batter_id)
            batters_debug.append({
                "batter_name": r.batter_name,
                "batter_row_exists": batter is not None,
                "batter_ab": batter.ab if batter else None,
                "batter_ab_meets_min_20": (batter.ab >= 20) if batter else False,
            })
        return {
            "opposing_pitcher_id": opposing_pitcher_id,
            "pitcher_row_exists": pitcher is not None,
            "pitcher_outs": pitcher.outs if pitcher else None,
            "pitcher_outs_meets_min_30": (pitcher.outs >= 30) if pitcher else False,
            "batters": batters_debug,
        }

    return {
        "game_pk": game_pk,
        "la_b9_hit_rate": la_b9,
        "hrr_rates": hrr_rates,
        "gate_requires_all_positive": {
            "la_b9_positive": la_b9 > 0,
            "obp_positive": hrr_rates["obp"] > 0,
            "hr_rate_positive": hrr_rates["hr_rate"] > 0,
        },
        "home": side_debug("home", game.away_probable_pitcher_id),
        "away": side_debug("away", game.home_probable_pitcher_id),
    }


@app.get("/api/debug/platoon-split/{batter_id}")
def debug_platoon_split(batter_id: int):
    """
    Diagnostic: shows the RAW vs-L/vs-R split response MLB's API
    actually returns for one batter, plus the computed factors - so
    this untested-against-live-data endpoint (see platoon_stats_sync.py's
    module docstring) can be verified before it's trusted in any
    formula's actual math. Pass any batter_id already visible in a
    /hits or /hrr response for this game.
    """
    import mlb_client
    import hits_stats_sync
    import hrr_stats_sync
    from datetime import datetime

    season = datetime.utcnow().year
    la_b9 = hits_stats_sync.get_league_average_hit_rate()
    la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]

    vl = mlb_client.get_batter_platoon_split(batter_id, season, "vl")
    vr = mlb_client.get_batter_platoon_split(batter_id, season, "vr")

    import platoon_stats_sync
    return {
        "batter_id": batter_id,
        "season": season,
        "la_b9_hit_rate": la_b9,
        "la_hr_rate": la_hr_rate,
        "raw_vs_l": vl,
        "raw_vs_r": vr,
        "computed_factors": {
            "hits_factor_vs_l": platoon_stats_sync._factor(vl, "hits", la_b9),
            "hits_factor_vs_r": platoon_stats_sync._factor(vr, "hits", la_b9),
            "hr_factor_vs_l": platoon_stats_sync._factor(vl, "homeRuns", la_hr_rate),
            "hr_factor_vs_r": platoon_stats_sync._factor(vr, "homeRuns", la_hr_rate),
        },
    }


@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
