"""
nfl_points_sync.py

NFL "Team Points" / "Game Total" prop - the live-dashboard port of
core_nfl_points.py (the NFL sibling of core_ncaa_points.py, which
cfb_points_sync.py already ports for CFB). Same log5-style
index-multiplication + Negative Binomial pattern, just NFL's own
validated constants and NFL's own data source.

FORMULA (verbatim from core_nfl_points.py, validated against the full
real 2023 NFL season - 448 team-games / 224 full games):
    predicted_team_points = team_scoring_index * opp_allowed_index * league_avg_points
    team_scoring_index = shrunk(team's own points-scored history) / league_avg_points
    opp_allowed_index  = shrunk(opponent's points-allowed history) / league_avg_points
    shrinkage_k = 8.0 games (both team_sd and game_sd curves were quite FLAT
        across k=5..20, so this wasn't a sharp minimum - 8 sits close to the
        minimum for both markets at once)
    team score Negative Binomial overdispersion  = 4.4 (sd_z = 0.996)
    game total Negative Binomial overdispersion  = 4.4 (sd_z = 1.001)
        - landed on almost the SAME overdispersion for both markets here,
        unlike CFB's team=5.2/game=4.6 split - not assumed, just what the
        real 2023 NFL data showed.

UNLIKE NFL Passing Yards (nfl_passing_yards_sync.py), this prop needs NO
estimation workaround: api.nfldata.org's /v1/games endpoint returns real,
final, per-game scores directly (home_score/away_score), the same way
CFBD's /games does for CFB - every completed REG-season game is real
ground truth, no per-opponent-average approximation needed.

NO PER-GAME NUMERIC ID: unlike CFBD's /games (which has a game "id"
field CFB's CfbGame stores for later grading), api.nfldata.org's /v1/games
has no such field anywhere this dashboard's NFL code has found (confirmed
by nfl_passing_yards_sync.py, which identifies games purely by
team/opponent pairs). So grading a tracked bet here re-fetches the week's
real games and matches by TEAM NAME instead of a numeric id - see
bet_grading.py's _nfl_team_points_actual / _nfl_game_total_actual.

POPULATION: every NFL team is the same tier (no FBS/FCS-style
classification split needed, unlike CFB) - only game_type == "REG" and
both scores present (i.e. actually completed) games count.

REBUILD STRATEGY: same as CFB - this fully rebuilds NflTeamPointsStat and
NflPointsGame from scratch every call, walking every week 1..current_week
and refetching each week's real games. Simple, always correct, cheap (at
most ~18 weeks of API calls for a full season).
"""
import logging
import math
from datetime import datetime, timedelta

import requests

from database import SessionLocal
from models_db import NflTeamPointsStat, NflPointsGame

log = logging.getLogger("nfl_points_sync")

API_BASE = "https://api.nfldata.org/v1"
TIMEOUT = 30

MIN_PRIOR_GAMES = 3          # validated threshold from core_nfl_points.py - min games before a team's own history is trusted
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 16  # half the NFL's 32 teams - see core_nfl_points.py's own comment for why this differs from CFB's 20

DEFAULT_POINTS_SHRINKAGE_K = 8.0          # validated via 2023 season k-sweep (flat curve - chosen as a single round value)
DEFAULT_TEAM_OVERDISPERSION = 4.4         # validated: sd_z = 0.996 on 448 real 2023 team-games
DEFAULT_GAME_TOTAL_OVERDISPERSION = 4.4   # validated: sd_z = 1.001 on 224 real 2023 full games

# ---- NFL Moneyline (win probability) - validated params straight from
# core_nfl_moneyline.py (the user-supplied production module), itself
# ported from core_ncaa_moneyline.py the same way cfb_points_sync.py's
# win_probability/compute_cfb_moneyline already are for CFB:
#   SHRINKAGE_K=8.0, TEAM_OVERDISPERSION=4.4 (both already defined above,
#   shared with the Team Points prop), HOME_FIELD_POINTS=3.0,
#   VARIANCE_SCALE=1.0. Out-of-sample: Brier=0.2205, log loss=0.6289 (real
#   2025 holdout) - a real edge over the naive 50% baseline, smaller than
#   CFB's own (0.1886), consistent with the NFL being more parity-driven.
#
# UNLIKE cfb_points_sync.win_probability, this takes no neutral_site flag:
# core_nfl_moneyline.py's own docstring documents a KNOWN GAP that
# HOME_FIELD_POINTS is applied to EVERY game, including the ~3-5/season
# International Series games (London, Munich, Madrid, Sao Paulo) that are
# a true neutral site - api.nfldata.org's /v1/games response has not been
# confirmed to expose any neutral-site field this dashboard could use to
# suppress it the way CFBD's neutralSite flag lets the CFB side do. This
# ports that exact (documented, accepted) limitation rather than guessing
# at a flag that may not exist. ----
HOME_FIELD_POINTS = 3.0       # VALIDATED - core_nfl_moneyline.py / backtest_nfl_win_probability.py --holdout (real 2025 data)
VARIANCE_SCALE = 1.0          # VALIDATED - core_nfl_moneyline.py / backtest_nfl_win_probability.py --holdout (real 2025 data)

# Validated empirical value from core_nfl_points.py: league_points_pool in
# nfl_points_total_progress_2023.json, 544 real 2023 team-games,
# sum(pool)/len(pool) = 21.768382352941178
LEAGUE_AVG_POINTS_STATIC = 21.77

CACHE_TTL = timedelta(hours=6)
_league_avg_cache = {"value": None, "computed_at": None}


def get_week_games(season: int, week: int) -> list[dict]:
    """Every game for one week, straight from api.nfldata.org/v1/games -
    real fields confirmed already working in nfl_passing_yards_sync.py:
    game_type, home_team, away_team, home_score, away_score."""
    resp = requests.get(
        f"{API_BASE}/games",
        params={"season": season, "week": week},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def is_completed_reg(game: dict) -> bool:
    return (
        game.get("game_type") == "REG"
        and game.get("home_score") is not None
        and game.get("away_score") is not None
    )


def _shrunk_avg(sum_val: float, count: int, k: float, league_avg: float) -> float:
    return (sum_val + k * league_avg) / (count + k)


def compute_live_league_avg_points(team_points_counts: dict,
                                    min_qualifying_teams: int = MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE,
                                    static_default: float = LEAGUE_AVG_POINTS_STATIC) -> float:
    """Hybrid live/static league-average-points baseline - identical
    pattern to cfb_points_sync.compute_live_league_avg_points, just NFL's
    own static default and qualifying-teams bar (16, half the league,
    not CFB's 20 out of 130+ FBS teams)."""
    qualifying = [c for c in team_points_counts.values() if c["games"] >= MIN_PRIOR_GAMES]
    if len(qualifying) < min_qualifying_teams:
        return static_default
    total_points = sum(c["points_scored_sum"] for c in qualifying)
    total_games = sum(c["games"] for c in qualifying)
    if total_games == 0:
        return static_default
    return total_points / total_games


def get_league_avg_points(db) -> float:
    """Cached (6h) live-computed league average - same pattern as
    cfb_points_sync.get_league_avg_points."""
    now = datetime.utcnow()
    if _league_avg_cache["value"] is not None and _league_avg_cache["computed_at"] is not None \
            and now - _league_avg_cache["computed_at"] < CACHE_TTL:
        return _league_avg_cache["value"]

    rows = db.query(NflTeamPointsStat).all()
    team_points_counts = {
        r.team: {"games": r.games, "points_scored_sum": r.points_scored_sum, "points_allowed_sum": r.points_allowed_sum}
        for r in rows
    }
    value = compute_live_league_avg_points(team_points_counts)
    _league_avg_cache["value"] = value
    _league_avg_cache["computed_at"] = now
    return value


def refresh_nfl_points_stats(season: int, current_week: int) -> dict:
    """
    Rebuilds NflTeamPointsStat (every team's real season-to-date
    points-scored/points-allowed totals, weeks 1..current_week) and
    NflPointsGame (this week's real matchups, including not-yet-played
    games, with is_home set correctly) from scratch. Safe to call any
    time, any number of times - same rebuild-from-scratch discipline as
    cfb_points_sync.refresh_cfb_points_stats.
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
                log.exception("Failed to fetch NFL week %s games", week)
                continue
            weeks_fetched += 1

            for game in games:
                if not is_completed_reg(game):
                    continue
                home, away = game.get("home_team"), game.get("away_team")
                if not home or not away:
                    continue
                home_pts, away_pts = game["home_score"], game["away_score"]

                for team, pts, allowed_pts in ((home, home_pts, away_pts), (away, away_pts, home_pts)):
                    row = team_totals.setdefault(team, {"games": 0, "points_scored_sum": 0, "points_allowed_sum": 0})
                    row["games"] += 1
                    row["points_scored_sum"] += pts
                    row["points_allowed_sum"] += allowed_pts
                games_used += 1

        db.query(NflTeamPointsStat).delete()
        for team, totals in team_totals.items():
            db.add(NflTeamPointsStat(
                team=team, games=totals["games"],
                points_scored_sum=totals["points_scored_sum"],
                points_allowed_sum=totals["points_allowed_sum"],
            ))

        # Current week's real matchups (may include not-yet-played games -
        # is_completed_reg isn't applied here, we want the schedule even
        # before it's final, same as CfbGame's own "this week's matchups").
        db.query(NflPointsGame).delete()
        try:
            this_week_games = get_week_games(season, current_week)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch NFL current week %s schedule", current_week)
            this_week_games = []
        for game in this_week_games:
            if game.get("game_type") != "REG":
                continue
            home, away = game.get("home_team"), game.get("away_team")
            if not home or not away:
                continue
            gameday = game.get("gameday")
            db.add(NflPointsGame(team=home, opponent=away, is_home=True, season=season, week=current_week, gameday=gameday))
            db.add(NflPointsGame(team=away, opponent=home, is_home=False, season=season, week=current_week, gameday=gameday))

        db.commit()
        _league_avg_cache["value"] = None  # force recompute next read, using the fresh data just written
        summary = {"weeks_fetched": weeks_fetched, "teams": len(team_totals), "games_used": games_used,
                   "current_week_matchups": len(this_week_games)}
        log.info("refresh_nfl_points_stats complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def compute_nfl_team_points_prediction(team: str, opponent: str, db, league_avg: float | None = None) -> dict | None:
    """Returns {"mean":, "team_index":, "opp_index":, "team_games_sample":,
    "opp_games_sample":} for `team`'s predicted points against `opponent`,
    or None if either side doesn't have MIN_PRIOR_GAMES yet. Mirrors
    cfb_points_sync.compute_cfb_team_points_prediction's shape for
    consistency across the dashboard's prop modules."""
    team_row = db.get(NflTeamPointsStat, team)
    opp_row = db.get(NflTeamPointsStat, opponent)
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


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    """Pure-stdlib Normal CDF via math.erf - same reasoning as
    cfb_points_sync._normal_cdf: this dashboard avoids scipy/numpy/pandas
    entirely, so core_nfl_moneyline.py's scipy.stats.norm.sf(...) call is
    ported to this instead of adding a new dependency to the Railway
    deploy."""
    if sigma <= 0:
        return 1.0 if x < mean else 0.0
    z = (x - mean) / (sigma * math.sqrt(2))
    return 0.5 * (1.0 + math.erf(z))


def win_probability(home_predicted: float | None, away_predicted: float | None,
                     home_field_points: float = HOME_FIELD_POINTS, variance_scale: float = VARIANCE_SCALE) -> float | None:
    """P(home team wins), ported verbatim (formula-wise) from
    core_nfl_moneyline.py's win_probability - a Normal approximation to
    the scoring-differential distribution, built on top of each side's
    already-validated predicted points
    (compute_nfl_team_points_prediction's "mean"). Returns None (never
    0%/100%) if either side's predicted points is missing or
    non-positive, same "can't trust this one" signal the source script
    calls `trusted`.

    No neutral_site suppression, unlike cfb_points_sync.win_probability -
    see this module's HOME_FIELD_POINTS comment above for why."""
    if home_predicted is None or away_predicted is None or home_predicted <= 0 or away_predicted <= 0:
        return None
    predicted_diff = (home_predicted - away_predicted) + home_field_points
    variance_diff = variance_scale * (DEFAULT_TEAM_OVERDISPERSION * home_predicted + DEFAULT_TEAM_OVERDISPERSION * away_predicted)
    sigma = math.sqrt(variance_diff) if variance_diff > 0 else 1.0
    # P(home wins) = P(diff > 0) = 1 - CDF(0) = norm.sf(0, loc=predicted_diff, scale=sigma)
    return 1.0 - _normal_cdf(0.0, predicted_diff, sigma)


def compute_nfl_moneyline(home_team: str, away_team: str, db, league_avg: float | None = None) -> dict:
    """Returns {"home_win_probability":, "away_win_probability":,
    "trusted":} for one matchup, built on top of
    compute_nfl_team_points_prediction's already-validated per-team
    predicted points (same shrinkage/overdispersion constants), adding
    the win-probability layer from core_nfl_moneyline.py. Mirrors
    cfb_points_sync.compute_cfb_moneyline's shape exactly (minus the
    neutral_site param NFL can't confirm) for consistency across the
    dashboard's moneyline endpoints.

    "trusted" is False - and both probabilities are None, NEVER 0% - when
    either side doesn't have MIN_PRIOR_GAMES of its own history yet
    (compute_nfl_team_points_prediction already returns None in that
    case). This is the exact contract the frontend must render as "no
    pick yet", not 0% and not a crash."""
    if league_avg is None:
        league_avg = get_league_avg_points(db)

    home_pred = compute_nfl_team_points_prediction(home_team, away_team, db, league_avg=league_avg)
    away_pred = compute_nfl_team_points_prediction(away_team, home_team, db, league_avg=league_avg)
    if home_pred is None or away_pred is None:
        return {"home_win_probability": None, "away_win_probability": None, "trusted": False}

    home_prob = win_probability(home_pred["mean"], away_pred["mean"])
    if home_prob is None:
        return {"home_win_probability": None, "away_win_probability": None, "trusted": False}

    return {
        "home_win_probability": home_prob,
        "away_win_probability": 1.0 - home_prob,
        "trusted": True,
    }
