"""
Background job: keeps the database in sync with upcoming MLB games.

- Every 60s: refresh today's AND the next few days' schedules (catches
  new games, status changes, AND probable-pitcher swaps - MLB doesn't
  always lock in a starter days out, so this keeps checking).
- For any game not yet started: recompute the 1st-inning prediction on
  EVERY cycle, not just once - if the probable pitcher changes, the
  next cycle picks it up automatically. This intentionally runs right
  up until first pitch, since that's exactly when a late scratch/swap
  would otherwise go unnoticed. Also checks that game's boxscore for a
  newly-confirmed lineup on either side (see hits_stats_sync.py), which
  is what drives the Hits prop and the "Confirmed"/"Not Confirmed"
  label shown for each team.
- For any game currently "In Progress": pull the live feed every ~15s
  and update score/inning/inning-lines.
- Once daily (end of day, see INNING_STATS_REFRESH_HOUR_UTC): runs the
  full end-of-day routine (see end_of_day.py) - 1st-inning stats sync
  PLUS a forced refresh of every Hits/HRR player who played that day.
  Also runs once immediately on every app startup/redeploy. Can also be
  triggered manually any time via GET /api/admin/run-daily-updates.

Tune POLL_INTERVAL_LIVE / POLL_INTERVAL_IDLE / SYNC_DAYS_AHEAD to be as
gentle as you like on MLB's public endpoint.
"""
import logging
from datetime import datetime, date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import mlb_client
import predictor
import hits_stats_sync
import end_of_day
from database import SessionLocal
from models_db import Game, InningLine, Prediction

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("poller")

POLL_INTERVAL_LIVE_SECONDS = 15
POLL_INTERVAL_IDLE_SECONDS = 60

# End-of-day inning-stats refresh time, in UTC. 09:00 UTC is 4-5am
# Eastern (depending on daylight saving) - safely after even a late
# West Coast game (including extra innings) has been marked Final by
# MLB's API, so "yesterday" is guaranteed complete by the time this runs.
INNING_STATS_REFRESH_HOUR_UTC = 9
INNING_STATS_REFRESH_MINUTE_UTC = 0

# How many days ahead to keep loaded/refreshed, so tomorrow's (and the
# day after's) games + probable pitchers show up before game day, not
# just once MLB's default "today" schedule call would surface them.
SYNC_DAYS_AHEAD = 2

# Statuses where the game hasn't started yet - keep recomputing the
# 1st-inning prediction every cycle for these, so a pitcher swap is
# reflected within one polling interval instead of only being computed
# once and then going stale.
NOT_STARTED_STATUSES = {"Scheduled", "Pre-Game", "Warmup", "Preview", "Delayed Start", "Delayed"}


def _upsert_first_inning_prediction(db, game: Game, game_info: dict):
    """Recomputes the 1st-inning prediction and updates the existing row
    in place (rather than inserting a new row every cycle), so a pitcher
    swap overwrites the old number instead of piling up duplicate rows."""
    prob, version = predictor.predict_first_inning_run_prob(game_info)

    existing_pred = (
        db.query(Prediction)
        .filter_by(game_pk=game.game_pk, market="first_inning_run")
        .order_by(Prediction.created_at.desc())
        .first()
    )
    if existing_pred is None:
        db.add(Prediction(game_pk=game.game_pk, probability=prob, model_version=version))
    else:
        existing_pred.probability = prob
        existing_pred.model_version = version
        existing_pred.created_at = datetime.utcnow()


def _sync_one_date(db, date_str: str):
    games = mlb_client.get_schedule(date_str)
    for g in games:
        existing = db.get(Game, g["game_pk"])
        if existing is None:
            existing = Game(game_pk=g["game_pk"])
            db.add(existing)
        existing.game_date = g["game_date"]
        existing.game_datetime_utc = g["game_datetime_utc"]
        existing.home_team = g["home_team"]
        existing.away_team = g["away_team"]
        existing.home_team_id = g["home_team_id"]
        existing.away_team_id = g["away_team_id"]
        existing.venue_id = g["venue_id"]
        existing.venue_name = g["venue_name"]
        existing.home_probable_pitcher = g["home_probable_pitcher"]
        existing.away_probable_pitcher = g["away_probable_pitcher"]
        existing.home_probable_pitcher_id = g["home_probable_pitcher_id"]
        existing.away_probable_pitcher_id = g["away_probable_pitcher_id"]
        existing.status = g["status"]
        existing.abstract_status = g["abstract_status"]

        if existing.status in NOT_STARTED_STATUSES:
            _upsert_first_inning_prediction(db, existing, g)
            _check_lineup_for_game(db, existing)


def _check_lineup_for_game(db, game: Game):
    """
    Fetches the dedicated boxscore endpoint for a not-yet-started game
    and hands it to hits_stats_sync to check for a newly-confirmed
    lineup on either side. Cheap no-op before MLB posts the lineup
    (boxscore comes back with no player marked as a starter yet).
    """
    try:
        boxscore = mlb_client.get_boxscore(game.game_pk)
    except Exception:
        log.warning("Failed to fetch boxscore for lineup check on game %s", game.game_pk)
        return
    hits_stats_sync.check_and_sync_lineups(db, game, boxscore)


def sync_schedule(days_ahead: int = SYNC_DAYS_AHEAD):
    """Loads/refreshes today plus the next `days_ahead` days."""
    db = SessionLocal()
    try:
        for offset in range(days_ahead + 1):
            date_str = (mlb_client.mlb_today() + timedelta(days=offset)).isoformat()
            _sync_one_date(db, date_str)
        db.commit()
    except Exception:
        log.exception("sync_schedule failed")
        db.rollback()
    finally:
        db.close()


def poll_live_games():
    db = SessionLocal()
    try:
        # abstract_status is always exactly "Live" while a game is
        # actually in progress, regardless of MLB's dozens of possible
        # verbose detailedState strings (challenges, reviews, delays,
        # etc.) - matching on the literal string "In Progress" alone
        # missed those, silently freezing score/inning-line updates for
        # any game caught mid-review at poll time.
        live_games = db.query(Game).filter(Game.abstract_status == "Live").all()
        for game in live_games:
            try:
                feed = mlb_client.get_live_feed(game.game_pk)
            except Exception:
                log.warning("Failed to fetch live feed for %s", game.game_pk)
                continue

            info = mlb_client.extract_linescore(feed)
            game.status = info["status"]
            game.inning = info["inning"]
            game.inning_half = info["inning_half"]
            game.home_score = info["home_score"]
            game.away_score = info["away_score"]
            game.updated_at = datetime.utcnow()

            existing_lines = {(l.inning, l.half) for l in game.inning_lines}
            for line in info["inning_lines"]:
                key = (line["inning"], line["half"])
                if key not in existing_lines:
                    db.add(InningLine(game_pk=game.game_pk, inning=line["inning"],
                                       half=line["half"], runs=line["runs"]))

        db.commit()
    except Exception:
        log.exception("poll_live_games failed")
        db.rollback()
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_schedule, "interval", seconds=POLL_INTERVAL_IDLE_SECONDS, id="sync_schedule")
    scheduler.add_job(poll_live_games, "interval", seconds=POLL_INTERVAL_LIVE_SECONDS, id="poll_live_games")
    # Runs once daily at a fixed end-of-day time (see
    # INNING_STATS_REFRESH_HOUR_UTC above) - the full end-of-day routine:
    # 1st-inning stats PLUS a forced refresh of every Hits/HRR player who
    # played yesterday (see end_of_day.py). next_run_time=now ALSO fires
    # it once immediately on every app startup/redeploy, in the
    # scheduler's own background thread so it never blocks startup -
    # useful since a fresh deploy (or the very first run ever) means the
    # season-to-date counts aren't just "yesterday", they're the whole
    # backfill, and you shouldn't have to wait until 4am for that.
    scheduler.add_job(
        end_of_day.run_end_of_day_update,
        CronTrigger(hour=INNING_STATS_REFRESH_HOUR_UTC, minute=INNING_STATS_REFRESH_MINUTE_UTC),
        id="end_of_day_update",
        next_run_time=datetime.utcnow(),
    )
    scheduler.start()
    # Run these two once immediately on startup rather than waiting for
    # the first interval - they're fast (a few days' schedule calls).
    sync_schedule()
    poll_live_games()
    return scheduler
