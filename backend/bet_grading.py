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

  - "f5_lines_home" / "f5_lines_away" / "f5_lines_combined": same real
    F5-runs data and grading path as "game_lines_*" above - these are
    the 1st 5 Innings tab's O/U bets (f5_lines_sync.py's model), a
    different prediction source over the identical real outcome.

  - "f5_moneyline_home" / "f5_moneyline_away": which side had more runs
    after 5 innings, from the same InningLine sum as "f5_lines_*" - a
    tie gives 0.0 to both sides (no push, same convention as every bet
    type here).

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

  - "cfb_moneyline_home" / "cfb_moneyline_away": which side (home/away,
    stored in team_side) actually won the game, fetched fresh from CFBD
    for the bet's own recorded season/week - same cache/lookup-by-
    game-id as "cfb_team_points" above (batter_id holds CFBD's own
    numeric game id). A tie gives 0.0 to both sides (no push, same
    convention as every other moneyline bet_type here, e.g.
    "f5_moneyline_home"/"away"). None (still pending) until CFBD marks
    that game "completed".

  - "cfb_spread_home" / "cfb_spread_away": which side (home/away, stored
    in team_side) actually covered the spread, fetched fresh from CFBD
    for the bet's own recorded season/week - both the real final score
    (same /games cache/lookup-by-game-id as cfb_moneyline_home/away,
    batter_id holds CFBD's own numeric game id) AND the real line itself
    (a SEPARATE /lines refetch, same pick_provider_line used at tracking
    time - deliberately NOT read from bet.line, which is left None here
    so _grade's own 0.5 default applies against this function's 1.0/0.0
    output, same convention as cfb_moneyline_home/away/full_game_
    moneyline_*). Re-fetching the line rather than storing a snapshot at
    tracking time matches this module's "never trust stale state, always
    ask the real source" discipline used everywhere else (e.g.
    cfb_team_points re-fetches the real score instead of trusting a
    cached prediction) - CFBD keeps returning a completed game's posted
    line, so this is safe after the fact. home_margin = home final -
    away final; home covers iff home_margin + home_spread > 0, away iff
    < 0. An exact push (sum == 0 - possible with a whole-number line)
    gives 0.0 to both sides, same no-push simplification as every other
    moneyline-shaped bet_type here. None (still pending) until CFBD
    marks the game "completed" AND still has a usable posted line for
    it.

  - "nfl_passing_yards": NOT gradeable by this module. The only real
    per-game data available for the third-party NFL API is season-
    cumulative totals (see nfl_passing_yards_sync.py's docstring for
    the full investigation) - there is no confirmed-working per-game
    breakdown endpoint to grade a single week's actual passing yards
    against. These bets are intentionally left pending forever unless
    graded by hand (there is no automated grading path for them yet).

  - "nfl_rushing_yards": NOT gradeable by this module, same reason and
    same permanent status as nfl_passing_yards - re-confirmed dead
    directly against the live API while building this prop (see
    nfl_rushing_yards_sync.py's module docstring). Left pending
    indefinitely unless graded by hand.

  - "nfl_qb_dvp_passing_yards" / "nfl_qb_dvp_passing_tds" /
    "nfl_qb_dvp_rushing_yards" / "nfl_qb_dvp_rushing_tds" (the "By
    Position" QB tab's four props): NOT gradeable, same permanent
    reason as nfl_passing_yards/nfl_rushing_yards - see
    nfl_qb_defense_props_sync.py's module docstring.

  - "nfl_rb_dvp_rushing_yards" / "nfl_rb_dvp_rushing_tds" /
    "nfl_rb_dvp_receiving_yards" / "nfl_rb_dvp_receiving_tds" /
    "nfl_rb_dvp_total_yards" / "nfl_rb_dvp_total_tds" /
    "nfl_rb_dvp_anytime_td" (the "By Position" RB tab's six props plus
    the derived anytime-TD probability): NOT gradeable, same permanent
    reason as every other By-Position/estimation-based NFL prop - see
    nfl_rb_defense_props_sync.py's module docstring.

  - "nfl_team_points": fetches that bet's own season/week schedule
    fresh from api.nfldata.org (nfl_points_sync.get_week_games) and
    reads the real final home_score/away_score for whichever team
    (stored by NAME in team_side, since api.nfldata.org has no numeric
    per-game id the way CFBD does - see models_db.py's TrackedBet
    docstring and nfl_points_sync.py's module docstring) played that
    week, matched by comparing team_side against home_team/away_team
    directly. None (still pending) until that game shows both scores
    and game_type == "REG". One fetch per (season, week) pair, cached
    and reused across every bet sharing it - same pattern as CFB.

  - "nfl_game_total": same fetch/cache as nfl_team_points, but
    team_side instead holds an "AWAY@HOME" pair string (e.g. "BUF@KC")
    identifying the whole game - both real final scores are summed
    once that exact away/home pair is found completed.

  - "nfl_moneyline": same fetch/cache as nfl_team_points (team_side
    holds the bet's own team NAME, matched against that week's real
    schedule the same way, since api.nfldata.org has no numeric game id
    to key off of the way CFBD's cfb_moneyline_home/away can). Unlike
    CFB, this is a SINGLE bet_type rather than a home/away split - the
    team name alone is enough to find the right game and side. Actual is
    1.0 if that team's real final score beat the opponent's, 0.0
    otherwise - a tie (possible in the NFL, unlike CFB) gives 0.0, same
    no-push convention as every other moneyline bet_type here. None
    (still pending) until that game shows both scores and
    game_type == "REG".

  - "nhl_goalie_saves": NOT gradeable by this module. The NHL Goalie
    Saves prop's own presumed-starter pick (see
    nhl_goalie_saves_sync.py's docstring) means the goalie who actually
    started a given game may not match the one a bet was tracked
    against, and there's no per-bet real-time re-verification path built
    yet. Left pending indefinitely unless graded by hand, same permanent
    status as nfl_passing_yards/nfl_rushing_yards/the NFL DvP props.

  - "full_game_moneyline_home" / "full_game_moneyline_away": MONEYLINE
    ONLY - full_game_lines_sync.py deliberately does not expose a
    totals/spread prediction (not validated - see its module docstring),
    so those bet_types don't exist. Both read straight off
    Game.home_score/away_score - already populated for every finished
    game by poll_live_games, so grading these needs NO new fetch at all
    (no boxscore, no third-party API). Actual is 1.0/0.0 (won/lost);
    bet.line is left None so _grade's own 0.5 default applies, same
    pattern as first_inning_run.

  - "npb_yrfi": IS gradeable, unlike the props above - the outcome
    itself (did either team score in the 1st) doesn't depend on which
    pitcher was guessed as the presumed starter, only on what actually
    happened in the game, which npb_yrfi_sync.py's daily collector
    already writes to NpbGame.visitor_scored_1st/home_scored_1st once a
    game finishes. No live NPB fetch needed at grading time - just a
    local DB lookup by game_id (stored in external_player_id, same
    reused-column pattern as models_db.py's TrackedBet docstring
    describes for other sports). None (still pending) until that game's
    row shows a real outcome.

GRADING RULE for every bet_type: "yes" wins if actual >= line (or, for
first_inning_run, if a run actually scored); "no" wins the opposite.
HR/Hits/HRR/Pitcher-Hits-Allowed/Game-Lines lines are always whole
numbers ("at least N"), so an exact tie at the line itself is still a
clean win for "yes" (>=), never a push. Pitcher K lines are always
X.5, so an exact tie is mathematically impossible - also never a push.
"""
import logging
import re
from datetime import datetime

import mlb_client
import cfb_points_sync
import nfl_points_sync
from database import SessionLocal
from models_db import TrackedBet, Game, InningLine

log = logging.getLogger("bet_grading")

# bet_types with no MLB game to look up at all (game_pk is always null for
# these, by design - see models_db.py's TrackedBet docstring). These must
# skip the Game/abstract_status check entirely and go straight to
# _actual_value_for_bet, which handles their own real-world "is this
# actually final yet" check itself (CFBD's own "completed" flag, api.
# nfldata.org's own game_type+scores check, or - for NFL Passing Yards -
# the permanent "not gradeable" case).
NO_MLB_GAME_BET_TYPES = {"nfl_passing_yards", "nfl_rushing_yards", "cfb_team_points", "cfb_game_total",
                          "cfb_moneyline_home", "cfb_moneyline_away",
                          "cfb_spread_home", "cfb_spread_away",
                          "nfl_team_points", "nfl_game_total", "nfl_moneyline",
                          "nfl_qb_dvp_passing_yards", "nfl_qb_dvp_passing_tds",
                          "nfl_qb_dvp_rushing_yards", "nfl_qb_dvp_rushing_tds",
                          "nfl_rb_dvp_rushing_yards", "nfl_rb_dvp_rushing_tds",
                          "nfl_rb_dvp_receiving_yards", "nfl_rb_dvp_receiving_tds",
                          "nfl_rb_dvp_total_yards", "nfl_rb_dvp_total_tds", "nfl_rb_dvp_anytime_td",
                          "nhl_goalie_saves", "npb_yrfi"}


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


def _full_game_actual(db, bet: TrackedBet) -> float | None:
    """Real outcome for one Full Game moneyline bet (MONEYLINE ONLY - see
    module docstring). None if the game's real final score isn't on file
    yet (Game.home_score/away_score are always set once poll_live_games
    has seen the game go Final)."""
    game = db.get(Game, bet.game_pk)
    if not game or game.home_score is None or game.away_score is None:
        return None

    if bet.bet_type == "full_game_moneyline_home":
        return 1.0 if game.home_score > game.away_score else 0.0
    if bet.bet_type == "full_game_moneyline_away":
        return 1.0 if game.away_score > game.home_score else 0.0
    return None


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


def _f5_moneyline_actual(db, game_pk: int, side: str) -> float | None:
    """Real F5 moneyline outcome (1.0/0.0) for one side ("home"/"away"),
    from the same live-populated InningLine rows the F5 total/Game Lines
    props already use, summed over innings 1-5. A tie after 5 gives 0.0
    to both sides - same non-push convention as every other bet_type
    this system tracks (see module docstring), and the identical
    strict-greater-than rule Full Game Lines' own moneyline actual uses."""
    lines = db.query(InningLine).filter_by(game_pk=game_pk).filter(InningLine.inning <= 5).all()
    if not lines:
        return None
    away_runs = sum(l.runs for l in lines if l.half == "top")
    home_runs = sum(l.runs for l in lines if l.half == "bottom")
    if side == "home":
        return 1.0 if home_runs > away_runs else 0.0
    if side == "away":
        return 1.0 if away_runs > home_runs else 0.0
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


def _cfb_game_total_actual(bet: TrackedBet, cfb_games_cache: dict) -> float | None:
    """Real final COMBINED points (home + away) for this CFB Game Total
    bet, fetched fresh from CFBD for the bet's own recorded season/week -
    same cache and lookup-by-game-id as _cfb_team_points_actual, but this
    bet type has no team_side (it's a whole-game total, not one side's
    score), so both homePoints and awayPoints are summed directly."""
    if bet.cfb_season is None or bet.cfb_week is None or bet.batter_id is None:
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
            home_pts, away_pts = game.get("homePoints"), game.get("awayPoints")
            return None if home_pts is None or away_pts is None else float(home_pts + away_pts)
    return None


def _cfb_moneyline_actual(bet: TrackedBet, cfb_games_cache: dict) -> float | None:
    """Real moneyline outcome (1.0/0.0) for this CFB Moneyline bet's side
    ("home"/"away"), fetched fresh from CFBD for the bet's own recorded
    season/week - same cache and lookup-by-game-id as
    _cfb_team_points_actual. A tie gives 0.0 to both sides (no push -
    same convention as every other bet_type here, e.g.
    _f5_moneyline_actual)."""
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
            home_pts, away_pts = game.get("homePoints"), game.get("awayPoints")
            if home_pts is None or away_pts is None:
                return None
            if bet.team_side == "home":
                return 1.0 if home_pts > away_pts else 0.0
            if bet.team_side == "away":
                return 1.0 if away_pts > home_pts else 0.0
            return None
    return None


def _cfb_spread_actual(bet: TrackedBet, cfb_games_cache: dict, cfb_lines_cache: dict) -> float | None:
    """Real spread-cover outcome (1.0/0.0) for this CFB Spread bet's side
    ("home"/"away", in team_side) - see module docstring for why the
    real line is RE-FETCHED here rather than read from bet.line. home_
    margin = home final - away final; home covers iff home_margin +
    home_spread > 0, away iff < 0. An exact push (sum == 0) gives 0.0 to
    both sides, same no-push convention as _cfb_moneyline_actual."""
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

    # The line the user actually bet at, own-team perspective (Hawaii
    # +10.5 -> +10.5). Preferred source is bet.spread_line, saved at
    # track time. Older bets lack it, so fall back to the number in the
    # display label ("Hawaii +10.5"). Only if neither exists do we
    # fall back to re-fetching the real posted line (converted to this
    # side's perspective) - that fallback can differ from what the user
    # bet if they adjusted the line, which is why it is last.
    own_spread = bet.spread_line
    if own_spread is None and bet.batter_name:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", bet.batter_name)
        if m:
            own_spread = float(m.group(1))
    if own_spread is None:
        if cache_key not in cfb_lines_cache:
            try:
                cfb_lines_cache[cache_key] = {
                    lg.get("id"): lg for lg in cfb_points_sync.get_week_lines(bet.cfb_season, bet.cfb_week)
                }
            except Exception:
                log.exception("Failed to fetch CFB week %s/%s lines for grading", bet.cfb_season, bet.cfb_week)
                cfb_lines_cache[cache_key] = None
        line_games_by_id = cfb_lines_cache[cache_key]
        if line_games_by_id is None:
            return None
        line_game = line_games_by_id.get(bet.batter_id)
        if line_game is None:
            return None
        _provider, home_spread = cfb_points_sync.pick_provider_line(line_game)
        if home_spread is None:
            return None
        own_spread = home_spread if bet.team_side == "home" else -home_spread

    for game in games:
        if game.get("id") == bet.batter_id:
            if not game.get("completed"):
                return None
            home_pts, away_pts = game.get("homePoints"), game.get("awayPoints")
            if home_pts is None or away_pts is None:
                return None
            if bet.team_side == "home":
                own_margin = home_pts - away_pts
            elif bet.team_side == "away":
                own_margin = away_pts - home_pts
            else:
                return None
            return 1.0 if own_margin + own_spread > 0 else 0.0
    return None


def _nfl_week_games(bet_season: int, bet_week: int, nfl_games_cache: dict) -> list | None:
    """Shared fetch+cache helper for both NFL Team Points and NFL Game
    Total grading - keyed the same way as CFB's cache, just against
    nfl_points_sync's own get_week_games. None means the fetch failed."""
    cache_key = (bet_season, bet_week)
    if cache_key not in nfl_games_cache:
        try:
            nfl_games_cache[cache_key] = nfl_points_sync.get_week_games(bet_season, bet_week)
        except Exception:
            log.exception("Failed to fetch NFL week %s/%s games for grading", bet_season, bet_week)
            nfl_games_cache[cache_key] = None
    return nfl_games_cache[cache_key]


def _nfl_team_points_actual(bet: TrackedBet, nfl_games_cache: dict) -> float | None:
    """Real final points for this NFL Team Points bet's team, fetched
    fresh from api.nfldata.org for the bet's own recorded season/week -
    see module docstring. team_side holds the team's own NAME (there's
    no numeric game id to match by, unlike CFB - see models_db.py's
    TrackedBet docstring). None if the season/week weren't recorded, the
    team can't be found in that week's real schedule, or that game isn't
    a completed REG game yet."""
    if bet.nfl_season is None or bet.nfl_week is None or not bet.team_side:
        return None
    games = _nfl_week_games(bet.nfl_season, bet.nfl_week, nfl_games_cache)
    if games is None:
        return None
    for game in games:
        if not nfl_points_sync.is_completed_reg(game):
            continue
        if game.get("home_team") == bet.team_side:
            return float(game["home_score"])
        if game.get("away_team") == bet.team_side:
            return float(game["away_score"])
    return None


def _nfl_game_total_actual(bet: TrackedBet, nfl_games_cache: dict) -> float | None:
    """Real final COMBINED points (home + away) for this NFL Game Total
    bet, fetched fresh from api.nfldata.org for the bet's own recorded
    season/week - same cache/fetch as _nfl_team_points_actual, but this
    bet type's team_side instead holds an "AWAY@HOME" pair string (e.g.
    "BUF@KC" - see models_db.py's TrackedBet docstring), matched against
    that exact away_team/home_team pair rather than a single team name."""
    if bet.nfl_season is None or bet.nfl_week is None or not bet.team_side or "@" not in bet.team_side:
        return None
    away_team, home_team = bet.team_side.split("@", 1)
    games = _nfl_week_games(bet.nfl_season, bet.nfl_week, nfl_games_cache)
    if games is None:
        return None
    for game in games:
        if not nfl_points_sync.is_completed_reg(game):
            continue
        if game.get("home_team") == home_team and game.get("away_team") == away_team:
            return float(game["home_score"] + game["away_score"])
    return None


def _nfl_moneyline_actual(bet: TrackedBet, nfl_games_cache: dict) -> float | None:
    """Real moneyline outcome (1.0/0.0) for this NFL Moneyline bet's
    team, fetched fresh from api.nfldata.org for the bet's own recorded
    season/week - same cache/fetch and team-name matching as
    _nfl_team_points_actual (team_side holds the team's own NAME, since
    there's no numeric game id to match by the way CFB's
    cfb_moneyline_home/away can). A tie gives 0.0 (no push - same
    convention as every other moneyline bet_type here, e.g.
    _cfb_moneyline_actual)."""
    if bet.nfl_season is None or bet.nfl_week is None or not bet.team_side:
        return None
    games = _nfl_week_games(bet.nfl_season, bet.nfl_week, nfl_games_cache)
    if games is None:
        return None
    for game in games:
        if not nfl_points_sync.is_completed_reg(game):
            continue
        if game.get("home_team") == bet.team_side:
            return 1.0 if game["home_score"] > game["away_score"] else 0.0
        if game.get("away_team") == bet.team_side:
            return 1.0 if game["away_score"] > game["home_score"] else 0.0
    return None


def _npb_yrfi_actual(db, bet: TrackedBet) -> float | None:
    """Real 1st-inning-scored outcome for this NPB YRFI bet, read
    directly from our own already-collected NpbGame row - no live NPB
    fetch needed, npb_yrfi_sync.daily_update already wrote
    visitor_scored_1st/home_scored_1st once the game finished. The
    game_id is stored in external_player_id (same reused-column pattern
    as every other non-MLB sport in this project - see models_db.py's
    TrackedBet docstring). None if the game hasn't finished yet or the
    id wasn't found."""
    from models_db import NpbGame
    if not bet.external_player_id:
        return None
    game = db.get(NpbGame, bet.external_player_id)
    if not game or game.visitor_scored_1st is None or game.home_scored_1st is None:
        return None
    return 1.0 if (game.visitor_scored_1st or game.home_scored_1st) else 0.0


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


def _actual_value_for_bet(db, bet: TrackedBet, boxscore_cache: dict, cfb_games_cache: dict, cfb_lines_cache: dict, nfl_games_cache: dict) -> float | None:
    """Returns the real outcome for one bet, in whatever unit its
    bet_type uses. None means "can't grade yet" (data not available),
    NOT "the outcome was zero" - callers must check for None explicitly."""
    if bet.bet_type == "first_inning_run":
        actual = _first_inning_actual(db, bet.game_pk)
        return None if actual is None else (1.0 if actual else 0.0)

    if bet.bet_type == "npb_yrfi":
        return _npb_yrfi_actual(db, bet)

    if bet.bet_type.startswith("game_lines_"):
        key = bet.bet_type[len("game_lines_"):]  # "home" / "away" / "combined"
        return _game_lines_actual(db, bet.game_pk, key)

    if bet.bet_type.startswith("f5_lines_"):
        key = bet.bet_type[len("f5_lines_"):]  # "home" / "away" / "combined"
        return _game_lines_actual(db, bet.game_pk, key)  # same real F5-runs data, different model source

    if bet.bet_type.startswith("f5_moneyline_"):
        side = bet.bet_type[len("f5_moneyline_"):]  # "home" / "away"
        return _f5_moneyline_actual(db, bet.game_pk, side)

    if bet.bet_type.startswith("full_game_"):
        return _full_game_actual(db, bet)

    if bet.bet_type == "cfb_team_points":
        return _cfb_team_points_actual(bet, cfb_games_cache)

    if bet.bet_type == "cfb_game_total":
        return _cfb_game_total_actual(bet, cfb_games_cache)

    if bet.bet_type.startswith("cfb_moneyline_"):
        return _cfb_moneyline_actual(bet, cfb_games_cache)

    if bet.bet_type.startswith("cfb_spread_"):
        return _cfb_spread_actual(bet, cfb_games_cache, cfb_lines_cache)

    if bet.bet_type == "nfl_passing_yards":
        # No working per-game data source exists for this - see module
        # docstring. Left pending indefinitely rather than guessed at.
        return None

    if bet.bet_type == "nfl_rushing_yards":
        # Same permanent limitation as nfl_passing_yards - see module
        # docstring. Left pending indefinitely rather than guessed at.
        return None

    if bet.bet_type.startswith("nfl_qb_dvp_"):
        # Same permanent limitation as nfl_passing_yards/nfl_rushing_yards
        # - see module docstring. Left pending indefinitely rather than
        # guessed at.
        return None

    if bet.bet_type.startswith("nfl_rb_dvp_"):
        # Same permanent limitation as every other By-Position/estimation
        # based NFL prop - see module docstring. Left pending indefinitely
        # rather than guessed at.
        return None

    if bet.bet_type == "nfl_team_points":
        return _nfl_team_points_actual(bet, nfl_games_cache)

    if bet.bet_type == "nfl_game_total":
        return _nfl_game_total_actual(bet, nfl_games_cache)

    if bet.bet_type == "nfl_moneyline":
        return _nfl_moneyline_actual(bet, nfl_games_cache)

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
        cfb_lines_cache = {}
        nfl_games_cache = {}
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

            actual = _actual_value_for_bet(db, bet, boxscore_cache, cfb_games_cache, cfb_lines_cache, nfl_games_cache)
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
