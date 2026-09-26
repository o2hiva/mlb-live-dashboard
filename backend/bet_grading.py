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

  - "hits" / "hrr" / "hr" / "pitcher_k" / "pitcher_hits_allowed": all
    five need this specific PLAYER's PER-GAME stats (not their season
    totals, which is all BatterSeasonStat/PitcherKStat/PitcherHitsStat
    store) - fetched via the same boxscore endpoint already used for
    lineup detection (mlb_client.get_boxscore), just reading each
    player's own "stats" sub-object this time instead of
    "battingOrder". One boxscore fetch per GAME, cached and reused
    across every bet on that same game (a game with several tracked
    bets doesn't refetch per bet). pitcher_hits_allowed reads
    stats.pitching.hits - MLB's boxscore/season-stats API uses the
    same "hits" key for "hits allowed by this pitcher" as it does for
    "hits by this batter" (confirmed against mlb_client.py's own
    season-stats fetch, which maps that identical field into
    PitcherHitsStat.hits_allowed).

  - "game_lines_home" / "game_lines_away" / "game_lines_combined":
    sum of InningLine.runs for innings 1-5 for that side (home =
    "bottom" half, away = "top" half, combined = both) - no new fetch
    needed, InningLine is already populated live for every inning
    (not just the 1st) by poll_live_games.

  - "cfb_team_points": fetches that bet's own season/week schedule
    fresh from CFBD (cfb_points_sync.get_week_games) and reads the real
    final homePoints/awayPoints for whichever side (home/away, stored
    in team_side) the bet's team was on, matched by CFBD's own numeric
    game id (stored in batter_id - see models_db.py's TrackedBet
    docstring for why these general-purpose columns were reused instead
    of adding CFB-specific ones). Unlike NFL, this needs no estimation
    workaround at all: CFBD's /games endpoint gives the real final score
    directly. None (still pending) until CFBD marks that game
    "completed". One CFBD fetch per (season, week) pair, cached and
    reused across every bet sharing it - same pattern as the boxscore
    cache above.

  - "nfl_passing_yards": NOT gradeable by this module. The only real
    per-game data available for the third-party NFL API is season-
    cumulative totals (see nfl_passing_yards_sync.py's docstring for
    the full investigation) - there is no confirmed-working per-game
    breakdown endpoint to grade a single week's actual passing yards
    against. These bets are intentionally left pending forever unless
    graded by hand (there is no automated grading path for them yet).

GRADING RULE for every bet_type: "yes" wins if actual >= line (or, for
first_inning_run, if a run actually scored); "no" wins the opposite.
HR/Hits/HRR/Pitcher-Hits-Allowed/Game-Lines lines are always whole
numbers ("at least N"), so an exact tie at the line itself is still a
clean win for "yes" (>=), never a push. Pitcher K lines are always
X.5, so an exact tie is mathematically impossible - also never a push.
"""
import logging
from datetime import datetime

import mlb_client
import cfb_points_sync
from database import SessionLocal
from models_db import TrackedBet, Game, InningLine

log = logging.getLogger("bet_grading")

# bet_types with no MLB game to look up at all (game_pk is always null for
# these, by design - see models_db.py's TrackedBet docstring). These must
# skip the Game/abstract_status check entirely and go straight to
# _actual_value_for_bet, which handles their own real-world "is this
# actually final yet" check itself (CFBD's own "completed" flag, or -
# for NFL - the permanent "not gradeable" case).
NO_MLB_GAME_BET_TYPES = {"nfl_passing_yards", "cfb_team_points"}


def _refresh_abstract_status(db, game: Game):
    """
    Live re-check of one game's true abstract status directly from MLB,
    bypassing the regular poller entirely - the poller only ever
    revisits today + the next 2 days, so a game whose date has already
    passed would otherwise never get its abstract_status corrected,
    even after the column itself started being populated correctly
    going forward. Safe to call for any game; a real network/API
    failure just leaves the stored value as-is rather than raising.
    """
    try:
        games_that_day = mlb_client.get_schedule(game.game_date)
    except Exception:
        log.exception("Failed to refresh abstract_status for game %s", game.game_pk)
        return
    for g in games_that_day:
        if g["game_pk"] == game.game_pk:
            game.status = g["status"]
            game.abstract_status = g["abstract_status"]
            db.commit()
            return


def _first_inning_actual(db, game_pk: int) -> bool | None:
    """True if either team scored in the 1st inning, False if neither
    did, None if we don't have any 1st-inning line data for this game
    at all (shouldn't happen for a genuinely finished game, but safer
    to skip grading than guess)."""
    lines = db.query(InningLine).filter_by(game_pk=game_pk, inning=1).all()
    if not lines:
        return None
    return sum(l.runs for l in lines) > 0


def _game_lines_actual(db, game_pk: int, key: str) -> float | None:
    """Real 1st-5-innings runs for one side of a Game Lines bet ("home",
    "away", or "combined"), summed from the same live-populated
    InningLine rows the 1st-inning market already uses - just over
    innings 1-5 and (for home/away) filtered to that side's own half.
    None if we have no inning-line data for this game at all."""
    lines = db.query(InningLine).filter_by(game_pk=game_pk).filter(InningLine.inning <= 5).all()
    if not lines:
        return None
    away_runs = sum(l.runs for l in lines if l.half == "top")
    home_runs = sum(l.runs for l in lines if l.half == "bottom")
    if key == "away":
        return float(away_runs)
    if key == "home":
        return float(home_runs)
    if key == "combined":
        return float(away_runs + home_runs)
    return None


def _cfb_team_points_actual(bet: TrackedBet, cfb_games_cache: dict) -> float | None:
    """Real final points for this CFB Team Points bet's team, fetched
    fresh from CFBD for the bet's own recorded season/week - see module
    docstring. None if the season/week weren't recorded, the game can't
    be found in that week's real schedule, or CFBD hasn't marked it
    completed yet."""
    if bet.cfb_season is None or bet.cfb_week is None or bet.batter_id is None or not bet.team_side:
        return None
    cache_key = (bet.cfb_season, bet.cfb_week)
    if cache_key not in cfb_games_cache:
        try:
            cfb_games_cache[cache_key] = cfb_points_sync.get_week_games(bet.cfb_season, bet.cfb_week)
        except Exception:
            log.exception("Failed to fetch CFB week %s/%s games for grading", bet.cfb_season, bet.cfb_week)
            cfb_games_cache[cache_key] = None
    games = cfb_games_cache[cache_key]
    if games is None:
        return None
    for game in games:
        if game.get("id") == bet.batter_id:
            if not game.get("completed"):
                return None
            pts = game.get("homePoints") if bet.team_side == "home" else game.get("awayPoints")
            return None if pts is None else float(pts)
    return None


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


def _actual_value_for_bet(db, bet: TrackedBet, boxscore_cache: dict, cfb_games_cache: dict) -> float | None:
    """Returns the real outcome for one bet, in whatever unit its
    bet_type uses. None means "can't grade yet" (data not available),
    NOT "the outcome was zero" - callers must check for None explicitly."""
    if bet.bet_type == "first_inning_run":
        actual = _first_inning_actual(db, bet.game_pk)
        return None if actual is None else (1.0 if actual else 0.0)

    if bet.bet_type.startswith("game_lines_"):
        key = bet.bet_type[len("game_lines_"):]  # "home" / "away" / "combined"
        return _game_lines_actual(db, bet.game_pk, key)

    if bet.bet_type == "cfb_team_points":
        return _cfb_team_points_actual(bet, cfb_games_cache)

    if bet.bet_type == "nfl_passing_yards":
        # No working per-game data source exists for this - see module
        # docstring. Left pending indefinitely rather than guessed at.
        return None

    # Self-heal: a bug (now fixed) in the Pitcher K tracking UI never
    # sent the pitcher's id, only their name - any bet caught by that
    # window has batter_id=None and would otherwise be stuck pending
    # forever. Since we know which pitcher started for a given
    # team_side in a given game (Game.home/away_probable_pitcher_id),
    # backfill it here rather than leave it permanently ungradeable.
    if bet.bet_type == "pitcher_k" and not bet.batter_id and bet.team_side:
        game = db.get(Game, bet.game_pk)
        if game:
            pitcher_id = game.home_probable_pitcher_id if bet.team_side == "home" else game.away_probable_pitcher_id
            if pitcher_id:
                bet.batter_id = pitcher_id
                log.info("Backfilled missing batter_id=%s for pitcher_k bet %s", pitcher_id, bet.id)

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
    if bet.bet_type == "pitcher_hits_allowed":
        pitching = stats.get("pitching", {})
        return float(pitching.get("hits", 0))

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
        cfb_games_cache = {}
        graded, wins, losses, still_pending = 0, 0, 0, 0

        for bet in pending:
            if bet.bet_type not in NO_MLB_GAME_BET_TYPES:
                game = db.get(Game, bet.game_pk)
                if not game:
                    still_pending += 1
                    continue

                # abstract_status is always exactly "Final" once a game is
                # truly over, regardless of what MLB's more verbose `status`
                # field happened to say at poll time - but the STORED value
                # only gets refreshed by the regular poller, which only ever
                # revisits today + the next 2 days. A game whose date has
                # already passed is never re-checked, so an old row can be
                # stuck at whatever abstract_status it had (or the column's
                # own migration-time default) forever. Rather than trust a
                # value that may simply never have been updated, re-check
                # live with MLB directly for anything not already "Final".
                if game.abstract_status != "Final":
                    _refresh_abstract_status(db, game)
                if game.abstract_status != "Final":
                    still_pending += 1
                    continue

            actual = _actual_value_for_bet(db, bet, boxscore_cache, cfb_games_cache)
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
