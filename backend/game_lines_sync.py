"""
Game Lines prop - runs scored in the first 5 innings (F5), both as a
combined total O/U and each team's own "at least N runs" probability.

COMBINED TOTAL: ported from core.py's runs5inn_total_probability -
fully validated (backtested against 1,374 real games, z=+0.16 at
overdispersion=1.65). Distribution: Negative Binomial (same family as
HRR, different overdispersion) - summing two teams' independent run
totals carries more variance than a pure Poisson assumes, the same gap
HRR had before its own fix.

PER-TEAM "AT LEAST N RUNS": core.py has NO separate validated formula
for this - only the combined total. Built here by reusing the exact
same per-team mean calculation the combined formula already computes
internally (team_predicted_runs) before summing the two teams together,
and applying the SAME negative-binomial shape/overdispersion to just
one team's own mean. This is a reasonable, disclosed EXTENSION of
validated logic, not something independently backtested the way the
combined total is - the overdispersion value was tuned for the SUM of
two teams, not one team's total alone.

HONEST LIMITATION, carried over directly from core.py's own docstring:
a starting pitcher isn't reliably still pitching through the whole 5th
inning - this formula uses their own OVERALL runs-allowed rate as a
proxy for quality, not runs allowed specifically in the innings they
personally covered. A named simplification, not a hidden one.

NO NEW DATA FETCHING: pitcher side reuses PitcherHitsStat.runs_allowed/
outs (already synced for the Hits/HRR/HR props); team side reuses
TeamRuns5InnStat (see inning_stats_sync.py - the same daily job that
already iterates every final game for the 1st-inning model, just
summing a wider inning range too).
"""
import logging
from datetime import datetime, timedelta

from database import SessionLocal
from models_db import PitcherHitsStat, TeamRuns5InnStat

log = logging.getLogger("game_lines_sync")

# Verbatim from core.py.
DEFAULT_RUNS5INN_PITCHER_SHRINKAGE_K = 30  # in innings pitched
DEFAULT_RUNS5INN_TEAM_SHRINKAGE_K = 15     # in games
DEFAULT_RUNS5INN_OVERDISPERSION = 1.65
LEAGUE_AVG_RUNS5INN = 2.487  # empirically derived static fallback
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 20  # smaller pool than pitchers' 30 - only 30 teams exist total

MIN_PITCHER_OUTS = 45  # 15 innings, verbatim from core.py (MIN_PITCHER_IP_OUTS equivalent)
MIN_TEAM_GAMES = 10

CACHE_TTL = timedelta(minutes=10)
_baseline_cache = None
_baseline_cache_time = None


def get_league_runs5inn_baseline(db) -> float:
    """
    Same hybrid live/static reasoning as Pitcher Hits Allowed's
    get_hybrid_baselines: below MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE
    real teams with 10+ games on file, returns the validated static
    LEAGUE_AVG_RUNS5INN unchanged. Once enough real data exists,
    returns the live, self-correcting equivalent instead. Cached
    (learned directly from a real performance bug in Pitcher Hits
    Allowed's own first version of this pattern - built with the cache
    from the start here rather than needing a second regression to
    teach the same lesson twice).
    """
    global _baseline_cache, _baseline_cache_time
    now = datetime.utcnow()
    if _baseline_cache is not None and now - _baseline_cache_time < CACHE_TTL:
        return _baseline_cache

    rows = db.query(TeamRuns5InnStat).all()
    qualifying = [r for r in rows if r.games >= MIN_TEAM_GAMES]

    if len(qualifying) < MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE:
        result = LEAGUE_AVG_RUNS5INN
    else:
        total_runs = sum(r.runs5inn for r in qualifying)
        total_games = sum(r.games for r in qualifying)
        result = total_runs / total_games

    _baseline_cache = result
    _baseline_cache_time = now
    return result


def _team_predicted_runs5inn(opposing_pitcher: PitcherHitsStat, this_team: TeamRuns5InnStat,
                              league_avg_runs5inn: float) -> float:
    """Verbatim from core.py's runs5inn_total_probability's own inner
    team_predicted_runs closure."""
    p_ip = opposing_pitcher.outs / 3
    league_runs_per_ip = league_avg_runs5inn / 5
    shrunk_pitcher_rate = (opposing_pitcher.runs_allowed + DEFAULT_RUNS5INN_PITCHER_SHRINKAGE_K * league_runs_per_ip) / \
        (p_ip + DEFAULT_RUNS5INN_PITCHER_SHRINKAGE_K)
    pitcher_index = shrunk_pitcher_rate / league_runs_per_ip

    shrunk_team_rate = (this_team.runs5inn + DEFAULT_RUNS5INN_TEAM_SHRINKAGE_K * league_avg_runs5inn) / \
        (this_team.games + DEFAULT_RUNS5INN_TEAM_SHRINKAGE_K)
    team_index = shrunk_team_rate / league_avg_runs5inn

    return pitcher_index * team_index * league_avg_runs5inn


def compute_game_lines_inputs(home_team: str, away_team: str, home_pitcher_id: int | None,
                               away_pitcher_id: int | None, league_avg_runs5inn: float | None = None) -> dict | None:
    """
    Returns {"away_mean":, "home_mean":, "combined_mean":} - each feeds
    a Negative Binomial "at least N runs" probability (frontend computes
    instantly for any line, same pattern as HRR/Pitcher K). None if
    either team or either starting pitcher lacks enough real sample:
    45+ outs (15 IP) for both pitchers, 10+ games for both teams -
    verbatim gates from core.py's runs5inn_total_probability.

    league_avg_runs5inn: pass in the caller's already-computed hybrid
    baseline to avoid a redundant lookup - same "once per request"
    reasoning as every other prop in this system.
    """
    db = SessionLocal()
    try:
        if league_avg_runs5inn is None:
            league_avg_runs5inn = get_league_runs5inn_baseline(db)

        away_p = db.get(PitcherHitsStat, away_pitcher_id) if away_pitcher_id else None
        home_p = db.get(PitcherHitsStat, home_pitcher_id) if home_pitcher_id else None
        away_t = db.get(TeamRuns5InnStat, away_team)
        home_t = db.get(TeamRuns5InnStat, home_team)

        if not away_p or not home_p or not away_t or not home_t:
            return None
        if away_p.outs < MIN_PITCHER_OUTS or home_p.outs < MIN_PITCHER_OUTS:
            return None
        if away_t.games < MIN_TEAM_GAMES or home_t.games < MIN_TEAM_GAMES:
            return None

        # Away team faces the HOME starter; home team faces the AWAY starter.
        away_mean = _team_predicted_runs5inn(home_p, away_t, league_avg_runs5inn)
        home_mean = _team_predicted_runs5inn(away_p, home_t, league_avg_runs5inn)

        return {
            "away_mean": away_mean,
            "home_mean": home_mean,
            "combined_mean": away_mean + home_mean,
        }
    finally:
        db.close()
