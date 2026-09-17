"""
Grades every tracked bet whose game has actually finished, against
real per-game results - closing out the "grading" side of the Track
checkbox (the TrackedBet table's resolved/actual_value/result fields
have been sitting ready for this since the Track feature was built).

DATA SOURCES, one per bet_type:
  - "first_inning_run": sums InningLine.runs for inning=1 across BOTH
    halves (top and bottom) - the model itself predicts "does EITHER
    team score in the 1st", not one specific team's half (confirmed
    directly from predictor.py's inning_scoring_probability: the final
    combining step is 1 - (1-away_prob)*(1-home_prob), i.e. "at least
    one of the two scores"). InningLine is already populated live by
    poll_live_games - no new fetch needed for this bet type at all.

  - "hits" / "hrr" / "hr" / "pitcher_k": all four need this specific
    PLAYER's PER-GAME stats (not their season totals, which is all
    BatterSeasonStat/PitcherKStat/PitcherHitsStat store) - fetched via
    the same boxscore endpoint already used for lineup detection
    (mlb_client.get_boxscore), just reading each player's own "stats"
    sub-object this time instead of "battingOrder". One boxscore fetch
    per GAME, cached and reused across every bet on that same game
    (a game with several tracked bets doesn't refetch per bet).

GRADING RULE for every bet_type: "yes" wins if actual >= line (or, for
first_inning_run, if a run actually scored); "no" wins the opposite.
HR/Hits/HRR lines are always whole numbers ("at least N"), so an exact
tie at the line itself is still a clean win for "yes" (>=), never a
push. Pitcher K lines are always X.5, so an exact tie is mathematically
impossible - also never a push.
"""
import logging
from datetime import datetime

import mlb_client
from database import SessionLocal
from models_db import TrackedBet, Game, InningLine

log = logging.getLogger("bet_grading")

FINAL_STATUSES = {"Final", "Game Over"}


def _first_inning_actual(db, game_pk: int) -> bool | None:
    """True if either team scored in the 1st inning, False if neither
    did, None if we don't have any 1st-inning line data for this game
    at all (shouldn't happen for a genuinely finished game, but safer
    to skip grading than guess)."""
    lines = db.query(InningLine).filter_by(game_pk=game_pk, inning=1).all()
    if not lines:
        return None
    return sum(l.runs for l in lines) > 0


def _player_game_stats(boxscore: dict, team_side: str, player_id: int) -> dict | None:
    """This player's own per-game stats from a boxscore response - the
    same 'players' dict extract_boxscore_lineup already reads for
    lineup detection, just pulling the 'stats' sub-object instead of
    'battingOrder' this time. Returns the raw {"batting": {...},
    "pitching": {...}} stats dict for this player, or None if they're
    not in this box score at all (e.g. a late scratch)."""
    players = boxscore.get("teams", {}).get(team_side, {}).get("players", {})
    pdata = players.get(f"ID{player_id}", {})
    stats = pdata.get("stats")
    return stats if stats else None


def _actual_value_for_bet(db, bet: TrackedBet, boxscore_cache: dict) -> float | None:
    """Returns the real outcome for one bet, in whatever unit its
    bet_type uses. None means "can't grade yet" (data not available),
    NOT "the outcome was zero" - callers must check for None explicitly."""
    if bet.bet_type == "first_inning_run":
        actual = _first_inning_actual(db, bet.game_pk)
        return None if actual is None else (1.0 if actual else 0.0)

    if bet.game_pk not in boxscore_cache:
        try:
            boxscore_cache[bet.game_pk] = mlb_client.get_boxscore(bet.game_pk)
        except Exception:
            log.exception("Failed to fetch boxscore for game %s", bet.game_pk)
            boxscore_cache[bet.game_pk] = None
    boxscore = boxscore_cache[bet.game_pk]
    if boxscore is None or not bet.team_side or not bet.batter_id:
        return None

    stats = _player_game_stats(boxscore, bet.team_side, bet.batter_id)
    if stats is None:
        return None

    if bet.bet_type == "hits":
        batting = stats.get("batting", {})
        return float(batting.get("hits", 0))
    if bet.bet_type == "hrr":
        batting = stats.get("batting", {})
        return float(batting.get("hits", 0) + batting.get("runs", 0) + batting.get("rbi", 0))
    if bet.bet_type == "hr":
        batting = stats.get("batting", {})
        return float(batting.get("homeRuns", 0))
    if bet.bet_type == "pitcher_k":
        pitching = stats.get("pitching", {})
        return float(pitching.get("strikeOuts", 0))

    log.warning("Unknown bet_type %r for tracked bet %s - skipping", bet.bet_type, bet.id)
    return None


def _grade(bet: TrackedBet, actual: float) -> str:
    """'win' or 'loss' - see module docstring for the >= rule and why
    a push is never possible for any bet_type this system tracks."""
    line = bet.line if bet.line is not None else bet.hits_threshold
    if line is None:
        line = 0.5  # first_inning_run has no line field - "yes" means actual==1.0, "no" means actual==0.0
    hit_the_over = actual >= line
    return "win" if (hit_the_over if bet.yn == "yes" else not hit_the_over) else "loss"


def grade_pending_bets() -> dict:
    """
    Grades every unresolved tracked bet whose game has actually
    finished. Safe to run any time and any number of times - already-
    resolved bets are never touched again, and a bet whose game hasn't
    finished yet (or whose per-game stats aren't available yet) is
    simply left pending for the next run rather than guessed at.

    Returns a summary dict: {"graded":, "wins":, "losses":,
    "still_pending": (games not final yet or data not available)}.
    """
    db = SessionLocal()
    try:
        pending = db.query(TrackedBet).filter_by(resolved=False).all()
        boxscore_cache = {}
        graded, wins, losses, still_pending = 0, 0, 0, 0

        for bet in pending:
            game = db.get(Game, bet.game_pk)
            if not game or game.status not in FINAL_STATUSES:
                still_pending += 1
                continue

            actual = _actual_value_for_bet(db, bet, boxscore_cache)
            if actual is None:
                still_pending += 1
                continue

            result = _grade(bet, actual)
            bet.actual_value = actual
            bet.result = result
            bet.resolved = True
            graded += 1
            if result == "win":
                wins += 1
            else:
                losses += 1

        db.commit()
        summary = {"graded": graded, "wins": wins, "losses": losses, "still_pending": still_pending}
        log.info("grade_pending_bets complete: %s", summary)
        return summary
    except Exception:
        log.exception("grade_pending_bets failed")
        db.rollback()
        return {"graded": 0, "wins": 0, "losses": 0, "still_pending": 0, "error": "grading failed - see logs"}
    finally:
        db.close()
