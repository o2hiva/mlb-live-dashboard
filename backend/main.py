import os
import logging
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI, Depends, HTTPException, Request
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
_ensure_column("tracked_bets", "external_player_id", "VARCHAR")
_ensure_column("batter_season_stats", "hr", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "bb", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "strikeouts", "INTEGER DEFAULT 0")
_ensure_column("batter_season_stats", "plate_appearances", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "hr_allowed", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "bb_allowed", "INTEGER DEFAULT 0")
_ensure_column("pitcher_hits_stats", "runs_allowed", "INTEGER DEFAULT 0")
_ensure_column("batter_platoon_splits", "bat_side", "VARCHAR")
_ensure_column("batter_platoon_splits", "bat_side_updated_at", "TIMESTAMP")
_ensure_column("tracked_bets", "cfb_season", "INTEGER")
_ensure_column("tracked_bets", "cfb_week", "INTEGER")
_ensure_column("tracked_bets", "nfl_season", "INTEGER")
_ensure_column("tracked_bets", "nfl_week", "INTEGER")
_ensure_column("nfl_qb_defense_prop_stats", "passing_completions_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_qb_defense_prop_stats", "passing_attempts_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_qb_defense_prop_stats", "rushing_attempts_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_defense_allowed_prop_stats", "passing_completions_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_defense_allowed_prop_stats", "passing_attempts_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_defense_allowed_prop_stats", "rushing_attempts_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_qb_defense_prop_games", "gameday", "VARCHAR")
_ensure_column("nfl_rb_defense_prop_games", "gameday", "VARCHAR")
_ensure_column("nfl_points_games", "gameday", "VARCHAR")
_ensure_column("cfb_games", "start_date_utc", "VARCHAR")
_ensure_column("cfb_games", "neutral_site", "BOOLEAN DEFAULT FALSE")
_ensure_column("nfl_rb_defense_prop_stats", "carries_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_rb_defense_prop_stats", "receptions_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_rb_defense_allowed_prop_stats", "carries_sum", "INTEGER DEFAULT 0")
_ensure_column("nfl_rb_defense_allowed_prop_stats", "receptions_sum", "INTEGER DEFAULT 0")
_ensure_column("npb_games", "start_time_jst", "VARCHAR")
_ensure_column("games", "home_moneyline_win_prob", "FLOAT")
_ensure_column("games", "away_moneyline_win_prob", "FLOAT")
_ensure_column("games", "moneyline_model_version", "VARCHAR")

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
            # Full Game moneyline - see full_game_lines_sync.py. Recomputed
            # every poll cycle up to first pitch, then frozen (poller.py),
            # same "compute pregame, keep for tracking/grading afterward"
            # pattern as first_inning_run above.
            "home_moneyline_win_prob": g.home_moneyline_win_prob,
            "away_moneyline_win_prob": g.away_moneyline_win_prob,
            "moneyline_model_version": g.moneyline_model_version,
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


@app.get("/api/admin/backfill-runs5inn")
def manual_backfill_runs5inn():
    """
    ONE-TIME fix for the Game Lines "not enough data" bug (traced via
    /api/debug/game-lines-inputs/{game_pk}): TeamRuns5InnStat was only
    accumulating from the day the Game Lines feature shipped onward,
    not the real season before it, because it was added to
    inning_stats_sync's daily job after that job's shared sync-date
    checkpoint had already advanced through most of the season. Replays
    the full season directly into TeamRuns5InnStat only - see
    inning_stats_sync.backfill_runs5inn's own docstring for the full
    story. Safe to re-run (always rebuilds TeamRuns5InnStat from
    scratch to the same correct totals, never doubles up). Takes a
    while (replays the whole season day by day) - the response only
    arrives once it's done.
    """
    import inning_stats_sync
    summary = inning_stats_sync.backfill_runs5inn()
    return {"status": "backfill complete", **summary}


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


def _build_hits_response(db, game):
    """Shared by /api/games/{game_pk}/hits and the /api/bybets-data
    aggregator - same logic, callable for one game at a time either way."""
    from models_db import LineupBatter
    import hits_stats_sync
    import platoon_stats_sync

    game_pk = game.game_pk

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
    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}
    return _build_hits_response(db, game)


def _build_hrr_response(db, game, la_b9, hrr_rates):
    """Shared by /api/games/{game_pk}/hrr and the /api/bybets-data
    aggregator. la_b9/hrr_rates: caller's already-computed shared
    values - the aggregator computes these ONCE for every game, not
    once per game, avoiding the same redundant-recomputation class of
    bug already fixed for Pitcher Hits Allowed's baseline."""
    from models_db import LineupBatter
    import hrr_stats_sync
    import platoon_stats_sync

    game_pk = game.game_pk

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


@app.get("/api/games/{game_pk}/hrr")
def game_hrr(game_pk: int, db: Session = Depends(get_db)):
    """
    Same shape as /api/games/{game_pk}/hits, but for the HRR (Hits+Runs+RBI)
    market: each batter gets Negative Binomial parameters (r, p) instead
    of (n_ab, p) - the frontend computes "P(HRR >= line)" for any line
    instantly via a Negative Binomial survival function, the same
    "compute once, adjust instantly client-side" pattern Hits uses.
    """
    import hrr_stats_sync
    import hits_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    # Computed ONCE per request, not once per batter - each of these can
    # be a real MLB API round-trip on a cold cache (dozens of calls), so
    # doing this per-batter instead (9-18 times) risks the whole request
    # timing out. See hrr_stats_sync's docstrings for the same note.
    la_b9 = hits_stats_sync.get_league_average_hit_rate()
    hrr_rates = hrr_stats_sync.get_league_hrr_rates()
    return _build_hrr_response(db, game, la_b9, hrr_rates)


def _build_hr_response(db, game, la_hr_rate):
    """Shared by /api/games/{game_pk}/hr and the /api/bybets-data
    aggregator. la_hr_rate: caller's already-computed shared value."""
    from models_db import LineupBatter
    import hr_stats_sync
    import platoon_stats_sync

    game_pk = game.game_pk

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


@app.get("/api/games/{game_pk}/hr")
def game_hr(game_pk: int, db: Session = Depends(get_db)):
    """
    Same shape as /api/games/{game_pk}/hits, but for HR ("at least 1
    home run" - always a fixed threshold, no adjustable line, matching
    core.py's own hr_probability_for_row: Player model's HR formulas
    never read an adjustable threshold cell the way Hits does).
    """
    import hrr_stats_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    # Computed ONCE per request, not once per batter - see hrr_stats_sync's
    # docstrings for why (a cold cache is otherwise a real timeout risk).
    la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]
    return _build_hr_response(db, game, la_hr_rate)


def _build_pitcher_k_response(db, game, la_b13):
    """Shared by /api/games/{game_pk}/pitcher-k and the /api/bybets-data
    aggregator. la_b13: caller's already-computed shared value."""
    import pitcher_k_sync
    import platoon_stats_sync

    game_pk = game.game_pk

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

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    la_b13 = pitcher_k_sync.get_league_k_rate()
    return _build_pitcher_k_response(db, game, la_b13)


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
    league_pitcher_hit_rate, league_avg_hits_per_start = pitcher_hits_allowed_sync.get_hybrid_baselines(db)

    def pitcher_row(pitcher_id, pitcher_name, batting_team_side):
        inputs = pitcher_hits_allowed_sync.compute_pitcher_hits_allowed_inputs(
            pitcher_id, game_pk, batting_team_side, la_b9=la_b9,
            league_pitcher_hit_rate=league_pitcher_hit_rate, league_avg_hits_per_start=league_avg_hits_per_start,
        ) if pitcher_id else None
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


@app.get("/api/games/{game_pk}/game-lines")
def game_lines(game_pk: int, db: Session = Depends(get_db)):
    """
    Game Lines prop - each team's own "at least N runs in the first 5
    innings" probability, plus the combined-total O/U. Game-level (2
    team rows + 1 combined row), not per-batter/pitcher. Needs no new
    sync trigger for the CALLER - reuses PitcherHitsStat (already
    synced at lineup confirmation) and TeamRuns5InnStat (populated by
    the existing daily inning_stats_sync.py job). See
    game_lines_sync.py for the full formula and its honest limitations
    (the per-team split specifically is a disclosed extension of the
    validated combined-total formula, not independently backtested).
    """
    import game_lines_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    league_avg_runs5inn = game_lines_sync.get_league_runs5inn_baseline(db)
    inputs = game_lines_sync.compute_game_lines_inputs(
        game.home_team, game.away_team, game.home_probable_pitcher_id, game.away_probable_pitcher_id,
        league_avg_runs5inn=league_avg_runs5inn,
    )

    return {
        "game_pk": game_pk,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "away_mean": inputs["away_mean"] if inputs else None,
        "home_mean": inputs["home_mean"] if inputs else None,
        "combined_mean": inputs["combined_mean"] if inputs else None,
    }


@app.get("/api/games/{game_pk}/f5-lines")
def f5_lines(game_pk: int, db: Session = Depends(get_db)):
    """
    1st 5 Innings (F5) Game Lines prop - each team's predicted F5 runs
    (feeding a client-side O/U probability for any line, same pattern as
    /api/games/{game_pk}/game-lines), the combined F5 total mean, and F5
    moneyline win probabilities. Game-level (2 team rows + 1 combined
    total row + moneyline), not per-batter/pitcher. Needs no new sync
    trigger for the caller - reuses PitcherHitsStat and TeamRuns5InnStat,
    same as game_lines_sync.py. See f5_lines_sync.py for the full
    formula, its validation summary, and honest limitations.
    """
    import f5_lines_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    inputs = f5_lines_sync.compute_f5_lines_inputs(
        game.home_team, game.away_team, game.home_probable_pitcher_id, game.away_probable_pitcher_id,
    )

    return {
        "game_pk": game_pk,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "away_mean": inputs["away_mean"] if inputs else None,
        "home_mean": inputs["home_mean"] if inputs else None,
        "combined_mean": inputs["combined_mean"] if inputs else None,
        "home_win_prob_5inn": inputs["home_win_prob_5inn"] if inputs else None,
        "away_win_prob_5inn": inputs["away_win_prob_5inn"] if inputs else None,
        "model_version": inputs["model_version"] if inputs else None,
    }


@app.get("/api/games/{game_pk}/moneyline")
def full_game_moneyline(game_pk: int, db: Session = Depends(get_db)):
    """
    On-demand recompute of the validated Full Game moneyline for one game
    (see full_game_lines_sync.py's module docstring for the formula and
    its 2-season backtest validation) - mainly useful for spot-checking a
    specific matchup. The main games list (/api/games/today) doesn't call
    this; it reads the already-computed Game.home_moneyline_win_prob/
    away_moneyline_win_prob columns instead (kept fresh every poll cycle
    up to first pitch - see poller.py), so this endpoint recomputing from
    scratch should normally agree with what's already stored.

    NOTE: intentionally returns win probabilities only - no totals/spread
    fields. That side of the model is explicitly NOT validated yet, see
    full_game_lines_sync.py.
    """
    import full_game_lines_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    result = full_game_lines_sync.compute_moneyline(
        game.home_team, game.away_team, game.home_probable_pitcher_id, game.away_probable_pitcher_id,
    )

    return {
        "game_pk": game_pk,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "home_win_prob": result["home_win_prob"] if result else None,
        "away_win_prob": result["away_win_prob"] if result else None,
        "model_version": result["model_version"] if result else None,
    }


@app.get("/api/admin/refresh-full-game-lines-stats")
def manual_refresh_full_game_lines_stats():
    """
    Manually triggers full_game_lines_sync's incremental daily update
    right now (checks the last few days for real FINAL games not yet
    folded into TeamRunsFullGameStat/TeamBullpenStat) instead of waiting
    for its scheduled run - see poller.py. Safe to run any time and more
    than once (the TeamBullpenCollectedGame ledger skips anything
    already folded).
    """
    import full_game_lines_sync
    summary = full_game_lines_sync.daily_update()
    return {"status": "daily update complete", **summary}


@app.get("/api/admin/backfill-full-game-lines")
def manual_backfill_full_game_lines(max_games: int = 300):
    """
    Full-season backfill for TeamRunsFullGameStat/TeamBullpenStat - ONE
    real boxscore fetch per game (unlike the day-granularity Game Lines
    backfill), so a full season (2000+ games) can't safely run in a
    single call. Each call folds up to `max_games` NEW games and picks
    up next time exactly where it left off (see
    full_game_lines_sync.backfill's own docstring). Call this
    repeatedly - e.g. hit it ~7-8 times in a row for a full season at
    the default max_games=300 - until the response says "finished": true.
    """
    import full_game_lines_sync
    summary = full_game_lines_sync.backfill(max_games=max_games)
    return {"status": "backfill step complete", **summary}


@app.get("/api/debug/game-lines-inputs/{game_pk}")
def debug_game_lines_inputs(game_pk: int, db: Session = Depends(get_db)):
    """
    Diagnostic: shows every raw value compute_game_lines_inputs() checks
    for this game, and exactly which gate (if any) is failing - same
    "trace it, don't guess" pattern as
    /api/debug/pitcher-hits-allowed-inputs. A None mean on the real
    /game-lines endpoint always traces back to one of the four rows or
    gates shown here.
    """
    from models_db import PitcherHitsStat, TeamRuns5InnStat
    import game_lines_sync

    game = db.get(Game, game_pk)
    if game is None:
        return {"error": "not found"}

    away_p = db.get(PitcherHitsStat, game.away_probable_pitcher_id) if game.away_probable_pitcher_id else None
    home_p = db.get(PitcherHitsStat, game.home_probable_pitcher_id) if game.home_probable_pitcher_id else None
    away_t = db.get(TeamRuns5InnStat, game.away_team)
    home_t = db.get(TeamRuns5InnStat, game.home_team)

    def pitcher_view(label, pid, row):
        if pid is None:
            return {"label": label, "pitcher_id": None, "issue": "no probable_pitcher_id set on this Game row yet"}
        if row is None:
            return {"label": label, "pitcher_id": pid, "issue": "no PitcherHitsStat row for this pitcher_id at all"}
        return {
            "label": label, "pitcher_id": pid, "pitcher_name": row.pitcher_name,
            "outs": row.outs, "runs_allowed": row.runs_allowed,
            "meets_min_outs_45": row.outs >= game_lines_sync.MIN_PITCHER_OUTS,
        }

    def team_view(label, name, row):
        if row is None:
            return {"label": label, "team_name": name, "issue": "no TeamRuns5InnStat row for this exact team_name string"}
        return {
            "label": label, "team_name": name, "games": row.games, "runs5inn": row.runs5inn,
            "meets_min_games_10": row.games >= game_lines_sync.MIN_TEAM_GAMES,
        }

    return {
        "game_pk": game_pk,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "home_probable_pitcher_id": game.home_probable_pitcher_id,
        "away_probable_pitcher_id": game.away_probable_pitcher_id,
        "away_pitcher": pitcher_view("away_pitcher (faced by home batters)", game.away_probable_pitcher_id, away_p),
        "home_pitcher": pitcher_view("home_pitcher (faced by away batters)", game.home_probable_pitcher_id, home_p),
        "away_team_runs5inn": team_view("away_team", game.away_team, away_t),
        "home_team_runs5inn": team_view("home_team", game.home_team, home_t),
    }


@app.get("/api/admin/refresh-nfl-stats")
def manual_refresh_nfl_stats(season: int, week: int):
    """
    Manually triggers the NFL passing-yards sync for a given
    season/current-week. No auto-detection of "the current NFL week"
    exists (byes, Thursday/Monday games make that fragile to guess
    reliably) - specify it explicitly, same safe pattern as every
    other manual admin trigger in this dashboard. Compares every QB's
    current season-cumulative total against their last stored snapshot
    and folds the difference into NflQbStat/NflTeamAllowedStat (see
    nfl_passing_yards_sync.py's module docstring for why - the
    original per-week fetch approach doesn't work against this API),
    then refreshes NflGame with the given week's real matchups.
    """
    import nfl_passing_yards_sync
    nfl_passing_yards_sync.refresh_nfl_stats(season, week)
    return {"status": "refreshed", "season": season, "week": week}


class NflManualStatEntry(BaseModel):
    gsis_id: str
    name: str
    team: str
    week: int
    attempts: int
    yards: int


class NflManualBackfillRequest(BaseModel):
    season: int
    entries: list[NflManualStatEntry]


@app.post("/api/admin/nfl-manual-backfill")
def nfl_manual_backfill(payload: NflManualBackfillRequest, db: Session = Depends(get_db)):
    """
    One-time manual backfill for weeks the automated snapshot-delta
    sync can never retroactively reconstruct (see
    nfl_passing_yards_sync.py's module docstring's honest limitation -
    there's no earlier baseline to diff against for weeks before the
    first real snapshot). Lets the person supply real per-game stats
    for those already-played weeks directly (easily looked up from any
    public NFL stats source), folded into the SAME running totals the
    automated sync uses - this only touches NflQbStat/
    NflTeamAllowedStat, never NflQbSnapshot, so it doesn't interfere
    with the ongoing automated delta computation going forward.

    NOT IDEMPOTENT, unlike the automated sync - submitting the same
    entry twice will double-count it. This is a deliberate one-time
    admin action, not something meant to be re-run repeatedly.
    """
    from models_db import NflQbStat, NflTeamAllowedStat
    import nfl_passing_yards_sync

    schedule_cache = {}
    results = []

    for entry in payload.entries:
        if entry.week not in schedule_cache:
            try:
                schedule_cache[entry.week] = nfl_passing_yards_sync.get_week_games(payload.season, entry.week)
            except Exception as e:
                results.append({"gsis_id": entry.gsis_id, "week": entry.week, "status": "failed", "reason": f"couldn't fetch week {entry.week} schedule: {e}"})
                continue

        opponent = schedule_cache[entry.week].get(entry.team)
        if opponent is None:
            results.append({"gsis_id": entry.gsis_id, "week": entry.week, "status": "failed", "reason": f"no opponent found for {entry.team} in week {entry.week}"})
            continue

        qb_stat = db.get(NflQbStat, entry.gsis_id)
        if qb_stat is None:
            qb_stat = NflQbStat(gsis_id=entry.gsis_id, name=entry.name, team=entry.team, total_yards=0, games=0, last_game_week=0)
            db.add(qb_stat)
        qb_stat.name = entry.name
        qb_stat.team = entry.team
        qb_stat.total_yards += entry.yards
        qb_stat.games += 1
        qb_stat.last_game_week = max(qb_stat.last_game_week, entry.week)

        opp_stat = db.get(NflTeamAllowedStat, opponent)
        if opp_stat is None:
            opp_stat = NflTeamAllowedStat(team=opponent, total_yards_allowed=0, games=0)
            db.add(opp_stat)
        opp_stat.total_yards_allowed += entry.yards
        opp_stat.games += 1

        results.append({"gsis_id": entry.gsis_id, "week": entry.week, "status": "added", "opponent": opponent, "yards": entry.yards})

    db.commit()
    nfl_passing_yards_sync._league_avg_cache = None
    nfl_passing_yards_sync._league_avg_cache_time = None

    return {"results": results}


@app.get("/api/debug/nfl-raw")
def debug_nfl_raw(season: int, week: int, db: Session = Depends(get_db)):
    """
    Diagnostic for the estimation-based sync (see
    nfl_passing_yards_sync.py's module docstring for the full story:
    no working per-game breakdown endpoint exists on this API, so the
    opponent-allowed side is estimated from each real opponent's own
    season average rather than reconstructed exactly). Shows current
    row counts, sample stored data, and live calls to the three
    confirmed-working fetch functions.
    """
    from models_db import NflQbStat, NflTeamAllowedStat, NflGame
    import nfl_passing_yards_sync

    result = {
        "stored_row_counts": {
            "NflQbStat": db.query(NflQbStat).count(),
            "NflTeamAllowedStat": db.query(NflTeamAllowedStat).count(),
            "NflGame": db.query(NflGame).count(),
        },
        "sample_qb_stat": None,
        "sample_team_allowed_stat": None,
    }
    qb_sample = db.query(NflQbStat).first()
    if qb_sample:
        result["sample_qb_stat"] = {"gsis_id": qb_sample.gsis_id, "name": qb_sample.name, "team": qb_sample.team,
                                     "total_yards": qb_sample.total_yards, "games": qb_sample.games}
    team_sample = db.query(NflTeamAllowedStat).first()
    if team_sample:
        result["sample_team_allowed_stat"] = {"team": team_sample.team, "total_yards_allowed": team_sample.total_yards_allowed, "games": team_sample.games}

    try:
        qbs = nfl_passing_yards_sync.get_season_qbs(season)
        result["get_season_qbs"] = {"count": len(qbs), "sample": qbs[:5]}
    except Exception as e:
        result["get_season_qbs"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        team_stats = nfl_passing_yards_sync.get_all_team_season_stats(season)
        result["get_all_team_season_stats"] = {"count": len(team_stats), "sample": dict(list(team_stats.items())[:5])}
    except Exception as e:
        result["get_all_team_season_stats"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        matchups = nfl_passing_yards_sync.get_week_games(season, week)
        result["get_week_games"] = {"count": len(matchups), "sample": dict(list(matchups.items())[:6])}
    except Exception as e:
        result["get_week_games"] = {"error": f"{type(e).__name__}: {e}"}

    return result


@app.get("/api/nfl/games")
def nfl_games(db: Session = Depends(get_db)):
    """
    Every team's real current-week matchup and passing-yards
    prediction for their most-recent starter, from whatever the last
    /api/admin/refresh-nfl-stats call populated. starter_is_heuristic
    is always true here (see nfl_passing_yards_sync.py's own honest
    limitation note) - the person should confirm or override the
    shown starter if they know about an injury or benching.
    """
    from models_db import NflGame
    import nfl_passing_yards_sync

    games = db.query(NflGame).all()
    if not games:
        return {"games": [], "season": None, "week": None}

    league_avg = nfl_passing_yards_sync.get_league_avg_allowed(db)

    rows = []
    for g in games:
        inputs = nfl_passing_yards_sync.compute_passing_yards_prediction(g.team, g.opponent, league_avg=league_avg)
        rows.append({
            "team": g.team,
            "opponent": g.opponent,
            "qb_name": inputs["qb_name"] if inputs else None,
            "predicted_mean": inputs["predicted_mean"] if inputs else None,
            "qb_index": inputs["qb_index"] if inputs else None,
            "opp_index": inputs["opp_index"] if inputs else None,
            "qb_games_sample": inputs["qb_games_sample"] if inputs else None,
            "opp_games_sample": inputs["opp_games_sample"] if inputs else None,
            "starter_is_heuristic": True,
        })

    return {"games": rows, "season": games[0].season, "week": games[0].week}


@app.get("/api/admin/refresh-nfl-rushing-stats")
def manual_refresh_nfl_rushing_stats(season: int, week: int):
    """
    Manually triggers the NFL Rushing Yards sync for a given
    season/current-week - same "no auto-detection of the current week"
    reasoning as every other NFL admin trigger. Rebuilds every
    qualifying RB's real season-to-date carries/yards/games from
    api.nfldata.org directly (exact, no estimation needed - see
    nfl_rushing_yards_sync.py's module docstring), then refreshes this
    week's real matchups for display.
    """
    import nfl_rushing_yards_sync
    summary = nfl_rushing_yards_sync.refresh_nfl_rushing_stats(season, week)
    return {"status": "refreshed", "season": season, "week": week, **summary}


@app.get("/api/nfl/rushing-games")
def nfl_rushing_games(db: Session = Depends(get_db)):
    """
    Every qualifying RB's real season-to-date rushing average and this
    week's real opponent, from whatever the last
    /api/admin/refresh-nfl-rushing-stats call populated. Unlike
    /api/nfl/games (one row per team, guessing a single starter), this
    returns one row per RB - a committee backfield naturally shows up
    as multiple rows for the same team, by design (see
    nfl_rushing_yards_sync.py's module docstring). An RB whose team
    isn't in this week's schedule (bye week) is omitted, not guessed at.
    """
    from models_db import NflRbStat, NflRbGame
    import nfl_rushing_yards_sync

    rbs = db.query(NflRbStat).filter(NflRbStat.games >= nfl_rushing_yards_sync.MIN_PRIOR_GAMES).all()
    if not rbs:
        return {"rbs": [], "season": None, "week": None}

    schedule = {g.team: g for g in db.query(NflRbGame).all()}

    rows = []
    season = week = None
    for rb in rbs:
        game = schedule.get(rb.team)
        if game is None:
            continue  # bye week, or team not in this week's real schedule
        if season is None:
            season, week = game.season, game.week
        predicted_mean = (rb.total_yards / rb.games) if rb.games else None
        rows.append({
            "rb_name": rb.name,
            "gsis_id": rb.gsis_id,
            "team": rb.team,
            "opponent": game.opponent,
            "predicted_mean": predicted_mean,
            "games_sample": rb.games,
            "carries": rb.carries,
        })

    return {"rbs": rows, "season": season, "week": week}


@app.get("/api/admin/refresh-nfl-qb-defense-props-stats")
def manual_refresh_nfl_qb_defense_props_stats(season: int, week: int):
    """
    Manually triggers the NFL "By Position" QB props sync (passing
    yards, passing TDs, rushing yards, rushing TDs vs. each defense's
    QB-position-allowed history) for a given season/current-week - same
    "no auto-detection of the current week" reasoning as every other
    NFL admin trigger. See nfl_qb_defense_props_sync.py's module
    docstring for the estimation approach this uses.
    """
    import nfl_qb_defense_props_sync
    summary = nfl_qb_defense_props_sync.refresh_nfl_qb_defense_props_stats(season, week)
    return {"status": "refreshed", "season": season, "week": week, **summary}


@app.get("/api/nfl/qb-defense-props")
def nfl_qb_defense_props(db: Session = Depends(get_db)):
    """
    Every team's real current-week matchup and predicted values for all
    four QB Defense-vs-Position props, from whatever the last
    /api/admin/refresh-nfl-qb-defense-props-stats call populated.
    starter_is_heuristic is always true (highest-attempts QB on record
    for that team - see nfl_qb_defense_props_sync.py's own note). A
    team can have some props populated and others null depending on
    real games played so far (MIN_PRIOR_GAMES == 3, not lowered).
    """
    from models_db import NflQbDefensePropGame
    import nfl_qb_defense_props_sync

    games = db.query(NflQbDefensePropGame).all()
    if not games:
        return {"qbs": [], "season": None, "week": None}

    league_avgs = nfl_qb_defense_props_sync.get_league_avgs(db)

    rows = []
    for g in games:
        inputs = nfl_qb_defense_props_sync.compute_qb_defense_props_prediction(g.team, g.opponent, db, league_avgs=league_avgs)
        row = {
            "team": g.team,
            "opponent": g.opponent,
            "gameday": g.gameday,
            "qb_name": inputs["qb_name"] if inputs else None,
            "qb_gsis_id": inputs["qb_gsis_id"] if inputs else None,
            "qb_games_sample": inputs["qb_games_sample"] if inputs else None,
            "opp_games_sample": inputs["opp_games_sample"] if inputs else None,
            "starter_is_heuristic": True,
        }
        for prop in nfl_qb_defense_props_sync.PROP_FIELDS:
            row[f"{prop}_mean"] = inputs[f"{prop}_mean"] if inputs else None
            row[f"{prop}_qb_index"] = inputs[f"{prop}_qb_index"] if inputs else None
            row[f"{prop}_opp_index"] = inputs[f"{prop}_opp_index"] if inputs else None
        rows.append(row)

    return {"qbs": rows, "season": games[0].season, "week": games[0].week}


@app.get("/api/admin/refresh-nfl-rb-defense-props-stats")
def manual_refresh_nfl_rb_defense_props_stats(season: int, week: int):
    """
    Manually triggers the NFL "By Position" RB props sync (rushing
    yards, rushing TDs, receiving yards, receiving TDs, total yards,
    total TDs vs. each defense's RB-position-allowed history) for a
    given season/current-week - same pattern as the QB version. See
    nfl_rb_defense_props_sync.py's module docstring for the two-
    separate-league-averages estimation approach this uses.
    """
    import nfl_rb_defense_props_sync
    summary = nfl_rb_defense_props_sync.refresh_nfl_rb_defense_props_stats(season, week)
    return {"status": "refreshed", "season": season, "week": week, **summary}


@app.get("/api/nfl/rb-defense-props")
def nfl_rb_defense_props(db: Session = Depends(get_db)):
    """
    Every QUALIFYING RB's real current-week matchup and predicted
    values for all six RB Defense-vs-Position props, from whatever the
    last /api/admin/refresh-nfl-rb-defense-props-stats call populated.
    Unlike the QB version, this returns ONE ROW PER QUALIFYING RB, not
    one per team - committee backfields show up as more than one row
    for the same team/opponent matchup, same "every real back, not just
    a guessed starter" choice NFL Rushing Yards already made. A row can
    have some props populated and others null depending on real games
    played so far (MIN_PRIOR_GAMES == 3, not lowered).
    """
    from models_db import NflRbDefensePropGame, NflRbDefensePropStat
    import nfl_rb_defense_props_sync

    games = db.query(NflRbDefensePropGame).all()
    if not games:
        return {"rbs": [], "season": None, "week": None}

    league_avgs = nfl_rb_defense_props_sync.get_league_avgs(db)

    rows = []
    for g in games:
        rbs_on_team = (
            db.query(NflRbDefensePropStat)
            .filter_by(team=g.team)
            .order_by(NflRbDefensePropStat.touches.desc())
            .all()
        )
        for rb in rbs_on_team:
            inputs = nfl_rb_defense_props_sync.compute_rb_defense_props_prediction(rb, g.opponent, db, league_avgs=league_avgs)
            row = {
                "team": g.team,
                "opponent": g.opponent,
                "gameday": g.gameday,
                "rb_name": inputs["rb_name"],
                "rb_gsis_id": inputs["rb_gsis_id"],
                "rb_games_sample": inputs["rb_games_sample"],
                "opp_games_sample": inputs["opp_games_sample"],
            }
            for prop in nfl_rb_defense_props_sync.PROP_FIELDS:
                row[f"{prop}_mean"] = inputs[f"{prop}_mean"]
                row[f"{prop}_rb_index"] = inputs[f"{prop}_rb_index"]
                row[f"{prop}_opp_index"] = inputs[f"{prop}_opp_index"]
            rows.append(row)

    return {"rbs": rows, "season": games[0].season, "week": games[0].week}


@app.get("/api/admin/refresh-cfb-stats")
def manual_refresh_cfb_stats(season: int, week: int):
    """
    Manually triggers the CFB Team Points sync for a given season/
    current-week - same "no auto-detection of the current week"
    reasoning as the NFL refresh endpoint (byes and irregular schedules
    make that fragile to guess). Rebuilds every FBS team's real
    season-to-date scored/allowed totals from CFBD directly (weeks
    1..week), then refreshes this week's real matchups.
    """
    import cfb_points_sync
    summary = cfb_points_sync.refresh_cfb_points_stats(season, week)
    return {"status": "refreshed", "season": season, "week": week, **summary}


@app.get("/api/cfb/games")
def cfb_games(db: Session = Depends(get_db)):
    """
    Every team's real current-week matchup, predicted points, and
    moneyline win probability, from whatever the last
    /api/admin/refresh-cfb-stats call populated. Unlike NFL, this is
    exact (not an estimation) - CFBD's real per-game final scores feed
    the history directly.

    Moneyline (home_win_prob/away_win_prob via moneyline_win_prob below,
    see cfb_points_sync.compute_cfb_moneyline) is computed ONCE PER GAME
    (needs both sides' predictions together), not once per team-row, so
    games are grouped by game_id first. "moneyline_trusted": false means
    "no pick yet" - either side lacked enough history
    (MIN_PRIOR_GAMES) - and moneyline_win_prob is None in that case,
    NEVER 0%. The frontend must render that as "no pick yet", not 0%
    and not crash on the null.
    """
    from models_db import CfbGame
    import cfb_points_sync

    games = db.query(CfbGame).all()
    if not games:
        return {"games": [], "season": None, "week": None}

    league_avg = cfb_points_sync.get_league_avg_points(db)

    by_game: dict = {}
    for g in games:
        by_game.setdefault(g.game_id, {})["home" if g.is_home else "away"] = g

    moneyline_by_game: dict = {}
    for game_id, pair in by_game.items():
        home_g, away_g = pair.get("home"), pair.get("away")
        if home_g and away_g:
            moneyline_by_game[game_id] = cfb_points_sync.compute_cfb_moneyline(
                home_g.team, away_g.team, db, league_avg=league_avg,
                neutral_site=bool(home_g.neutral_site),
            )
        else:
            moneyline_by_game[game_id] = {"home_win_probability": None, "away_win_probability": None, "trusted": False}

    rows = []
    for g in games:
        inputs = cfb_points_sync.compute_cfb_team_points_prediction(g.team, g.opponent, db, league_avg=league_avg)
        ml = moneyline_by_game.get(g.game_id, {"home_win_probability": None, "away_win_probability": None, "trusted": False})
        own_win_prob = ml["home_win_probability"] if g.is_home else ml["away_win_probability"]
        rows.append({
            "team": g.team,
            "opponent": g.opponent,
            "game_id": g.game_id,
            "is_home": g.is_home,
            "start_date_utc": g.start_date_utc,
            "predicted_mean": inputs["mean"] if inputs else None,
            "team_index": inputs["team_index"] if inputs else None,
            "opp_index": inputs["opp_index"] if inputs else None,
            "team_games_sample": inputs["team_games_sample"] if inputs else None,
            "opp_games_sample": inputs["opp_games_sample"] if inputs else None,
            "moneyline_win_prob": own_win_prob,
            "moneyline_trusted": ml["trusted"],
        })

    return {"games": rows, "season": games[0].season, "week": games[0].week}


@app.get("/api/admin/refresh-nfl-points-stats")
def manual_refresh_nfl_points_stats(season: int, week: int):
    """
    Manually triggers the NFL Team Points sync for a given season/
    current-week - same "no auto-detection of the current week"
    reasoning as CFB's refresh endpoint (byes/irregular schedules make
    that fragile to guess). Rebuilds every team's real season-to-date
    scored/allowed totals from api.nfldata.org directly (weeks
    1..week), then refreshes this week's real matchups.
    """
    import nfl_points_sync
    summary = nfl_points_sync.refresh_nfl_points_stats(season, week)
    return {"status": "refreshed", "season": season, "week": week, **summary}


@app.get("/api/nfl/points-games")
def nfl_points_games(db: Session = Depends(get_db)):
    """
    Every team's real current-week matchup and predicted points, from
    whatever the last /api/admin/refresh-nfl-points-stats call
    populated. Mirrors /api/cfb/games's shape exactly (same frontend
    card-grouping code handles both), except NFL has no numeric
    per-game id from the API - so each row's "game_id" here is a
    synthetic sorted-team-pair string (e.g. "BUF|KC"), stable and
    identical for both sides of the same matchup either way it's
    computed, which is all the frontend's grouping-by-game_id needs.

    Moneyline (home_win_prob/away_win_prob via moneyline_win_prob below,
    see nfl_points_sync.compute_nfl_moneyline) is computed ONCE PER GAME
    (needs both sides' predictions together), not once per team-row, so
    rows are grouped by game_id first - same pattern as /api/cfb/games.
    "moneyline_trusted": false means "no pick yet" - either side lacked
    enough history (MIN_PRIOR_GAMES) - and moneyline_win_prob is None in
    that case, NEVER 0%. The frontend must render that as "no pick yet",
    not 0% and not crash on the null.
    """
    from models_db import NflPointsGame
    import nfl_points_sync

    games = db.query(NflPointsGame).all()
    if not games:
        return {"games": [], "season": None, "week": None}

    league_avg = nfl_points_sync.get_league_avg_points(db)

    by_game: dict = {}
    for g in games:
        by_game.setdefault("|".join(sorted([g.team, g.opponent])), {})["home" if g.is_home else "away"] = g

    moneyline_by_game: dict = {}
    for game_id, pair in by_game.items():
        home_g, away_g = pair.get("home"), pair.get("away")
        if home_g and away_g:
            moneyline_by_game[game_id] = nfl_points_sync.compute_nfl_moneyline(
                home_g.team, away_g.team, db, league_avg=league_avg,
            )
        else:
            moneyline_by_game[game_id] = {"home_win_probability": None, "away_win_probability": None, "trusted": False}

    rows = []
    for g in games:
        game_id = "|".join(sorted([g.team, g.opponent]))
        inputs = nfl_points_sync.compute_nfl_team_points_prediction(g.team, g.opponent, db, league_avg=league_avg)
        ml = moneyline_by_game.get(game_id, {"home_win_probability": None, "away_win_probability": None, "trusted": False})
        own_win_prob = ml["home_win_probability"] if g.is_home else ml["away_win_probability"]
        rows.append({
            "team": g.team,
            "opponent": g.opponent,
            "game_id": game_id,
            "is_home": g.is_home,
            "gameday": g.gameday,
            "predicted_mean": inputs["mean"] if inputs else None,
            "team_index": inputs["team_index"] if inputs else None,
            "opp_index": inputs["opp_index"] if inputs else None,
            "team_games_sample": inputs["team_games_sample"] if inputs else None,
            "opp_games_sample": inputs["opp_games_sample"] if inputs else None,
            "moneyline_win_prob": own_win_prob,
            "moneyline_trusted": ml["trusted"],
        })

    return {"games": rows, "season": games[0].season, "week": games[0].week}


@app.post("/api/admin/seed-npb-yrfi")
async def seed_npb_yrfi_endpoint(request: Request):
    """
    ONE-TIME bulk import of an existing npb_first_inning_progress.json
    (built locally by npb_backtest_first_inning.py) into the live
    Postgres tables - lets the NPB tab start with real season history
    instead of an empty slate. Safe to call more than once (already-
    collected games are skipped by game_id, same dedup as the daily
    incremental collector). POST the raw progress file's JSON body
    directly, e.g.:
        curl -X POST --data-binary @npb_first_inning_progress.json \\
             -H "Content-Type: application/json" \\
             https://sports-hub-live.up.railway.app/api/admin/seed-npb-yrfi
    """
    body = await request.json()
    import npb_yrfi_sync
    summary = npb_yrfi_sync.seed_from_progress(body)
    return {"status": "seeded", **summary}


@app.get("/api/admin/refresh-npb-yrfi-stats")
def manual_refresh_npb_yrfi_stats(days_back: int = 3, days_ahead: int = 1):
    """
    Manually triggers the NPB YRFI daily update - sweeps a small window
    of real dates (JST) around today, collecting any newly-finished
    games into the running team-scoring/pitcher-allowing totals and
    refreshing each swept date's real schedule for display. Safe to call
    any number of times. Also runs automatically once daily (see
    poller.py).
    """
    import npb_yrfi_sync
    summary = npb_yrfi_sync.daily_update(days_back=days_back, days_ahead=days_ahead)
    return {"status": "refreshed", **summary}


@app.get("/api/npb/yrfi-games")
def npb_yrfi_games(date: str | None = None, db: Session = Depends(get_db)):
    """
    One date's (YYYYMMDD, default: today JST) real NPB games with each
    one's pre-game YRFI/NRFI probability - presumed starters (see
    npb_yrfi_sync.py's module docstring for why NPB's API never exposes a
    confirmed starter ahead of time). No lineup data (not available for
    NPB, unlike MLB) - otherwise mirrors the MLB Live Games card shape.
    """
    from models_db import NpbGame
    import npb_yrfi_sync

    if date is None:
        date = npb_yrfi_sync._jst_today().strftime("%Y%m%d")

    games = db.query(NpbGame).filter_by(date=date).all()
    if not games:
        return {"games": [], "date": date}

    league_avg = npb_yrfi_sync.get_league_avg(db)

    rows = []
    for g in games:
        inputs = npb_yrfi_sync.compute_npb_yrfi_prediction(g.home_team, g.away_team, db, league_avg=league_avg)
        rows.append({
            "game_id": g.game_id,
            "date": g.date,
            # Real team names/status come back from spaia.jp in Japanese -
            # translated here for display; home_team/away_team stay the
            # ORIGINAL Japanese strings too (home_team_ja/away_team_ja)
            # since that's the exact primary-key value the rest of this
            # app (bet tracking, NpbTeamScoreStat/NpbPitcherAllowStat) is
            # keyed on.
            "home_team": npb_yrfi_sync.team_name_en(g.home_team),
            "away_team": npb_yrfi_sync.team_name_en(g.away_team),
            "home_team_ja": g.home_team,
            "away_team_ja": g.away_team,
            "status": npb_yrfi_sync.status_en(g.status),
            "start_time_pacific": npb_yrfi_sync.start_time_pacific(g.date, g.start_time_jst),
            "home_score": g.home_score,
            "away_score": g.away_score,
            "p_yrfi": inputs["p_yrfi"] if inputs else None,
            "home_starter_name": inputs["home_starter_name"] if inputs else None,
            "away_starter_name": inputs["away_starter_name"] if inputs else None,
            "starters_are_heuristic": inputs["starters_are_heuristic"] if inputs else None,
        })

    return {"games": rows, "date": date}


@app.get("/api/admin/refresh-nhl-goalie-saves-stats")
def manual_refresh_nhl_goalie_saves_stats(season: str, target_date: str | None = None):
    """
    Manually triggers the NHL Goalie Saves daily update for a given
    season (NHL's own 8-digit code, e.g. "20262027" for the 2026-27
    season). INCREMENTAL, not a full rebuild (see nhl_goalie_saves_sync.py's
    module docstring for why) - only fetches games not already collected,
    folds them into the running team-shots/goalie-saves totals, and
    refreshes target_date's (default: tomorrow, UTC) real matchups. Safe
    to call any number of times. Also runs automatically once daily (see
    poller.py), which fires once immediately on every deploy too.
    """
    import nhl_goalie_saves_sync
    summary = nhl_goalie_saves_sync.daily_update(season, target_date)
    return {"status": "refreshed", "season": season, **summary}


@app.get("/api/nhl/goalie-saves")
def nhl_goalie_saves(target_date: str | None = None, db: Session = Depends(get_db)):
    """
    One date's (YYYY-MM-DD, default: today UTC) real matchups and
    predicted goalie saves (presumed starter picked as the team's most-
    games-on-record goalie - see
    nhl_goalie_saves_sync.compute_goalie_saves_prediction). Mirrors
    /api/nfl/points-games's shape - one row per team per game, grouped by
    game_id client-side into a single card per matchup.

    NhlGoalieGame now keeps every date it's ever been refreshed for (see
    its own docstring) rather than a single global row per team, so
    picking a date here never depends on which date was MOST RECENTLY
    refreshed - that's what let refreshing tomorrow's matchups silently
    make today's disappear before this endpoint took a date param.
    """
    from datetime import datetime
    from models_db import NhlGoalieGame
    import nhl_goalie_saves_sync

    if target_date is None:
        target_date = datetime.utcnow().date().isoformat()

    games = db.query(NhlGoalieGame).filter_by(date=target_date).all()
    if not games:
        return {"games": [], "season": None, "target_date": target_date}

    league_avgs = nhl_goalie_saves_sync.get_league_avgs(db)

    rows = []
    for g in games:
        inputs = nhl_goalie_saves_sync.compute_goalie_saves_prediction(g.team, g.opponent, db, league_avgs=league_avgs)
        rows.append({
            "team": g.team,
            "opponent": g.opponent,
            "game_id": g.game_id if g.game_id is not None else "|".join(sorted([g.team, g.opponent])),
            "is_home": g.is_home,
            "date": g.date,
            "goalie_name": inputs["goalie_name"] if inputs else None,
            "goalie_player_id": inputs["goalie_player_id"] if inputs else None,
            "starter_is_heuristic": inputs["starter_is_heuristic"] if inputs else None,
            "predicted_mean": inputs["mean"] if inputs else None,
            "team_index": inputs["team_index"] if inputs else None,
            "opp_index": inputs["opp_index"] if inputs else None,
            "team_games_sample": inputs["team_games_sample"] if inputs else None,
            "opp_games_sample": inputs["opp_games_sample"] if inputs else None,
            "goalie_shots_sample": inputs["goalie_shots_sample"] if inputs else None,
        })

    return {"games": rows, "season": games[0].season, "target_date": games[0].date}


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
    game_pk: int | None = None  # null for non-MLB bets (e.g. NFL), which have no MLB game to reference
    bet_type: str = "hits"  # "hits", "first_inning_run", "hrr", etc.
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
    cfb_season: int | None = None  # CFB team-points bets only - needed to re-fetch the real final score at grading time
    cfb_week: int | None = None
    nfl_season: int | None = None  # NFL team-points/game-total bets only - needed to re-fetch that week's real games at grading time (no numeric id to store, see nfl_points_sync.py)
    nfl_week: int | None = None
    external_player_id: str | None = None  # gsis_id, for NFL rushing yards / by-position props - batter_id can't hold this (see models_db.py)


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


@app.post("/api/admin/manual-resolve-bet/{tracked_bet_id}")
def manual_resolve_bet(tracked_bet_id: int, result: str, actual_value: float | None = None, db: Session = Depends(get_db)):
    """
    Manually marks a tracked bet win/loss/push - an escape hatch for bets
    the automated grader can never reach (e.g. nfl_passing_yards, which
    has no working per-game data source at all - see bet_grading.py's
    module docstring) or any other one-off case that needs a human call.
    Once resolved=True here, grade_pending_bets() will never touch this
    row again (it only queries resolved=False), so this is a one-way,
    permanent override - there's no "undo" endpoint, just re-run this
    with a different result if a mistake needs correcting.
    """
    if result not in ("win", "loss", "push"):
        raise HTTPException(status_code=400, detail="result must be 'win', 'loss', or 'push'")
    from models_db import TrackedBet
    row = db.get(TrackedBet, tracked_bet_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tracked bet not found")
    row.resolved = True
    row.result = result
    if actual_value is not None:
        row.actual_value = actual_value
    db.commit()
    return {"id": row.id, "resolved": row.resolved, "result": row.result, "actual_value": row.actual_value}


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
            "external_player_id": r.external_player_id,
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


@app.get("/api/debug/pitcher-hits-allowed-inputs/{pitcher_id}")
def debug_pitcher_hits_allowed_inputs(pitcher_id: int, game_pk: int, batting_team_side: str, db: Session = Depends(get_db)):
    """
    Diagnostic: shows every raw value feeding into a Pitcher Hits
    Allowed probability - the stored season stats, the hybrid league
    baseline's CURRENT state (live-computed vs still the static
    fallback, and its exact value), the opposing lineup factor, and the
    final mean - so an implausible result can be traced to its actual
    cause instead of guessed at. batting_team_side: the side this
    pitcher FACES (e.g. "away" if he's the home starter).
    """
    from models_db import PitcherHitsStat, PitcherKStat
    import pitcher_hits_allowed_sync
    import hits_stats_sync

    pitcher_hits = db.get(PitcherHitsStat, pitcher_id)
    pitcher_k = db.get(PitcherKStat, pitcher_id)
    if pitcher_hits is None or pitcher_k is None:
        return {"error": f"Missing PitcherHitsStat or PitcherKStat row for pitcher_id {pitcher_id} - stats haven't fully synced for them yet"}

    league_rate, league_avg_per_start = pitcher_hits_allowed_sync.get_hybrid_baselines(db)
    pitcher_k_by_id = {pk.pitcher_id: pk for pk in db.query(PitcherKStat).all()}
    qualifying_count = len([
        1 for ph in db.query(PitcherHitsStat).all()
        if (pk := pitcher_k_by_id.get(ph.pitcher_id)) and pk.batters_faced >= pitcher_hits_allowed_sync.MIN_PITCHER_BATTERS_FACED and pk.games_started > 0
    ])
    la_b9 = hits_stats_sync.get_league_average_hit_rate()
    lineup_hit_index, batters_with_data = pitcher_hits_allowed_sync._lineup_hit_factor(db, game_pk, batting_team_side, la_b9)

    raw = {
        "pitcher_id": pitcher_id,
        "pitcher_name": pitcher_hits.pitcher_name,
        "hits_allowed": pitcher_hits.hits_allowed,
        "batters_faced": pitcher_k.batters_faced,
        "games_started": pitcher_k.games_started,
        "meets_min_batters_faced_50": pitcher_k.batters_faced >= pitcher_hits_allowed_sync.MIN_PITCHER_BATTERS_FACED,
    }

    baseline = {
        "using_live_baseline": qualifying_count >= pitcher_hits_allowed_sync.MIN_QUALIFYING_PITCHERS_FOR_LIVE_BASELINE,
        "qualifying_pitchers_in_pool": qualifying_count,
        "min_needed_for_live": pitcher_hits_allowed_sync.MIN_QUALIFYING_PITCHERS_FOR_LIVE_BASELINE,
        "static_fallback_rate": pitcher_hits_allowed_sync.LEAGUE_PITCHER_HIT_RATE_PER_BF,
        "static_fallback_avg_per_start": pitcher_hits_allowed_sync.LEAGUE_AVG_HITS_ALLOWED_PER_START,
        "actual_league_rate_used": league_rate,
        "actual_league_avg_per_start_used": league_avg_per_start,
    }

    derived = {
        "opposing_lineup_hit_index": lineup_hit_index,
        "opposing_lineup_batters_with_data": batters_with_data,
        "meets_min_lineup_batters_5": batters_with_data >= pitcher_hits_allowed_sync.MIN_LINEUP_BATTERS_WITH_DATA,
    }

    inputs = pitcher_hits_allowed_sync.compute_pitcher_hits_allowed_inputs(pitcher_id, game_pk, batting_team_side, la_b9=la_b9)

    return {"raw_stats": raw, "hybrid_baseline": baseline, "derived_values": derived, "final_result": inputs}


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
