"""
1st 5 Innings (F5) Game Lines prop - each team's predicted F5 runs (with
adjustable O/U probabilities, computed client-side the same way the
existing Game Lines prop already does), the combined F5 total O/U, and
F5 moneyline win probabilities. Ported from the user-supplied, validated
mlb_f5_lines.py module.

VALIDATION SUMMARY (from mlb_f5_lines.py's own docstring - carried over
verbatim so this caveat isn't lost): walk-forward, no-leakage backtest
against two full, independent MLB seasons.
  - 2025 within-season holdout: Brier edge over naive +0.0049.
  - 2026 full-season blind test (these exact constants, zero re-tuning):
    Brier edge over naive +0.0022 - same direction, about HALF the 2025
    edge.
This is a real, reproducible, same-direction edge in both seasons - but
a SMALL one, and most of it is NOT coming from the team/starter indices:
nearly all of the edge over a zero-information baseline traces back to
the home-field-advantage term (HOME_FIELD_RUNS_BOOST_5INN), not real
team-scoring or starter-allowing skill differentiation. Smaller edge
than Full Game Lines' moneyline (full_game_lines_sync.py), which is
more starter/team-driven on both test seasons.

THE FORMULA (per team):
    predicted_runs_5inn = team_runs_5inn_index * opposing_starter_allow_index * league_avg_runs_5inn

NO NEW DATA COLLECTION - this reuses real data already being gathered
for other props:
  - Team F5 scoring: TeamRuns5InnStat, already populated daily by
    inning_stats_sync.py for the existing Game Lines prop (a DEDICATED
    first-5-innings scoring accumulator, tracked separately from full-
    game scoring - matches mlb_f5_lines.py's TeamF5OffenseStats exactly).
  - Starting pitcher runs-allowed proxy: PitcherHitsStat, already
    populated for Hits/HRR/HR/Pitcher K and reused as-is by
    full_game_lines_sync.py for the identical whole-appearance-based
    stat (NOT isolated to a starter's first-5-innings-specific
    performance - a disclosed simplification carried over from the
    source module, since that needs play-by-play data a boxscore
    doesn't give).

Computed fresh on every request - NOT frozen pregame (unlike Full Game
Lines' moneyline). Same live-recomputed pattern the existing Game Lines
prop already uses, so team/pitcher baselines always reflect the latest
completed games right up to first pitch. No Game table columns, no
poller changes.
"""
import math
from datetime import datetime, timedelta

from database import SessionLocal
from models_db import PitcherHitsStat, TeamRuns5InnStat

MODEL_VERSION = "f5-lines-v1-validated"

# Validated constants (2025 holdout tune phase, confirmed not a grid-edge
# artifact, used as-is for the 2026 blind test) - verbatim from
# mlb_f5_lines.py.
TEAM_SHRINKAGE_K_5INN = 120     # in games - much heavier than Full Game Lines' 15
STARTER_SHRINKAGE_K_5INN = 600  # in outs - heavier than Full Game Lines' validated 200

LEAGUE_AVG_RUNS_5INN = 2.5           # static fallback until enough real data exists
LEAGUE_RUNS_PER_START_INNING = 0.52  # same static fallback as Full Game Lines

MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 20
MIN_TEAM_GAMES = 10      # gate for using a team's own F5 scoring index at all
MIN_STARTER_OUTS = 45    # 15 IP - gate for a starter qualifying into the LEAGUE BASELINE pool only
                          # (not a per-game gate - a thin-sample starter still contributes to their
                          # own game's prediction via the shrinkage formula, same as the source module)

OVERDISPERSION_5INN = 2.5           # empirically grounded from real F5 total-runs variance, capped
                                     # rather than left to climb freely - see module docstring
HOME_FIELD_RUNS_BOOST_5INN = 0.25   # validated split: home +0.125, away -0.125

MAX_RUNS_5INN = 20  # distribution truncation point (smaller window than Full Game Lines' 30)

CACHE_TTL = timedelta(minutes=10)
_baseline_cache = None
_baseline_cache_time = None


def get_f5_league_baselines(db) -> dict:
    """Hybrid live/static baseline, same reasoning as every other prop's
    hybrid baseline in this dashboard. Cached (10 min) to avoid
    recomputing from every row on every single request."""
    global _baseline_cache, _baseline_cache_time
    now = datetime.utcnow()
    if _baseline_cache is not None and now - _baseline_cache_time < CACHE_TTL:
        return _baseline_cache

    team_rows = db.query(TeamRuns5InnStat).all()
    qualifying_teams = [t for t in team_rows if t.games >= MIN_TEAM_GAMES]
    if len(qualifying_teams) >= MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE:
        runs_per_5inn = sum(t.runs5inn for t in qualifying_teams) / sum(t.games for t in qualifying_teams)
    else:
        runs_per_5inn = LEAGUE_AVG_RUNS_5INN

    starter_rows = db.query(PitcherHitsStat).filter(PitcherHitsStat.outs >= MIN_STARTER_OUTS).all()
    if len(starter_rows) >= 30:
        total_outs = sum(r.outs for r in starter_rows)
        runs_per_start_inning = (sum(r.runs_allowed for r in starter_rows) / (total_outs / 3)) \
            if total_outs else LEAGUE_RUNS_PER_START_INNING
    else:
        runs_per_start_inning = LEAGUE_RUNS_PER_START_INNING

    result = {"runs_per_5inn": runs_per_5inn, "runs_per_start_inning": runs_per_start_inning}
    _baseline_cache = result
    _baseline_cache_time = now
    return result


def has_enough_f5_data(team_row) -> bool:
    """Below MIN_TEAM_GAMES, don't predict F5 for this team at all -
    verbatim gate from mlb_f5_lines.py."""
    return team_row is not None and team_row.games >= MIN_TEAM_GAMES


def predicted_team_runs_5inn(team_row: TeamRuns5InnStat, opposing_starter_row: PitcherHitsStat | None,
                              baselines: dict) -> float:
    """One team's predicted runs across the first 5 innings:
    team_5inn_index * opposing_starter_allow_index * league_avg_runs_5inn.
    Verbatim formula from mlb_f5_lines.py's predicted_team_runs_5inn."""
    league_avg_runs_5inn = baselines["runs_per_5inn"]
    league_runs_per_start_inning = baselines["runs_per_start_inning"]

    shrunk_team_rate = (team_row.runs5inn + TEAM_SHRINKAGE_K_5INN * league_avg_runs_5inn) / \
        (team_row.games + TEAM_SHRINKAGE_K_5INN)
    team_5inn_index = shrunk_team_rate / league_avg_runs_5inn

    if opposing_starter_row and opposing_starter_row.outs > 0:
        starter_ip = opposing_starter_row.outs / 3
        shrunk_starter_rate = (opposing_starter_row.runs_allowed +
                                (STARTER_SHRINKAGE_K_5INN / 3) * league_runs_per_start_inning) / \
            (starter_ip + STARTER_SHRINKAGE_K_5INN / 3)
        starter_allow_index = shrunk_starter_rate / league_runs_per_start_inning
    else:
        starter_allow_index = 1.0

    return team_5inn_index * starter_allow_index * league_avg_runs_5inn


# ---------------------------------------------------------------------------
# Negative-binomial moneyline math (pure stdlib) - identical approach to
# full_game_lines_sync.py, just with this module's own overdispersion/
# truncation constants.
# ---------------------------------------------------------------------------
def _poisson_pmf(k: int, mean: float) -> float:
    if mean <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mean + k * math.log(mean) - math.lgamma(k + 1))


def _negbinom_pmf(k: int, mean: float, overdispersion: float) -> float:
    if overdispersion <= 1.0 + 1e-9:
        return _poisson_pmf(k, mean)
    r = mean / (overdispersion - 1.0)
    p = r / (r + mean)
    log_pmf = math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * math.log(p) + k * math.log(1 - p)
    return math.exp(log_pmf)


def _run_distribution(mean: float, overdispersion: float, max_runs: int = MAX_RUNS_5INN) -> list[float]:
    dist = [_negbinom_pmf(k, mean, overdispersion) for k in range(max_runs + 1)]
    total = sum(dist)
    return [p / total for p in dist] if total > 0 else dist


def moneyline_probabilities(home_mean: float, away_mean: float,
                             overdispersion: float = OVERDISPERSION_5INN) -> tuple[float, float]:
    """Exact joint-distribution F5 moneyline win probabilities (ties split 50/50)."""
    home_dist = _run_distribution(home_mean, overdispersion)
    away_dist = _run_distribution(away_mean, overdispersion)
    home_win = 0.0
    tie = 0.0
    for h, ph in enumerate(home_dist):
        if ph <= 0:
            continue
        for a, pa in enumerate(away_dist):
            if pa <= 0:
                continue
            joint = ph * pa
            if h > a:
                home_win += joint
            elif h == a:
                tie += joint
    home_win_prob = home_win + 0.5 * tie
    return home_win_prob, 1.0 - home_win_prob


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def compute_f5_lines_inputs(home_team: str, away_team: str, home_pitcher_id: int | None,
                             away_pitcher_id: int | None,
                             home_field_runs: float = HOME_FIELD_RUNS_BOOST_5INN) -> dict | None:
    """Returns {"home_team":, "away_team":, "home_mean":, "away_mean":,
    "combined_mean":, "home_win_prob_5inn":, "away_win_prob_5inn":,
    "model_version":} - the means feed a client-side Negative Binomial
    "at least N runs" probability for any O/U line (same instant-compute
    pattern as HRR/Pitcher K/the existing Game Lines prop), while the
    moneyline win probabilities are the exact joint-distribution values
    computed here. None if either team hasn't played enough F5 games yet
    (has_enough_f5_data gate - verbatim from mlb_f5_lines.py, no starter-
    side gate)."""
    db = SessionLocal()
    try:
        baselines = get_f5_league_baselines(db)

        away_t = db.get(TeamRuns5InnStat, away_team)
        home_t = db.get(TeamRuns5InnStat, home_team)
        if not has_enough_f5_data(away_t) or not has_enough_f5_data(home_t):
            return None

        home_starter_row = db.get(PitcherHitsStat, home_pitcher_id) if home_pitcher_id else None
        away_starter_row = db.get(PitcherHitsStat, away_pitcher_id) if away_pitcher_id else None

        # Away team's offense faces the HOME starter; home team's offense faces the AWAY starter.
        away_mean = predicted_team_runs_5inn(away_t, home_starter_row, baselines)
        home_mean = predicted_team_runs_5inn(home_t, away_starter_row, baselines)

        if home_field_runs:
            home_mean += home_field_runs / 2.0
            away_mean = max(0.05, away_mean - home_field_runs / 2.0)

        home_win_prob, away_win_prob = moneyline_probabilities(home_mean, away_mean)

        return {
            "home_team": home_team, "away_team": away_team,
            "home_mean": home_mean, "away_mean": away_mean,
            "combined_mean": home_mean + away_mean,
            "home_win_prob_5inn": home_win_prob, "away_win_prob_5inn": away_win_prob,
            "model_version": MODEL_VERSION,
        }
    finally:
        db.close()
