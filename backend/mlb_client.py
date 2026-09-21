"""
Thin wrapper around MLB's public Stats API.

No API key required. This is the same free/unofficial-but-widely-used
endpoint set that powers most hobby live-scoreboard projects. Be a good
citizen: don't poll faster than every ~10-15 seconds per live game.

Endpoints used:
  - Schedule:  https://statsapi.mlb.com/api/v1/schedule
  - Live feed: https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live
  - Boxscore:  https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore
    (confirmed starting lineups - see extract_boxscore_lineup)
  - Person stats: https://statsapi.mlb.com/api/v1/people/{id}/stats
  - Team stats:   https://statsapi.mlb.com/api/v1/teams/{id}/stats

NOTE: this sandbox's outbound network is restricted to package registries
(pypi/npm/github etc.) and cannot reach statsapi.mlb.com, so these calls
are untested from inside this environment. The endpoint shapes below match
MLB's documented public schema; verify against a live response once you
run this on a machine with normal internet access.
"""
import requests
from datetime import date, timedelta

BASE = "https://statsapi.mlb.com/api"
TIMEOUT = 10


def mlb_today() -> date:
    """
    MLB's schedule/game-day boundary is anchored to US Eastern time, NOT
    whatever timezone this server's clock happens to be set to (Railway
    containers run in UTC). Using naive date.today() here would be wrong
    for a meaningful chunk of each day - e.g. at 1am UTC, it's still
    "yesterday" in Eastern, but date.today() would already say "today".

    This is the exact bug class already hit and fixed in the Excel-era
    scripts (see fill_lineups.py's own mlb_today(), same reasoning) -
    ported here so the live dashboard doesn't repeat it.
    """
    from zoneinfo import ZoneInfo
    import datetime as _datetime
    return _datetime.datetime.now(ZoneInfo("America/New_York")).date()


def mlb_yesterday() -> date:
    return mlb_today() - timedelta(days=1)


def get_schedule(game_date: str | None = None) -> list[dict]:
    """Return today's (or a given date's) MLB games with basic status info."""
    game_date = game_date or mlb_today().isoformat()
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
                # MLB's detailedState has dozens of possible verbose values
                # (challenges, reviews, delays, etc.) - abstractGameState is
                # always exactly one of "Preview"/"Live"/"Final", regardless
                # of what's happening mid-game. Using this as the
                # authoritative "is this game actually over" signal avoids
                # having to enumerate every possible transient status string
                # (confirmed a real gap: "Player challenge: Pitch Result"
                # was being treated as "not finished" by bet grading, even
                # for a game that had already ended).
                "abstract_status": g.get("status", {}).get("abstractGameState", "Preview"),
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


def get_boxscore(game_pk: int) -> dict:
    """
    Dedicated boxscore endpoint - verified working shape/endpoint,
    ported from a previously-working script (fill_lineups.py's own
    get_confirmed_lineup()) rather than guessed at.
    """
    url = f"{BASE}/v1/game/{game_pk}/boxscore"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def extract_boxscore_lineup(boxscore: dict, side: str) -> tuple:
    """
    Confirmed starting lineup for one side ('home'/'away'), ported
    verbatim from fill_lineups.py's get_confirmed_lineup() - MLB marks
    each player's spot with a "battingOrder" STRING like "100", "200",
    ..., "900" (first digit = batting order 1-9; trailing "00" means
    the ORIGINAL starter in that spot, as opposed to a mid-game
    substitute who'd show "101", "102", etc.). Confirmed working
    against real live data in that script - this is not a guess.

    Returns (confirmed: bool, batters: list[dict]) where each batter is
    {"id":, "name":, "batting_order":} (1-indexed, sorted). confirmed
    is False with an empty list if MLB hasn't posted the lineup yet
    (no player in this boxscore has an "00" batting order yet).
    """
    team_box = boxscore.get("teams", {}).get(side, {})
    players = team_box.get("players", {})

    starters = []
    for pdata in players.values():
        order = pdata.get("battingOrder")
        if order and str(order).endswith("00"):
            person = pdata.get("person", {})
            starters.append((int(order), person.get("id"), person.get("fullName")))

    if not starters:
        return False, []

    starters.sort(key=lambda x: x[0])
    batters = [
        {"id": pid, "name": name, "batting_order": order // 100}
        for order, pid, name in starters
        if pid and name
    ]
    return (True, batters) if batters else (False, [])


def get_season_hitting_totals(person_id: int, season: int) -> dict | None:
    """Real season-to-date at-bats/hits/HR/walks/strikeouts/PA for one
    batter. Same endpoint and field mapping as fetch_raw_batting_stats.py's
    get_season_hitting_totals(). strikeouts/pa were added for the
    lineup-specific Pitcher K formula - same API response already had
    them, no new endpoint needed. Returns None if this player has no
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
    return {
        "ab": ab, "hits": hits,
        "hr": stat.get("homeRuns", 0) or 0,
        "bb": stat.get("baseOnBalls", 0) or 0,
        "strikeouts": stat.get("strikeOuts", 0) or 0,
        "pa": stat.get("plateAppearances", 0) or 0,
    }


def get_season_pitching_totals(person_id: int, season: int) -> dict | None:
    """Real season-to-date outs recorded/hits-HR-walks-runs-allowed for
    one pitcher. Same endpoint and field mapping as
    fetch_raw_batting_stats.py's get_season_pitching_totals(). Also
    includes strikeouts/battersFaced/gamesStarted for the Pitcher K
    prop - same endpoint already returns these, no extra call needed."""
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
    return {
        "outs": outs, "hits_allowed": hits_allowed,
        "hr_allowed": stat.get("homeRuns", 0) or 0,
        "bb_allowed": stat.get("baseOnBalls", 0) or 0,
        "runs_allowed": stat.get("runs", 0) or 0,
        "strikeouts": stat.get("strikeOuts", 0) or 0,
        "batters_faced": stat.get("battersFaced", 0) or 0,
        "games_started": stat.get("gamesStarted", 0) or 0,
    }


def get_team_season_k_stats(team_id: int, season: int) -> dict | None:
    """
    This team's own real season-to-date strikeouts AND plate
    appearances AS HITTERS (their own batters' K-proneness) - needed
    for the Pitcher K prop's "opposing team" factor, which is about how
    often that team's LINEUP strikes out, not anything pitching-related.
    Same team/hitting endpoint already used elsewhere - just two more
    fields off the same response.
    """
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
    strikeouts = stat.get("strikeOuts")
    plate_appearances = stat.get("plateAppearances")
    if strikeouts is None or plate_appearances is None:
        return None
    return {"strikeouts": strikeouts, "plate_appearances": plate_appearances}


def get_pitch_hand(person_id: int) -> str | None:
    """
    This pitcher's throwing hand ('L' or 'R'), straight from their
    player record - unlike everything else in this file, this never
    changes for a given player, so it's safe to cache indefinitely
    rather than on the usual 24h cycle. Ported from
    fetch_pitcher_hands.py's get_pitch_hand().
    """
    resp = requests.get(f"{BASE}/v1/people/{person_id}", timeout=TIMEOUT)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    if not people:
        return None
    return people[0].get("pitchHand", {}).get("code")


def get_bat_side(person_id: int) -> str | None:
    """
    This batter's batting side ('L', 'R', or 'S' for switch-hitter),
    straight from their player record - same endpoint as
    get_pitch_hand, just a different field. Never changes for a given
    player, safe to cache indefinitely.
    """
    resp = requests.get(f"{BASE}/v1/people/{person_id}", timeout=TIMEOUT)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    if not people:
        return None
    return people[0].get("batSide", {}).get("code")


def get_batter_platoon_split(person_id: int, season: int, sit_code: str) -> dict | None:
    """
    One split ('vl' or 'vr') of a batter's real season hitting stats -
    ported from fetch_batter_platoon_splits.py's get_platoon_split().
    NOTE: this endpoint is explicitly flagged by that script's own
    author as untested against live data ("the most speculative part").
    Returns the raw stat dict (atBats, hits, homeRuns, etc.) or None if
    no splits exist for this situation code.
    """
    resp = requests.get(
        f"{BASE}/v1/people/{person_id}/stats",
        params={"stats": "statSplits", "sitCodes": sit_code, "group": "hitting", "season": season},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    splits = resp.json().get("stats", [{}])[0].get("splits", [])
    if not splits:
        return None
    return splits[0].get("stat", {})


ALL_TEAM_IDS = [
    108, 109, 110, 111, 112, 113, 114, 115, 116, 117,
    118, 119, 120, 121, 133, 134, 135, 136, 137, 138,
    139, 140, 141, 142, 143, 144, 145, 146, 147, 158,
]


def get_team_season_hitting_totals(team_id: int, season: int) -> dict | None:
    """This team's own real season-to-date at-bats/hits/HR/walks - used
    to compute real live league-average rates (hit rate, HR rate, walk
    rate, on-base rate) by summing across all 30 teams, rather than
    relying on a static/manually-set constant."""
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
    return {
        "ab": ab, "hits": hits,
        "hr": stat.get("homeRuns", 0) or 0,
        "bb": stat.get("baseOnBalls", 0) or 0,
    }


def get_team_season_pitching_totals(team_id: int, season: int) -> dict | None:
    """This team's own real season-to-date runs-allowed/outs-recorded -
    used to compute a live league-average runs-allowed rate (per
    batter faced) for the HRR model's pitcher "Runs allowed" factor."""
    resp = requests.get(
        f"{BASE}/v1/teams/{team_id}/stats",
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
    runs_allowed = stat.get("runs")
    if runs_allowed is None:
        return None
    return {"outs": outs, "runs_allowed": runs_allowed}
