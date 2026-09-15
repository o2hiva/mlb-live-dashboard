"""
Keeps TeamInningStat / PitcherInningStat in sync with real, season-to-date
per-inning scoring/allowed counts from MLB's Stats API.

This is a live-dashboard port of your fetch_inning_scoring_stats.py:
same data source (schedule hydrate=linescore,team,probablePitcher), same
field mapping, same accumulation logic - it just runs as a recurring
background job writing to the app's own database instead of a manually-run
script writing to an Excel workbook.

Currently tracks INNING 1 ONLY, matching this dashboard's current scope.
The same pattern extends to innings 2-3 (still pitcher-level, just add
the inning number to TRACKED_INNINGS) - innings 4+ would need the
team-vs-team model from core.py's inning_scoring_probability_team_level
instead, which isn't ported here yet.
"""
import logging
from datetime import date, timedelta

import mlb_client
from database import SessionLocal
from models_db import TeamInningStat, PitcherInningStat, SyncState

log = logging.getLogger("inning_stats_sync")

TRACKED_INNINGS = [1]

# Matches fetch_inning_scoring_stats.py's own default --start-date - the
# same validated backtest window your core.py model was tuned against.
DEFAULT_START_DATE = "2026-07-16"
SYNC_STATE_KEY = "inning_stats_last_synced_date"


def _get_last_synced_date(db) -> date:
    row = db.get(SyncState, SYNC_STATE_KEY)
    if row is None:
        return date.fromisoformat(DEFAULT_START_DATE) - timedelta(days=1)
    return date.fromisoformat(row.value)


def _set_last_synced_date(db, d: date):
    row = db.get(SyncState, SYNC_STATE_KEY)
    if row is None:
        db.add(SyncState(key=SYNC_STATE_KEY, value=d.isoformat()))
    else:
        row.value = d.isoformat()


def _get_or_create_team_row(db, team_name: str, inning: int) -> TeamInningStat:
    row = db.query(TeamInningStat).filter_by(team_name=team_name, inning=inning).first()
    if row is None:
        row = TeamInningStat(team_name=team_name, inning=inning, games=0, scored=0)
        db.add(row)
        db.flush()
    return row


def _get_or_create_pitcher_row(db, pitcher_name: str, inning: int) -> PitcherInningStat:
    row = db.query(PitcherInningStat).filter_by(pitcher_name=pitcher_name, inning=inning).first()
    if row is None:
        row = PitcherInningStat(pitcher_name=pitcher_name, inning=inning, starts=0, allowed=0)
        db.add(row)
        db.flush()
    return row


def refresh_inning_stats():
    """
    Fetches any FINAL games since the last successful sync and folds
    their per-inning results into the running season totals. Safe to
    call repeatedly - only processes days not already synced, and stops
    (without advancing the sync date) on the first day that fails to
    fetch, so that day gets retried on the next run rather than skipped.
    """
    db = SessionLocal()
    try:
        last_synced = _get_last_synced_date(db)
        yesterday = date.today() - timedelta(days=1)
        if last_synced >= yesterday:
            log.info("Inning stats already up to date through %s", last_synced)
            return

        day = last_synced + timedelta(days=1)
        days_processed = 0

        while day <= yesterday:
            date_str = day.isoformat()
            try:
                games = mlb_client.get_final_games_with_linescore(date_str)
            except Exception:
                log.exception("Failed to fetch games for %s - stopping, will retry next run", date_str)
                break

            for game in games:
                teams = game.get("teams", {})
                away_team_name = teams.get("away", {}).get("team", {}).get("name")
                home_team_name = teams.get("home", {}).get("team", {}).get("name")
                if not away_team_name or not home_team_name:
                    continue

                _, away_pname = mlb_client.get_game_starting_pitcher(game, "away")
                _, home_pname = mlb_client.get_game_starting_pitcher(game, "home")

                for inning in TRACKED_INNINGS:
                    away_runs, home_runs = mlb_client.get_inning_runs_from_raw_game(game, inning)
                    if away_runs is None or home_runs is None:
                        continue

                    away_t = _get_or_create_team_row(db, away_team_name, inning)
                    home_t = _get_or_create_team_row(db, home_team_name, inning)
                    away_t.games += 1
                    home_t.games += 1
                    if away_runs > 0:
                        away_t.scored += 1
                    if home_runs > 0:
                        home_t.scored += 1

                    if away_pname and home_pname:
                        away_p = _get_or_create_pitcher_row(db, away_pname, inning)
                        home_p = _get_or_create_pitcher_row(db, home_pname, inning)
                        away_p.starts += 1
                        home_p.starts += 1
                        if home_runs > 0:  # away pitcher allows to the HOME team
                            away_p.allowed += 1
                        if away_runs > 0:  # home pitcher allows to the AWAY team
                            home_p.allowed += 1

            _set_last_synced_date(db, day)
            db.commit()
            days_processed += 1
            day += timedelta(days=1)

        log.info("Inning stats sync processed %d day(s)", days_processed)
    except Exception:
        log.exception("refresh_inning_stats failed")
        db.rollback()
    finally:
        db.close()


def load_team_counts(inning: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(TeamInningStat).filter_by(inning=inning).all()
        return {r.team_name: {"games": r.games, "scored": r.scored} for r in rows}
    finally:
        db.close()


def load_pitcher_counts(inning: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(PitcherInningStat).filter_by(inning=inning).all()
        return {r.pitcher_name: {"starts": r.starts, "allowed": r.allowed} for r in rows}
    finally:
        db.close()
