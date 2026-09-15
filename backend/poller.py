"""
Background job: keeps the database in sync with today's MLB games.

- Every 60s: refresh today's schedule (catches new games, status changes).
- For any game currently "Live": pull the live feed every ~15s and
  update score/inning/inning-lines, and (re)compute a fresh prediction.
- For any game "Preview" (not yet started): compute a pre-game
  prediction once probable pitchers are known.

Tune POLL_INTERVAL_LIVE / POLL_INTERVAL_IDLE to be as gentle as you like
on MLB's public endpoint.
"""
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

import mlb_client
import predictor
from database import SessionLocal
from models_db import Game, InningLine, Prediction

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("poller")

POLL_INTERVAL_LIVE_SECONDS = 15
POLL_INTERVAL_IDLE_SECONDS = 60


def sync_schedule():
    db = SessionLocal()
    try:
        games = mlb_client.get_schedule()
        for g in games:
            existing = db.get(Game, g["game_pk"])
            if existing is None:
                existing = Game(game_pk=g["game_pk"])
                db.add(existing)
            existing.game_date = g["game_date"]
            existing.home_team = g["home_team"]
            existing.away_team = g["away_team"]
            existing.home_probable_pitcher = g["home_probable_pitcher"]
            existing.away_probable_pitcher = g["away_probable_pitcher"]
            existing.status = g["status"]

            # Pre-game prediction, computed once probable pitchers are known.
            if existing.status in ("Preview", "Pre-Game", "Scheduled") and not existing.predictions:
                prob, version = predictor.predict_first_inning_run_prob(g)
                db.add(Prediction(game_pk=existing.game_pk, probability=prob, model_version=version))

        db.commit()
    except Exception:
        log.exception("sync_schedule failed")
        db.rollback()
    finally:
        db.close()


def poll_live_games():
    db = SessionLocal()
    try:
        live_games = db.query(Game).filter(Game.status == "In Progress").all()
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
    scheduler.start()
    # Run once immediately on startup rather than waiting for the first interval.
    sync_schedule()
    poll_live_games()
    return scheduler
