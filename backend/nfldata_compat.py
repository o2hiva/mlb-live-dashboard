"""
Drop-in replacement for the api.nfldata.org calls the NFL syncs used (GET /v1/games, /v1/stats/season, /v1/stats/team).

api.nfldata.org began answering Railway's server with a Cloudflare "Just a moment..." bot check (HTTP 403) instead of data, so every NFL refresh
failed. The same numbers are published as open data by nflverse (the source nfldata itself is built from), so this module serves the same three
endpoints, with the same field names and the same team spellings, from:
    schedules/games.csv                        -> /games     (one row per game: teams, scores, gameday, gametime)
    stats_player/stats_player_week_{season}.csv -> /stats/season (summed per player) and /stats/team (summed per team)
Use:  nfldata_compat.get(url, params=..., timeout=...)  exactly like requests.get; the result has .status_code, .raise_for_status(), .json().
Network / HTTP failures raise requests exceptions, so the callers' existing `except requests.exceptions.RequestException` handling still works.
Files are cached in memory for 20 minutes.
"""
import csv
import io
import threading
import time

import requests

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
URL_GAMES = BASE + "/schedules/games.csv"
URL_WEEK = BASE + "/stats_player/stats_player_week_{season}.csv"
CACHE_SECONDS = 20 * 60
HEADERS = {"User-Agent": "sports-hub-dashboard"}

# same spellings the old API's schedule used (WSH, JAX, LA, LV, LAC) - see nfl_passing_yards_sync.TEAM_CANON
TEAM_CANON = {"WAS": "WSH", "JAC": "JAX", "LAR": "LA", "OAK": "LV", "SD": "LAC", "STL": "LA"}

_cache = {}
_lock = threading.Lock()


def canon(team):
    return TEAM_CANON.get(team, team) if team else team


def _download(url, timeout):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _rows(url, timeout=60):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    rows = list(csv.DictReader(io.StringIO(_download(url, timeout))))
    with _lock:
        _cache[url] = (now, rows)
    return rows


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _int_or_none(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _games(params, timeout):
    season, week = str(params.get("season")), params.get("week")
    out = []
    for g in _rows(URL_GAMES, timeout):
        if g.get("season") != season or (week is not None and g.get("week") != str(week)):
            continue
        out.append({
            "game_id": g.get("game_id"), "season": _int_or_none(g.get("season")), "week": _int_or_none(g.get("week")),
            "game_type": g.get("game_type"), "gameday": g.get("gameday") or None, "gametime": g.get("gametime") or None,
            "home_team": canon(g.get("home_team")), "away_team": canon(g.get("away_team")),
            "home_score": _int_or_none(g.get("home_score")), "away_score": _int_or_none(g.get("away_score")),
        })
    return {"data": out, "total": len(out)}


def _weekly(season, timeout):
    # regular season only, like the old API's season totals
    return [r for r in _rows(URL_WEEK.format(season=season), timeout) if (r.get("season_type") or "REG") == "REG"]


_SUM_FIELDS = ("completions", "attempts", "passing_yards", "passing_tds", "carries", "rushing_yards", "rushing_tds",
               "receptions", "targets", "receiving_yards", "receiving_tds")


def _stats_season(params, timeout):
    players = {}
    for r in _weekly(params.get("season"), timeout):
        pid = r.get("player_id")
        if not pid:
            continue
        wk = _int_or_none(r.get("week")) or 0
        p = players.get(pid)
        if p is None:
            p = players[pid] = {"player_id": pid, "player_name": r.get("player_name"), "player_display_name": r.get("player_display_name"),
                                "position": r.get("position"), "recent_team": None, "games": 0, "_last": -1,
                                **{f: 0.0 for f in _SUM_FIELDS}}
        p["games"] += 1
        for f in _SUM_FIELDS:
            p[f] += _num(r.get(f))
        if wk >= p["_last"]:
            p["_last"], p["recent_team"], p["position"] = wk, canon(r.get("team") or r.get("recent_team")), r.get("position") or p["position"]
    rows = []
    for pid in sorted(players):
        p = players[pid]
        p.pop("_last", None)
        for f in _SUM_FIELDS:
            p[f] = int(round(p[f]))
        rows.append(p)
    limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
    return {"data": rows[offset:offset + limit], "total": len(rows)}


def _stats_team(params, timeout):
    teams = {}
    for r in _weekly(params.get("season"), timeout):
        t = canon(r.get("team") or r.get("recent_team"))
        if not t:
            continue
        e = teams.setdefault(t, {"team": t, "weeks": set(), "attempts": 0.0, "passing_yards": 0.0})
        e["weeks"].add(r.get("week"))
        e["attempts"] += _num(r.get("attempts"))
        e["passing_yards"] += _num(r.get("passing_yards"))
    rows = [{"team": t, "games": len(e["weeks"]), "attempts": int(round(e["attempts"])), "passing_yards": int(round(e["passing_yards"]))}
            for t, e in sorted(teams.items())]
    return {"data": rows, "total": len(rows)}


def get(url, params=None, timeout=30, **_ignored):
    params = params or {}
    path = url.split("api.nfldata.org/v1", 1)[-1].split("?", 1)[0].rstrip("/")
    if path == "/games":
        return _Resp(_games(params, timeout))
    if path == "/stats/season":
        return _Resp(_stats_season(params, timeout))
    if path == "/stats/team":
        return _Resp(_stats_team(params, timeout))
    raise requests.exceptions.InvalidURL(f"nfldata_compat does not serve {path!r}")
