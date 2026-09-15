"""
Thin wrapper around MLB's public Stats API.

No API key required. This is the same free/unofficial-but-widely-used
endpoint set that powers most hobby live-scoreboard projects. Be a good
citizen: don't poll faster than every ~10-15 seconds per live game.

Endpoints used:
  - Schedule:  https://statsapi.mlb.com/api/v1/schedule
  - Live feed: https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live
    (also carries the boxscore, including confirmed starting lineups)
  - Person stats: https://statsapi.mlb.com/api/v1/people/{id}/stats
  - Team stats:   https://statsapi.mlb.com/api/v1/teams/{id}/stats

NOTE: this sandbox's outbound network is restricted to package registries
(pypi/npm/github etc.) and cannot reach statsapi.mlb.com, so these calls
are untested from inside this environment. The endpoint shapes below match
MLB's documented public schema; verify against a live response once you
run this on a machine with normal internet access.
"""
import requests
from datetime import date

BASE = "https://statsapi.mlb.com/api"
TIMEOUT = 10


def get_schedule(game_date: str | None = None) -> list[dict]:
    """Return today's (or a given date's) MLB games with basic status info."""
    game_date = game_date or date.today().isoformat()
    url = f"{BASE}/v1/schedule"
    params = {"sportId": 1, "date": game_date, "hydrate": "probablePitcher,team"}
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            games.append({
                "game_pk": g["gamePk"],
                "game_date": game_date,
                "game_datetime_utc": g.get("gameDate"),  # ISO 8601 UTC, e.g. "2026-09-15T02:10:00Z"
                "status": g.get("status", {}).get("detailedState", "Unknown"),
                "venue_id": g.get("venue", {}).get("id"),
                "venue_name": g.get("venue", {}).get("name"),
                "home_team": home.get("team", {}).get("name"),
                "away_team": away.get("team", {}).get("name"),
                "home_team_id": home.get("team", {}).get("id"),
                "away_team_id": away.get("team", {}).get("id"),
                "home_probable_pitcher": home.get("probablePitcher", {}).get("fullName"),
                "away_probable_pitcher": away.get("probablePitcher", {}).get("fullName"),
                "home_probable_pitcher_id": home.get("probablePitcher", {}).get("id"),
                "away_probable_pitcher_id": away.get("probablePitcher", {}).get("id"),
            })
    return games


def get_final_games_with_linescore(date_str: str) -> list[dict]:
    """
    All FINAL games on a given date, with per-inning linescore and
    probable pitchers - same field mapping as
    fetch_inning_scoring_stats.py's get_days_games_with_linescore(),
    confirmed working there against real results.
    """
    url = f"{BASE}/v1/schedule"
    params = {"sportId": 1, "date": date_str, "hydrate": "linescore,team,probablePitcher"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    games = []
    for date_block in resp.json().get("dates", []):
        for game in date_block.get("games", []):
            if game.get("status", {}).get("abstractGameState") == "Final":
                games.append(game)
    return games


def get_inning_runs_from_raw_game(game: dict, inning_num: int) -> tuple:
    """Runs scored by (away, home) in a specific inning, from a raw game
    dict as returned by get_final_games_with_linescore(). Returns
    (None, None) if that inning wasn't played (e.g. home team didn't
    need to bat in the 9th)."""
    innings = game.get("linescore", {}).get("innings", [])
    for inn in innings:
        if inn.get("num") == inning_num:
            return inn.get("away", {}).get("runs"), inn.get("home", {}).get("runs")
    return None, None


def get_game_starting_pitcher(game: dict, side: str) -> tuple:
    """(pitcher_id, pitcher_name) for the probable/starting pitcher on
    one side ('home' or 'away') of a raw game dict."""
    team_data = game.get("teams", {}).get(side, {})
    pitcher = team_data.get("probablePitcher")
    if not pitcher:
        return None, None
    return pitcher.get("id"), pitcher.get("fullName", "")


def get_live_feed(game_pk: int) -> dict:
    """Return the full live-feed payload for a game (linescore, boxscore, plays)."""
    url = f"{BASE}/v1.1/game/{game_pk}/feed/live"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def extract_linescore(feed: dict) -> dict:
    """Pull the bits we care about out of a live-feed payload."""
    live_data = feed.get("liveData", {})
    linescore = live_data.get("linescore", {})
    innings = linescore.get("innings", [])

    inning_lines = []
    for inn in innings:
        num = inn.get("num")
        if "home" in inn and "runs" in inn["home"]:
            inning_lines.append({"inning": num, "half": "bottom", "runs": inn["home"]["runs"]})
        if "away" in inn and "runs" in inn["away"]:
            inning_lines.append({"inning": num, "half": "top", "runs": inn["away"]["runs"]})

    return {
        "status": feed.get("gameData", {}).get("status", {}).get("detailedState", "Unknown"),
        "inning": linescore.get("currentInning", 0),
        "inning_half": linescore.get("inningHalf"),
        "home_score": linescore.get("teams", {}).get("home", {}).get("runs", 0),
        "away_score": linescore.get("teams", {}).get("away", {}).get("runs", 0),
        "inning_lines": inning_lines,
    }


def extract_boxscore_lineup(feed: dict, side: str) -> tuple:
    """
    Confirmed starting lineup for one side ('home'/'away') from a
    live-feed payload's boxscore, in real batting order. MLB posts this
    (liveData.boxscore.teams.{side}.battingOrder - a list of person IDs
    in batting order) once the lineup is official, usually shortly
    before first pitch - empty/absent before that.

    Returns (confirmed: bool, batters: list[dict]) where each batter is
    {"id":, "name":, "batting_order":} (1-indexed). confirmed is False
    with an empty list if MLB hasn't posted the lineup yet.

    NOTE: unverified against a live response from this sandbox (no
    internet access here) - the battingOrder/players field shape below
    matches MLB's documented public schema and the pattern used by
    several open-source MLB stats tools, but confirm once running
    somewhere with real internet access.
    """
    boxscore = feed.get("liveData", {}).get("boxscore", {})
    team_box = boxscore.get("teams", {}).get(side, {})
    batting_order_ids = team_box.get("battingOrder", [])
    if not batting_order_ids:
        return False, []

    players = team_box.get("players", {})
    batters = []
    for order_idx, pid in enumerate(batting_order_ids, start=1):
        person = players.get(f"ID{pid}", {}).get("person", {})
        name = person.get("fullName")
        if name:
            batters.append({"id": pid, "name": name, "batting_order": order_idx})
    return True, batters


def get_season_hitting_totals(person_id: int, season: int) -> dict | None:
    """Real season-to-date at-bats/hits for one batter. Same endpoint
    and field mapping as fetch_raw_batting_stats.py's
    get_season_hitting_totals(). Returns None if this player has no
    hitting stats this season (e.g. a pure pitcher)."""
    resp = requests.get(
        f"{BASE}/v1/people/{person_id}/stats",
        params={"stats": "season", "season": season, "group": "hitting"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    stats_list = resp.json().get("stats") or []
    if not stats_list:
        return None
    splits = stats_list[0].get("splits") or []
    if not splits:
        return None
    stat = splits[0].get("stat", {})
    ab = stat.get("atBats")
    hits = stat.get("hits")
    if ab is None or hits is None:
        return None
    return {"ab": ab, "hits": hits}


def get_season_pitching_totals(person_id: int, season: int) -> dict | None:
    """Real season-to-date outs recorded/hits-allowed for one pitcher.
    Same endpoint and field mapping as fetch_raw_batting_stats.py's
    get_season_pitching_totals()."""
    resp = requests.get(
        f"{BASE}/v1/people/{person_id}/stats",
        params={"stats": "season", "season": season, "group": "pitching"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    stats_list = resp.json().get("stats") or []
    if not stats_list:
        return None
    splits = stats_list[0].get("splits") or []
    if not splits:
        return None
    stat = splits[0].get("stat", {})
    outs = stat.get("outs")
    if outs is None:
        ip = stat.get("inningsPitched", "0.0")
        whole, _, frac = str(ip).partition(".")
        outs = int(whole or 0) * 3 + int(frac or 0)
    hits_allowed = stat.get("hits")
    if hits_allowed is None:
        return None
    return {"outs": outs, "hits_allowed": hits_allowed}


ALL_TEAM_IDS = [
    108, 109, 110, 111, 112, 113, 114, 115, 116, 117,
    118, 119, 120, 121, 133, 134, 135, 136, 137, 138,
    139, 140, 141, 142, 143, 144, 145, 146, 147, 158,
]


def get_team_season_hitting_totals(team_id: int, season: int) -> dict | None:
    """This team's own real season-to-date at-bats/hits - used to
    compute a real live league-average hit rate by summing across all
    30 teams, rather than relying on a static/manually-set constant."""
    resp = requests.get(
        f"{BASE}/v1/teams/{team_id}/stats",
        params={"stats": "season", "season": season, "group": "hitting"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    stats_list = resp.json().get("stats") or []
    if not stats_list:
        return None
    splits = stats_list[0].get("splits") or []
    if not splits:
        return None
    stat = splits[0].get("stat", {})
    ab = stat.get("atBats")
    hits = stat.get("hits")
    if ab is None or hits is None:
        return None
    return {"ab": ab, "hits": hits}
