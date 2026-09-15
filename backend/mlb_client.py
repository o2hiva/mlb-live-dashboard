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
    params = {"sportId": 1, "date": game_date, "hydrate": "probablePitcher"}
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            teams = g.get("teams", {})
            games.append({
                "game_pk": g["gamePk"],
                "game_date": game_date,
                "status": g.get("status", {}).get("detailedState", "Unknown"),
                "home_team": teams.get("home", {}).get("team", {}).get("name"),
                "away_team": teams.get("away", {}).get("team", {}).get("name"),
                "home_probable_pitcher": teams.get("home", {}).get("probablePitcher", {}).get("fullName"),
                "away_probable_pitcher": teams.get("away", {}).get("probablePitcher", {}).get("fullName"),
            })
    return games


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
