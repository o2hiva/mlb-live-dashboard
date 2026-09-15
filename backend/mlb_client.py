"""
Thin wrapper around MLB's public Stats API.

No API key required. This is the same free/unofficial-but-widely-used
endpoint set that powers most hobby live-scoreboard projects. Be a good
citizen: don't poll faster than every ~10-15 seconds per live game.

Endpoints used:
  - Schedule:  https://statsapi.mlb.com/api/v1/schedule
  - Live feed: https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live

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
