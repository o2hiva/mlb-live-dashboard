"""
nhl_goalie_saves_sync.py

NHL "Goalie Saves" prop - the live-dashboard port of
core_nhl_goalie_saves.py, wired to real data collected from NHL's public
api-web.nhle.com API (no key needed - same source as
nhl_backtest_team_shots.py).

FORMULA (verbatim from core_nhl_goalie_saves.py, validated against the
full real 2023-24 NHL season - 2,143 real single-goalie
goalie-appearances):
    predicted_shots_faced = team_index * opponent_index * league_avg_shots
    predicted_saves       = predicted_shots_faced * shrunk_goalie_save_pct
    team_index     = shrunk(team's own shots-ALLOWED history) / league_avg_shots
    opponent_index = shrunk(opponent's own shots-GENERATED history) / league_avg_shots
    shrinkage_k (team shots, unit=games)   = 8.0
    save_pct_k  (goalie save%, unit=shots) = 300.0
    distribution: negbinom, overdispersion = 1.4

SINGLE-GOALIE-GAME GATING (see core_nhl_goalie_saves.py's docstring for
the full story): predicted_shots_faced is a TEAM-level number - it only
equals a specific goalie's actual shots faced when that goalie played the
ENTIRE game. A goalie's own history only accumulates from games where
they were the ONLY goalie who actually played (toi_seconds > 0, filtering
out 0-TOI dressed-but-unused backups) AND faced >= MIN_SHOTS_AGAINST_OWN
shots. Team-level shots-for/shots-against totals, by contrast, accumulate
from EVERY game regardless of goalie situation.

INCREMENTAL / RESUMABLE COLLECTION - THE KEY DIFFERENCE FROM EVERY OTHER
SYNC MODULE IN THIS PROJECT: NFL (~18 weeks) and CFB (~15 weeks) are
small enough to fully rebuild from scratch on every refresh. An NHL
season is ~1,312 games across 82 games x 32 teams - refetching everything
every time would be far too expensive. Instead, NhlCollectedGame (see
models_db.py) is a dedup ledger: daily_update() only fetches games NOT
yet recorded there, and incrementally adds each newly-finished game's
numbers into the running NhlTeamShotsStat/NhlGoalieSavesStat totals -
never deleting or rebuilding those two tables. This directly ports
nhl_backtest_team_shots.py's own resumable JSON-progress-file design,
just onto Postgres tables instead of a local file (same "run it daily,
it only processes what's new" discipline that script's own docstring
describes for live in-season use).

NO CONFIRMED STARTING GOALIE FOR A FUTURE GAME: NHL's schedule API only
ever exposes FINISHED-game boxscores, never a probable/confirmed starter
for an upcoming game. Same "heuristic" pattern already used elsewhere in
this dashboard (NFL QB/RB DvP's highest-volume-on-record starter guess):
each team's presumed starter is whichever of its goalies has the most
games on record so far this season - see _pick_starter() below. Every
prediction this module returns is flagged starter_is_heuristic=True so
the frontend can label it clearly rather than presenting it as confirmed.

DATA SOURCE: https://api-web.nhle.com/v1 (no key needed):
    /club-schedule-season/{team}/{season} -> that team's full schedule
    /gamecenter/{game_id}/boxscore         -> team SOG + goalie lines
Season format: NHL's own 8-digit season code, start-year+end-year
concatenated (2026-27 season = 20262027) - see current_nhl_season().
"""
import logging
from datetime import datetime, timedelta

import requests

from database import SessionLocal
from models_db import NhlCollectedGame, NhlTeamShotsStat, NhlGoalieSavesStat, NhlGoalieGame

log = logging.getLogger("nhl_goalie_saves_sync")

API_BASE = "https://api-web.nhle.com/v1"
TIMEOUT = 30

# 32 teams as of the 2025-26 season - same list/relocation note as
# nhl_backtest_team_shots.py. A wrong/stale code here just wastes one
# schedule request; every game is still discovered and every real
# home/away abbreviation stored comes straight from the real boxscore.
NHL_TEAMS = [
    "ANA", "UTA", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ", "DAL",
    "DET", "EDM", "FLA", "LAK", "MIN", "MTL", "NSH", "NJD", "NYI", "NYR",
    "OTT", "PHI", "PIT", "SJS", "SEA", "STL", "TBL", "TOR", "VAN", "VGK",
    "WSH", "WPG",
]

REGULAR_SEASON_GAME_TYPE = 2
MIN_SHOTS_AGAINST_OWN = 10     # a goalie's own history only counts a "real appearance" at this shots-faced floor
MIN_PRIOR_GAMES = 3            # min games required on BOTH the team's own shots-allowed side and the opponent's shots-generated side
MIN_PRIOR_SHOTS_GOALIE = 90    # min prior shots faced before trusting a goalie's own save% history
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 16    # half the NHL's 32 teams
MIN_QUALIFYING_SHOTS_FOR_LIVE_BASELINE = 5000  # pooled shots across all goalies

DEFAULT_SHRINKAGE_K = 8.0
DEFAULT_SAVE_PCT_K = 300.0
DEFAULT_OVERDISPERSION = 1.4

# Real 2023-24 validated values from core_nhl_goalie_saves.py.
LEAGUE_AVG_SHOTS_STATIC = 30.3074
LEAGUE_AVG_SAVE_PCT_STATIC = 0.90843

CACHE_TTL = timedelta(hours=6)
_league_avg_cache = {"shots": None, "save_pct": None, "computed_at": None}


def current_nhl_season() -> str:
    """NHL's own 8-digit season code for 'right now'. A season that
    starts in the fall of year Y runs into the spring of Y+1, so from
    July of year Y onward (off-season / preseason ramp-up through the
    regular season) counts as that season - matches the reasoning
    nfl_points_sync/cfb_points_sync leave to an explicit season-year
    input, just computed here since NHL's season code is two concatenated
    years rather than one, so there's no single obvious "current" value
    to just pass through."""
    today = datetime.utcnow().date()
    start_year = today.year if today.month >= 7 else today.year - 1
    return f"{start_year}{start_year + 1}"


def get_team_schedule(team: str, season: str) -> dict:
    resp = requests.get(f"{API_BASE}/club-schedule-season/{team}/{season}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_boxscore(game_id: int) -> dict:
    resp = requests.get(f"{API_BASE}/gamecenter/{game_id}/boxscore", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _toi_to_seconds(toi: str) -> int:
    """'MM:SS' -> total seconds. 0 on anything unparseable, same
    "don't crash the collector over one malformed field" reasoning as
    nhl_backtest_team_shots.py's identical helper."""
    try:
        minutes, seconds = toi.split(":")
        return int(minutes) * 60 + int(seconds)
    except (ValueError, AttributeError):
        return 0


def _extract_goalies(side_stats: dict) -> list:
    out = []
    for g in side_stats.get("goalies", []):
        out.append({
            "player_id": g.get("playerId"),
            "name": g.get("name", {}).get("default", "Unknown"),
            "shots_against": g.get("shotsAgainst", 0),
            "saves": g.get("saves", 0),
            "toi_seconds": _toi_to_seconds(g.get("toi", "0:00")),
        })
    return out


def _goalies_who_played(side_goalies: list) -> list:
    """A real boxscore lists every DRESSED goalie, including 0-TOI
    backups who never played - filter to who actually took the ice
    before checking whether exactly one goalie played the whole game."""
    return [g for g in side_goalies if g.get("toi_seconds", 0) > 0]


def _shrunk_avg(sum_val: float, count: int, k: float, league_avg: float) -> float:
    return (sum_val + k * league_avg) / (count + k)


def _shrunk_rate(successes_sum: float, trials_sum: float, k: float, league_avg_rate: float) -> float:
    return (successes_sum + k * league_avg_rate) / (trials_sum + k)


def get_league_avgs(db) -> tuple[float, float]:
    """Cached (6h) hybrid live/static (league_avg_shots, league_avg_save_pct) -
    same pattern as nfl_points_sync.get_league_avg_points, just two values
    computed together since both feed the same prediction."""
    now = datetime.utcnow()
    if _league_avg_cache["shots"] is not None and _league_avg_cache["computed_at"] is not None \
            and now - _league_avg_cache["computed_at"] < CACHE_TTL:
        return _league_avg_cache["shots"], _league_avg_cache["save_pct"]

    team_rows = db.query(NhlTeamShotsStat).all()
    qualifying_teams = [r for r in team_rows if r.games >= MIN_PRIOR_GAMES]
    if len(qualifying_teams) >= MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE:
        total_shots = sum(r.shots_for_sum for r in qualifying_teams)
        total_games = sum(r.games for r in qualifying_teams)
        avg_shots = (total_shots / total_games) if total_games else LEAGUE_AVG_SHOTS_STATIC
    else:
        avg_shots = LEAGUE_AVG_SHOTS_STATIC

    goalie_rows = db.query(NhlGoalieSavesStat).all()
    total_pooled_shots = sum(r.shots_against_sum for r in goalie_rows)
    if total_pooled_shots >= MIN_QUALIFYING_SHOTS_FOR_LIVE_BASELINE:
        total_saves = sum(r.saves_sum for r in goalie_rows)
        avg_save_pct = (total_saves / total_pooled_shots) if total_pooled_shots else LEAGUE_AVG_SAVE_PCT_STATIC
    else:
        avg_save_pct = LEAGUE_AVG_SAVE_PCT_STATIC

    _league_avg_cache["shots"] = avg_shots
    _league_avg_cache["save_pct"] = avg_save_pct
    _league_avg_cache["computed_at"] = now
    return avg_shots, avg_save_pct


def _apply_game_to_stats(db, team: str, opponent: str, own_sog: int, opp_sog: int, side_goalies: list, season: str) -> None:
    """Incrementally folds one finished game into `team`'s running
    NhlTeamShotsStat (unconditionally - team-level shots are well-defined
    regardless of goalie situation) and, when gating passes, into the one
    goalie who played the whole game's NhlGoalieSavesStat. own_sog is
    this team's own shots-on-goal for the game; opp_sog is the
    opponent's (= shots this team's goalie(s) actually faced)."""
    row = db.get(NhlTeamShotsStat, team)
    if row is None:
        row = NhlTeamShotsStat(team=team, season=season, games=0, shots_for_sum=0, shots_against_sum=0)
        db.add(row)
        # The session is autoflush=False (see database.py), so without this
        # flush a second _apply_game_to_stats() call for the SAME team
        # later in this same uncommitted batch (any team playing more than
        # one game between commits, which happens routinely) would not see
        # this pending row via db.get() and would add a second one -
        # colliding on the primary key at commit time
        # (psycopg2.errors.UniqueViolation). Flushing sends the pending
        # INSERT to the DB now (not a commit), making it visible to
        # subsequent db.get() calls within this transaction. This is the
        # same bug class that broke npb_yrfi_sync.py's seed import.
        db.flush()
    row.season = season
    row.games += 1
    row.shots_for_sum += own_sog
    row.shots_against_sum += opp_sog

    played = _goalies_who_played(side_goalies)
    if len(played) == 1 and played[0]["shots_against"] >= MIN_SHOTS_AGAINST_OWN:
        g = played[0]
        pid = g["player_id"]
        if pid is not None:
            grow = db.get(NhlGoalieSavesStat, pid)
            if grow is None:
                grow = NhlGoalieSavesStat(player_id=pid, name=g["name"], team=team, season=season,
                                           games=0, saves_sum=0, shots_against_sum=0)
                db.add(grow)
                db.flush()  # same autoflush=False reasoning as the NhlTeamShotsStat block above
            grow.name = g["name"]
            grow.team = team  # keep the goalie's most-recently-seen team current
            grow.season = season
            grow.games += 1
            grow.saves_sum += g["saves"]
            grow.shots_against_sum += g["shots_against"]


def daily_update(season: str, target_date: str | None = None) -> dict:
    """
    Incremental daily update - the NHL equivalent of
    nfl_points_sync.refresh_nfl_points_stats, but resumable/incremental
    instead of a full rebuild (see module docstring for why). In one pass
    over every team's schedule, this:
      1. Discovers any REGULAR-SEASON games that are finished
         (gameState OFF/FINAL) and NOT already in NhlCollectedGame,
         fetches each one's real boxscore once, and folds it into the
         running NhlTeamShotsStat/NhlGoalieSavesStat totals.
      2. Discovers target_date's (default: tomorrow, UTC) real matchups
         and refreshes NhlGoalieGame with them.
    Safe to call any number of times - already-collected games are
    skipped, so a same-day repeat call is cheap (mostly no-ops). Also
    runs automatically once daily (see poller.py), which additionally
    fires once immediately on every app startup/redeploy so the very
    first deploy already has tomorrow's game list ready.
    """
    if target_date is None:
        target_date = (datetime.utcnow().date() + timedelta(days=1)).isoformat()

    db = SessionLocal()
    try:
        teams_scanned = 0
        games_collected = 0
        target_matchups: dict[str, dict] = {}

        for team in NHL_TEAMS:
            try:
                schedule = get_team_schedule(team, season)
            except requests.exceptions.RequestException:
                log.exception("Failed to fetch NHL schedule for %s", team)
                continue
            teams_scanned += 1

            for game in schedule.get("games", []):
                if game.get("gameType") != REGULAR_SEASON_GAME_TYPE:
                    continue
                game_id = game.get("id")
                game_date = game.get("gameDate")
                home_abbrev = game.get("homeTeam", {}).get("abbrev")
                away_abbrev = game.get("awayTeam", {}).get("abbrev")

                if game_date == target_date and home_abbrev and away_abbrev:
                    if team == home_abbrev:
                        target_matchups[team] = {"opponent": away_abbrev, "is_home": True, "game_id": game_id, "date": game_date}
                    elif team == away_abbrev:
                        target_matchups[team] = {"opponent": home_abbrev, "is_home": False, "game_id": game_id, "date": game_date}

                if game.get("gameState") not in ("OFF", "FINAL"):
                    continue  # not finished yet - nothing to collect
                if game_id is None or db.get(NhlCollectedGame, game_id) is not None:
                    continue  # already collected (or seen earlier in this same pass - see re-check below)

                try:
                    box = get_boxscore(game_id)
                except requests.exceptions.RequestException:
                    log.exception("Failed to fetch NHL boxscore for game %s", game_id)
                    continue

                home_sog = box.get("homeTeam", {}).get("sog")
                away_sog = box.get("awayTeam", {}).get("sog")
                if home_sog is None or away_sog is None:
                    continue
                player_stats = box.get("playerByGameStats", {})
                home_goalies = _extract_goalies(player_stats.get("homeTeam", {}))
                away_goalies = _extract_goalies(player_stats.get("awayTeam", {}))

                # Re-check right before writing - this exact game_id also
                # appears in the OTHER participant's schedule, which may
                # already have applied it earlier in this same loop. The
                # session is autoflush=False (see database.py), so db.get()
                # does NOT see a just-added, not-yet-committed row on its
                # own - it only catches that case here because
                # _apply_game_to_stats() below (and every db.add() in this
                # module) explicitly db.flush()es right after adding, which
                # is what actually makes the pending NhlCollectedGame add a
                # few lines down visible to this same check next time
                # around. Without those flushes this re-check would still
                # miss it and hit psycopg2.errors.UniqueViolation on
                # NhlCollectedGame's primary key, the same bug class that
                # broke npb_yrfi_sync.py's seed import.
                if db.get(NhlCollectedGame, game_id) is not None:
                    continue

                _apply_game_to_stats(db, home_abbrev, away_abbrev, home_sog, away_sog, home_goalies, season)
                _apply_game_to_stats(db, away_abbrev, home_abbrev, away_sog, home_sog, away_goalies, season)
                db.add(NhlCollectedGame(game_id=game_id, season=season, date=game_date,
                                         home=home_abbrev, away=away_abbrev,
                                         home_sog=home_sog, away_sog=away_sog))
                db.flush()  # makes this NhlCollectedGame row visible to the re-check above next time this game_id is seen
                games_collected += 1
                if games_collected % 10 == 0:
                    db.commit()  # periodic commit - resumable if a long first backfill run is interrupted

        db.commit()

        db.query(NhlGoalieGame).delete()
        for team, info in target_matchups.items():
            db.add(NhlGoalieGame(team=team, opponent=info["opponent"], is_home=info["is_home"],
                                  game_id=info["game_id"], date=info["date"], season=season))
        db.commit()

        _league_avg_cache["computed_at"] = None  # force recompute next read, using the fresh totals just written
        summary = {"teams_scanned": teams_scanned, "games_collected": games_collected,
                   "target_date": target_date, "target_matchups": len(target_matchups)}
        log.info("nhl_goalie_saves_sync.daily_update complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _pick_starter(team: str, db) -> NhlGoalieSavesStat | None:
    """Heuristic 'presumed starter' - the goalie on this team with the
    most games on record so far this season. See module docstring for
    why this is necessarily a guess (NHL's API never exposes a
    confirmed/probable starter for a future game)."""
    return (
        db.query(NhlGoalieSavesStat)
        .filter_by(team=team)
        .order_by(NhlGoalieSavesStat.games.desc())
        .first()
    )


def compute_goalie_saves_prediction(team: str, opponent: str, db, league_avgs: tuple | None = None) -> dict | None:
    """Returns {"mean":, "goalie_name":, "goalie_player_id":,
    "starter_is_heuristic":, "team_index":, "opp_index":,
    "team_games_sample":, "opp_games_sample":, "goalie_shots_sample":}
    for `team`'s presumed starting goalie against `opponent`, or None if
    either team lacks MIN_PRIOR_GAMES yet or the presumed starter lacks
    MIN_PRIOR_SHOTS_GOALIE yet. Mirrors
    nfl_points_sync.compute_nfl_team_points_prediction's shape."""
    team_row = db.get(NhlTeamShotsStat, team)
    opp_row = db.get(NhlTeamShotsStat, opponent)
    if not team_row or not opp_row or team_row.games < MIN_PRIOR_GAMES or opp_row.games < MIN_PRIOR_GAMES:
        return None

    goalie_row = _pick_starter(team, db)
    if not goalie_row or goalie_row.shots_against_sum < MIN_PRIOR_SHOTS_GOALIE:
        return None

    if league_avgs is None:
        league_avgs = get_league_avgs(db)
    league_avg_shots, league_avg_save_pct = league_avgs
    if league_avg_shots <= 0 or league_avg_save_pct <= 0:
        return None

    shrunk_team_against = _shrunk_avg(team_row.shots_against_sum, team_row.games, DEFAULT_SHRINKAGE_K, league_avg_shots)
    shrunk_opp_for = _shrunk_avg(opp_row.shots_for_sum, opp_row.games, DEFAULT_SHRINKAGE_K, league_avg_shots)
    team_index = shrunk_team_against / league_avg_shots
    opp_index = shrunk_opp_for / league_avg_shots
    predicted_shots_faced = team_index * opp_index * league_avg_shots

    shrunk_save_pct = _shrunk_rate(goalie_row.saves_sum, goalie_row.shots_against_sum, DEFAULT_SAVE_PCT_K, league_avg_save_pct)
    mean = predicted_shots_faced * shrunk_save_pct

    return {
        "mean": mean,
        "goalie_name": goalie_row.name,
        "goalie_player_id": goalie_row.player_id,
        "starter_is_heuristic": True,
        "team_index": team_index,
        "opp_index": opp_index,
        "team_games_sample": team_row.games,
        "opp_games_sample": opp_row.games,
        "goalie_shots_sample": goalie_row.shots_against_sum,
    }
