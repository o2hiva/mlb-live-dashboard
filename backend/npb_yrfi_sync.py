"""
npb_yrfi_sync.py

NPB (Japan) YRFI/NRFI (1st-inning-run-scored) prop - the live-dashboard
port of core_npb_yrfi.py, wired to real data from spaia.jp's public NPB
API (no key needed - same source as npb_backtest_first_inning.py /
npb_backtest_yrfi.py).

PITCHER-LEVEL ALLOWING RATE, NOT TEAM-LEVEL (see core_npb_yrfi.py's
docstring for the full story): the batting side stays TEAM-level (a
lineup's scoring tendency isn't attributable to one player), but the
allowing side uses the STARTING PITCHER's own 1st-inning-allowed history -
a team-wide allowing rate showed no real predictive edge in backtesting,
since it pools every starter on the roster together.

FORMULA (verbatim from core_npb_yrfi.py, validated against the real 2026
season - 725 games, 162 starters, 327 picks, Brier=0.2480):
    p_visitor_scores = log5(shrunk(away team's own scoring rate),
                             shrunk(home starter's own allowing rate),
                             league_avg)
    p_home_scores    = log5(shrunk(home team's own scoring rate),
                             shrunk(away starter's own allowing rate),
                             league_avg)
    P(YRFI) = 1 - (1 - p_visitor_scores) * (1 - p_home_scores)
Bill James' log5 is used (not index-multiplication) because this is a
probability bounded in [0, 1], not an open-ended count.
VALIDATED CONSTANTS: team_shrinkage_k=200.0 (games), pitcher_shrinkage_k=
30.0 (starts), LEAGUE_AVG_STATIC=0.2607 (real pooled rate).

NO CONFIRMED STARTING PITCHER FOR A FUTURE GAME: spaia.jp's
both_pitcher_game_stats endpoint is real per-game BOX SCORE data - it
only has a value once the game has actually been played, never ahead of
time. Same "heuristic" pattern used elsewhere in this dashboard (NHL's
presumed goalie, NFL QB/RB's highest-volume starter): each team's
presumed starter for an upcoming game is whichever of its pitchers has
the most starts on record so far this season - see _pick_presumed_starter()
below. Every prediction is flagged starters_are_heuristic=True.

TWO WAYS DATA GETS IN:
  1. seed_from_progress() - a ONE-TIME bulk import of an existing
     npb_first_inning_progress.json (built by npb_backtest_first_inning.py)
     via POST /api/admin/seed-npb-yrfi, so the tab starts with real season
     history instead of an empty slate. Idempotent - safe to re-run (dedup
     via NpbCollectedGame, same as #2).
  2. daily_update() - incremental daily collection, same resumable
     discipline as nhl_goalie_saves_sync.py: sweeps a small window of
     real dates (JST) around today, fetches any newly-finished game's
     final score/starters, folds them into the running team-scoring/
     pitcher-allowing totals, and refreshes each date's real schedule
     (NpbGame) for display.

DATA SOURCE: https://spaia.jp/baseball/npb/api (no key needed):
    games_info_by_date?gameDate=YYYYMMDD -> that date's real games
    current_score?game_id=X               -> per-inning score (JSON-
        encoded STRING under the "score" key - see _parse_first_inning)
    both_pitcher_game_stats?gameId=X       -> starter identification
        (no="1" marks the starter - see get_starters)
REGULAR SEASON ONLY: gameTypeName containing "公式戦" (confirmed to cover
both Central/Pacific League official games and interleague). COMPLETED
ONLY (for collection): gameStateName == "試合終了".
"""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from database import SessionLocal
from models_db import NpbCollectedGame, NpbTeamScoreStat, NpbPitcherAllowStat, NpbGame

log = logging.getLogger("npb_yrfi_sync")

API_BASE = "https://spaia.jp/baseball/npb/api"
TIMEOUT = 30

FINISHED_STATE = "試合終了"
REGULAR_SEASON_MARKER = "公式戦"

_JST = ZoneInfo("Asia/Tokyo")
_PACIFIC = ZoneInfo("America/Los_Angeles")

# All 12 real NPB teams' full Japanese name (exactly as spaia.jp's
# homeTeamName/visitorTeamName return them, and the same primary-key
# string NpbTeamScoreStat/NpbPitcherAllowStat/NpbGame are keyed on) ->
# English name, for display only. Confirmed against real API responses
# for every team (spaia.jp/baseball/npb/api/games_info_by_date).
TEAM_NAME_EN = {
    "読売ジャイアンツ": "Yomiuri Giants",
    "東京ヤクルトスワローズ": "Tokyo Yakult Swallows",
    "横浜DeNAベイスターズ": "Yokohama DeNA BayStars",
    "中日ドラゴンズ": "Chunichi Dragons",
    "阪神タイガース": "Hanshin Tigers",
    "広島東洋カープ": "Hiroshima Toyo Carp",
    "埼玉西武ライオンズ": "Saitama Seibu Lions",
    "北海道日本ハムファイターズ": "Hokkaido Nippon-Ham Fighters",
    "千葉ロッテマリーンズ": "Chiba Lotte Marines",
    "福岡ソフトバンクホークス": "Fukuoka SoftBank Hawks",
    "オリックス・バファローズ": "Orix Buffaloes",
    "東北楽天ゴールデンイーグルス": "Tohoku Rakuten Golden Eagles",
}

# Real gameStateName values seen from spaia.jp -> English label. Falls
# back to the raw Japanese string for anything not in this table (an
# unusual/unrecognized status still displays as something rather than
# going blank).
STATUS_EN = {
    "試合前": "Scheduled",
    "試合中": "In Progress",
    "試合終了": "Final",
    "試合中止": "Cancelled",
    "コールドゲーム": "Called Game",
    "延期": "Postponed",
    "順延": "Postponed",
}


def team_name_en(name: str | None) -> str | None:
    """English display name for a real NPB team's full Japanese name.
    Falls back to the original string for anything not in TEAM_NAME_EN
    (e.g. a franchise rename spaia.jp hasn't been updated for here)."""
    if name is None:
        return None
    return TEAM_NAME_EN.get(name, name)


def status_en(status: str | None) -> str | None:
    """English label for a real NPB gameStateName. Falls back to the
    original string for anything not in STATUS_EN."""
    if status is None:
        return None
    return STATUS_EN.get(status, status)


def start_time_pacific(date_str: str | None, start_time_jst: str | None) -> dict | None:
    """Converts a real NPB game's JST calendar date (NpbGame.date,
    "YYYYMMDD") + start time (NpbGame.start_time_jst, "HHMM" 24h JST
    local clock time - spaia.jp's own "startTime" field, confirmed
    against a real games_info_by_date response) into Pacific time.
    Returns {"time_24h": "HH:MM", "date_iso": "YYYY-MM-DD",
    "same_calendar_day": bool} or None if either input is missing or
    unparseable. same_calendar_day is False whenever the Pacific date
    differs from the JST schedule date - which is normal (JST is
    16-17 hours ahead of Pacific, so most evening NPB first pitches land
    on the Pacific calendar day BEFORE the JST date), not an error; the
    caller decides whether/how to flag it."""
    if not date_str or not start_time_jst or len(start_time_jst) < 3:
        return None
    try:
        hour = int(start_time_jst[:-2])
        minute = int(start_time_jst[-2:])
        jst_dt = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]), hour, minute, tzinfo=_JST)
    except (ValueError, IndexError):
        return None
    pacific_dt = jst_dt.astimezone(_PACIFIC)
    return {
        "time_24h": pacific_dt.strftime("%H:%M"),
        "date_iso": pacific_dt.date().isoformat(),
        "same_calendar_day": pacific_dt.strftime("%Y%m%d") == date_str,
    }

MIN_PRIOR_GAMES = 10          # team games of batting history required
MIN_PRIOR_STARTS = 5          # pitcher starts of allowing history required
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 8   # teams needing >= MIN_PRIOR_GAMES before the live league average is trusted
MIN_LEAGUE_POOL = 60          # team-games pooled before the running league average is trusted over the static fallback

TEAM_SHRINKAGE_K = 200.0      # VALIDATED (games unit) - see core_npb_yrfi.py
PITCHER_SHRINKAGE_K = 30.0    # VALIDATED (starts unit) - see core_npb_yrfi.py
LEAGUE_AVG_STATIC = 0.2607    # VALIDATED - real pooled rate, see core_npb_yrfi.py

CACHE_TTL = timedelta(hours=6)
_league_avg_cache = {"value": None, "computed_at": None}


def get(path: str, **params) -> dict:
    url = f"{API_BASE}/{path}"
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _jst_today():
    """NPB's calendar day is Japan Standard Time (UTC+9, no DST) - "today"
    for collection/display purposes is computed directly from UTC rather
    than the server's own local time."""
    return (datetime.utcnow() + timedelta(hours=9)).date()


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scored_in_first(score_array: list, tb: str) -> bool:
    """True if the tb ("1"=top/visitor, "2"=bottom/home) entry for inn="1"
    has runs > 0. A missing 1st-inning entry is treated as 0 runs rather
    than an error - some feeds omit a scoreless inning entirely."""
    for entry in score_array:
        if str(entry.get("inn")) == "1" and str(entry.get("tb")) == tb:
            try:
                return int(entry.get("r", 0)) > 0
            except (TypeError, ValueError):
                return False
    return False


def get_boxscore(game_id) -> dict | None:
    """Returns the raw single-game summary object from current_score, or
    None if there's no usable data yet. REAL SHAPE (confirmed against a
    raw requests.get dump, not an AI-summarized read - see
    npb_backtest_first_inning.py's docstring for the bug this caught): the
    endpoint returns a LIST with ONE summary object; the inning-by-inning
    array is a JSON-ENCODED STRING under "score", not a nested list."""
    response = get("current_score", game_id=game_id)
    if not response:
        return None
    return response[0] if isinstance(response, list) else response


def _parse_first_inning(entry: dict) -> dict | None:
    score_raw = entry.get("score")
    if not score_raw:
        return None
    try:
        score_array = json.loads(score_raw)
    except (TypeError, ValueError):
        return None
    if not score_array:
        return None
    return {
        "visitor_scored_1st": _scored_in_first(score_array, "1"),
        "home_scored_1st": _scored_in_first(score_array, "2"),
    }


def get_starters(game_id) -> dict | None:
    """Returns {team_id_str: {"id":, "name":}} for both sides, or None if
    either side's starter can't be identified. no="1" in
    both_pitcher_game_stats reliably marks the starter (confirmed against
    a real game - see npb_backtest_first_inning.py's docstring)."""
    pitchers = get("both_pitcher_game_stats", gameId=game_id)
    if not pitchers:
        return None
    starters = {}
    for p in pitchers:
        if str(p.get("no")) == "1":
            starters[str(p.get("teamId"))] = {"id": p.get("playerId"), "name": p.get("playerName")}
    return starters if len(starters) == 2 else None


def _shrunk_rate(hits: int, games: int, k: float, league_avg: float) -> float:
    return (hits + k * league_avg) / (games + k)


def _log5(p_a: float, p_b: float, league_avg: float) -> float:
    league_avg = min(max(league_avg, 1e-6), 1 - 1e-6)
    p_a = min(max(p_a, 1e-6), 1 - 1e-6)
    p_b = min(max(p_b, 1e-6), 1 - 1e-6)
    num = (p_a * p_b) / league_avg
    den = num + ((1 - p_a) * (1 - p_b)) / (1 - league_avg)
    return num / den


def _apply_batting(db, team: str, scored: bool) -> None:
    row = db.get(NpbTeamScoreStat, team)
    if row is None:
        row = NpbTeamScoreStat(team=team, games=0, scored=0)
        db.add(row)
        # The session is autoflush=False (see database.py), so without this
        # flush a second _apply_batting() call for the SAME team later in
        # this same uncommitted batch would not see this pending row via
        # db.get() and would create a second one - colliding on the primary
        # key at commit time (psycopg2.errors.UniqueViolation). Flushing
        # sends the pending INSERT to the DB now (not a commit), making it
        # visible to subsequent db.get() calls within this transaction.
        db.flush()
    row.games += 1
    if scored:
        row.scored += 1


def _apply_pitching(db, pitcher_id, name: str, team: str, allowed: bool) -> None:
    pitcher_id = str(pitcher_id)
    row = db.get(NpbPitcherAllowStat, pitcher_id)
    if row is None:
        row = NpbPitcherAllowStat(pitcher_id=pitcher_id, name=name, team=team, starts=0, allowed=0)
        db.add(row)
        # Same reasoning as _apply_batting() above - required so a starter
        # who starts more than once within one uncommitted commit batch
        # (any real starter, across a season's worth of games) doesn't get
        # inserted twice and collide on pitcher_id at commit time. This is
        # the exact bug that produced the reported
        # "npb_pitcher_allow_stats_pkey" UniqueViolation during
        # seed_from_progress().
        db.flush()
    row.name = name
    row.team = team  # keep the pitcher's most-recently-seen team current
    row.starts += 1
    if allowed:
        row.allowed += 1


def seed_from_progress(progress: dict) -> dict:
    """
    ONE-TIME bulk import of an existing npb_first_inning_progress.json
    (built by npb_backtest_first_inning.py) - accumulates its FINAL
    totals directly into NpbTeamScoreStat/NpbPitcherAllowStat (no walk-
    forward needed here, unlike the backtest - every collected game is
    real prior history for the next real live prediction). Idempotent:
    each game is recorded in NpbCollectedGame, so calling this again (or
    running daily_update afterward) never double-counts an already-seeded
    game. Games missing starter IDs (pre-backfill legacy games) still
    count for the batting side, matching core_npb_yrfi.py's own
    load_counts_from_progress behavior.
    """
    db = SessionLocal()
    try:
        games = progress.get("games", {})
        applied = 0
        skipped_existing = 0
        skipped_no_starters = 0

        for game_id, g in games.items():
            game_id = str(game_id)
            if db.get(NpbCollectedGame, game_id) is not None:
                skipped_existing += 1
                continue

            away, home = g["away"], g["home"]
            visitor_scored = bool(g["visitor_scored_1st"])
            home_scored = bool(g["home_scored_1st"])

            _apply_batting(db, away, visitor_scored)
            _apply_batting(db, home, home_scored)

            has_starters = "home_starter_id" in g and "away_starter_id" in g
            if has_starters:
                _apply_pitching(db, g["home_starter_id"], g.get("home_starter_name"), home, visitor_scored)
                _apply_pitching(db, g["away_starter_id"], g.get("away_starter_name"), away, home_scored)
            else:
                skipped_no_starters += 1

            db.add(NpbCollectedGame(game_id=game_id, date=g.get("date"), home=home, away=away))
            applied += 1
            if applied % 100 == 0:
                db.commit()

        db.commit()
        _league_avg_cache["computed_at"] = None
        summary = {"applied": applied, "skipped_existing": skipped_existing,
                   "skipped_no_starters": skipped_no_starters, "total_in_file": len(games)}
        log.info("npb_yrfi_sync.seed_from_progress complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sync_date(date_str: str, db) -> dict:
    """
    Fetches one real date's NPB schedule, upserts NpbGame rows for
    display (every regular-season game on that date, whatever its
    status), and - for any newly-finished game not already in
    NpbCollectedGame - fetches its final score/starters and folds them
    into the running team-scoring/pitcher-allowing totals. Same
    incremental dedup discipline as nhl_goalie_saves_sync.py.
    """
    games_seen = 0
    games_collected = 0
    try:
        response = get("games_info_by_date", gameDate=date_str)
    except requests.exceptions.RequestException:
        log.exception("Failed to fetch NPB schedule for %s", date_str)
        return {"games_seen": games_seen, "games_collected": games_collected}

    game_list = response if isinstance(response, list) else response.get("games", response)

    for g in game_list:
        game_type = g.get("gameTypeName", "") or ""
        if REGULAR_SEASON_MARKER not in game_type:
            continue
        game_id = g.get("gameId")
        if game_id is None:
            continue
        game_id = str(game_id)

        home_team, away_team = g.get("homeTeamName"), g.get("visitorTeamName")
        home_team_id, away_team_id = g.get("homeTeamId"), g.get("visitorTeamId")
        status = g.get("gameStateName")

        row = db.get(NpbGame, game_id)
        if row is None:
            row = NpbGame(game_id=game_id, date=date_str)
            db.add(row)
            db.flush()  # same autoflush=False reasoning as _apply_batting/_apply_pitching above
        row.date = date_str
        row.home_team = home_team
        row.away_team = away_team
        row.home_team_id = str(home_team_id) if home_team_id is not None else None
        row.away_team_id = str(away_team_id) if away_team_id is not None else None
        row.game_type = game_type
        row.status = status
        row.start_time_jst = g.get("startTime")
        games_seen += 1

        if status != FINISHED_STATE or db.get(NpbCollectedGame, game_id) is not None:
            continue  # not finished yet, or already collected - nothing more to do

        try:
            entry = get_boxscore(game_id)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch NPB score for game %s", game_id)
            continue
        if entry is None:
            continue
        first_inning = _parse_first_inning(entry)
        row.away_score = _safe_int(entry.get("totalRunVisitor"))
        row.home_score = _safe_int(entry.get("totalRunHome"))
        if first_inning is None:
            continue

        try:
            starters = get_starters(game_id)
        except requests.exceptions.RequestException:
            log.exception("Failed to fetch NPB pitcher stats for game %s", game_id)
            continue
        if starters is None:
            continue
        home_starter = starters.get(str(home_team_id))
        away_starter = starters.get(str(away_team_id))
        if not home_starter or not away_starter:
            continue

        _apply_batting(db, away_team, first_inning["visitor_scored_1st"])
        _apply_batting(db, home_team, first_inning["home_scored_1st"])
        _apply_pitching(db, home_starter["id"], home_starter["name"], home_team, first_inning["visitor_scored_1st"])
        _apply_pitching(db, away_starter["id"], away_starter["name"], away_team, first_inning["home_scored_1st"])
        db.add(NpbCollectedGame(game_id=game_id, date=date_str, home=home_team, away=away_team))
        games_collected += 1

        row.home_starter_id = str(home_starter["id"])
        row.home_starter_name = home_starter["name"]
        row.away_starter_id = str(away_starter["id"])
        row.away_starter_name = away_starter["name"]
        row.visitor_scored_1st = first_inning["visitor_scored_1st"]
        row.home_scored_1st = first_inning["home_scored_1st"]

    return {"games_seen": games_seen, "games_collected": games_collected}


def daily_update(days_back: int = 3, days_ahead: int = 1) -> dict:
    """
    Incremental daily update - sweeps a small real-date window (JST)
    around today: `days_back` days of catch-up (in case a game finished
    late or a prior sweep missed it) through `days_ahead` days ahead (to
    populate upcoming real matchups for display). Cheap even swept daily,
    since already-collected games are skipped via NpbCollectedGame. Also
    runs automatically once daily (see poller.py).
    """
    anchor = _jst_today()
    dates = [(anchor + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(-days_back, days_ahead + 1)]

    db = SessionLocal()
    try:
        total_seen = 0
        total_collected = 0
        for d in dates:
            result = sync_date(d, db)
            total_seen += result["games_seen"]
            total_collected += result["games_collected"]
            db.commit()

        _league_avg_cache["computed_at"] = None
        summary = {"dates_swept": dates, "games_seen": total_seen, "games_collected": total_collected}
        log.info("npb_yrfi_sync.daily_update complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_league_avg(db) -> float:
    """Cached (6h) hybrid live/static league-average YRFI-half rate."""
    now = datetime.utcnow()
    if _league_avg_cache["value"] is not None and _league_avg_cache["computed_at"] is not None \
            and now - _league_avg_cache["computed_at"] < CACHE_TTL:
        return _league_avg_cache["value"]

    team_rows = db.query(NpbTeamScoreStat).all()
    teams_ready = [r for r in team_rows if r.games >= MIN_PRIOR_GAMES]
    total_pooled = sum(r.games for r in team_rows)
    if len(teams_ready) >= MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE and total_pooled >= MIN_LEAGUE_POOL:
        total_scored = sum(r.scored for r in team_rows)
        value = (total_scored / total_pooled) if total_pooled else LEAGUE_AVG_STATIC
    else:
        value = LEAGUE_AVG_STATIC

    _league_avg_cache["value"] = value
    _league_avg_cache["computed_at"] = now
    return value


def _pick_presumed_starter(team: str, db) -> NpbPitcherAllowStat | None:
    """Heuristic presumed starter - the pitcher on this team with the most
    starts on record so far this season. See module docstring for why
    this is necessarily a guess for an upcoming game."""
    return (
        db.query(NpbPitcherAllowStat)
        .filter_by(team=team)
        .order_by(NpbPitcherAllowStat.starts.desc())
        .first()
    )


def compute_npb_yrfi_prediction(home_team: str, away_team: str, db, league_avg: float | None = None) -> dict | None:
    """Returns {"p_yrfi":, "home_starter_name":, "home_starter_id":,
    "away_starter_name":, "away_starter_id":, "starters_are_heuristic":,
    "home_batting_games":, "away_batting_games":, "home_starter_starts":,
    "away_starter_starts":} or None if either team lacks MIN_PRIOR_GAMES
    of batting history, or either side's presumed starter lacks
    MIN_PRIOR_STARTS of allowing history, yet."""
    home_batting = db.get(NpbTeamScoreStat, home_team)
    away_batting = db.get(NpbTeamScoreStat, away_team)
    if not home_batting or not away_batting or home_batting.games < MIN_PRIOR_GAMES or away_batting.games < MIN_PRIOR_GAMES:
        return None

    home_starter = _pick_presumed_starter(home_team, db)
    away_starter = _pick_presumed_starter(away_team, db)
    if not home_starter or not away_starter or home_starter.starts < MIN_PRIOR_STARTS or away_starter.starts < MIN_PRIOR_STARTS:
        return None

    if league_avg is None:
        league_avg = get_league_avg(db)

    shrunk_away_score = _shrunk_rate(away_batting.scored, away_batting.games, TEAM_SHRINKAGE_K, league_avg)
    shrunk_home_allow = _shrunk_rate(home_starter.allowed, home_starter.starts, PITCHER_SHRINKAGE_K, league_avg)
    p_visitor_scores = _log5(shrunk_away_score, shrunk_home_allow, league_avg)

    shrunk_home_score = _shrunk_rate(home_batting.scored, home_batting.games, TEAM_SHRINKAGE_K, league_avg)
    shrunk_away_allow = _shrunk_rate(away_starter.allowed, away_starter.starts, PITCHER_SHRINKAGE_K, league_avg)
    p_home_scores = _log5(shrunk_home_score, shrunk_away_allow, league_avg)

    p_yrfi = 1 - (1 - p_visitor_scores) * (1 - p_home_scores)

    return {
        "p_yrfi": p_yrfi,
        "home_starter_name": home_starter.name,
        "home_starter_id": home_starter.pitcher_id,
        "away_starter_name": away_starter.name,
        "away_starter_id": away_starter.pitcher_id,
        "starters_are_heuristic": True,
        "home_batting_games": home_batting.games,
        "away_batting_games": away_batting.games,
        "home_starter_starts": home_starter.starts,
        "away_starter_starts": away_starter.starts,
    }
