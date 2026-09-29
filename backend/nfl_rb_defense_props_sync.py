"""
nfl_rb_defense_props_sync.py

NFL "By Position" -> RB tab: six props wired to Defense-vs-RB-position
data (rushing_yards, rushing_tds, receiving_yards, receiving_tds,
total_yards, total_tds), plus a derived anytime-TD probability - the
live-dashboard port of core_nfl_rb_defense_props.py, validated against
the real 2023 NFL season (523 RB-games).

FORMULA (verbatim from core_nfl_rb_defense_props.py) - TWO SEPARATE
LEAGUE AVERAGES, NOT ONE, unlike the QB version of this tab
(nfl_qb_defense_props_sync.py): the RB's own side is ONE PLAYER's own
number, shrunk toward and indexed against the population of INDIVIDUAL
RBs; the opponent-allowed side is a TEAM-WEEK TOTAL (summed across
every RB who touched the ball against that defense - committee
backfields are common), shrunk toward and indexed against the
population of TEAM-WEEK TOTALS. Those are genuinely different scales,
so each side gets its own league average, then the recombined index is
scaled back to the individual-player scale:

    predicted = rb_index * opp_allowed_index * own_league_avg
    rb_index          = shrunk(RB's own history, toward own_league_avg)  / own_league_avg
    opp_allowed_index = shrunk(opponent's allowed-to-RB, toward opp_league_avg) / opp_league_avg
    shrinkage_k = 8.0 (verbatim, both sides, all six props)

DISTRIBUTIONS (verbatim, validated 2023):
    rushing_yards:   normal, residual_sd = 31.2
    receiving_yards: normal, residual_sd = 19.8
    total_yards:     normal, residual_sd = 36.0
    rushing_tds:     negbinom, overdispersion = 1.08
    receiving_tds:   negbinom, overdispersion = 1.36
    total_tds:       poisson (no real overdispersion found)
Probability math for these lives in the frontend, same split as every
other prop in this dashboard - this module only computes predicted
means + indexes. ANYTIME-TD is NOT a 7th prop with its own formula -
it's P(total_tds > 0), computed client-side off total_tds_mean via the
same Poisson the total_tds prop itself uses (see
core_nfl_rb_defense_props.py's rb_anytime_td_probability - a thin
wrapper around the total_tds distribution, not a separate index).

THE SAME CONFIRMED DEAD END nfl_passing_yards_sync.py/
nfl_qb_defense_props_sync.py ALREADY DOCUMENTED: core_nfl_rb_defense_
props.py's own loader expects real per-game logs (from a local backtest
progress file this dashboard doesn't have and can't build against this
live API - the per-player weekly endpoint returns empty). So this
module uses the same estimation approach every other By-Position/props
module in this dashboard already had to adopt.

TOTAL_YARDS/TOTAL_TDS ARE DERIVED, NEVER STORED SEPARATELY - exactly
like core_nfl_rb_defense_props.py's own loader (_game_value sums
rushing+receiving on the fly): this module's own DB tables only carry
the four raw fields (rushing_yards, rushing_tds, receiving_yards,
receiving_tds); _derived_sum below adds them together at prediction/
league-avg time.

OWN-SIDE QUALIFYING FLOOR IS A SEASON TOTAL, NOT A PER-GAME ONE: the
core script's MIN_TOUCHES_OWN=8 filters individual GAMES to "real
workload" games before averaging them - impossible without per-game
logs. This module substitutes a SEASON-LEVEL touches (carries+targets)
floor, LIVE_MIN_SEASON_TOUCHES=20, for which RBs get shown/tracked
individually at all - same substitution nfl_rushing_yards_sync.py
already made (LIVE_MIN_SEASON_CARRIES=20) and nfl_qb_defense_props_
sync.py made (LIVE_MIN_SEASON_ATTEMPTS=20). A real, disclosed accuracy
tradeoff: a committee back's season average is diluted by every game
played, including low-workload ones the original filter would have
excluded.

OPPONENT-ALLOWED SIDE: built from team_rb_aggregate, which sums EVERY
RB who played for a team this season (no touches floor at all, mirroring
the core loader's OPPONENT_MIN_TOUCHES=1 - "any real involvement counts
toward what a defense allowed"), with games taken as the MAX games
value across those RBs (same honest team-week-granularity approximation
nfl_qb_defense_props_sync.py's team_qb_aggregate already uses, and the
same limitation: not exact when touches were split more evenly across
a committee than one RB's own game count implies).

MIN_PRIOR_GAMES: the validated value is 3 (same choice made for NFL/CFB
Team Points and the QB version of this tab), but it's TEMPORARILY
LOWERED TO 2 by explicit request so early-season data can be previewed
before week 4 - see the constant's own comment below. Revert to 3 once
previewing is done.
"""
import logging
import time

import requests

from database import SessionLocal
from models_db import NflRbDefensePropStat, NflRbDefenseAllowedPropStat, NflRbDefensePropGame

log = logging.getLogger("nfl_rb_defense_props_sync")

API_BASE = "https://api.nfldata.org/v1"
TIMEOUT = 30

RAW_FIELDS = ["rushing_yards", "rushing_tds", "receiving_yards", "receiving_tds", "carries", "receptions"]
PROP_FIELDS = RAW_FIELDS + ["total_yards", "total_tds"]

# Verbatim from core_nfl_rb_defense_props.py.
DEFAULT_SHRINKAGE_K = 8.0
MIN_QUALIFYING_FOR_LIVE_BASELINE = 16  # applies to BOTH qualifying RBs (own side) and qualifying defenses (opp side)

# TEMPORARILY LOWERED FROM THE VALIDATED VALUE (3) TO 2, BY EXPLICIT
# REQUEST, so early-season data can be previewed before week 4 - same
# temporary change already made to nfl_qb_defense_props_sync.py's own
# MIN_PRIOR_GAMES, for the same reason. Below 3 real games, the
# shrinkage/index math still runs, it's just leaning more on the
# league-average prior than the validated backtest assumed.
MIN_PRIOR_GAMES = 2

# Season-level substitute for the core script's per-game MIN_TOUCHES_OWN=8
# floor - see module docstring's disclosed tradeoff.
LIVE_MIN_SEASON_TOUCHES = 20

# distribution config per prop: (distribution, param) - verbatim from
# core_nfl_rb_defense_props.py. Kept here for reference/debug endpoints;
# the frontend has its own copy for the actual probability calculation.
PROP_DISTRIBUTION = {
    "rushing_yards": ("normal", 31.2),
    "receiving_yards": ("normal", 19.8),
    "total_yards": ("normal", 36.0),
    "rushing_tds": ("negbinom", 1.08),
    "receiving_tds": ("negbinom", 1.36),
    "total_tds": ("poisson", None),
    "carries": ("negbinom", 2.0),
    "receptions": ("negbinom", 1.34),
}

# Validated empirical values: own_league_avg_snapshot / opp_league_avg_snapshot
# on the last pick of a full 2023-season run of backtest_nfl_rb_defense_props.py
# (523 real RB-games) - verbatim from core_nfl_rb_defense_props.py.
OWN_LEAGUE_AVG_STATIC = {
    "rushing_yards": 55.29,
    "rushing_tds": 0.40,
    "receiving_yards": 18.759,
    "receiving_tds": 0.104,
    "total_yards": 74.049,
    "total_tds": 0.504,
    "carries": 13.155,
    "receptions": 2.565,
}
OPP_LEAGUE_AVG_STATIC = {
    "rushing_yards": 83.81,
    "rushing_tds": 0.584,
    "receiving_yards": 29.955,
    "receiving_tds": 0.157,
    "total_yards": 113.765,
    "total_tds": 0.741,
    "carries": 20.082,
    "receptions": 4.157,
}

CACHE_TTL_SECONDS = 6 * 60 * 60
_own_league_avg_cache = {}
_opp_league_avg_cache = {}
_league_avg_cache_time = None


def get_season_rbs_raw(season: int) -> list:
    """EVERY RB with a real row this season, NO touches floor - used to
    build each team's true RB-position aggregate (every back who
    touched the ball, committee backfields included), not just the
    qualifying backs shown individually. Confirmed field names (live
    /v1/stats/season, 2026 season): player_id, player_display_name/
    player_name, recent_team, games, carries, targets, rushing_yards,
    rushing_tds, receptions, receiving_yards, receiving_tds."""
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
            if row.get("position") == "RB":
                carries = row.get("carries", 0) or 0
                targets = row.get("targets", 0) or 0
                rbs.append({
                    "gsis_id": row["player_id"],
                    "name": row.get("player_display_name", row.get("player_name", "")),
                    "team": row.get("recent_team"),
                    "games": row.get("games", 0) or 0,
                    "touches": carries + targets,
                    "rushing_yards": row.get("rushing_yards", 0) or 0,
                    "rushing_tds": row.get("rushing_tds", 0) or 0,
                    "receiving_yards": row.get("receiving_yards", 0) or 0,
                    "receiving_tds": row.get("receiving_tds", 0) or 0,
                    "carries": carries,
                    "receptions": row.get("receptions", 0) or 0,
                })
        total = payload.get("total", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.15)
    return rbs


def team_rb_aggregate(all_rbs_raw: list) -> dict:
    """{team: {games, rushing_yards, rushing_tds, receiving_yards,
    receiving_tds}} - every RB who has played for that team this
    season, summed (numerator), games taken as the MAX across those RBs
    (proxy for the team's own real games played - see module
    docstring's honest limitation). This is the RB-POSITION team-week
    total used as the opponent-allowed estimate."""
    agg = {}
    for rb in all_rbs_raw:
        team = rb["team"]
        if not team:
            continue
        entry = agg.setdefault(team, {"games": 0, "rushing_yards": 0, "rushing_tds": 0,
                                       "receiving_yards": 0, "receiving_tds": 0,
                                       "carries": 0, "receptions": 0})
        entry["games"] = max(entry["games"], rb["games"])
        entry["rushing_yards"] += rb["rushing_yards"]
        entry["rushing_tds"] += rb["rushing_tds"]
        entry["receiving_yards"] += rb["receiving_yards"]
        entry["receiving_tds"] += rb["receiving_tds"]
        entry["carries"] += rb["carries"]
        entry["receptions"] += rb["receptions"]
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


def _derived_sum(row, prop: str) -> float:
    """Reads a raw field directly, or derives total_yards/total_tds by
    adding rushing + receiving on the fly - these are never stored as
    their own columns, matching core_nfl_rb_defense_props.py's own
    _game_value helper."""
    if prop == "total_yards":
        return row.rushing_yards_sum + row.receiving_yards_sum
    if prop == "total_tds":
        return row.rushing_tds_sum + row.receiving_tds_sum
    return getattr(row, f"{prop}_sum")


def refresh_nfl_rb_defense_props_stats(season: int, current_week: int) -> dict:
    """
    Rebuilds NflRbDefensePropStat directly from /stats/season (exact, no
    estimation needed - every qualifying RB's own real season totals for
    the four raw props). Rebuilds NflRbDefenseAllowedPropStat by walking
    the real schedule for every completed week and, for each real game a
    team played, adding their opponent's own RB-position aggregate
    (team_rb_aggregate) as the ESTIMATE of what that opponent's RB(s)
    produced in that game. Then refreshes NflRbDefensePropGame with the
    given week's real matchups.
    """
    db = SessionLocal()
    try:
        all_rbs_raw = get_season_rbs_raw(season)
        qualifying_rbs = [r for r in all_rbs_raw if r["touches"] >= LIVE_MIN_SEASON_TOUCHES]

        db.query(NflRbDefensePropStat).delete()
        for rb in qualifying_rbs:
            db.add(NflRbDefensePropStat(
                gsis_id=rb["gsis_id"], name=rb["name"], team=rb["team"],
                games=rb["games"], touches=rb["touches"],
                rushing_yards_sum=rb["rushing_yards"], rushing_tds_sum=rb["rushing_tds"],
                receiving_yards_sum=rb["receiving_yards"], receiving_tds_sum=rb["receiving_tds"],
                carries_sum=rb["carries"], receptions_sum=rb["receptions"],
            ))

        team_agg = team_rb_aggregate(all_rbs_raw)
        defense_allowed_totals = {}  # team -> {games, rushing_yards, rushing_tds, receiving_yards, receiving_tds}

        for week in range(1, current_week):
            try:
                matchups = get_week_games(season, week)
            except requests.exceptions.RequestException:
                log.exception("Failed to fetch NFL RB DvP week %s schedule", week)
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
                        team, {"games": 0, "rushing_yards": 0.0, "rushing_tds": 0.0,
                               "receiving_yards": 0.0, "receiving_tds": 0.0,
                               "carries": 0.0, "receptions": 0.0})
                    entry["games"] += 1
                    for prop in RAW_FIELDS:
                        entry[prop] += opp_agg[prop] / opp_agg["games"]

                team_agg_self = team_agg.get(team)
                if team_agg_self and team_agg_self["games"] > 0:
                    entry2 = defense_allowed_totals.setdefault(
                        opponent, {"games": 0, "rushing_yards": 0.0, "rushing_tds": 0.0,
                                   "receiving_yards": 0.0, "receiving_tds": 0.0,
                                   "carries": 0.0, "receptions": 0.0})
                    entry2["games"] += 1
                    for prop in RAW_FIELDS:
                        entry2[prop] += team_agg_self[prop] / team_agg_self["games"]

        db.query(NflRbDefenseAllowedPropStat).delete()
        for team, entry in defense_allowed_totals.items():
            db.add(NflRbDefenseAllowedPropStat(
                team=team, games=entry["games"],
                rushing_yards_sum=round(entry["rushing_yards"]), rushing_tds_sum=round(entry["rushing_tds"]),
                receiving_yards_sum=round(entry["receiving_yards"]), receiving_tds_sum=round(entry["receiving_tds"]),
                carries_sum=round(entry["carries"]), receptions_sum=round(entry["receptions"]),
            ))

        try:
            this_week_matchups = get_current_week_matchups_with_dates(season, current_week)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch NFL RB DvP current week %s schedule", current_week)
            this_week_matchups = {}

        db.query(NflRbDefensePropGame).delete()
        for team, info in this_week_matchups.items():
            db.add(NflRbDefensePropGame(team=team, opponent=info["opponent"], season=season,
                                         week=current_week, gameday=info["gameday"]))

        db.commit()
        global _own_league_avg_cache, _opp_league_avg_cache, _league_avg_cache_time
        _own_league_avg_cache = {}
        _opp_league_avg_cache = {}
        _league_avg_cache_time = None

        summary = {"rbs": len(qualifying_rbs), "defenses": len(defense_allowed_totals),
                   "current_week_matchups": len(this_week_matchups)}
        log.info("refresh_nfl_rb_defense_props_stats complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_league_avgs(db) -> tuple:
    """Cached (6h) live-computed league averages for all six props, as a
    (own_avgs, opp_avgs) pair - own and opp are separate populations
    (see module docstring), so each gets its OWN hybrid live/static
    baseline rather than sharing one the way the QB version of this tab
    does."""
    import datetime as _dt
    global _own_league_avg_cache, _opp_league_avg_cache, _league_avg_cache_time
    now = _dt.datetime.utcnow()
    if _own_league_avg_cache and _opp_league_avg_cache and _league_avg_cache_time is not None \
            and (now - _league_avg_cache_time).total_seconds() < CACHE_TTL_SECONDS:
        return _own_league_avg_cache, _opp_league_avg_cache

    own_rows = db.query(NflRbDefensePropStat).all()
    own_qualifying = [r for r in own_rows if r.games >= MIN_PRIOR_GAMES]
    own_result = {}
    for prop in PROP_FIELDS:
        if len(own_qualifying) < MIN_QUALIFYING_FOR_LIVE_BASELINE:
            own_result[prop] = OWN_LEAGUE_AVG_STATIC[prop]
            continue
        total = sum(_derived_sum(r, prop) for r in own_qualifying)
        games = sum(r.games for r in own_qualifying)
        own_result[prop] = (total / games) if games else OWN_LEAGUE_AVG_STATIC[prop]

    opp_rows = db.query(NflRbDefenseAllowedPropStat).all()
    opp_qualifying = [r for r in opp_rows if r.games >= MIN_PRIOR_GAMES]
    opp_result = {}
    for prop in PROP_FIELDS:
        if len(opp_qualifying) < MIN_QUALIFYING_FOR_LIVE_BASELINE:
            opp_result[prop] = OPP_LEAGUE_AVG_STATIC[prop]
            continue
        total = sum(_derived_sum(r, prop) for r in opp_qualifying)
        games = sum(r.games for r in opp_qualifying)
        opp_result[prop] = (total / games) if games else OPP_LEAGUE_AVG_STATIC[prop]

    _own_league_avg_cache = own_result
    _opp_league_avg_cache = opp_result
    _league_avg_cache_time = now
    return own_result, opp_result


def compute_rb_defense_props_prediction(rb: NflRbDefensePropStat, opponent: str, db,
                                         league_avgs: tuple = None) -> dict:
    """
    Returns {"rb_name":, "rb_gsis_id":, "rb_games_sample":,
    "opp_games_sample":, "<prop>_mean":, "<prop>_rb_index":,
    "<prop>_opp_index":  (for each of the six props)} for ONE specific
    RB (caller picks which - unlike the QB version, this is called once
    per qualifying RB on a team, not just the top one, so committee
    backfields show up as multiple cards). A prop's own mean/index
    fields are None individually if either side lacks MIN_PRIOR_GAMES
    (== 3, the validated value - see module docstring), so a matchup
    can show data for none, some, or all six props depending on real
    games played so far.
    """
    opp = db.get(NflRbDefenseAllowedPropStat, opponent)

    if league_avgs is None:
        league_avgs = get_league_avgs(db)
    own_league_avgs, opp_league_avgs = league_avgs

    result = {
        "rb_name": rb.name,
        "rb_gsis_id": rb.gsis_id,
        "rb_games_sample": rb.games,
        "opp_games_sample": opp.games if opp else None,
    }

    for prop in PROP_FIELDS:
        result[f"{prop}_mean"] = None
        result[f"{prop}_rb_index"] = None
        result[f"{prop}_opp_index"] = None
        if opp is None or rb.games < MIN_PRIOR_GAMES or opp.games < MIN_PRIOR_GAMES:
            continue
        own_avg = own_league_avgs.get(prop, OWN_LEAGUE_AVG_STATIC[prop])
        opp_avg = opp_league_avgs.get(prop, OPP_LEAGUE_AVG_STATIC[prop])
        if own_avg <= 0 or opp_avg <= 0:
            continue
        rb_sum = _derived_sum(rb, prop)
        opp_sum = _derived_sum(opp, prop)
        shrunk_rb = _shrunk_avg(rb_sum, rb.games, DEFAULT_SHRINKAGE_K, own_avg)
        shrunk_opp = _shrunk_avg(opp_sum, opp.games, DEFAULT_SHRINKAGE_K, opp_avg)
        rb_index = shrunk_rb / own_avg
        opp_index = shrunk_opp / opp_avg
        result[f"{prop}_mean"] = rb_index * opp_index * own_avg
        result[f"{prop}_rb_index"] = rb_index
        result[f"{prop}_opp_index"] = opp_index

    return result
