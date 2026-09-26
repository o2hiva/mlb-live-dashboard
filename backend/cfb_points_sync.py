"""
cfb_points_sync.py

CFB "Team Points" prop (each team's predicted points scored, with a
Yes/No O/U line) - the live-dashboard port of core_ncaa_points.py /
backtest_ncaa_points_total.py, ported from a local, resumable-JSON
backtest into a live backend sync, same pattern as
nfl_passing_yards_sync.py.

UNLIKE the NFL passing-yards prop, this one needed NO estimation
workaround: CFBD's own /games endpoint (api.collegefootballdata.com)
returns real, final, per-game scores directly (homePoints/awayPoints,
confirmed camelCase fields per backtest_ncaa_points_total.py's own
investigation) - there's no missing-per-game-data problem here at all.
Every completed FBS-vs-FBS game this season is real ground truth.

FORMULA (verbatim from core_ncaa_points.py, validated against the full
2025 season):
    predicted_team_points = team_scoring_index * opp_allowed_index * league_avg_points
    team_scoring_index = shrunk(team's own points-scored history) / league_avg_points
    opp_allowed_index  = shrunk(opponent's points-allowed history) / league_avg_points
    shrinkage_k = 5.0 games (validated via a real k-sweep)
    Negative Binomial overdispersion = 5.2 for a single team's score
    (game-total/combined prop deliberately NOT built yet - team points only, per scope)

POPULATION MATCH: only FBS-vs-FBS games count (mixing in FCS blowouts
would bias both a team's "points allowed" history and the league
average - the same population-mismatch bug class already fixed in the
MLB Hits Allowed prop).

REBUILD STRATEGY: unlike NFL (where a full rebuild needs care around
estimation), this fully rebuilds CfbTeamPointsStat and CfbGame from
scratch every call - walking every week 1..current_week, refetching
each week's real games from CFBD. This is simple, always correct, and
cheap (at most ~15 API calls for a full season) - no incremental/
snapshot state to keep in sync.
"""
import logging
import os
from datetime import datetime, timedelta

import requests

from database import SessionLocal
from models_db import CfbTeamPointsStat, CfbGame

log = logging.getLogger("cfb_points_sync")

API_BASE = "https://api.collegefootballdata.com"
TIMEOUT = 30

MIN_PRIOR_GAMES = 3          # min games of a team's own history before its scoring/allowed avg is trusted
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 20  # min teams w/ >= MIN_PRIOR_GAMES before trusting a LIVE league average

DEFAULT_POINTS_SHRINKAGE_K = 5.0          # validated via 2025 season k-sweep (core_ncaa_points.py)
DEFAULT_TEAM_OVERDISPERSION = 5.2         # validated: sd_z = 0.994 on 1080 real 2025 team-games

# Validated empirical value from ncaa_points_total_progress_2025.json's
# league_points_pool: 1522 real 2025 FBS-vs-FBS team-games, sum/len = 26.239159900131406
LEAGUE_AVG_POINTS_STATIC = 26.24

CACHE_TTL = timedelta(hours=6)
_league_avg_cache = {"value": None, "computed_at": None}


def get_api_key() -> str:
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        raise RuntimeError(
            "CFBD_API_KEY environment variable not set. Get a free key at "
            "https://collegefootballdata.com/key and set it in Railway's variables."
        )
    return key


def get_week_games(year: int, week: int, season_type: str = "regular") -> list[dict]:
    """Every game for one week, in CFBD's real, confirmed /games shape
    (camelCase: homeTeam, homePoints, awayTeam, awayPoints, completed,
    homeClassification, awayClassification, id)."""
    resp = requests.get(
        f"{API_BASE}/games",
        params={"year": year, "week": week, "seasonType": season_type},
        headers={"Authorization": f"Bearer {get_api_key()}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def is_fbs_vs_fbs(game: dict) -> bool:
    return (
        bool(game.get("completed"))
        and game.get("homeClassification") == "fbs"
        and game.get("awayClassification") == "fbs"
        and game.get("homePoints") is not None
        and game.get("awayPoints") is not None
    )


def _shrunk_avg(sum_val: float, count: int, k: float, league_avg: float) -> float:
    return (sum_val + k * league_avg) / (count + k)


def compute_live_league_avg_points(team_points_counts: dict,
                                    min_qualifying_teams: int = MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE,
                                    static_default: float = LEAGUE_AVG_POINTS_STATIC) -> float:
    """Hybrid live/static league-average-points baseline (same pattern as
    every other prop's hybrid baseline in this dashboard). Below
    min_qualifying_teams teams with enough games, falls back to the
    validated static constant from the 2025 season; above it, computes
    the average live from real accumulated data."""
    qualifying = [c for c in team_points_counts.values() if c["games"] >= MIN_PRIOR_GAMES]
    if len(qualifying) < min_qualifying_teams:
        return static_default
    total_points = sum(c["points_scored_sum"] for c in qualifying)
    total_games = sum(c["games"] for c in qualifying)
    if total_games == 0:
        return static_default
    return total_points / total_games


def get_league_avg_points(db) -> float:
    """Cached (6h) live-computed league average, same pattern as
    nfl_passing_yards_sync.get_league_avg_allowed - avoids recomputing
    from every row on every single request."""
    now = datetime.utcnow()
    if _league_avg_cache["value"] is not None and _league_avg_cache["computed_at"] is not None \
            and now - _league_avg_cache["computed_at"] < CACHE_TTL:
        return _league_avg_cache["value"]

    rows = db.query(CfbTeamPointsStat).all()
    team_points_counts = {
        r.team: {"games": r.games, "points_scored_sum": r.points_scored_sum, "points_allowed_sum": r.points_allowed_sum}
        for r in rows
    }
    value = compute_live_league_avg_points(team_points_counts)
    _league_avg_cache["value"] = value
    _league_avg_cache["computed_at"] = now
    return value


def refresh_cfb_points_stats(season: int, current_week: int) -> dict:
    """
    Rebuilds CfbTeamPointsStat (every FBS team's real season-to-date
    points-scored/points-allowed totals, weeks 1..current_week) and
    CfbGame (this week's real matchups, from whatever week=current_week
    games CFBD has posted so far, whether or not they've been played
    yet) from scratch. Safe to call any time, any number of times -
    always fully rebuilt from real CFBD data, no stale incremental state.
    """
    db = SessionLocal()
    try:
        team_totals: dict[str, dict] = {}
        weeks_fetched = 0
        games_used = 0

        for week in range(1, current_week + 1):
            try:
                games = get_week_games(season, week)
            except requests.exceptions.RequestException:
                log.exception("Failed to fetch CFB week %s games", week)
                continue
            weeks_fetched += 1

            for game in games:
                if not is_fbs_vs_fbs(game):
                    continue
                home, away = game["homeTeam"], game["awayTeam"]
                home_pts, away_pts = game["homePoints"], game["awayPoints"]

                for team, pts, allowed_pts in ((home, home_pts, away_pts), (away, away_pts, home_pts)):
                    row = team_totals.setdefault(team, {"games": 0, "points_scored_sum": 0, "points_allowed_sum": 0})
                    row["games"] += 1
                    row["points_scored_sum"] += pts
                    row["points_allowed_sum"] += allowed_pts
                games_used += 1

        db.query(CfbTeamPointsStat).delete()
        for team, totals in team_totals.items():
            db.add(CfbTeamPointsStat(
                team=team, games=totals["games"],
                points_scored_sum=totals["points_scored_sum"],
                points_allowed_sum=totals["points_allowed_sum"],
            ))

        # Current week's real matchups (may include not-yet-played games -
        # is_fbs_vs_fbs isn't applied here, we want the schedule even
        # before it's final, same as NflGame's own "this week's matchups"
        # table). A team can appear at most once per week in a normal
        # schedule, so team-as-primary-key (like NflGame) is safe here too.
        db.query(CfbGame).delete()
        try:
            this_week_games = get_week_games(season, current_week)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch CFB current week %s schedule", current_week)
            this_week_games = []
        for game in this_week_games:
            home, away = game.get("homeTeam"), game.get("awayTeam")
            if not home or not away:
                continue
            db.add(CfbGame(team=home, opponent=away, game_id=game.get("id"), is_home=True, season=season, week=current_week))
            db.add(CfbGame(team=away, opponent=home, game_id=game.get("id"), is_home=False, season=season, week=current_week))

        db.commit()
        _league_avg_cache["value"] = None  # force recompute next read, using the fresh data just written
        summary = {"weeks_fetched": weeks_fetched, "teams": len(team_totals), "games_used": games_used,
                   "current_week_matchups": len(this_week_games)}
        log.info("refresh_cfb_points_stats complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def compute_cfb_team_points_prediction(team: str, opponent: str, db, league_avg: float | None = None) -> dict | None:
    """Returns {"mean":, "team_index":, "opp_index":, "team_games_sample":,
    "opp_games_sample":} for `team`'s predicted points against `opponent`,
    or None if either side doesn't have MIN_PRIOR_GAMES yet. Mirrors
    nfl_passing_yards_sync.compute_passing_yards_prediction's shape for
    consistency across the dashboard's prop modules."""
    team_row = db.get(CfbTeamPointsStat, team)
    opp_row = db.get(CfbTeamPointsStat, opponent)
    if not team_row or not opp_row or team_row.games < MIN_PRIOR_GAMES or opp_row.games < MIN_PRIOR_GAMES:
        return None

    if league_avg is None:
        league_avg = get_league_avg_points(db)

    shrunk_scored = _shrunk_avg(team_row.points_scored_sum, team_row.games, DEFAULT_POINTS_SHRINKAGE_K, league_avg)
    shrunk_allowed = _shrunk_avg(opp_row.points_allowed_sum, opp_row.games, DEFAULT_POINTS_SHRINKAGE_K, league_avg)

    team_index = shrunk_scored / league_avg
    opp_index = shrunk_allowed / league_avg
    mean = team_index * opp_index * league_avg

    return {
        "mean": mean,
        "team_index": team_index,
        "opp_index": opp_index,
        "team_games_sample": team_row.games,
        "opp_games_sample": opp_row.games,
        "league_avg_points": league_avg,
    }
