"""
end_of_day.py

The live-dashboard equivalent of run_daily_updates.py's orchestration
role - runs the full end-of-day routine in one call instead of several:

  1. Force-refresh season stats (Hits/HRR) for every batter and pitcher
     who appeared in YESTERDAY's and TODAY's games, bypassing the normal
     24h staleness cache. Without this, a player's stats only update the
     next time their team's lineup happens to get (re-)confirmed and
     24h have passed - this guarantees everyone who played (or has a
     confirmed lineup today) is current. Including today specifically
     matters for the MANUAL trigger - lets you self-heal a lineup
     confirmed today whose stat sync failed for any reason, right away,
     without waiting for the scheduled run.
  2. Force-refresh the league-average rates (hit rate, OBP, HR rate,
     walk rate, runs-allowed rate) the Hits/HRR formulas depend on -
     same reasoning, don't wait for the cache to expire naturally.
  3. Run the 1st-inning log5 model's own daily sync
     (inning_stats_sync.refresh_inning_stats) - already had its own
     daily job/manual-trigger pair; folded in here too so there's ONE
     button/schedule for the whole end-of-day routine, matching how
     run_daily_updates.py itself was "six steps in one command instead
     of six."

WHAT THIS DOESN'T NEED TO DO, unlike the Excel-era version: load next
day's games (the poller already does this automatically, continuously)
or keep pushing lineups through the day as they're announced (also
already continuous - see poller.py's _check_lineup_for_game, called
every 60s for every not-yet-started game). Those were manual, one-shot
steps in the Excel workflow (fill_games.py once, fill_lineups.py
pushed through the day); the live site just never stops doing them.

DATE HANDLING: uses mlb_client.mlb_yesterday()/mlb_today() (both
Eastern-anchored, see their own docstrings) rather than naive
date.today() - the exact bug class already hit and fixed in the
Excel-era scripts (mlb_today() being Eastern-anchored specifically to
avoid a traveling user's local timezone picking the wrong day and
returning zero games).
"""
import logging

import mlb_client
import hits_stats_sync
import hrr_stats_sync
import inning_stats_sync
from database import SessionLocal
from models_db import Game

log = logging.getLogger("end_of_day")


def run_end_of_day_update() -> dict:
    """
    Runs all three steps in sequence, continuing even if one fails
    (same "don't let one hiccup stop the rest" philosophy as
    run_daily_updates.py's own run_step()). Returns a summary dict
    suitable for both the scheduled job's logs and the manual-trigger
    API endpoint's response.
    """
    summary = {"dates": [mlb_client.mlb_yesterday().isoformat(), mlb_client.mlb_today().isoformat()]}

    # Step 1+2: force-refresh player stats + league rates for yesterday's
    # AND today's games. Including today matters for the MANUAL trigger
    # specifically - self-heals any lineup that got confirmed today but
    # whose underlying stat sync failed for some reason (e.g. a bug
    # deployed mid-day), without waiting for the scheduled run.
    try:
        summary["players_refreshed"] = _refresh_recent_player_stats()
        summary["league_rates"] = "refreshed"
    except Exception:
        log.exception("Player/league-rate refresh failed")
        summary["players_refreshed"] = 0
        summary["league_rates"] = "FAILED"

    # Step 3: the 1st-inning model's own daily sync (already exists,
    # already safe to call any time - just folded into this one routine too).
    try:
        inning_stats_sync.refresh_inning_stats()
        summary["inning_stats"] = "refreshed"
    except Exception:
        log.exception("Inning-stats refresh failed")
        summary["inning_stats"] = "FAILED"

    log.info("End-of-day update complete: %s", summary)
    return summary


def _refresh_recent_player_stats() -> int:
    """
    Finds every game from yesterday AND today (Eastern-anchored) and
    force-refreshes season stats for each one's confirmed batters and
    probable pitchers. Games with no confirmed lineup (rare for a
    completed day, but possible for a postponement, or simply not
    confirmed yet for today) simply have nothing to refresh - not an
    error. Returns total batter+pitcher rows touched.
    """
    db = SessionLocal()
    try:
        dates = [mlb_client.mlb_yesterday().isoformat(), mlb_client.mlb_today().isoformat()]
        games = db.query(Game).filter(Game.game_date.in_(dates)).all()

        total_refreshed = 0
        for game in games:
            try:
                total_refreshed += hits_stats_sync.refresh_all_stats_for_game(db, game, force=True)
            except Exception:
                log.exception("Failed to refresh stats for game %s", game.game_pk)

        # League-wide rates depend on the same underlying team totals -
        # force them fresh too, once, after the per-player pass above.
        hits_stats_sync.get_league_average_hit_rate(force=True)
        hrr_stats_sync.get_league_hrr_rates(force=True)

        db.commit()
        return total_refreshed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
