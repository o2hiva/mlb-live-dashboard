"""
NFL Passing Yards prop - first NFL prop in this dashboard, added
alongside the MLB props rather than as a separate app (per explicit
request, replacing the original standalone-Streamlit-app plan).

FORMULA: ported directly from the uploaded core_nfl.py, validated
across two real seasons (2023: z=+0.05, 2022: z=-1.50, pooled
z=-1.02) at DEFAULT_SHRINKAGE_K=5. Algebraically confirmed equivalent
to the "QB index x Opponent-allowed index x league average" framing:
qb_avg * (shrunk_opp_allowed/league_avg) = (qb_avg/league_avg) *
(shrunk_opp_allowed/league_avg) * league_avg - same three-factor
structure as every MLB prop, just written in a simplified 2-term form
in the original code.

REAL API FINDING - THE ORIGINAL PER-WEEK FETCH APPROACH DOESN'T WORK:
core_nfl.py's own get_week_stats (GET /players/{id}/stats?week=N) was
confirmed, directly against the live API, to return an EMPTY data
array for every single week tested (1, 2, and 3) for a real, active
QB (Josh Allen) who has definitely played real games this season -
confirmed via that same player's /stats/season response showing
games=2, real attempts and yards on file. Also tried adding a week
param directly to /stats/season itself (a common pattern in sports-
stats APIs) - that came back with the EXACT SAME season-cumulative
numbers regardless of the week value, meaning the parameter is
silently ignored. Conclusion: this API only exposes a continuously-
updating SEASON-CUMULATIVE total, no working per-game breakdown
endpoint at all, at least not one found through direct testing.

THE SNAPSHOT-DELTA WORKAROUND, used instead: since the season-
cumulative endpoint reliably works and updates as real games get
played, this SNAPSHOTS each QB's cumulative total every sync
(NflQbSnapshot), and computes "this week's real game" as the
DIFFERENCE between today's cumulative total and the last stored
snapshot - that delta IS the real game that happened in between, with
no need for a per-week endpoint at all. This directly matches running
this sync once each week as the season progresses.

HONEST LIMITATION: this can only accumulate real per-game data GOING
FORWARD from whenever it first runs for a given QB - already-played
weeks before that FIRST snapshot can't be retroactively split into
individual games (there's no earlier baseline to diff against). The
very first sync for any QB stores a baseline with zero attributed
games, not a false "one giant game" equal to their whole season so far.

DISTRIBUTION: Normal, using core_nfl.py's own DEFAULT_RESIDUAL_STD_DEV
(75.0 yards - pooled from both real backtested seasons: 73.0 and 76.9,
averaged). No continuity correction (passing yards aren't a small
discrete count the way strikeouts are), matching core_nfl.py's own
prob_over_line, which uses the raw threshold directly.

HONEST LIMITATION, carried forward directly from core_nfl.py's own
docstring: "who's starting at QB this week" has no confirmed
probable-starters endpoint, so this uses each team's most-recent
ACTUAL starter as a heuristic, not a confirmed one - the app should
let the person override it if they know about an injury or benching
this heuristic can't see.

NO PARK/WEATHER FACTOR: unlike MLB's Hits/HR, this formula has no
dome-vs-outdoor or weather adjustment - not confirmed whether this
matters enough to need one, flagged as an open question.
"""
import logging
import time
from datetime import datetime, timedelta

import requests

from database import SessionLocal
from models_db import NflQbStat, NflTeamAllowedStat, NflGame, NflQbSnapshot

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
# showing from the season's first completed REAL game onward, at the
# cost of real added noise early on - and asymmetrically so: the
# OPPONENT side is shrinkage-protected (DEFAULT_SHRINKAGE_K blends a
# small sample toward league average), but the QB's OWN side has NO
# shrinkage in the validated formula (qb_avg is used raw).
MIN_PRIOR_GAMES = 1
MIN_OPPONENT_GAMES = 1

CACHE_TTL = timedelta(hours=6)  # NFL games are weekly, not daily - no need to recompute the league average often
_league_avg_cache = None
_league_avg_cache_time = None


# ---------------------------------------------------------------------------
# Fetch functions.
# ---------------------------------------------------------------------------

def get_season_qbs(season: int, min_attempts: int = LIVE_MIN_SEASON_ATTEMPTS) -> list:
    """All QBs with real season-to-date attempts, from /v1/stats/season
    (paginated). Returns a list of dicts with the FULL cumulative row
    needed for the snapshot-delta approach (gsis_id, name, team,
    games, attempts, yards) - confirmed this endpoint works correctly
    against live data."""
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
                qbs.append({
                    "gsis_id": row["player_id"],
                    "name": row.get("player_display_name", row.get("player_name", "")),
                    "team": row.get("recent_team"),
                    "games": row.get("games", 0) or 0,
                    "attempts": row.get("attempts", 0) or 0,
                    "yards": row.get("passing_yards", 0) or 0,
                })
        total = payload.get("total", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.15)
    return qbs


def get_week_games(season: int, week: int) -> dict:
    """{team: opponent} for every team playing this week - confirmed
    working correctly against live data (unlike the per-player weekly
    endpoint below)."""
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
    """CONFIRMED NON-FUNCTIONAL against the real live API as of direct
    testing: returns an empty data array for every week tried, for a
    real QB known to have played real games this season. No longer
    called by refresh_nfl_stats (see module docstring's snapshot-delta
    workaround) - kept only in case this endpoint gets fixed/documented
    later, not part of the active sync path."""
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
# Sync - snapshot-delta approach (see module docstring for why). Safe
# to call any number of times: a call with no new real games since the
# last snapshot correctly computes a zero delta and changes nothing.
# ---------------------------------------------------------------------------

def refresh_nfl_stats(season: int, current_week: int):
    """
    For every real QB, compares today's season-cumulative total
    against the last stored snapshot (NflQbSnapshot) and folds the
    DIFFERENCE into the running totals (NflQbStat/NflTeamAllowedStat) -
    that difference is exactly the real game(s) played since the last
    sync. Then refreshes NflGame with the given week's real matchups
    (unaffected by any of this - that endpoint works normally).

    A QB seen for the first time (no prior snapshot) gets a baseline
    snapshot with NO attributed delta - see module docstring's honest
    limitation about not being able to retroactively split past weeks.

    A sanity floor (MIN_GAME_ATTEMPTS x delta_games) guards against
    folding in a spurious tiny delta as if it were a real game.
    """
    db = SessionLocal()
    try:
        current_qbs = get_season_qbs(season)

        # The delta needs an opponent to attribute yards-allowed to -
        # fetch the most recently completed week's schedule ONCE, not
        # per QB, the same "compute shared values once per request"
        # lesson learned throughout every MLB prop.
        try:
            recent_matchups = get_week_games(season, max(1, current_week - 1)) if current_week > 1 else {}
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch week %s schedule for delta attribution", current_week - 1)
            recent_matchups = {}

        for qb in current_qbs:
            gsis_id = qb["gsis_id"]
            snapshot = db.get(NflQbSnapshot, gsis_id)

            if snapshot is not None:
                delta_games = qb["games"] - snapshot.cumulative_games
                delta_yards = qb["yards"] - snapshot.cumulative_yards
                delta_attempts = qb["attempts"] - snapshot.cumulative_attempts

                if delta_games > 0 and delta_attempts >= MIN_GAME_ATTEMPTS * delta_games:
                    qb_stat = db.get(NflQbStat, gsis_id)
                    if qb_stat is None:
                        qb_stat = NflQbStat(gsis_id=gsis_id, name=qb["name"], team=qb["team"],
                                             total_yards=0, games=0, last_game_week=0)
                        db.add(qb_stat)
                    qb_stat.name = qb["name"]
                    qb_stat.team = qb["team"]
                    qb_stat.total_yards += delta_yards
                    qb_stat.games += delta_games
                    qb_stat.last_game_week = max(1, current_week - 1)

                    opponent = recent_matchups.get(qb["team"])
                    if opponent:
                        opp_stat = db.get(NflTeamAllowedStat, opponent)
                        if opp_stat is None:
                            opp_stat = NflTeamAllowedStat(team=opponent, total_yards_allowed=0, games=0)
                            db.add(opp_stat)
                        opp_stat.total_yards_allowed += delta_yards
                        opp_stat.games += delta_games

            if snapshot is None:
                db.add(NflQbSnapshot(gsis_id=gsis_id, name=qb["name"], team=qb["team"],
                                      cumulative_games=qb["games"], cumulative_attempts=qb["attempts"],
                                      cumulative_yards=qb["yards"]))
            else:
                snapshot.name = qb["name"]
                snapshot.team = qb["team"]
                snapshot.cumulative_games = qb["games"]
                snapshot.cumulative_attempts = qb["attempts"]
                snapshot.cumulative_yards = qb["yards"]

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
    exists for this prop yet). Returns None if no real deltas have
    accumulated yet."""
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
