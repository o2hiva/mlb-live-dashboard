"""
First-inning run-probability predictor - ported directly from your
existing core.py model (inning_scoring_probability), NOT a trained
machine-learning model. Combines each team's real season-to-date rate
of scoring in the 1st inning with the OPPOSING starting pitcher's real
rate of allowing a run in the 1st, via the log5 sabermetric formula.

The real season counts this needs (team games/scored, pitcher
starts/allowed) are kept in sync by inning_stats_sync.py - a live port
of your fetch_inning_scoring_stats.py, reading the same MLB Stats API
schedule+linescore data instead of writing to an Excel workbook.

The constants below (league rate, shrinkage strengths, minimum sample
sizes) are copied VERBATIM from your validated core.py. Do not change
these without re-running your own backtests - they were tuned against
real results (see core.py's own comments for the full validation
history per inning).
"""
import logging

import inning_stats_sync

log = logging.getLogger("predictor")

MAX_SUPPORTED_INNING = 3
INNING_LEAGUE_RATES = {1: 0.25, 2: 0.25, 3: 0.30}
INNING_TEAM_SHRINKAGE_K = {1: 0, 2: 0, 3: 0}
INNING_PITCHER_SHRINKAGE_K = {1: 10, 2: 10, 3: 10}

# Same minimum-sample gates as core.py's inning_scoring_probability -
# below these, real data is treated as not-yet-reliable rather than
# fabricating a guess.
MIN_TEAM_GAMES = 10
MIN_PITCHER_STARTS = 3

# Rough MLB league-average 1st-inning scoring rate, used ONLY when a
# team or pitcher doesn't have enough real season data yet (very early
# season, a rookie's first start, etc.) - not a model prediction.
PLACEHOLDER_PROBABILITY = 0.47


def _log5(rate_a: float, rate_b: float, league_rate: float) -> float:
    """Verbatim from core.py. Verified property: _log5(L, L, L) == L."""
    a = min(max(rate_a, 0.02), 0.98)
    b = min(max(rate_b, 0.02), 0.98)
    lg = min(max(league_rate, 0.02), 0.98)
    numerator = (a * b) / lg
    denominator = numerator + ((1 - a) * (1 - b)) / (1 - lg)
    return numerator / denominator if denominator > 0 else lg


def inning_scoring_probability(inning: int, row: dict, team_counts: dict, pitcher_counts: dict) -> tuple:
    """
    Verbatim port of core.py's inning_scoring_probability (innings 1-3,
    pitcher-level model). row needs "away_team", "home_team",
    "away_pitcher", "home_pitcher" keys.

    Returns (probability, used_real_data: bool) - used_real_data is
    False if either team or either pitcher doesn't have enough real
    counts yet.
    """
    if inning > MAX_SUPPORTED_INNING:
        raise ValueError(
            f"Inning {inning} isn't supported by the pitcher-level model "
            f"(max {MAX_SUPPORTED_INNING})."
        )

    league_rate = INNING_LEAGUE_RATES[inning]
    team_shrinkage_k = INNING_TEAM_SHRINKAGE_K[inning]
    pitcher_shrinkage_k = INNING_PITCHER_SHRINKAGE_K[inning]

    away_team = team_counts.get(row.get("away_team"))
    home_team = team_counts.get(row.get("home_team"))
    away_pitcher = pitcher_counts.get(row.get("away_pitcher"))
    home_pitcher = pitcher_counts.get(row.get("home_pitcher"))

    if not away_team or not home_team or not away_pitcher or not home_pitcher:
        return None, False
    if away_team["games"] < MIN_TEAM_GAMES or home_team["games"] < MIN_TEAM_GAMES:
        return None, False
    if away_pitcher["starts"] < MIN_PITCHER_STARTS or home_pitcher["starts"] < MIN_PITCHER_STARTS:
        return None, False

    away_team_rate = (away_team["scored"] + team_shrinkage_k * league_rate) / \
        (away_team["games"] + team_shrinkage_k)
    home_team_rate = (home_team["scored"] + team_shrinkage_k * league_rate) / \
        (home_team["games"] + team_shrinkage_k)
    home_pitcher_rate = (home_pitcher["allowed"] + pitcher_shrinkage_k * league_rate) / \
        (home_pitcher["starts"] + pitcher_shrinkage_k)
    away_pitcher_rate = (away_pitcher["allowed"] + pitcher_shrinkage_k * league_rate) / \
        (away_pitcher["starts"] + pitcher_shrinkage_k)

    away_scoring_prob = _log5(away_team_rate, home_pitcher_rate, league_rate)
    home_scoring_prob = _log5(home_team_rate, away_pitcher_rate, league_rate)

    return 1 - (1 - away_scoring_prob) * (1 - home_scoring_prob), True


def predict_first_inning_run_prob(game_info: dict) -> tuple[float, str]:
    """
    Returns (probability, model_version). game_info is one entry from
    mlb_client.get_schedule().
    """
    try:
        team_counts = inning_stats_sync.load_team_counts(inning=1)
        pitcher_counts = inning_stats_sync.load_pitcher_counts(inning=1)
    except Exception:
        log.exception("Failed to load inning-stats counts - using placeholder")
        return PLACEHOLDER_PROBABILITY, "placeholder-v0-db-error"

    row = {
        "away_team": game_info.get("away_team"),
        "home_team": game_info.get("home_team"),
        "away_pitcher": game_info.get("away_probable_pitcher"),
        "home_pitcher": game_info.get("home_probable_pitcher"),
    }

    prob, used_real_data = inning_scoring_probability(1, row, team_counts, pitcher_counts)
    if used_real_data:
        return prob, "log5-real-data-v1"

    return PLACEHOLDER_PROBABILITY, "placeholder-v0-insufficient-data"
