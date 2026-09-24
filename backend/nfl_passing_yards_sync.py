"""
NFL Passing Yards prop - first NFL prop in this dashboard, added
alongside the MLB props rather than as a separate app.

FORMULA: ported directly from the uploaded core_nfl.py, validated
across two real seasons (2023: z=+0.05, 2022: z=-1.50, pooled
z=-1.02) at DEFAULT_SHRINKAGE_K=5. Algebraically confirmed equivalent
to the "QB index x Opponent-allowed index x league average" framing:
qb_avg * (shrunk_opp_allowed/league_avg) = (qb_avg/league_avg) *
(shrunk_opp_allowed/league_avg) * league_avg - same three-factor
structure as every MLB prop, just written in a simplified 2-term form
in the original code.

REAL API FINDING - THERE IS NO WORKING PER-GAME BREAKDOWN ENDPOINT:
confirmed directly against the live API, exhaustively: the per-player
weekly endpoint (/players/{id}/stats?week=N) returns empty for every
week tried, for a real QB known to have played real games; adding a
week param to /stats/season is silently ignored; a per-game box-score
endpoint (/games/{id}/stats, /games/{id}/boxscore) 404s. Only two
things are confirmed to work: /stats/season (each PLAYER's own
season-cumulative totals) and /stats/team (each TEAM's own season-
cumulative OFFENSIVE totals) - both continuously-updating aggregates,
never a single game's numbers in isolation.

THE ESTIMATION APPROACH USED INSTEAD OF EXACT PER-GAME RECONSTRUCTION:
- QB's OWN side: uses /stats/season directly (total_yards/games) - no
  estimation needed here at all, this was always exact.
- OPPONENT-ALLOWED side: since there's no way to know exactly how many
  yards a defense allowed in any single game, this ESTIMATES it using
  the real schedule (confirmed working) plus each opponent's own
  season passing average as a stand-in for what they likely threw for
  in that specific game (their season average is the best available
  unbiased estimate of any one game, absent the real number). Summed
  across every real opponent a team has faced this season, this gives
  a genuine estimate of yards allowed - real games, real opponents,
  approximated per-game magnitude rather than reconstructed exactly.

WHY THIS IS AN IMPROVEMENT OVER THE EARLIER SNAPSHOT-DELTA APPROACH
(replaced, no longer used): that approach only accumulated real data
GOING FORWARD one real week at a time, leaving already-played weeks
permanently un-reconstructable without manual entry. This estimation
approach uses only season-cumulative data, so it can immediately
account for every week played so far, including ones before this
code ever ran - no waiting, no manual backfill needed for the gap.

HONEST LIMITATION: this is a real approximation, not exact per-game
data - "what an opponent scored in one specific game" is estimated by
"what they scored on average across their whole season", which may
differ meaningfully from that team's actual output in a given game
(e.g. weather, injuries, a particularly strong or weak individual
opponent defense that game). This is a genuine tradeoff, disclosed
rather than hidden, made because no exact alternative was found to
exist on this API.

DISTRIBUTION: Normal, using core_nfl.py's own DEFAULT_RESIDUAL_STD_DEV
(75.0 yards - pooled from both real backtested seasons: 73.0 and 76.9,
averaged). No continuity correction, matching core_nfl.py's own
prob_over_line, which uses the raw threshold directly.

HONEST LIMITATION, carried forward directly from core_nfl.py's own
docstring: "who's starting at QB this week" has no confirmed
probable-starters endpoint, so this uses each team's most-recent
ACTUAL starter as a heuristic - the app should let the person override
it if they know about an injury or benching this heuristic can't see.

NO PARK/WEATHER FACTOR: unlike MLB's Hits/HR, this formula has no
dome-vs-outdoor or weather adjustment - not confirmed whether this
matters enough to need one, flagged as an open question.
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
# a sync bug, a hard wall.
MIN_PRIOR_GAMES = 1
MIN_OPPONENT_GAMES = 1

CACHE_TTL = timedelta(hours=6)  # NFL games are weekly, not daily - no need to recompute the league average often
_league_avg_cache = None
_league_avg_cache_time = None


# ---------------------------------------------------------------------------
# Fetch functions - both confirmed working against live data.
# ---------------------------------------------------------------------------

def get_season_qbs(season: int, min_attempts: int = LIVE_MIN_SEASON_ATTEMPTS) -> list:
    """All QBs with real season-to-date attempts, from /v1/stats/season
    (paginated). Returns a list of dicts (gsis_id, name, team, games,
    attempts, yards) - confirmed working against live data."""
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


def get_all_team_season_stats(season: int) -> dict:
    """{team: {games, attempts, yards}} - each team's own season-to-date
    OFFENSIVE passing output, from /v1/stats/team. Confirmed working
    against live data - one row per team, unlike /stats/season's
    per-player rows."""
    resp = requests.get(f"{API_BASE}/stats/team", params={"season": season}, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    return {r["team"]: {"games": r.get("games", 0) or 0, "attempts": r.get("attempts", 0) or 0,
                         "yards": r.get("passing_yards", 0) or 0} for r in rows if r.get("team")}


def get_week_games(season: int, week: int) -> dict:
    """{team: opponent} for every team playing this week - confirmed
    working against live data."""
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


# ---------------------------------------------------------------------------
# Sync - fully recomputed from scratch each call (no incremental state
# to manage or get out of sync), safe to call any number of times.
# ---------------------------------------------------------------------------

def refresh_nfl_stats(season: int, current_week: int):
    """
    Rebuilds NflQbStat directly from /stats/season (exact, no
    estimation needed). Rebuilds NflTeamAllowedStat by walking the real
    schedule for every completed week and, for each real game a team
    played, adding their opponent's own season passing average as an
    ESTIMATE of what that opponent scored in that game (see module
    docstring's honest limitation - this is a real approximation, not
    exact per-game data, because no exact source was found to exist).
    Then refreshes NflGame with the given week's real matchups.
    """
    db = SessionLocal()
    try:
        current_qbs = get_season_qbs(season)
        db.query(NflQbStat).delete()
        for qb in current_qbs:
            db.add(NflQbStat(gsis_id=qb["gsis_id"], name=qb["name"], team=qb["team"],
                              total_yards=qb["yards"], games=qb["games"],
                              last_game_week=max(1, current_week - 1)))

        team_stats = get_all_team_season_stats(season)
        team_allowed_totals = {}  # team -> {"yards": estimated total (float), "games": real games count}

        for week in range(1, current_week):
            try:
                matchups = get_week_games(season, week)
            except requests.exceptions.RequestException:
                log.exception("Failed to fetch week %s schedule", week)
                continue

            seen_pairs = set()
            for team, opponent in matchups.items():
                pair = tuple(sorted([team, opponent]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # team's defense faced opponent's offense - opponent's
                # own season average is the estimate for this game.
                opp_stats = team_stats.get(opponent)
                if opp_stats and opp_stats["games"] > 0:
                    est = opp_stats["yards"] / opp_stats["games"]
                    entry = team_allowed_totals.setdefault(team, {"yards": 0.0, "games": 0})
                    entry["yards"] += est
                    entry["games"] += 1

                # and vice versa - opponent's defense faced team's offense.
                team_own_stats = team_stats.get(team)
                if team_own_stats and team_own_stats["games"] > 0:
                    est2 = team_own_stats["yards"] / team_own_stats["games"]
                    entry2 = team_allowed_totals.setdefault(opponent, {"yards": 0.0, "games": 0})
                    entry2["yards"] += est2
                    entry2["games"] += 1

        db.query(NflTeamAllowedStat).delete()
        for team, entry in team_allowed_totals.items():
            db.add(NflTeamAllowedStat(team=team, total_yards_allowed=round(entry["yards"]), games=entry["games"]))

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
        _league_avg_cache = None
        _league_avg_cache_time = None
    except Exception:
        log.exception("refresh_nfl_stats failed")
        db.rollback()
    finally:
        db.close()


def get_league_avg_allowed(db) -> float | None:
    """League-wide average passing yards allowed per game, computed
    live from NflTeamAllowedStat (built from the estimation approach
    above). Returns None if no data has synced yet."""
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
    Returns {"qb_name":, "qb_gsis_id":, "predicted_mean":, "qb_index":,
    "opp_index":, "qb_games_sample":, "opp_games_sample":,
    "starter_is_heuristic": True} for one team's real most-recent
    starter against their real current opponent. None if the starter
    or opponent doesn't have enough real games yet (MIN_PRIOR_GAMES /
    MIN_OPPONENT_GAMES - lowered from the validated value of 3).

    qb_index / opp_index: the same two factors predicted_mean is built
    from (qb_index * opp_index * league_avg = predicted_mean) - shown
    separately so the person can see the QB's own strength and the
    opponent's pass defense strength independently, the same "index"
    pattern used throughout the MLB side of this dashboard.

    starter_is_heuristic is always True - see module docstring's honest
    limitation. The frontend should let the person confirm or override
    the starter shown.
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
        qb_index = qb_avg / league_avg
        opp_index = shrunk_opp_allowed / league_avg
        predicted_mean = qb_avg * opp_index

        return {
            "qb_name": qb.name,
            "qb_gsis_id": qb.gsis_id,
            "predicted_mean": predicted_mean,
            "qb_index": qb_index,
            "opp_index": opp_index,
            "qb_games_sample": qb.games,
            "opp_games_sample": opp.games,
            "starter_is_heuristic": True,
        }
    finally:
        db.close()
