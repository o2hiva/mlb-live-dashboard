"""
nfl_rushing_yards_sync.py

NFL Rushing Yards prop - the live-dashboard port of core_nfl_rushing.py,
added alongside NFL Passing Yards using the SAME estimation approach
nfl_passing_yards_sync.py already had to adopt, for the same reason.

FORMULA (from core_nfl_rushing.py, validated against the real 2023
season, 520 RB-games): predicted_yards = RB's own rolling season
average, WITH NO OPPONENT ADJUSTMENT - the opponent-adjusted version
was tested at every shrinkage strength (k=0..20) in the original
backtest and never beat the plain baseline's residual std dev (34.2
yards), so this is deliberately the simple one-factor formula, not
the three-factor "index x index x league avg" pattern every other
prop in this dashboard uses.

CONFIRMED DEAD END, THE SAME ONE nfl_passing_yards_sync.py ALREADY
DOCUMENTED: core_nfl_rushing.py's own design calls /players/{id}/stats
per week to build each RB's real game log (filtering to real-workload
games >= MIN_GAME_CARRIES, then averaging only those). That endpoint
was re-tested live for this port (a real 2026 RB, a real played week)
and confirmed to return {"data":[],"total":0} - empty, exactly the
same dead end nfl_passing_yards_sync.py found for QBs. So the
per-game game-log design as written CANNOT run against this API.

THE ESTIMATION APPROACH USED INSTEAD, mirroring nfl_passing_yards_sync.py:
uses /v1/stats/season directly - each RB's real season-cumulative
carries/rushing_yards/games, confirmed working (this is the SAME
"QB's own side" call the passing module already relies on, just
filtered to position=="RB" and the rushing fields instead). predicted
yards = total_yards / games, a plain season-to-date average.

HONEST LIMITATION, on top of the one nfl_passing_yards_sync.py already
carries: the original formula's own MIN_GAME_CARRIES=5 filter existed
specifically to exclude low-workload games (committee change-ups,
blowout benchings, injury-shortened games) from the average - with no
per-game log available, that filter can no longer be applied, so a
committee back's season average is diluted by every game they played,
including their smallest-workload ones. This is a real accuracy
tradeoff, disclosed rather than hidden, forced by the same missing
per-game endpoint documented in nfl_passing_yards_sync.py.

MULTIPLE RBS PER TEAM, ON PURPOSE: unlike passing yards (one starter
guessed per team), this predicts for EVERY qualifying RB individually
- a committee backfield naturally shows up as more than one row for
the same team, exactly as core_nfl_rushing.py's own docstring says a
"single starter" guess would be less honest here than showing every
back who has real carries. The frontend renders this as a flat table
(same shape as Passing Yards' table), not grouped matchup cards -
grouping by team happens implicitly since each row already carries
its own team.

DISTRIBUTION: Normal, using core_nfl_rushing.py's own validated
DEFAULT_RESIDUAL_STD_DEV (34.2 yards, one real 2023 season, mean
residual -0.4, std dev 34.2 - see core_nfl_rushing.py's own docstring
for the right-skew caveat on extreme thresholds).

NOT GRADEABLE, same reason and same permanent status as
nfl_passing_yards: no confirmed per-game breakdown endpoint exists to
check a single week's real rushing yards against - see
bet_grading.py's module docstring.
"""
import logging
import time

import requests

from database import SessionLocal
from models_db import NflRbStat, NflRbGame

log = logging.getLogger("nfl_rushing_yards_sync")

API_BASE = "https://api.nfldata.org/v1"
TIMEOUT = 30

# Verbatim from core_nfl_rushing.py.
LIVE_MIN_SEASON_CARRIES = 20  # season-level qualifying bar - lower than the backtest's 100, mid-season doesn't have a full year's volume yet

# LOWERED FROM core_nfl_rushing.py'S VALIDATED VALUE (3), SAME REASON
# nfl_passing_yards_sync.py already lowered its own MIN_PRIOR_GAMES:
# with one real game per team per week, requiring 3 is impossible
# before week 4 - a hard wall, not a sync bug.
MIN_PRIOR_GAMES = 1

# Empirical residual std dev from the real, validated 2023 backtest
# (backtest_nfl_rushing_yards.py, baseline/no-adjustment formula, 520
# RB-games). ONE SEASON ONLY so far - same caveat as every other
# "one season in" number in this project.
DEFAULT_RESIDUAL_STD_DEV = 34.2


def get_season_rbs(season: int, min_carries: int = LIVE_MIN_SEASON_CARRIES) -> list:
    """All RBs with real season-to-date carries, from /v1/stats/season
    (paginated) - same confirmed-working call nfl_passing_yards_sync.py's
    get_season_qbs uses, just filtered to position=="RB" and reading the
    rushing fields instead of passing ones."""
    rbs = []
    offset = 0
    limit = 50
    while True:
        resp = requests.get(
            f"{API_BASE}/stats/season",
            params={"season": season, "limit": limit, "offset": offset},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", [])
        if not rows:
            break
        for row in rows:
            if row.get("position") == "RB" and (row.get("carries") or 0) >= min_carries:
                rbs.append({
                    "gsis_id": row["player_id"],
                    "name": row.get("player_display_name", row.get("player_name", "")),
                    "team": row.get("recent_team"),
                    "games": row.get("games", 0) or 0,
                    "carries": row.get("carries", 0) or 0,
                    "yards": row.get("rushing_yards", 0) or 0,
                })
        total = payload.get("total", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.15)
    return rbs


def get_week_games(season: int, week: int) -> dict:
    """{team: opponent} for every team playing this week - same
    confirmed-working call every other NFL sync module in this
    dashboard has its own copy of."""
    resp = requests.get(
        f"{API_BASE}/games",
        params={"season": season, "week": week},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    games = resp.json().get("data", [])
    matchups = {}
    for g in games:
        if g.get("game_type") != "REG":
            continue
        away, home = g.get("away_team"), g.get("home_team")
        if away and home:
            matchups[away] = home
            matchups[home] = away
    return matchups


def refresh_nfl_rushing_stats(season: int, current_week: int) -> dict:
    """
    Rebuilds NflRbStat directly from /stats/season (exact, no
    estimation needed - same as NflQbStat's own side). Rebuilds
    NflRbGame with the given week's real matchups (team -> opponent,
    for display only, since this formula has no opponent adjustment to
    compute from it). Safe to call any time, any number of times.
    """
    db = SessionLocal()
    try:
        rbs = get_season_rbs(season)
        db.query(NflRbStat).delete()
        for rb in rbs:
            db.add(NflRbStat(
                gsis_id=rb["gsis_id"], name=rb["name"], team=rb["team"],
                total_yards=rb["yards"], carries=rb["carries"], games=rb["games"],
            ))

        try:
            this_week_matchups = get_week_games(season, current_week)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch NFL rushing current week %s schedule", current_week)
            this_week_matchups = {}

        db.query(NflRbGame).delete()
        for team, opponent in this_week_matchups.items():
            db.add(NflRbGame(team=team, opponent=opponent, season=season, week=current_week))

        db.commit()
        summary = {"rbs": len(rbs), "current_week_matchups": len(this_week_matchups)}
        log.info("refresh_nfl_rushing_stats complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
