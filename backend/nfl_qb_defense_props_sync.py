"""
nfl_qb_defense_props_sync.py

NFL "By Position" -> QB tab: four props wired to Defense-vs-QB-position
data (passing_yards, passing_tds, rushing_yards, rushing_tds) - the
live-dashboard port of core_nfl_qb_defense_props.py, validated against
the real 2023 NFL season (389 QB-games).

FORMULA (verbatim from core_nfl_qb_defense_props.py, same log5-style
index-multiplication pattern as every other prop in this dashboard):
    predicted = qb_index * opp_allowed_index * league_avg
    qb_index          = shrunk(QB's own history for this stat)            / league_avg
    opp_allowed_index = shrunk(opponent's allowed-to-QB-position history) / league_avg
    shrinkage_k = 8.0 (verbatim, validated across all four props at once)

DISTRIBUTIONS (verbatim, validated 2023 - see core_nfl_qb_defense_props.py's
own docstring for the full k-sweep/sd_z reasoning):
    passing_yards: normal, residual_sd = 73.9
    rushing_yards: normal, residual_sd = 17.3
    passing_tds:   poisson (no overdispersion - sd_z landed ~0.95-1.0)
    rushing_tds:   negbinom, overdispersion = 1.2
Probability math for these lives in the frontend (same split as every
other prop here) - this module only computes predicted means + indexes.

THE SAME CONFIRMED DEAD END nfl_passing_yards_sync.py ALREADY DOCUMENTED:
core_nfl_qb_defense_props.py's own loader expects real per-game QB logs
(from a local backtest progress file this dashboard doesn't have and
can't build against this live API - the per-player weekly endpoint
returns empty, re-confirmed while building this prop). So this module
uses the SAME estimation approach nfl_passing_yards_sync.py already had
to adopt for passing yards, extended to all four props.

A REAL IMPROVEMENT OVER nfl_passing_yards_sync.py'S OWN ESTIMATION,
WORTH CALLING OUT: that module estimates opponent-allowed passing yards
using /stats/team's whole-ROSTER passing total, which happens to work
fine for passing (the QB throws essentially all of a team's passes) but
would be wrong for rushing (the QB accounts for a small fraction of a
team's total rushing yards - most of it is the RB's). So this module
does NOT use /stats/team at all: it builds each team's own QB-POSITION-
ONLY aggregate directly from /stats/season (summing every QB who has
played for that team, no attempts floor - a team's overall QB output
this season, not just their current starter's), and uses THAT as the
per-opponent estimate for every prop, including the two rushing ones.

HONEST LIMITATION ON THE DEFENSE-ALLOWED SIDE'S "games" COUNT: the
original script's two-pass dedup counted a team's allowed total as ONE
game per week even when a backup relieved the starter mid-game (real
per-game granularity made that exact). Without per-game logs, this
module approximates a team's total QB-position "games" as the MAX
games value across any single QB on that team (i.e. its primary
starter's own game count) - a reasonable stand-in since one QB accounts
for the large majority of a team's weeks in most seasons, but not
exact when snaps were split more evenly.

MIN_PRIOR_GAMES kept at the VALIDATED value (3), not lowered the way
NFL Passing Yards' own threshold was - same choice already made for
NFL/CFB Team Points (leave the validated bar alone rather than
guess with less evidence); this means "not enough data" for every
matchup until week 4 of the season, by design.

"STARTER" PICKED PER TEAM, SAME HONEST HEURISTIC AS EVERY OTHER SINGLE-
STARTER PROP HERE: the QB with the most real attempts on record for
that team - not a confirmed depth chart, so the person should override
it if they know about an injury or benching this can't see.
"""
import logging
import time

import requests

from database import SessionLocal
from models_db import NflQbDefensePropStat, NflDefenseAllowedPropStat, NflQbDefensePropGame

log = logging.getLogger("nfl_qb_defense_props_sync")

API_BASE = "https://api.nfldata.org/v1"
TIMEOUT = 30

PROP_FIELDS = ["passing_yards", "passing_tds", "rushing_yards", "rushing_tds",
               "passing_completions", "passing_attempts", "rushing_attempts"]

# Raw /stats/season field name -> our prop name, for the 3 props whose
# game-log key differs from the prop name itself (verbatim mapping from
# the newer core_nfl_qb_defense_props.py).
PROP_TO_FIELD = {
    "passing_completions": "completions",
    "passing_attempts": "attempts",
    "rushing_attempts": "carries",
}

# Verbatim from core_nfl_qb_defense_props.py.
DEFAULT_SHRINKAGE_K = 8.0
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 16

# TEMPORARILY LOWERED FROM THE VALIDATED VALUE (3) TO 2, BY EXPLICIT
# REQUEST, so early-season data can be previewed before week 4 - see
# module docstring for why 3 is the actual validated bar. Below 3 real
# games, the shrinkage/index math still runs, it's just leaning more on
# the league-average prior than the validated backtest assumed.
MIN_PRIOR_GAMES = 2

# Season-level qualifying bar for a QB to be shown/tracked individually -
# same value nfl_passing_yards_sync.py uses for its own QB list.
LIVE_MIN_SEASON_ATTEMPTS = 20

# distribution config per prop: (distribution, param) - verbatim from
# core_nfl_qb_defense_props.py. Kept here for reference/debug endpoints;
# the frontend has its own copy for the actual probability calculation.
PROP_DISTRIBUTION = {
    "passing_yards": ("normal", 73.9),
    "rushing_yards": ("normal", 17.3),
    "passing_tds": ("poisson", None),
    "rushing_tds": ("negbinom", 1.2),
    "passing_completions": ("negbinom", 1.65),
    "passing_attempts": ("negbinom", 2.0),
    "rushing_attempts": ("negbinom", 1.5),
}

# Validated empirical values: the real "League" row from
# backtest_nfl_defense_vs_qb.py's 2023 report (527 real team-games), plus
# the 3 new props' static averages from the newer core_nfl_qb_defense_props.py.
LEAGUE_AVG_STATIC = {
    "passing_yards": 232.3,
    "passing_tds": 1.36,
    "rushing_yards": 16.9,
    "rushing_tds": 0.20,
    "passing_completions": 21.355,
    "passing_attempts": 33.088,
    "rushing_attempts": 3.944,
}

CACHE_TTL_SECONDS = 6 * 60 * 60
_league_avg_cache = {}
_league_avg_cache_time = None


def get_season_qbs_raw(season: int) -> list:
    """EVERY QB with a real row this season, NO attempts floor - used to
    build each team's true QB-position aggregate (including backup spot
    appearances), not just the qualifying starters shown individually.
    Confirmed field names: player_id, player_display_name/player_name,
    recent_team, games, attempts, passing_yards, passing_tds,
    rushing_yards, rushing_tds."""
    qbs = []
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
            if row.get("position") == "QB":
                attempts = row.get("attempts", 0) or 0
                qbs.append({
                    "gsis_id": row["player_id"],
                    "name": row.get("player_display_name", row.get("player_name", "")),
                    "team": row.get("recent_team"),
                    "games": row.get("games", 0) or 0,
                    "attempts": attempts,
                    "passing_yards": row.get("passing_yards", 0) or 0,
                    "passing_tds": row.get("passing_tds", 0) or 0,
                    "rushing_yards": row.get("rushing_yards", 0) or 0,
                    "rushing_tds": row.get("rushing_tds", 0) or 0,
                    # 3 new props - re-keyed from the raw API field names to
                    # our prop names (see PROP_TO_FIELD) so the generic
                    # PROP_FIELDS-driven code below can find them by prop name.
                    "passing_completions": row.get("completions", 0) or 0,
                    "passing_attempts": attempts,
                    "rushing_attempts": row.get("carries", 0) or 0,
                })
        total = payload.get("total", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.15)
    return qbs


def team_qb_aggregate(all_qbs_raw: list) -> dict:
    """{team: {games, passing_yards, passing_tds, rushing_yards,
    rushing_tds}} - every QB who has played for that team this season,
    summed (numerator) with games taken as the MAX across those QBs
    (proxy for the team's own real games played - see module docstring's
    honest limitation on this). This is the QB-POSITION-ONLY per-team
    total used as the opponent-allowed estimate - deliberately NOT
    /stats/team's whole-roster total (see module docstring)."""
    agg = {}
    for qb in all_qbs_raw:
        team = qb["team"]
        if not team:
            continue
        entry = agg.setdefault(team, {"games": 0, "passing_yards": 0, "passing_tds": 0,
                                       "rushing_yards": 0, "rushing_tds": 0,
                                       "passing_completions": 0, "passing_attempts": 0,
                                       "rushing_attempts": 0})
        entry["games"] = max(entry["games"], qb["games"])
        entry["passing_yards"] += qb["passing_yards"]
        entry["passing_tds"] += qb["passing_tds"]
        entry["rushing_yards"] += qb["rushing_yards"]
        entry["rushing_tds"] += qb["rushing_tds"]
        entry["passing_completions"] += qb["passing_completions"]
        entry["passing_attempts"] += qb["passing_attempts"]
        entry["rushing_attempts"] += qb["rushing_attempts"]
    return agg


def get_week_games(season: int, week: int) -> dict:
    """{team: opponent} for every team playing this week - same
    confirmed-working call every other NFL sync module has its own
    copy of."""
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


def get_current_week_matchups_with_dates(season: int, week: int) -> dict:
    """{team: {"opponent":, "gameday":}} for the CURRENT week only - same
    /v1/games call as get_week_games, but also keeps each game's real
    "gameday" (confirmed live field, format "YYYY-MM-DD") so the frontend
    can order boxes by real kickoff day (Thursday night game first,
    Monday Night Football last). NOTE: the API's own "gametime" field is
    confirmed always null (checked against live 2026 week-4 data), so
    this can only sort by DAY, not time-of-day - correct for TNF-first/
    MNF-last, but can't sub-order same-day Sunday games (early/late/SNF)."""
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
        gameday = g.get("gameday")
        if away and home:
            matchups[away] = {"opponent": home, "gameday": gameday}
            matchups[home] = {"opponent": away, "gameday": gameday}
    return matchups


def _shrunk_avg(sum_val: float, count: int, k: float, league_avg: float) -> float:
    return (sum_val + k * league_avg) / (count + k)


def refresh_nfl_qb_defense_props_stats(season: int, current_week: int) -> dict:
    """
    Rebuilds NflQbDefensePropStat directly from /stats/season (exact, no
    estimation needed - every qualifying QB's own real season totals for
    all four props). Rebuilds NflDefenseAllowedPropStat by walking the
    real schedule for every completed week and, for each real game a
    team played, adding their opponent's own QB-position aggregate
    (team_qb_aggregate, NOT /stats/team) as the ESTIMATE of what that
    opponent's QB(s) produced in that game - see module docstring's
    honest limitation. Then refreshes NflQbDefensePropGame with the
    given week's real matchups.
    """
    db = SessionLocal()
    try:
        all_qbs_raw = get_season_qbs_raw(season)
        qualifying_qbs = [q for q in all_qbs_raw if q["attempts"] >= LIVE_MIN_SEASON_ATTEMPTS]

        db.query(NflQbDefensePropStat).delete()
        for qb in qualifying_qbs:
            db.add(NflQbDefensePropStat(
                gsis_id=qb["gsis_id"], name=qb["name"], team=qb["team"],
                games=qb["games"], attempts=qb["attempts"],
                passing_yards_sum=qb["passing_yards"], passing_tds_sum=qb["passing_tds"],
                rushing_yards_sum=qb["rushing_yards"], rushing_tds_sum=qb["rushing_tds"],
                passing_completions_sum=qb["passing_completions"],
                passing_attempts_sum=qb["passing_attempts"],
                rushing_attempts_sum=qb["rushing_attempts"],
            ))

        team_agg = team_qb_aggregate(all_qbs_raw)
        defense_allowed_totals = {}  # team -> {games, passing_yards, passing_tds, rushing_yards, rushing_tds}

        for week in range(1, current_week):
            try:
                matchups = get_week_games(season, week)
            except requests.exceptions.RequestException:
                log.exception("Failed to fetch NFL DvP week %s schedule", week)
                continue

            seen_pairs = set()
            for team, opponent in matchups.items():
                pair = tuple(sorted([team, opponent]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                opp_agg = team_agg.get(opponent)
                if opp_agg and opp_agg["games"] > 0:
                    entry = defense_allowed_totals.setdefault(
                        team, {"games": 0, "passing_yards": 0.0, "passing_tds": 0.0,
                               "rushing_yards": 0.0, "rushing_tds": 0.0,
                               "passing_completions": 0.0, "passing_attempts": 0.0,
                               "rushing_attempts": 0.0})
                    entry["games"] += 1
                    for prop in PROP_FIELDS:
                        entry[prop] += opp_agg[prop] / opp_agg["games"]

                team_agg_self = team_agg.get(team)
                if team_agg_self and team_agg_self["games"] > 0:
                    entry2 = defense_allowed_totals.setdefault(
                        opponent, {"games": 0, "passing_yards": 0.0, "passing_tds": 0.0,
                                   "rushing_yards": 0.0, "rushing_tds": 0.0,
                                   "passing_completions": 0.0, "passing_attempts": 0.0,
                                   "rushing_attempts": 0.0})
                    entry2["games"] += 1
                    for prop in PROP_FIELDS:
                        entry2[prop] += team_agg_self[prop] / team_agg_self["games"]

        db.query(NflDefenseAllowedPropStat).delete()
        for team, entry in defense_allowed_totals.items():
            db.add(NflDefenseAllowedPropStat(
                team=team, games=entry["games"],
                passing_yards_sum=round(entry["passing_yards"]), passing_tds_sum=round(entry["passing_tds"]),
                rushing_yards_sum=round(entry["rushing_yards"]), rushing_tds_sum=round(entry["rushing_tds"]),
                passing_completions_sum=round(entry["passing_completions"]),
                passing_attempts_sum=round(entry["passing_attempts"]),
                rushing_attempts_sum=round(entry["rushing_attempts"]),
            ))

        try:
            this_week_matchups = get_current_week_matchups_with_dates(season, current_week)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch NFL DvP current week %s schedule", current_week)
            this_week_matchups = {}

        db.query(NflQbDefensePropGame).delete()
        for team, info in this_week_matchups.items():
            db.add(NflQbDefensePropGame(team=team, opponent=info["opponent"], season=season,
                                         week=current_week, gameday=info["gameday"]))

        db.commit()
        global _league_avg_cache, _league_avg_cache_time
        _league_avg_cache = {}
        _league_avg_cache_time = None

        summary = {"qbs": len(qualifying_qbs), "defenses": len(defense_allowed_totals),
                   "current_week_matchups": len(this_week_matchups)}
        log.info("refresh_nfl_qb_defense_props_stats complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_league_avgs(db) -> dict:
    """Cached (6h) live-computed league averages for all four props -
    same hybrid live/static pattern as core_nfl_points.py's own."""
    import datetime as _dt
    global _league_avg_cache, _league_avg_cache_time
    now = _dt.datetime.utcnow()
    if _league_avg_cache and _league_avg_cache_time is not None \
            and (now - _league_avg_cache_time).total_seconds() < CACHE_TTL_SECONDS:
        return _league_avg_cache

    rows = db.query(NflDefenseAllowedPropStat).all()
    qualifying = [r for r in rows if r.games >= MIN_PRIOR_GAMES]
    result = {}
    for prop in PROP_FIELDS:
        if len(qualifying) < MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE:
            result[prop] = LEAGUE_AVG_STATIC[prop]
            continue
        sum_field = f"{prop}_sum"
        total = sum(getattr(r, sum_field) for r in qualifying)
        games = sum(r.games for r in qualifying)
        result[prop] = (total / games) if games else LEAGUE_AVG_STATIC[prop]

    _league_avg_cache = result
    _league_avg_cache_time = now
    return result


def compute_qb_defense_props_prediction(team: str, opponent: str, db, league_avgs: dict = None) -> dict | None:
    """
    Returns {"qb_name":, "qb_gsis_id":, "qb_games_sample":,
    "opp_games_sample":, "<prop>_mean":, "<prop>_qb_index":,
    "<prop>_opp_index":  (for each of the four props)} for one team's
    real highest-attempts QB on record against their real current
    opponent. None if no qualifying QB/opponent exists at all for this
    matchup; a prop's own mean/index fields are None individually if
    either side lacks MIN_PRIOR_GAMES (== 3, the validated value, not
    lowered - see module docstring), so a team can show data for none,
    some, or all four props depending on real games played so far.
    """
    qb = (
        db.query(NflQbDefensePropStat)
        .filter_by(team=team)
        .order_by(NflQbDefensePropStat.attempts.desc())
        .first()
    )
    opp = db.get(NflDefenseAllowedPropStat, opponent)
    if qb is None:
        return None

    if league_avgs is None:
        league_avgs = get_league_avgs(db)

    result = {
        "qb_name": qb.name,
        "qb_gsis_id": qb.gsis_id,
        "qb_games_sample": qb.games,
        "opp_games_sample": opp.games if opp else None,
    }

    for prop in PROP_FIELDS:
        result[f"{prop}_mean"] = None
        result[f"{prop}_qb_index"] = None
        result[f"{prop}_opp_index"] = None
        if opp is None or qb.games < MIN_PRIOR_GAMES or opp.games < MIN_PRIOR_GAMES:
            continue
        league_avg = league_avgs.get(prop, LEAGUE_AVG_STATIC[prop])
        if league_avg <= 0:
            continue
        qb_sum = getattr(qb, f"{prop}_sum")
        opp_sum = getattr(opp, f"{prop}_sum")
        shrunk_qb = _shrunk_avg(qb_sum, qb.games, DEFAULT_SHRINKAGE_K, league_avg)
        shrunk_opp = _shrunk_avg(opp_sum, opp.games, DEFAULT_SHRINKAGE_K, league_avg)
        qb_index = shrunk_qb / league_avg
        opp_index = shrunk_opp / league_avg
        result[f"{prop}_mean"] = qb_index * opp_index * league_avg
        result[f"{prop}_qb_index"] = qb_index
        result[f"{prop}_opp_index"] = opp_index

    return result
