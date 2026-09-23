"""
NFL Passing Yards prop - first NFL prop in this dashboard, added
alongside the MLB props rather than as a separate app (per explicit
request, replacing the original standalone-Streamlit-app plan).

FORMULA: ported directly from the uploaded core_nfl.py, validated
across two real seasons (2023: z=+0.05, 2022: z=-1.50, pooled
z=-1.02) at DEFAULT_SHRINKAGE_K=5. Algebraically confirmed equivalent
to the "QB index x Opponent-allowed index x league average" framing
before building this: qb_avg * (shrunk_opp_allowed/league_avg) =
(qb_avg/league_avg) * (shrunk_opp_allowed/league_avg) * league_avg -
same three-factor structure as every MLB prop, just written in a
simplified 2-term form in the original code.

DISTRIBUTION: Normal, using core_nfl.py's own DEFAULT_RESIDUAL_STD_DEV
(75.0 yards - pooled from both real backtested seasons: 73.0 and 76.9,
averaged). Same "at least N" semantics as Pitcher K's own Normal
model - no continuity correction here since passing yards aren't a
small discrete count the way strikeouts are (typically no half-yard
rounding concerns at these magnitudes), matching core_nfl.py's own
prob_over_line, which uses the raw threshold directly.

HONEST LIMITATION, carried forward directly from core_nfl.py's own
docstring: "who's starting at QB this week" has no confirmed
probable-starters endpoint, so this uses each team's most-recent
ACTUAL starter as a heuristic, not a confirmed one - the app should
let the person override it if they know about an injury or benching
this heuristic can't see. Not hidden, and not fixed here either
(no better data source available) - surfaced the same way in the API
response (see main.py's /api/nfl/games endpoint).

REAL, UNVERIFIED NETWORK CALLS: this module calls api.nfldata.org via
core_nfl.py's own already-written fetch functions (get_season_qbs,
get_week_games, get_week_stats), used AS-IS/trusted rather than
rewritten - that domain isn't reachable from the sandbox this was
built in, so the actual response shapes/field names could not be
verified here the way every MLB fetch function was checked against
real live data during this session. Needs real-world verification
once deployed, the same way several MLB endpoints needed a live
debug-endpoint round of correction earlier today.

NO PARK/WEATHER FACTOR: unlike MLB's Hits/HR, this formula has no
dome-vs-outdoor or weather adjustment - not confirmed whether this
matters enough to need one, flagged as an open question rather than
either assumed to matter or assumed not to.
"""
import logging
import time
from datetime import datetime, timedelta

import requests

from database import SessionLocal
from models_db import NflQbStat, NflTeamAllowedStat, NflGame

log = logging.getLogger("nfl_passing_yards_sync")

API_BASE = "https://api.nfldata.org/v1"

# Verbatim from core_nfl.py, EXCEPT the two minimums below - see
# MIN_PRIOR_GAMES/MIN_OPPONENT_GAMES's own comments for why those were
# lowered from the validated value.
DEFAULT_SHRINKAGE_K = 5.0
MIN_GAME_ATTEMPTS = 10
LIVE_MIN_SEASON_ATTEMPTS = 20
DEFAULT_RESIDUAL_STD_DEV = 75.0

# LOWERED FROM THE VALIDATED core_nfl.py VALUE (3), BY EXPLICIT REQUEST:
# with an NFL team playing exactly one game per week, requiring 3 real
# games is mathematically impossible before week 4 of the season - not
# a sync bug, a hard wall. Lowered to 1 so predictions can start
# showing from the season's first completed week onward, at the cost
# of real added noise EARLY on - and asymmetrically so: the OPPONENT
# side is shrinkage-protected (DEFAULT_SHRINKAGE_K blends a small
# sample toward league average), but the QB's OWN side has NO
# shrinkage in the validated formula (qb_avg is used raw) - a QB's
# single-game average is exactly as noisy as that one game was. This
# self-corrects as real weeks accumulate; there was no way to lower
# the wall without accepting that early-week tradeoff somewhere.
MIN_PRIOR_GAMES = 1
MIN_OPPONENT_GAMES = 1

CACHE_TTL = timedelta(hours=6)  # NFL games are weekly, not daily - no need to recompute the league average often
_league_avg_cache = None
_league_avg_cache_time = None


# ---------------------------------------------------------------------------
# Fetch functions - copied verbatim from the uploaded core_nfl.py rather
# than rewritten, since they're already real/tested code from that file,
# not something built fresh here. See module docstring: unverified
# against live data in this specific environment (no network access to
# api.nfldata.org from this sandbox).
# ---------------------------------------------------------------------------

def get_season_qbs(season: int, min_attempts: int = LIVE_MIN_SEASON_ATTEMPTS) -> list:
    qbs = []
    offset = 0
    limit = 50
    while True:
        resp = requests.get(f"{API_BASE}/stats/season", params={"season": season, "limit": limit, "offset": offset}, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", [])
        if not rows:
            break
        for row in rows:
            if row.get("position") == "QB" and (row.get("attempts") or 0) >= min_attempts:
                qbs.append((row["player_id"], row.get("player_display_name", row.get("player_name", ""))))
        total = payload.get("total", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.15)
    return qbs


def get_week_games(season: int, week: int) -> dict:
    resp = requests.get(f"{API_BASE}/games", params={"season": season, "week": week}, timeout=30)
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


def get_week_stats(gsis_id: str, season: int, week: int):
    resp = requests.get(f"{API_BASE}/players/{gsis_id}/stats", params={"season": season, "week": week}, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    if not rows:
        return None
    row = rows[0]
    if row.get("season_type") != "REG":
        return None
    return row


# ---------------------------------------------------------------------------
# Sync - refreshes NflQbStat/NflTeamAllowedStat/NflGame from real data.
# Weekly cadence, but safe to call more often (a no-op for weeks already
# fully accounted for) - called from end_of_day.py's existing daily job.
# ---------------------------------------------------------------------------

def refresh_nfl_stats(season: int, current_week: int):
    """
    Rebuilds NflQbStat/NflTeamAllowedStat FROM SCRATCH each call (not an
    incremental append) by replaying every completed week 1..current_week-1,
    then refreshes NflGame with the current week's real matchups. This
    is simpler and safer than incremental updates (no risk of double-
    counting a game if this runs more than once for the same week), at
    the cost of re-fetching everything each time - acceptable given
    this only needs to run a few times a week, not continuously the way
    MLB's live-game polling does.
    """
    db = SessionLocal()
    try:
        qbs = get_season_qbs(season)

        schedule = {}
        for week in range(1, current_week):
            try:
                schedule[week] = get_week_games(season, week)
            except requests.exceptions.RequestException:
                log.exception("Failed to fetch week %s schedule", week)
                schedule[week] = {}
            time.sleep(0.1)

        game_logs = {}
        for gsis_id, name in qbs:
            log_entries = []
            for week in range(1, current_week):
                try:
                    row = get_week_stats(gsis_id, season, week)
                except requests.exceptions.RequestException:
                    continue
                if row is None:
                    continue
                log_entries.append({
                    "week": week, "team": row.get("recent_team"),
                    "yards": row.get("passing_yards", 0), "attempts": row.get("attempts", 0),
                })
                time.sleep(0.08)
            game_logs[gsis_id] = {"name": name, "log": log_entries}

        qb_totals = {}       # gsis_id -> {"name":, "team":, "yards":, "games":, "last_week":}
        team_allowed_totals = {}  # team -> {"yards":, "games":}

        for week in range(1, current_week):
            matchups = schedule.get(week, {})
            for gsis_id, info in game_logs.items():
                game = next((g for g in info["log"] if g["week"] == week), None)
                if game is None or game["attempts"] < MIN_GAME_ATTEMPTS:
                    continue
                team = game["team"]
                opponent = matchups.get(team)
                if opponent is None:
                    continue

                qb_entry = qb_totals.setdefault(gsis_id, {"name": info["name"], "team": team, "yards": 0, "games": 0, "last_week": 0})
                qb_entry["yards"] += game["yards"]
                qb_entry["games"] += 1
                if week >= qb_entry["last_week"]:
                    qb_entry["last_week"] = week
                    qb_entry["team"] = team  # most recent team on file

                opp_entry = team_allowed_totals.setdefault(opponent, {"yards": 0, "games": 0})
                opp_entry["yards"] += game["yards"]
                opp_entry["games"] += 1

        # Replace, not merge - see function docstring for why "rebuild
        # from scratch" is the safer choice here.
        db.query(NflQbStat).delete()
        for gsis_id, entry in qb_totals.items():
            db.add(NflQbStat(gsis_id=gsis_id, name=entry["name"], team=entry["team"],
                              total_yards=entry["yards"], games=entry["games"], last_game_week=entry["last_week"]))

        db.query(NflTeamAllowedStat).delete()
        for team, entry in team_allowed_totals.items():
            db.add(NflTeamAllowedStat(team=team, total_yards_allowed=entry["yards"], games=entry["games"]))

        try:
            this_week_matchups = get_week_games(season, current_week)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch current week %s matchups", current_week)
            this_week_matchups = {}

        db.query(NflGame).delete()
        for team, opponent in this_week_matchups.items():
            db.add(NflGame(team=team, opponent=opponent, season=season, week=current_week))

        db.commit()
        global _league_avg_cache, _league_avg_cache_time
        _league_avg_cache = None  # force recompute next request, real data just changed
        _league_avg_cache_time = None
    except Exception:
        log.exception("refresh_nfl_stats failed")
        db.rollback()
    finally:
        db.close()


def get_league_avg_allowed(db) -> float | None:
    """League-wide average passing yards allowed per game, computed
    live from NflTeamAllowedStat (real data only - no static fallback
    exists for this prop yet, unlike MLB's hybrid baselines, since
    there's no equivalent "empirically derived historical constant"
    available here). Returns None if no real data has synced yet."""
    global _league_avg_cache, _league_avg_cache_time
    now = datetime.utcnow()
    if _league_avg_cache is not None and now - _league_avg_cache_time < CACHE_TTL:
        return _league_avg_cache

    rows = db.query(NflTeamAllowedStat).all()
    total_yards = sum(r.total_yards_allowed for r in rows)
    total_games = sum(r.games for r in rows)
    if total_games == 0:
        return None

    result = total_yards / total_games
    _league_avg_cache = result
    _league_avg_cache_time = now
    return result


def compute_passing_yards_prediction(team: str, opponent: str, league_avg: float | None = None) -> dict | None:
    """
    Returns {"qb_name":, "qb_gsis_id":, "predicted_mean":,
    "qb_games_sample":, "opp_games_sample":, "starter_is_heuristic": True}
    for one team's real most-recent starter against their real current
    opponent. None if the starter or opponent doesn't have enough real
    games yet (MIN_PRIOR_GAMES / MIN_OPPONENT_GAMES - lowered from the
    validated value of 3, see their own comments for why).

    starter_is_heuristic is always True here - see module docstring's
    HONEST LIMITATION note. The frontend should let the person confirm
    or override the starter shown.
    """
    db = SessionLocal()
    try:
        if league_avg is None:
            league_avg = get_league_avg_allowed(db)
        if league_avg is None:
            return None

        qb = db.query(NflQbStat).filter_by(team=team).order_by(NflQbStat.last_game_week.desc()).first()
        if qb is None or qb.games < MIN_PRIOR_GAMES:
            return None

        opp = db.get(NflTeamAllowedStat, opponent)
        if opp is None or opp.games < MIN_OPPONENT_GAMES:
            return None

        qb_avg = qb.total_yards / qb.games
        shrunk_opp_allowed = (opp.total_yards_allowed + DEFAULT_SHRINKAGE_K * league_avg) / (opp.games + DEFAULT_SHRINKAGE_K)
        predicted_mean = qb_avg * (shrunk_opp_allowed / league_avg)

        return {
            "qb_name": qb.name,
            "qb_gsis_id": qb.gsis_id,
            "predicted_mean": predicted_mean,
            "qb_games_sample": qb.games,
            "opp_games_sample": opp.games,
            "starter_is_heuristic": True,
        }
    finally:
        db.close()
