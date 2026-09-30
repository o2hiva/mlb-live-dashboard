"""
Full Game Lines prop - MONEYLINE ONLY, validated.

VALIDATION SUMMARY: walk-forward, no-leakage backtest against two full,
independent MLB seasons (see the user-supplied
full-game-lines-backtest-findings.md for full detail).
  - 2025 within-season holdout (tuned on games before 2025-08-01, validated
    on games from 2025-08-01 on, never touched during tuning): Brier edge
    over naive +0.0057, matching the +0.0059 in-sample edge almost exactly.
  - 2026 full-season blind test (these exact constants, zero re-tuning, a
    season the tuning process never saw at all): Brier 0.2454 vs a true
    naive baseline's ~0.2493+.
Two different seasons, same ~0.005 Brier edge both times - a real,
reproducible improvement.

NOT VALIDATED: total-runs / spread. This module deliberately does NOT
expose a totals-line or spread-cover probability. predicted_team_runs9inn's
two means feed the moneyline math (and are kept on Game/API responses only
insofar as the moneyline needs them), but their calibration as a STANDALONE
totals/spread prediction has not been validated - in both backtest seasons
the model's total-runs distribution fit came out roughly tied with or
slightly WORSE than a naive "always predict league average" guess. Do not
resurrect a totals or spread UI/bet_type from this module until that gets
its own validation pass.

WHAT CHANGED vs. this module's own first version (all found via the
2-season backtest, see MODEL_VERSION below):
  1. A real bug FIXED: the starter-shrinkage formula's numerator used the
     raw, un-converted STARTER_SHRINKAGE_K (defined in OUTS) where it
     needed STARTER_SHRINKAGE_K/3 (to match the denominator's own units,
     innings) - a 3x-too-heavy prior that inflated every starter's
     allow-index by ~40% on average (confirmed against real 2025 data:
     avg index 1.41 -> 1.015 after the fix, and independently via
     synthetic starters with known league-average true talent: buggy
     formula averaged 1.425, fixed formula averaged 1.0002).
  2. Two constants RETUNED via a full grid search plus the two-season
     out-of-sample check above: STARTER_SHRINKAGE_K 30->200 (real
     starter-quality signal is much weaker than originally assumed at
     these sample sizes, needs much heavier shrinkage) and
     OVERDISPERSION_RUNS9 1.5->4.0 (real full-game team-run totals are
     far more over-dispersed than the original disclosed guess).
  3. One feature ADDED that didn't exist before: a home-field-advantage
     term (HOME_FIELD_RUNS_BOOST, split symmetrically across both means).

DATA COLLECTION - COLD START BY DESIGN: TeamRunsFullGameStat / TeamBullpenStat
are reconstructed here, one game at a time, from get_boxscore()'s real
per-pitcher lines compared against that game's real starter
(get_game_starting_pitcher). Both tables start at ZERO and need real games
to accumulate before their indices mean anything - an accepted, explicit
tradeoff (see backfill() below for catching a whole season up in one
call-per-max_games batch). The shrinkage formula degrades gracefully in the
meantime: with 0 games on file, shrunk_rate collapses to exactly the league
average (index = 1.0), so a cold table doesn't error out or return
nonsense - it just assumes league-average until real data appears.
"""
import logging
import math
from datetime import datetime, date, timedelta

import mlb_client
from database import SessionLocal
from models_db import (
    PitcherHitsStat, PitcherKStat, TeamRunsFullGameStat, TeamBullpenStat,
    TeamBullpenCollectedGame, SyncState,
)

log = logging.getLogger("full_game_lines_sync")

# Bumped from the original's implicit "v1" the moment the starter-shrinkage
# bug was fixed and the constants below were retuned/validated - anything
# computed under the old, buggy formula should not be confused with this
# one (e.g. in Prediction/tracked-bet history, model_version strings).
MODEL_VERSION = "full-game-moneyline-v2-validated"

# --- Validated constants (2-season walk-forward backtest) ---------------
TEAM_SHRINKAGE_K = 15          # in games - unchanged, no evidence it needs to move
STARTER_SHRINKAGE_K = 200      # in outs - was 30; real starter-quality signal is much
                                # weaker than originally assumed at these sample sizes
BULLPEN_SHRINKAGE_K = 60       # in outs - unchanged, no meaningful backtest difference vs. 120

LEAGUE_AVG_RUNS_PER_GAME = 4.5          # static fallback until enough real data exists
LEAGUE_RUNS_PER_START_INNING = 0.52
LEAGUE_RUNS_PER_BULLPEN_INNING = 0.48
DEFAULT_AVG_START_INNINGS = 5.2

MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 20
MIN_TEAM_GAMES = 10        # gate for using a team's own scoring index at all
MIN_STARTER_OUTS = 45      # 15 IP, gate for a starter qualifying into the live baseline pool

OVERDISPERSION_RUNS9 = 4.0      # was 1.5 - real full-game team-run totals are far more
                                 # over-dispersed than the original disclosed guess

HOME_FIELD_RUNS_BOOST = 0.5     # NEW - split symmetrically: home +0.25, away -0.25

MAX_RUNS = 30                   # distribution truncation point for the moneyline math below

DEFAULT_START_DATE = "2026-07-16"  # matches every other sync's own backtest/collection window
BACKFILL_RESUME_KEY = "full_game_lines_backfill_resume_date"

CACHE_TTL = timedelta(minutes=10)
_baseline_cache = None
_baseline_cache_time = None


# --- find-or-create helpers ---------------------------------------------
# Always db.flush() right after db.add() - this exact find-or-create
# shape is what caused the autoflush=False duplicate-key bug fixed
# earlier in npb_yrfi_sync.py/nhl_goalie_saves_sync.py. Never repeat that here.

def _get_or_create_fullgame_row(db, team_name: str) -> TeamRunsFullGameStat:
    row = db.get(TeamRunsFullGameStat, team_name)
    if row is None:
        row = TeamRunsFullGameStat(team_name=team_name, games=0, runs_scored=0)
        db.add(row)
        db.flush()
    return row


def _get_or_create_bullpen_row(db, team_name: str) -> TeamBullpenStat:
    row = db.get(TeamBullpenStat, team_name)
    if row is None:
        row = TeamBullpenStat(team_name=team_name, games=0, outs=0, runs_allowed=0)
        db.add(row)
        db.flush()
    return row


# --- boxscore parsing -----------------------------------------------------

def _innings_pitched_to_outs(ip_str) -> int:
    """MLB's inningsPitched is a string like "5.2" where the decimal is
    OUTS within the inning, not a real decimal fraction (".1"=1 out,
    ".2"=2 outs, never ".3")."""
    if not ip_str:
        return 0
    try:
        whole_str, _, frac_str = str(ip_str).partition(".")
        whole = int(whole_str) if whole_str else 0
        frac = int(frac_str) if frac_str else 0
        return whole * 3 + frac
    except (ValueError, TypeError):
        return 0


def _team_pitching_lines(boxscore: dict, side: str) -> dict:
    """{pitcher_id: {"outs":, "runs":}} for every pitcher who actually
    recorded an out for one side of a real boxscore."""
    team_box = boxscore.get("teams", {}).get(side, {})
    players = team_box.get("players", {})
    lines = {}
    for pdata in players.values():
        person = pdata.get("person", {})
        pid = person.get("id")
        pitching = (pdata.get("stats") or {}).get("pitching")
        if not pid or not pitching:
            continue
        outs = _innings_pitched_to_outs(pitching.get("inningsPitched"))
        if outs <= 0:
            continue
        lines[pid] = {"outs": outs, "runs": pitching.get("runs", 0) or 0}
    return lines


# --- per-game fold (shared by daily_update and backfill) -----------------

def _fold_game(db, game: dict, game_pk: int) -> bool:
    """Fetches ONE game's real boxscore and folds its full-game team runs
    + bullpen-only outs/runs into the running totals. Returns True if the
    game was actually folded."""
    teams = game.get("teams", {})
    away_team_name = teams.get("away", {}).get("team", {}).get("name")
    home_team_name = teams.get("home", {}).get("team", {}).get("name")
    away_final = teams.get("away", {}).get("score")
    home_final = teams.get("home", {}).get("score")
    if not away_team_name or not home_team_name or away_final is None or home_final is None:
        return False

    away_starter_id, _ = mlb_client.get_game_starting_pitcher(game, "away")
    home_starter_id, _ = mlb_client.get_game_starting_pitcher(game, "home")

    try:
        boxscore = mlb_client.get_boxscore(game_pk)
    except Exception:
        log.warning("full_game_lines_sync: failed to fetch boxscore for game %s", game_pk)
        return False

    for side, team_name, starter_id in (
        ("away", away_team_name, away_starter_id),
        ("home", home_team_name, home_starter_id),
    ):
        lines = _team_pitching_lines(boxscore, side)
        bullpen_outs = sum(v["outs"] for pid, v in lines.items() if pid != starter_id)
        bullpen_runs = sum(v["runs"] for pid, v in lines.items() if pid != starter_id)
        bp_row = _get_or_create_bullpen_row(db, team_name)
        bp_row.games += 1
        bp_row.outs += bullpen_outs
        bp_row.runs_allowed += bullpen_runs

    away_fg = _get_or_create_fullgame_row(db, away_team_name)
    away_fg.games += 1
    away_fg.runs_scored += away_final
    home_fg = _get_or_create_fullgame_row(db, home_team_name)
    home_fg.games += 1
    home_fg.runs_scored += home_final

    db.add(TeamBullpenCollectedGame(
        game_pk=game_pk, date=game.get("officialDate") or "",
        home=home_team_name, away=away_team_name,
    ))
    return True


# --- daily incremental update --------------------------------------------

def daily_update(days_back: int = 3) -> dict:
    """INCREMENTAL: checks the last `days_back` days (today inclusive) for
    real FINAL games not yet in TeamBullpenCollectedGame, fetches each
    one's boxscore, and folds it in. Safe to call daily forever."""
    db = SessionLocal()
    try:
        today = mlb_client.mlb_today()
        games_collected = 0
        days_checked = 0
        for offset in range(days_back, -1, -1):
            date_str = (today - timedelta(days=offset)).isoformat()
            try:
                games = mlb_client.get_final_games_with_linescore(date_str)
            except Exception:
                log.exception("full_game_lines_sync.daily_update: failed to fetch %s", date_str)
                continue
            days_checked += 1
            for game in games:
                game_pk = game.get("gamePk")
                if game_pk is None or db.get(TeamBullpenCollectedGame, game_pk) is not None:
                    continue
                if _fold_game(db, game, game_pk):
                    games_collected += 1
            db.commit()

        summary = {"days_checked": days_checked, "games_collected": games_collected}
        log.info("full_game_lines_sync.daily_update: %s", summary)
        return summary
    except Exception:
        log.exception("full_game_lines_sync.daily_update failed")
        db.rollback()
        return {"days_checked": 0, "games_collected": 0}
    finally:
        db.close()


# --- resumable full-season backfill ---------------------------------------

def backfill(max_games: int = 300) -> dict:
    """ONE call folds up to `max_games` NEW games starting from wherever
    the last call left off (SyncState row BACKFILL_RESUME_KEY). Call
    repeatedly until the response says "finished": true."""
    db = SessionLocal()
    try:
        resume_row = db.get(SyncState, BACKFILL_RESUME_KEY)
        day = date.fromisoformat(resume_row.value) if resume_row else date.fromisoformat(DEFAULT_START_DATE)
        yesterday = mlb_client.mlb_yesterday()

        games_folded = 0
        days_scanned = 0

        while day <= yesterday and games_folded < max_games:
            date_str = day.isoformat()
            try:
                games = mlb_client.get_final_games_with_linescore(date_str)
            except Exception:
                log.exception("full_game_lines_sync.backfill: failed to fetch %s - stopping, will retry next call", date_str)
                break

            day_fully_done = True
            for game in games:
                game_pk = game.get("gamePk")
                if game_pk is None or db.get(TeamBullpenCollectedGame, game_pk) is not None:
                    continue
                if games_folded >= max_games:
                    day_fully_done = False
                    break
                if _fold_game(db, game, game_pk):
                    games_folded += 1

            days_scanned += 1
            if not day_fully_done:
                break
            day += timedelta(days=1)

        finished = day > yesterday
        resume_date = day if not finished else yesterday
        if resume_row is None:
            db.add(SyncState(key=BACKFILL_RESUME_KEY, value=resume_date.isoformat()))
        else:
            resume_row.value = resume_date.isoformat()
        db.commit()

        summary = {
            "days_scanned": days_scanned, "games_folded": games_folded,
            "finished": finished, "resume_date": resume_date.isoformat(),
        }
        log.info("full_game_lines_sync.backfill: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --- league baselines -------------------------------------------------------

def get_hybrid_full_game_baselines(db) -> dict:
    """Hybrid live/static: below MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE real
    teams with 10+ games on file, returns the disclosed static fallbacks
    unchanged; once enough real data exists, returns the live equivalents.
    Cached for CACHE_TTL."""
    global _baseline_cache, _baseline_cache_time
    now = datetime.utcnow()
    if _baseline_cache is not None and now - _baseline_cache_time < CACHE_TTL:
        return _baseline_cache

    fg_rows = db.query(TeamRunsFullGameStat).all()
    fg_qualifying = [r for r in fg_rows if r.games >= MIN_TEAM_GAMES]
    if len(fg_qualifying) >= MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE:
        runs_per_game = sum(r.runs_scored for r in fg_qualifying) / sum(r.games for r in fg_qualifying)
    else:
        runs_per_game = LEAGUE_AVG_RUNS_PER_GAME

    starter_rows = db.query(PitcherHitsStat).filter(PitcherHitsStat.outs >= MIN_STARTER_OUTS).all()
    if len(starter_rows) >= 30:
        total_outs = sum(r.outs for r in starter_rows)
        runs_per_start_inning = (sum(r.runs_allowed for r in starter_rows) / (total_outs / 3)) if total_outs else LEAGUE_RUNS_PER_START_INNING
    else:
        runs_per_start_inning = LEAGUE_RUNS_PER_START_INNING

    bp_rows = db.query(TeamBullpenStat).all()
    bp_qualifying = [r for r in bp_rows if r.outs >= BULLPEN_SHRINKAGE_K]
    if len(bp_qualifying) >= MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE:
        total_outs = sum(r.outs for r in bp_qualifying)
        runs_per_bullpen_inning = sum(r.runs_allowed for r in bp_qualifying) / (total_outs / 3)
    else:
        runs_per_bullpen_inning = LEAGUE_RUNS_PER_BULLPEN_INNING

    result = {
        "runs_per_game": runs_per_game,
        "runs_per_start_inning": runs_per_start_inning,
        "runs_per_bullpen_inning": runs_per_bullpen_inning,
    }
    _baseline_cache = result
    _baseline_cache_time = now
    return result


def _avg_start_innings(starter_k) -> float:
    """This starter's own real average innings/start (outs/3 /
    games_started), clamped to [3.0, 8.0]. Falls back to the MLB-wide
    default when there isn't a real games_started count yet."""
    if not starter_k or not starter_k.games_started:
        return DEFAULT_AVG_START_INNINGS
    innings = (starter_k.outs / 3) / starter_k.games_started
    return max(3.0, min(8.0, innings))


def predicted_team_runs9inn(
    team: TeamRunsFullGameStat, opposing_starter, opposing_starter_k,
    opposing_bullpen: TeamBullpenStat, baselines: dict,
) -> float:
    """One team's predicted runs across a full 9-inning game. Formula
    structure: team_scoring_index * blended opponent-pitching term. The
    starter-shrinkage numerator bug is FIXED here (STARTER_SHRINKAGE_K/3,
    matching the denominator's innings units) - see this module's own
    docstring for the bug that this fixes and its confirmed impact."""
    league_runs_per_game = baselines["runs_per_game"]
    league_runs_per_start_inning = baselines["runs_per_start_inning"]
    league_runs_per_bullpen_inning = baselines["runs_per_bullpen_inning"]

    shrunk_team_rate = (team.runs_scored + TEAM_SHRINKAGE_K * league_runs_per_game) / \
        (team.games + TEAM_SHRINKAGE_K)
    team_scoring_index = shrunk_team_rate / league_runs_per_game

    if opposing_starter and opposing_starter.outs > 0:
        starter_ip = opposing_starter.outs / 3
        shrunk_starter_rate = (opposing_starter.runs_allowed + (STARTER_SHRINKAGE_K / 3) * league_runs_per_start_inning) / \
            (starter_ip + STARTER_SHRINKAGE_K / 3)
        starter_allow_index = shrunk_starter_rate / league_runs_per_start_inning
    else:
        starter_allow_index = 1.0

    bullpen_ip = opposing_bullpen.outs / 3
    shrunk_bullpen_rate = (opposing_bullpen.runs_allowed + (BULLPEN_SHRINKAGE_K / 3) * league_runs_per_bullpen_inning) / \
        (bullpen_ip + BULLPEN_SHRINKAGE_K / 3)
    bullpen_allow_index = shrunk_bullpen_rate / league_runs_per_bullpen_inning

    starter_innings_share = _avg_start_innings(opposing_starter_k)
    bullpen_innings_share = max(9.0 - starter_innings_share, 1.0)

    expected_allowed_per_inning = (
        starter_innings_share * league_runs_per_start_inning * starter_allow_index
        + bullpen_innings_share * league_runs_per_bullpen_inning * bullpen_allow_index
    )
    return team_scoring_index * expected_allowed_per_inning


# --- negative-binomial moneyline math (pure stdlib) -----------------------

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


def _run_distribution(mean: float, overdispersion: float, max_runs: int = MAX_RUNS) -> list:
    dist = [_negbinom_pmf(k, mean, overdispersion) for k in range(max_runs + 1)]
    total = sum(dist)
    return [p / total for p in dist] if total > 0 else dist


def moneyline_probabilities(home_mean: float, away_mean: float, overdispersion: float = OVERDISPERSION_RUNS9) -> tuple:
    """Exact joint-distribution moneyline win probabilities (ties split
    50/50) - the validated output."""
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


def has_enough_data(team: TeamRunsFullGameStat) -> bool:
    return team is not None and team.games >= MIN_TEAM_GAMES


# --- top-level entry point --------------------------------------------------

def compute_moneyline(
    home_team: str, away_team: str, home_pitcher_id, away_pitcher_id,
    home_field_runs: float = HOME_FIELD_RUNS_BOOST,
) -> dict | None:
    """
    Returns {"home_win_prob":, "away_win_prob":, "home_mean":, "away_mean":,
    "model_version":}, or None if either team hasn't played
    MIN_TEAM_GAMES yet - matches the validated reference implementation's
    "don't predict at all" behavior rather than guessing on a tiny sample.

    home_mean/away_mean are included only because the moneyline math needs
    them anyway - they are NOT validated as a standalone totals/spread
    prediction, see this module's own docstring, and must not be surfaced
    to users as an O/U or spread line.
    """
    db = SessionLocal()
    try:
        away_t = db.get(TeamRunsFullGameStat, away_team)
        home_t = db.get(TeamRunsFullGameStat, home_team)
        if not has_enough_data(home_t) or not has_enough_data(away_t):
            return None

        baselines = get_hybrid_full_game_baselines(db)

        away_bp = _get_or_create_bullpen_row(db, away_team)
        home_bp = _get_or_create_bullpen_row(db, home_team)
        db.commit()  # persist any just-created zero rows so future reads see them

        away_starter = db.get(PitcherHitsStat, home_pitcher_id) if home_pitcher_id else None
        home_starter = db.get(PitcherHitsStat, away_pitcher_id) if away_pitcher_id else None
        away_starter_k = db.get(PitcherKStat, home_pitcher_id) if home_pitcher_id else None
        home_starter_k = db.get(PitcherKStat, away_pitcher_id) if away_pitcher_id else None

        # Away team's offense faces the HOME starter/bullpen; home team's
        # offense faces the AWAY starter/bullpen.
        away_mean = predicted_team_runs9inn(away_t, away_starter, away_starter_k, home_bp, baselines)
        home_mean = predicted_team_runs9inn(home_t, home_starter, home_starter_k, away_bp, baselines)

        if home_field_runs:
            home_mean += home_field_runs / 2.0
            away_mean = max(0.1, away_mean - home_field_runs / 2.0)

        home_win_prob, away_win_prob = moneyline_probabilities(home_mean, away_mean, OVERDISPERSION_RUNS9)

        return {
            "home_win_prob": home_win_prob,
            "away_win_prob": away_win_prob,
            "home_mean": home_mean,
            "away_mean": away_mean,
            "model_version": MODEL_VERSION,
        }
    finally:
        db.close()
