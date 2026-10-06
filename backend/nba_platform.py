#!/usr/bin/env python
"""
nba_platform.py  --  ONE FILE for everything NBA.

Contains (embedded, unchanged and individually tested):
  nba_io, nba_collect_games, nba_collect_boxscores            data collection (ESPN) + safe file helpers
  backtest_nba_win_pct / _spread_probability / _lineup_adjustment   model-building and backtests
  core_nba_moneyline        team ratings -> win probability
  core_nba_spread           margin / spread-cover probability with lineup (injury) adjustment   [SHADOW MODE]
  backtest_nba_player_props, core_nba_player_props          PTS / REB / AST / PRA player props  [SHADOW MODE]
  nba_track_lineup_results, nba_track_prop_results          scorecards for the two shadow logs
  nba_service               website-facing game layer
plus the platform layer below: ONE class, NBAPlatform, with the read/write paths a website needs.

STATUS: no part has been tested against real sportsbook prices (no historical NBA odds / prop lines exist in
our data). Out-of-sample calibration is validated; market edge is not. The logs + scorecards answer that.

------------------------------------------------------------------ WIRING (library)
    from nba_platform import NBAPlatform
    nba = NBAPlatform("/data/nba")                      # folder holding the nba_*.json files
    nba.refresh_data()                                  # WRITE path, schedule every ~30 min (lock-protected, never raises)
    nba.get_slate("20261022")                           # games: win prob, margin, spread cover, lineup adjustments
    nba.get_props("20261022")                           # players: PTS/REB/AST/PRA projection, fair line, P(over/under)
    nba.get_scorecard(2027); nba.get_props_scorecard(2027); nba.health()
  Every read call is cheap, thread-safe, never raises, returns JSON-safe dicts with a "status" field
  ("ok" | "degraded" | "stale" | "error") and a "warnings" list. Slow work happens only in refresh_data().

------------------------------------------------------------------ WIRING (HTTP, stdlib only)
    python nba_platform.py serve --port 8051 --refresh-minutes 30 [--cors] [--dir PATH]
      GET /api/nba/slate?date=YYYYMMDD            GET /api/nba/props?date=YYYYMMDD
      GET /api/nba/scorecard?season=2027          GET /api/nba/props/scorecard?season=2027
      GET /health

------------------------------------------------------------------ COMMAND LINE (run from the NBA folder)
    python nba_platform.py daily [--date YYYYMMDD] [--no-refresh]   refresh data, print games + props, log both
    python nba_platform.py slate | props [--date ..] [--only NAME]  one of the two
    python nba_platform.py refresh                                  one data refresh (for a scheduler)
    python nba_platform.py track --season 2027                      both scorecards
    python nba_platform.py run <module> [args...]                   any embedded tool, e.g.
        run nba_collect_games --seasons 2027      run nba_collect_boxscores --seasons 2027 --upgrade
        run backtest_nba_player_props --seasons 2022,2023,2024,2025,2026 --tune-seasons 2023,2024 --validate-seasons 2025,2026 --out nba_prop_params.json
    python nba_platform.py unpack OUTDIR                            write the embedded modules out as .py files

FILES (all in --dir): nba_games_*.json, nba_boxscores_*.json (schema 2), nba_prop_params.json (from the props backtest),
  nba_player_values.json (auto), nba_lines.json (optional game spreads {"Away @ Home": home_spread}),
  nba_prop_lines.json (optional {"Player Name|pts": 27.5, "Player Name|pra": {"line": 41.5, "over": -115, "under": -105}}),
  nba_prediction_log.jsonl and nba_prop_log.jsonl (shadow-mode logs).
Needs: python 3.9+, numpy, scipy, requests.
"""

import argparse
import datetime
import importlib.abc
import importlib.util
import json
import math
import os
import sys
import threading
import time

# ===================================================================== embedded modules
_ORDER = ['nba_io', 'nba_collect_games', 'nba_collect_boxscores', 'backtest_nba_win_pct', 'backtest_nba_spread_probability', 'backtest_nba_lineup_adjustment', 'core_nba_moneyline', 'core_nba_spread', 'backtest_nba_player_props', 'core_nba_player_props', 'nba_track_lineup_results', 'nba_track_prop_results', 'nba_service']
_SRC = {}
# ======================================================================
# embedded module: nba_io.py (96 lines)
# ======================================================================
_SRC["nba_io"] = r'''"""
nba_io.py -- small file helpers so the NBA data files are safe to read while a
background job is writing them (a self-reloading website reads constantly).

  atomic_json_dump(path, obj)   write to a temp file in the same folder, then os.replace()
                                (readers see the old file or the new one, never half a file)
  load_json_retry(path, default)  read JSON, retrying briefly if it is mid-replace / unreadable
  FileLock(path, timeout, stale_after)  cross-process mutex via an exclusive lock file
                                (stale locks from a crashed job are broken after stale_after seconds)
"""

import json
import os
import tempfile
import time


def atomic_json_dump(path, obj):
    folder = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        for attempt in range(15):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:      # Windows: a reader has the file open for a moment
                if attempt == 14:
                    raise
                time.sleep(0.2)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json_retry(path, default=None, attempts=5, delay=0.2):
    if not os.path.exists(path):
        return default
    last = None
    for _ in range(attempts):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, PermissionError, OSError) as e:  # Windows can briefly lock during replace
            last = e
            time.sleep(delay)
    raise last


class LockBusy(Exception):
    pass


class FileLock:
    def __init__(self, path, timeout=0.0, stale_after=900.0):
        self.path, self.timeout, self.stale_after = path, timeout, stale_after
        self._fd = None

    def acquire(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale_after:
                        os.unlink(self.path)   # crashed holder
                        continue
                except OSError:
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def __enter__(self):
        if not self.acquire():
            raise LockBusy(self.path)
        return self

    def __exit__(self, *a):
        self.release()
'''
# ======================================================================
# embedded module: nba_collect_games.py (234 lines)
# ======================================================================
_SRC["nba_collect_games"] = r'''"""
nba_collect_games.py

Pure ETL for NBA game results: walks ESPN's public (keyless, unofficial)
scoreboard endpoint day by day and caches every game to a per-season JSON
progress file, resumable. The backtest (backtest_nba_win_pct.py) reads these
files; nothing here does any modeling.

SEASON NAMING: a season is identified by the calendar year it ENDS in
(2025 = the 2024-25 season, 2026 = 2025-26).

DATA SOURCE SHAPE -- NOT YET CONFIRMED AGAINST REAL DATA. This sandbox has no
route to ESPN, so the field names below are written from the endpoint's
known general shape, NOT verified. Same rule as every other data source in
this project: run --diagnose on one real day FIRST and check that the printed
values look right (real team names, plausible scores, home/away correct, a
season type for regular-season games) before collecting anything:

    python nba_collect_games.py --diagnose --date 20250115

Assumed shape (verify with --diagnose):
    GET .../basketball/nba/scoreboard?dates=YYYYMMDD
    events[]: id, date, season{year,type,slug}, status{type{completed}},
              competitions[0]{neutralSite, competitors[]}
    competitors[]: homeAway ("home"/"away"), score (string), team{id,abbreviation,displayName}

If --diagnose shows a different shape, tell me what it printed and I'll fix
parse_event() -- do not hand-edit around it.

TEAM KEY: ESPN's numeric team id (stable across relocations/abbreviation
changes, unlike abbreviations), stored as a string. Display name is kept
alongside for readability only.

SEASON TYPE is stored raw (season_type / season_slug) and filtered in the
backtest, not here, so a wrong assumption about which value means "regular
season" is fixable without re-collecting. NOTE for the backtest: whether
NBA Cup games and the play-in tournament carry the same season type as
ordinary regular-season games is also something to confirm from real data.

Usage:
    python nba_collect_games.py --diagnose --date 20250115
    python nba_collect_games.py --season 2025
    python nba_collect_games.py --season 2021 --season 2022 ... (repeat, or use --seasons 2021,2022,2023,2024,2025,2026)

Output: nba_games_{season}.json  ({"completed_days": [...], "games": {game_id: {...}}})
Needs internet access -- run locally.
"""

import argparse
import datetime
import json
import os
import time

import requests

API_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# Default collection windows, by season end-year. Wide enough to cover the
# whole regular season incl. the late-season stretch; days with no games just
# return an empty list. 2021 was the COVID-shortened 72-game season that
# started late.
DEFAULT_WINDOWS = {
    2021: ("2020-12-20", "2021-05-25"),
}
DEFAULT_WINDOW_MONTH_DAY = (("10", "15"), ("04", "25"))  # (start, end) month/day for a normal season


def progress_file_for(season):
    return f"nba_games_{season}.json"


def season_window(season):
    if season in DEFAULT_WINDOWS:
        return DEFAULT_WINDOWS[season]
    (sm, sd), (em, ed) = DEFAULT_WINDOW_MONTH_DAY
    return f"{season - 1}-{sm}-{sd}", f"{season}-{em}-{ed}"


def daterange(start_iso, end_iso):
    d = datetime.date.fromisoformat(start_iso)
    end = datetime.date.fromisoformat(end_iso)
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def get_day(date_obj, retries=3):
    """Raw ESPN scoreboard JSON for one day."""
    params = {"dates": date_obj.strftime("%Y%m%d"), "limit": 200}
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(API_URL, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def _to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def parse_event(ev):
    """One scoreboard event -> a flat game dict, or None if it can't be
    parsed as a two-team game. Returns completed=False games too (the
    caller decides what to keep)."""
    comps = ev.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if home is None or away is None:
        return None
    status_type = ((ev.get("status") or comp.get("status") or {}).get("type")) or {}
    season = ev.get("season") or {}
    h_team, a_team = home.get("team") or {}, away.get("team") or {}
    return {
        "game_id": str(ev.get("id")),
        "date": ev.get("date"),
        "completed": bool(status_type.get("completed")),
        "season_type": season.get("type"),
        "season_slug": season.get("slug"),
        "neutral_site": bool(comp.get("neutralSite", False)),
        "home_id": str(h_team.get("id")), "home_name": h_team.get("displayName"),
        "away_id": str(a_team.get("id")), "away_name": a_team.get("displayName"),
        "home_score": _to_int(home.get("score")), "away_score": _to_int(away.get("score")),
    }


def diagnose(date_str):
    d = datetime.datetime.strptime(date_str, "%Y%m%d").date()
    data = get_day(d)
    print(f"\nTop-level keys: {sorted(data.keys())}")
    events = data.get("events") or []
    print(f"{len(events)} event(s) on {d}\n")
    for ev in events:
        print(f"event keys: {sorted(ev.keys())}")
        break
    for ev in events:
        g = parse_event(ev)
        if g is None:
            print(f"  [UNPARSEABLE] id={ev.get('id')}")
            continue
        print(f"  id={g['game_id']} completed={g['completed']} season_type={g['season_type']!r} "
              f"slug={g['season_slug']!r} neutral={g['neutral_site']}\n"
              f"      {g['away_name']} ({g['away_id']}) {g['away_score']}  @  "
              f"{g['home_name']} ({g['home_id']}) {g['home_score']}   date={g['date']}")
    print("\nMANUAL CHECK before collecting: (1) home/away are the right way round (the home team is the "
          "one named AFTER the '@'), (2) scores look like real final NBA scores, (3) season_type is the "
          "same non-null value for ordinary regular-season games, (4) completed=True for finished games. "
          "Paste this output back if anything looks off.")


def load_progress(path):
    from nba_io import load_json_retry
    return load_json_retry(path, {"completed_days": [], "games": {}})


def save_progress(progress, path):
    from nba_io import atomic_json_dump
    atomic_json_dump(path, progress)


def collect_season(season, sleep=0.15, directory=".", quiet=False):
    say = (lambda *a, **k: None) if quiet else print
    path = os.path.join(directory, progress_file_for(season))
    progress = load_progress(path)
    done = set(progress["completed_days"])
    start, end = season_window(season)
    today = datetime.date.today()
    days = [d for d in daterange(start, end) if d.isoformat() not in done]
    say(f"{season}: {len(done)} day(s) already collected, {len(days)} to fetch ({start} .. {end}).")

    added_total = 0
    for i, d in enumerate(days):
        if d > today:
            break  # future dates: nothing to collect yet
        try:
            data = get_day(d)
        except requests.exceptions.RequestException as e:
            say(f"  {d}: error, will retry next run: {e}")
            continue
        for ev in data.get("events") or []:
            g = parse_event(ev)
            if g is None or not g["completed"] or g["home_score"] is None or g["away_score"] is None:
                continue
            progress["games"][g["game_id"]] = g
            added_total += 1
        if d < today:  # today may still have games in progress -- re-fetch next run
            progress["completed_days"].append(d.isoformat())
        if (i + 1) % 30 == 0:
            save_progress(progress, path)
            say(f"  ...through {d}: {len(progress['games'])} game(s) so far")
        time.sleep(sleep)
    save_progress(progress, path)
    say(f"{season}: done, {len(progress['games'])} completed game(s) cached ({added_total} written this run) -> {path}")


def main():
    parser = argparse.ArgumentParser(description="Collect NBA game results from ESPN's scoreboard")
    parser.add_argument("--diagnose", action="store_true", help="Print raw/parsed games for --date and exit.")
    parser.add_argument("--date", type=str, default=None, help="YYYYMMDD, used with --diagnose.")
    parser.add_argument("--season", type=int, action="append", default=[], help="Season end-year (repeatable).")
    parser.add_argument("--seasons", type=str, default=None, help="Comma-separated season end-years.")
    args = parser.parse_args()

    if args.diagnose:
        if not args.date:
            print("--diagnose requires --date YYYYMMDD")
            return
        diagnose(args.date)
        return

    seasons = list(args.season)
    if args.seasons:
        seasons += [int(s) for s in args.seasons.split(",") if s.strip()]
    if not seasons:
        print("Give --season YEAR (repeatable) or --seasons 2021,2022,...")
        return
    for s in sorted(set(seasons)):
        collect_season(s)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: nba_collect_boxscores.py (223 lines)
# ======================================================================
_SRC["nba_collect_boxscores"] = r'''"""
nba_collect_boxscores.py

Pure ETL: for every regular-season game already cached by nba_collect_games.py
(nba_games_{season}.json), fetches ESPN's game summary and stores a compact
per-player record, resumable, to nba_boxscores_{season}.json. This is the data
the lineup/availability adjustment will be built from. No modeling here.

CONFIRMED against a real game (2025-01-15 Knicks @ 76ers, via
nba_diagnose_sources.py --game): summary -> boxscore.players[] has one block
per team (block['team']['id'], block['statistics'][0]) with:
    labels  = ['MIN','PTS','FG','3PT','FT','REB','AST','TO','STL','BLK','OREB','DREB','PF','+/-']
    athletes[] rows: athlete{id,displayName,position{abbreviation}}, starter (bool),
                     didNotPlay (bool), reason (text, e.g. "COACH'S DECISION" or an
                     injury like "SPRAINED LEFT TOE"), active (bool -- NOT reliable
                     as "played"; a starter who played 23 minutes showed active=False,
                     so it is stored but never used), stats[] aligned to labels.
Stats are parsed BY LABEL (never by position), so a reordered label list can't
silently shift minutes into another column.
Players who did not dress (e.g. a star who is out) do NOT appear in the
boxscore at all -- absences have to be inferred from who normally plays.

The game summary also carries a top-level 'injuries' list. Its structure was
too long to read in the diagnostic and it is NOT yet known whether it is a
pre-game snapshot or today's feed, so it is stored as-is (minus link/logo/
image noise) under 'injuries_raw' and interpreted later, not here.

Usage:
    python nba_collect_boxscores.py --seasons 2022,2023,2024,2025,2026
(needs nba_games_{season}.json from nba_collect_games.py in the same folder;
~1,230 requests per season, resumable -- re-run after any interruption)

Needs internet access -- run locally.
"""

import argparse
import json
import os
import time

import requests

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
NOISE_KEYS = {"links", "logo", "logos", "headshot", "uid", "guid", "images"}
REGULAR_SEASON_TYPE = 2


def boxscore_file_for(season):
    return f"nba_boxscores_{season}.json"


def games_file_for(season):
    return f"nba_games_{season}.json"


def strip_noise(obj):
    """Recursively drop link/logo/image keys so the stored injuries block stays small."""
    if isinstance(obj, dict):
        return {k: strip_noise(v) for k, v in obj.items() if k not in NOISE_KEYS}
    if isinstance(obj, list):
        return [strip_noise(v) for v in obj]
    return obj


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    try:
        return int(str(x).replace("+", ""))
    except (TypeError, ValueError):
        return None


SCHEMA = 2   # v2 adds the full counting stats (reb/ast/...); v1 stored only min/pts/pm


def _made_att(x):
    """'5-12' -> (5, 12); anything else -> (None, None)."""
    try:
        m, a = str(x).split("-")
        return int(m), int(a)
    except (TypeError, ValueError):
        return None, None


def parse_player_row(row, labels):
    """One boxscore athlete row -> compact dict. Parsed by label."""
    ath = row.get("athlete") or {}
    stats = row.get("stats") or []
    idx = {lab: i for i, lab in enumerate(labels)}

    def stat(label):
        i = idx.get(label)
        return stats[i] if i is not None and i < len(stats) else None

    minutes = _num(stat("MIN"))
    fgm, fga = _made_att(stat("FG"))
    tpm, tpa = _made_att(stat("3PT"))
    ftm, fta = _made_att(stat("FT"))
    return {
        "id": str(ath.get("id")),
        "name": ath.get("displayName"),
        "pos": (ath.get("position") or {}).get("abbreviation"),
        "starter": bool(row.get("starter")),
        "dnp": bool(row.get("didNotPlay")),
        "reason": row.get("reason") if row.get("didNotPlay") else None,
        "min": minutes,
        "pts": _int(stat("PTS")),
        "pm": _int(stat("+/-")),
        "reb": _int(stat("REB")), "ast": _int(stat("AST")), "tov": _int(stat("TO")),
        "stl": _int(stat("STL")), "blk": _int(stat("BLK")), "oreb": _int(stat("OREB")), "dreb": _int(stat("DREB")),
        "pf": _int(stat("PF")), "fgm": fgm, "fga": fga, "tpm": tpm, "tpa": tpa, "ftm": ftm, "fta": fta,
    }


def parse_summary(data, home_id, away_id):
    """summary JSON -> {'home': [players], 'away': [players], 'injuries_raw': ...}, or None
    if the boxscore has no usable player blocks."""
    out = {"home": None, "away": None}
    for block in (data.get("boxscore") or {}).get("players") or []:
        team_id = str((block.get("team") or {}).get("id"))
        sbs = block.get("statistics") or []
        if not sbs:
            continue
        sb = sbs[0]
        labels = sb.get("labels") or []
        players = [parse_player_row(r, labels) for r in (sb.get("athletes") or [])]
        if team_id == str(home_id):
            out["home"] = players
        elif team_id == str(away_id):
            out["away"] = players
    if out["home"] is None or out["away"] is None:
        return None
    out["injuries_raw"] = strip_noise(data.get("injuries"))
    out["v"] = SCHEMA
    return out


def get_summary(event_id, retries=3):
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(SUMMARY_URL, params={"event": event_id}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def load_json(path, default):
    from nba_io import load_json_retry
    return load_json_retry(path, default)


def _save(path, obj):
    from nba_io import atomic_json_dump
    atomic_json_dump(path, obj)


def collect_season(season, directory=".", sleep=0.1, fetch=get_summary, save_every=50, quiet=False, upgrade=False):
    say = (lambda *a, **k: None) if quiet else print
    games_path = os.path.join(directory, games_file_for(season))
    if not os.path.exists(games_path):
        say(f"[SKIP {season}] {games_file_for(season)} not found -- run nba_collect_games.py --season {season} first.")
        return
    games = load_json(games_path, {"games": {}})["games"]
    wanted = {gid: g for gid, g in games.items()
              if g.get("season_type") == REGULAR_SEASON_TYPE and g.get("completed")}
    out_path = os.path.join(directory, boxscore_file_for(season))
    store = load_json(out_path, {"games": {}, "failed": {}})
    old_v = sum(1 for r in store["games"].values() if r.get("v", 1) < SCHEMA)
    todo = [gid for gid in sorted(wanted, key=lambda x: wanted[x]["date"])
            if gid not in store["games"] or (upgrade and store["games"][gid].get("v", 1) < SCHEMA)]
    say(f"{season}: {len(store['games'])} boxscore(s) cached, {len(todo)} to fetch"
        + (f" ({old_v} old-format game(s) lack rebounds/assists; add --upgrade to re-fetch them)" if old_v and not upgrade else "") + ".")

    added = 0
    for i, gid in enumerate(todo):
        g = wanted[gid]
        try:
            data = fetch(gid)
        except requests.exceptions.RequestException as e:
            say(f"  {gid}: error, will retry next run: {e}")
            continue
        parsed = parse_summary(data, g["home_id"], g["away_id"])
        if parsed is None:
            store["failed"][gid] = "no usable boxscore players"
            continue
        parsed["date"] = g["date"]
        prev = store["games"].get(gid)
        if prev is not None and "injuries_raw" in prev:
            parsed["injuries_raw"] = prev["injuries_raw"]      # an upgrade never rewrites what the lineup work already uses
        store["games"][gid] = parsed
        store["failed"].pop(gid, None)
        added += 1
        if (i + 1) % save_every == 0:
            _save(out_path, store)
            say(f"  ...{i + 1}/{len(todo)} ({len(store['games'])} cached)")
        time.sleep(sleep)
    _save(out_path, store)
    say(f"{season}: done, {len(store['games'])} boxscore(s) cached ({added} added, {len(store['failed'])} failed) -> {out_path}")


def main():
    p = argparse.ArgumentParser(description="Collect per-player NBA boxscores from ESPN game summaries")
    p.add_argument("--seasons", required=True, help="Comma-separated season end-years")
    p.add_argument("--dir", default=".")
    p.add_argument("--upgrade", action="store_true",
                   help="re-fetch games cached in the old format so they also carry rebounds/assists/etc. (needed for player props)")
    args = p.parse_args()
    for s in [int(x) for x in args.seasons.split(",") if x.strip()]:
        collect_season(s, args.dir, upgrade=args.upgrade)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: backtest_nba_win_pct.py (385 lines)
# ======================================================================
_SRC["backtest_nba_win_pct"] = r'''"""
backtest_nba_win_pct.py

Walk-forward backtest of PROJECTED FINAL SEASON WIN% for every NBA team.
At a series of checkpoints through a season (preseason, after ~10 games per
team, ~20, ...), project each team's final win% using only games played so
far, then compare to the real final win%.

MODEL (same shrinkage idea as the rest of this project, applied to margin):
  1. Team strength = average point margin per game (MOV), adjusted for home
     court, shrunk toward a PRIOR:
         prior_i  = rho * (last season's final adjusted MOV for team i)
         rating_i = (sum of adjusted margins so far + k * prior_i) / (games + k)
     rho (<1) is how much of last year's strength carries over; k is the
     prior's weight in games. Both are tuned on earlier seasons only.
  2. For every REMAINING game, P(win) = Phi((rating_team - rating_opp +/- home_edge) / sigma),
     using the real remaining schedule (the schedule is known in advance, so
     using it in a backtest is not leakage).
  3. Projected final wins = wins so far + sum of P(win) over remaining games.

WHY MARGIN, NOT W-L: point margin is a much less noisy measure of strength
than wins and losses, so it is expected to out-predict a record-only
estimate -- but that is a claim this backtest TESTS (the record-only
baseline below gets its own tuned prior), it is not assumed.

BASELINES (each scored on the same teams/checkpoints, each tuned with the
same discipline where it has parameters):
    record_blend : (wins + k_b * regressed last-season win%) / (games + k_b),
                   rolled forward over the remaining games. Own (rho_b, k_b) tuned.
    last_season  : last season's final win%, unchanged.
    carry        : current win% carried forward (0.500 before any games).
    half         : everyone .500.

NOT MODELED YET (named, not forgotten): strength-of-schedule adjustment on
PAST margins (early margins depend on who was played), roster changes /
injuries / minutes, rest and back-to-backs, in-season trades. The preseason
(n=0) projection is purely last-year carryover, so it cannot see offseason
roster moves -- expect that checkpoint to be the weakest.

SEASON NAMING: end-year (2025 = 2024-25). Input files: nba_games_{season}.json
from nba_collect_games.py. Every target season needs the PREVIOUS season
loaded too (that is its prior).

TUNE/VALIDATE DISCIPLINE: --holdout tunes (rho, k) on --tune-seasons only,
then scores the chosen values on --validate-seasons they never saw. Home
edge is estimated in closed form from the tune seasons (mean home margin),
not swept. Sweeps warn when the best value sits on a grid edge.

Usage:
    python backtest_nba_win_pct.py --report  --seasons 2021,2022,2023,2024,2025,2026 --targets 2022,2023,2024,2025,2026
    python backtest_nba_win_pct.py --holdout --seasons 2021,2022,2023,2024,2025,2026 --tune-seasons 2022,2023,2024 --validate-seasons 2025,2026

Needs: the nba_games_*.json files (collect locally first). No network access.
"""

import argparse
import json
import math
import os

TEAMS_IN_LEAGUE = 30
CHECKPOINTS = [0, 10, 20, 30, 41, 55, 70]          # avg games played per team at the checkpoint
TUNE_CHECKPOINTS = [0, 10, 20, 30, 41, 55]          # checkpoints the sweep objective averages over

DEFAULT_RHO = 0.6
DEFAULT_K = 25.0
DEFAULT_SIGMA = 12.0                                # per-game margin sd (points)
RHO_GRID = [0.3, 0.45, 0.6, 0.75, 0.9]
K_GRID = [8.0, 15.0, 25.0, 40.0, 60.0]
RHO_B_GRID = [0.2, 0.35, 0.5, 0.65, 0.8]
K_B_GRID = [8.0, 15.0, 25.0, 40.0, 60.0]


# ----------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------
def load_season_games(season, allowed_types=(2,), directory="."):
    """Completed games for one season, chronological. Filters to the allowed
    season_type values (default 2 = regular season, UNCONFIRMED until
    nba_collect_games.py --diagnose has been checked on real data)."""
    path = os.path.join(directory, f"nba_games_{season}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        progress = json.load(f)
    games = []
    for g in progress["games"].values():
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        if allowed_types is not None and g.get("season_type") not in allowed_types:
            continue
        games.append(g)
    games.sort(key=lambda g: (g["date"], g["game_id"]))
    return games


# ----------------------------------------------------------------------------
# core pieces
# ----------------------------------------------------------------------------
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_home_edge(games_by_season, seasons):
    """Mean home margin (points) over non-neutral games in `seasons` -- the
    closed-form home-court estimate (no sweep needed)."""
    total, n = 0.0, 0
    for s in seasons:
        for g in games_by_season.get(s, []):
            if g.get("neutral_site"):
                continue
            total += g["home_score"] - g["away_score"]
            n += 1
    return total / n if n else 0.0


def adjusted_margin_stats(games, home_edge):
    """Per team: sum of home-court-adjusted margins, games played, wins."""
    stats = {}
    for g in games:
        margin = g["home_score"] - g["away_score"]
        edge = 0.0 if g.get("neutral_site") else home_edge
        for team, adj, won in ((g["home_id"], margin - edge, margin > 0),
                               (g["away_id"], -margin + edge, margin < 0)):
            s = stats.setdefault(team, {"sum_adj": 0.0, "n": 0, "wins": 0})
            s["sum_adj"] += adj
            s["n"] += 1
            s["wins"] += 1 if won else 0
    return stats


def season_summary(games, home_edge):
    """Final adjusted MOV and win% per team for a full season."""
    stats = adjusted_margin_stats(games, home_edge)
    return {t: {"mov": s["sum_adj"] / s["n"], "win_pct": s["wins"] / s["n"], "n": s["n"]}
            for t, s in stats.items()}


def project_final_wins(games, cut, prior_rating, rho, k, sigma, home_edge):
    """Model projection at checkpoint `cut` (number of games already played
    league-wide). Returns {team: projected final win%}, plus the ratings."""
    played, remaining = games[:cut], games[cut:]
    stats = adjusted_margin_stats(played, home_edge)
    teams = set()
    for g in games:
        teams.add(g["home_id"])
        teams.add(g["away_id"])
    rating = {}
    for t in teams:
        s = stats.get(t, {"sum_adj": 0.0, "n": 0})
        prior = rho * prior_rating.get(t, 0.0)
        rating[t] = (s["sum_adj"] + k * prior) / (s["n"] + k)

    exp_wins = {t: float(stats.get(t, {"wins": 0})["wins"]) for t in teams}
    total_games = {t: float(stats.get(t, {"n": 0})["n"]) for t in teams}
    for g in remaining:
        h, a = g["home_id"], g["away_id"]
        edge = 0.0 if g.get("neutral_site") else home_edge
        p_home = norm_cdf((rating[h] - rating[a] + edge) / sigma)
        exp_wins[h] += p_home
        exp_wins[a] += 1.0 - p_home
        total_games[h] += 1.0
        total_games[a] += 1.0
    return {t: exp_wins[t] / total_games[t] for t in teams if total_games[t] > 0}, rating


def project_record_blend(games, cut, prev_win_pct, rho_b, k_b):
    """Record-only baseline: regress current win% toward a shrunk last-season
    win%, then roll that rate forward over each team's remaining games."""
    played, remaining = games[:cut], games[cut:]
    stats = adjusted_margin_stats(played, 0.0)
    teams = set()
    rem_n = {}
    for g in games:
        teams.add(g["home_id"])
        teams.add(g["away_id"])
    for g in remaining:
        rem_n[g["home_id"]] = rem_n.get(g["home_id"], 0) + 1
        rem_n[g["away_id"]] = rem_n.get(g["away_id"], 0) + 1
    out = {}
    for t in teams:
        s = stats.get(t, {"wins": 0, "n": 0})
        prior = 0.5 + rho_b * (prev_win_pct.get(t, 0.5) - 0.5)
        rate = (s["wins"] + k_b * prior) / (s["n"] + k_b)
        r = rem_n.get(t, 0)
        total = s["n"] + r
        out[t] = (s["wins"] + rate * r) / total if total else 0.5
    return out


def project_simple_baselines(games, cut, prev_win_pct):
    played = games[:cut]
    stats = adjusted_margin_stats(played, 0.0)
    teams = {g["home_id"] for g in games} | {g["away_id"] for g in games}
    last = {t: prev_win_pct.get(t, 0.5) for t in teams}
    carry = {t: (stats[t]["wins"] / stats[t]["n"] if t in stats and stats[t]["n"] else 0.5) for t in teams}
    half = {t: 0.5 for t in teams}
    return {"last_season": last, "carry": carry, "half": half}


# ----------------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------------
METHODS = ["model", "record_blend", "last_season", "carry", "half"]


def evaluate(games_by_season, targets, rho, k, rho_b, k_b, sigma, home_edge, checkpoints):
    """Returns {checkpoint: {method: {"se": sum squared error, "ae": sum
    abs error, "n": team-seasons}}} pooled across every target season."""
    acc = {c: {m: {"se": 0.0, "ae": 0.0, "n": 0} for m in METHODS} for c in checkpoints}
    for season in targets:
        games = games_by_season.get(season)
        prev = games_by_season.get(season - 1)
        if not games or not prev:
            continue
        prev_sum = season_summary(prev, home_edge)
        prior_rating = {t: v["mov"] for t, v in prev_sum.items()}
        prev_wp = {t: v["win_pct"] for t, v in prev_sum.items()}
        truth = {t: v["win_pct"] for t, v in season_summary(games, home_edge).items()}
        for c in checkpoints:
            cut = min(len(games), int(round(c * TEAMS_IN_LEAGUE / 2)))
            preds = {}
            preds["model"], _ = project_final_wins(games, cut, prior_rating, rho, k, sigma, home_edge)
            preds["record_blend"] = project_record_blend(games, cut, prev_wp, rho_b, k_b)
            preds.update(project_simple_baselines(games, cut, prev_wp))
            for m in METHODS:
                for t, true_wp in truth.items():
                    if t not in preds[m]:
                        continue
                    err = preds[m][t] - true_wp
                    a = acc[c][m]
                    a["se"] += err * err
                    a["ae"] += abs(err)
                    a["n"] += 1
    return acc


def rmse(a):
    return math.sqrt(a["se"] / a["n"]) if a["n"] else float("nan")


def mae(a):
    return a["ae"] / a["n"] if a["n"] else float("nan")


def objective(acc, method, checkpoints):
    vals = [rmse(acc[c][method]) for c in checkpoints if acc[c][method]["n"]]
    return sum(vals) / len(vals) if vals else float("inf")


def sweep_model(games_by_season, tune_seasons, sigma, home_edge, checkpoints=TUNE_CHECKPOINTS,
                rho_grid=RHO_GRID, k_grid=K_GRID, verbose=True):
    best = (None, None, float("inf"))
    rows = []
    for rho in rho_grid:
        for k in k_grid:
            acc = evaluate(games_by_season, tune_seasons, rho, k, 0.5, 25.0, sigma, home_edge, checkpoints)
            obj = objective(acc, "model", checkpoints)
            rows.append((rho, k, obj))
            if obj < best[2]:
                best = (rho, k, obj)
    if verbose:
        print(f"\nModel sweep on tune seasons {tune_seasons}: mean RMSE of final win% over checkpoints {checkpoints}")
        print(f"{'rho':>6} {'k':>6} {'mean RMSE':>10}")
        for rho, k, obj in rows:
            mark = "  <-- best" if (rho, k) == (best[0], best[1]) else ""
            print(f"{rho:>6.2f} {k:>6.0f} {obj:>10.5f}{mark}")
        warn_grid_edge("rho", best[0], rho_grid)
        warn_grid_edge("k", best[1], k_grid)
    return best


def sweep_baseline(games_by_season, tune_seasons, sigma, home_edge, checkpoints=TUNE_CHECKPOINTS,
                   rho_grid=RHO_B_GRID, k_grid=K_B_GRID, verbose=True):
    best = (None, None, float("inf"))
    for rho_b in rho_grid:
        for k_b in k_grid:
            acc = evaluate(games_by_season, tune_seasons, DEFAULT_RHO, DEFAULT_K, rho_b, k_b, sigma, home_edge,
                           checkpoints)
            obj = objective(acc, "record_blend", checkpoints)
            if obj < best[2]:
                best = (rho_b, k_b, obj)
    if verbose:
        print(f"\nRecord-only baseline tuned on the same tune seasons: rho_b={best[0]}, k_b={best[1]} "
              f"(mean RMSE {best[2]:.5f})")
        warn_grid_edge("rho_b", best[0], rho_grid)
        warn_grid_edge("k_b", best[1], k_grid)
    return best


def warn_grid_edge(name, value, grid):
    if value in (min(grid), max(grid)):
        print(f"[WARNING] best {name}={value} sits at the EDGE of its sweep grid ({min(grid)}-{max(grid)}) -- "
              f"widen the grid before trusting it.")


def print_results_table(acc, checkpoints, title):
    print(f"\n{'=' * 92}\n{title}\n{'=' * 92}")
    print("RMSE / MAE of projected vs. actual FINAL win% (lower is better); n = team-seasons")
    header = f"{'avg GP':>7} {'n':>5}  " + "  ".join(f"{m:>14}" for m in METHODS)
    print(header)
    print("-" * len(header))
    for c in checkpoints:
        n = acc[c]["model"]["n"]
        cells = "  ".join(f"{rmse(acc[c][m]):>6.4f}/{mae(acc[c][m]):<6.4f}" for m in METHODS)
        print(f"{c:>7} {n:>5}  {cells}")
    print("\nThe model earns its keep only if its column beats record_blend (the strongest baseline) at "
          "most checkpoints -- beating 'half' or 'carry' alone proves nothing.")


def home_edge_report(games_by_season, seasons, home_edge):
    margins = [g["home_score"] - g["away_score"] for s in seasons for g in games_by_season.get(s, [])
               if not g.get("neutral_site")]
    if len(margins) > 1:
        mean = sum(margins) / len(margins)
        sd = math.sqrt(sum((m - mean) ** 2 for m in margins) / (len(margins) - 1))
        print(f"Home-court edge (mean home margin, non-neutral games): {home_edge:+.2f} pts over {len(margins)} games; "
              f"empirical per-game margin sd = {sd:.2f} (compare to --sigma).")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _parse_years(s):
    return [int(x) for x in s.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="NBA projected season win% walk-forward backtest")
    parser.add_argument("--seasons", required=True, help="All season end-years to load, incl. prior-only ones.")
    parser.add_argument("--targets", default=None, help="(--report) season(s) to score.")
    parser.add_argument("--report", action="store_true", help="Single run at --rho/--k over --targets.")
    parser.add_argument("--holdout", action="store_true", help="Tune on --tune-seasons, score on --validate-seasons.")
    parser.add_argument("--tune-seasons", default=None)
    parser.add_argument("--validate-seasons", default=None)
    parser.add_argument("--rho", type=float, default=DEFAULT_RHO)
    parser.add_argument("--k", type=float, default=DEFAULT_K)
    parser.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    parser.add_argument("--season-types", default="2", help="Allowed season_type values, comma-separated "
                        "(default 2 = regular season; confirm with nba_collect_games.py --diagnose).")
    parser.add_argument("--dir", default=".")
    args = parser.parse_args()

    allowed = tuple(_parse_years(args.season_types))
    games_by_season = {}
    for s in _parse_years(args.seasons):
        games = load_season_games(s, allowed, args.dir)
        if games is None:
            print(f"[SKIP {s}] nba_games_{s}.json not found -- run `python nba_collect_games.py --season {s}` first.")
            continue
        games_by_season[s] = games
        print(f"{s}: {len(games)} regular-season game(s) loaded")

    if args.report:
        targets = _parse_years(args.targets) if args.targets else sorted(games_by_season)[1:]
        home_edge = estimate_home_edge(games_by_season, targets)
        home_edge_report(games_by_season, targets, home_edge)
        acc = evaluate(games_by_season, targets, args.rho, args.k, 0.5, 25.0, args.sigma, home_edge, CHECKPOINTS)
        print_results_table(acc, CHECKPOINTS, f"Single run: rho={args.rho}, k={args.k}, sigma={args.sigma} -- seasons {targets}")
        print("\n(record_blend here uses UNTUNED defaults rho_b=0.5, k_b=25 -- use --holdout for a fair baseline.)")
        return

    if args.holdout:
        if not args.tune_seasons or not args.validate_seasons:
            print("--holdout needs --tune-seasons and --validate-seasons.")
            return
        tune, validate = _parse_years(args.tune_seasons), _parse_years(args.validate_seasons)
        home_edge = estimate_home_edge(games_by_season, tune)
        home_edge_report(games_by_season, tune, home_edge)
        rho, k, _ = sweep_model(games_by_season, tune, args.sigma, home_edge)
        rho_b, k_b, _ = sweep_baseline(games_by_season, tune, args.sigma, home_edge)
        acc_tune = evaluate(games_by_season, tune, rho, k, rho_b, k_b, args.sigma, home_edge, CHECKPOINTS)
        print_results_table(acc_tune, CHECKPOINTS, f"TUNE seasons {tune} at rho={rho}, k={k} (in-sample)")
        acc_val = evaluate(games_by_season, validate, rho, k, rho_b, k_b, args.sigma, home_edge, CHECKPOINTS)
        print_results_table(acc_val, CHECKPOINTS, f"VALIDATE seasons {validate} (never seen in tuning) at rho={rho}, k={k}")
        wins = sum(1 for c in CHECKPOINTS if acc_val[c]["model"]["n"] and
                   rmse(acc_val[c]["model"]) < rmse(acc_val[c]["record_blend"]))
        print(f"\nModel beats the tuned record-only baseline at {wins}/{len(CHECKPOINTS)} checkpoints on VALIDATE data.")
        return

    print("Choose --report or --holdout.")


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: backtest_nba_spread_probability.py (194 lines)
# ======================================================================
_SRC["backtest_nba_spread_probability"] = r'''"""
backtest_nba_spread_probability.py

Checks whether the NBA moneyline model's PREDICTIVE DISTRIBUTION of the final
margin is trustworthy -- which is exactly what a spread-cover probability is:
    P(home covers line L) = P(home margin > L) = 1 - Phi((L - mu) / sigma)
    mu = rating_home - rating_away + home_edge   (same model as the moneyline)
Evaluated walk-forward (no leakage) on real results.

WHAT THIS DOES AND DOES NOT TEST. ESPN carries NO historical sportsbook
lines for past games (confirmed with nba_diagnose_sources.py: scoreboard has no
'odds', summary 'odds'/'pickcenter' are empty), so this CANNOT say whether the
model beats the market, and it is NOT the CFB-style market-line validation.
It tests the model's own distribution: is the normal-with-constant-sigma shape
right, and is it right EVERYWHERE -- in particular for lopsided games, where the
CFB spread work found the model overconfident and where a constant sigma is the
most likely thing to break.

Three reports:
  1. z = (actual margin - mu) / sigma. If the distribution is right, z has mean
     ~0 and sd ~1 -- overall, and separately in buckets of |mu| (how lopsided
     the model thinks the game is). A rising sd with |mu| means big-favorite
     games are noisier than a constant sigma assumes (CFB's lopsided problem).
  2. Cover calibration at synthetic lines mu + offset, offset in {-9,-6,-3,+3,+6,+9}.
     OFFSET 0 IS DELIBERATELY EXCLUDED: a line equal to mu gives exactly 50% for
     every game regardless of sigma (a normal CDF at its own mean), so it can
     reveal nothing -- the same trap hit three times in the CFB spread tests.
     Predicted cover probability vs actual cover rate, plus Brier at each offset.
  3. Push handling: an integer margin exactly equal to the line is excluded.

Parameters default to the validated moneyline values (rho 0.75, k 15,
sigma 13, home edge from the tune seasons). Nothing is re-tuned here.

Usage:
    python backtest_nba_spread_probability.py --seasons 2021,2022,2023,2024,2025,2026 --tune-seasons 2022,2023,2024 --validate-seasons 2025,2026
"""

import argparse
import math

from backtest_nba_win_pct import (
    estimate_home_edge,
    load_season_games,
    norm_cdf,
    season_summary,
)

DEFAULT_RHO = 0.75
DEFAULT_K = 15.0
DEFAULT_SIGMA = 13.0
OFFSETS = [-9.0, -6.0, -3.0, 3.0, 6.0, 9.0]


def walk_forward_margins(games, prior_mov, rho, k, home_edge):
    """Per game, in order, BEFORE updating with its result: predicted mean margin mu."""
    sums = {}
    out = []
    for g in games:
        h, a = g["home_id"], g["away_id"]
        edge = 0.0 if g.get("neutral_site") else home_edge
        sh = sums.setdefault(h, [0.0, 0])
        sa = sums.setdefault(a, [0.0, 0])
        r_h = (sh[0] + k * rho * prior_mov.get(h, 0.0)) / (sh[1] + k)
        r_a = (sa[0] + k * rho * prior_mov.get(a, 0.0)) / (sa[1] + k)
        margin = g["home_score"] - g["away_score"]
        out.append({"mu": r_h - r_a + edge, "margin": margin, "edge": edge, "game_id": g.get("game_id"),
                    "date": g.get("date"), "home_id": h, "away_id": a})
        sh[0] += margin - edge
        sh[1] += 1
        sa[0] += -margin + edge
        sa[1] += 1
    return out


def collect_rows(games_by_season, targets, rho, k, home_edge):
    rows = []
    for season in targets:
        games, prev = games_by_season.get(season), games_by_season.get(season - 1)
        if not games or not prev:
            continue
        prior = {t: v["mov"] for t, v in season_summary(prev, home_edge).items()}
        for r in walk_forward_margins(games, prior, rho, k, home_edge):
            r["season"] = season
            rows.append(r)
    return rows


def _mean_sd(xs):
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else float("nan")
    return m, sd


def estimate_sigma(rows):
    """Closed-form sigma: RMS of (actual margin - predicted margin). No sweep, so no degeneracy."""
    return math.sqrt(sum((r["margin"] - r["mu"]) ** 2 for r in rows) / len(rows))


def z_report(rows, sigma, n_buckets=4, quiet=False):
    """Returns (overall (mean, sd), [(lo, hi, n, mean, sd) per |mu| bucket])."""
    z = [(r["margin"] - r["mu"]) / sigma for r in rows]
    overall = _mean_sd(z)
    srt = sorted(range(len(rows)), key=lambda i: abs(rows[i]["mu"]))
    size = len(srt) // n_buckets
    buckets = []
    for b in range(n_buckets):
        idx = srt[b * size:(b + 1) * size] if b < n_buckets - 1 else srt[b * size:]
        zs = [z[i] for i in idx]
        m, sd = _mean_sd(zs)
        buckets.append((abs(rows[idx[0]]["mu"]), abs(rows[idx[-1]]["mu"]), len(idx), m, sd))
    if not quiet:
        print(f"\n{'=' * 78}\nz = (actual margin - predicted margin) / sigma, sigma={sigma}  ({len(rows)} games)\n{'=' * 78}")
        print(f"Overall: mean {overall[0]:+.3f} (want ~0), sd {overall[1]:.3f} (want ~1.0)")
        print(f"\n{'|predicted margin|':>20}  {'n':>5}  {'mean z':>8}  {'sd z':>7}")
        for lo, hi, n, m, sd in buckets:
            print(f"{lo:>8.1f} - {hi:>6.1f}pts  {n:>5}  {m:>+8.3f}  {sd:>7.3f}")
        print("\nsd z rising with |predicted margin| = lopsided games are NOISIER than a constant sigma "
              "assumes (cover probabilities there are overconfident). sd z roughly flat near 1 = the constant "
              "sigma is fine at every game size. mean z away from 0 = a level/home-edge bias.")
    return overall, buckets


def cover_calibration(rows, sigma, offsets=OFFSETS, quiet=False):
    """For each synthetic line mu+offset: predicted P(home covers) vs the actual cover rate and Brier.
    Returns {offset: (n, avg_pred, actual_rate, brier)}."""
    result = {}
    for off in offsets:
        preds, ys = [], []
        for r in rows:
            line = r["mu"] + off
            diff = r["margin"] - line
            if diff == 0:
                continue  # push
            preds.append(1.0 - norm_cdf((line - r["mu"]) / sigma))
            ys.append(1 if diff > 0 else 0)
        if not ys:
            continue
        brier = sum((p - y) ** 2 for p, y in zip(preds, ys)) / len(ys)
        result[off] = (len(ys), sum(preds) / len(ys), sum(ys) / len(ys), brier)
    if not quiet:
        print(f"\n{'=' * 78}\nCover calibration at synthetic lines (line = predicted margin + offset)\n{'=' * 78}")
        print(f"{'offset':>7}  {'n':>5}  {'avg predicted':>14}  {'actual cover rate':>18}  {'Brier':>7}")
        for off, (n, p, a, b) in result.items():
            print(f"{off:>+7.1f}  {n:>5}  {p * 100:>13.1f}%  {a * 100:>17.1f}%  {b:>7.4f}")
        print("(offset 0 is excluded on purpose -- it is exactly 50% for every game and says nothing.) "
              "Predicted and actual should agree at every offset; the 0.25 Brier line is 'no information'.")
    return result


def _years(s):
    return [int(x) for x in s.split(",") if x.strip()]


def main():
    p = argparse.ArgumentParser(description="NBA spread-cover distribution calibration (no market lines needed)")
    p.add_argument("--seasons", required=True)
    p.add_argument("--tune-seasons", required=True, help="Used ONLY to estimate the home edge.")
    p.add_argument("--validate-seasons", required=True)
    p.add_argument("--rho", type=float, default=DEFAULT_RHO)
    p.add_argument("--k", type=float, default=DEFAULT_K)
    p.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    p.add_argument("--tune-sigma", action="store_true",
                   help="Estimate sigma in closed form from the TUNE seasons only, then evaluate validate at it.")
    p.add_argument("--season-types", default="2")
    p.add_argument("--dir", default=".")
    args = p.parse_args()

    allowed = tuple(_years(args.season_types))
    gbs = {}
    for s in _years(args.seasons):
        g = load_season_games(s, allowed, args.dir)
        if g is None:
            print(f"[SKIP {s}] nba_games_{s}.json not found.")
            continue
        gbs[s] = g
    tune, validate = _years(args.tune_seasons), _years(args.validate_seasons)
    edge = estimate_home_edge(gbs, tune)
    print(f"Home edge from tune seasons only: {edge:+.2f} pts. Parameters: rho={args.rho} k={args.k} sigma={args.sigma} (not re-tuned).")
    rows = collect_rows(gbs, validate, args.rho, args.k, edge)
    if not rows:
        print("No validate games found.")
        return
    sigma = args.sigma
    if args.tune_sigma:
        tune_rows = collect_rows(gbs, tune, args.rho, args.k, edge)
        sigma = estimate_sigma(tune_rows)
        print(f"Sigma estimated from tune seasons only ({len(tune_rows)} games): {sigma:.2f} (validate evaluated at this value)")
    z_report(rows, sigma)
    cover_calibration(rows, sigma)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: backtest_nba_lineup_adjustment.py (307 lines)
# ======================================================================
_SRC["backtest_nba_lineup_adjustment"] = r'''"""
backtest_nba_lineup_adjustment.py

Does knowing WHO IS PLAYING improve the NBA margin forecast beyond the team
rating? (The NBA analogue of MLB's lineup effect.)

MODEL
  1. Baseline: the validated shrinkage rating, mu0 = r_home - r_away + home_edge.
  2. Player values: ridge regression of the PRE-GAME FORECAST ERROR (margin - mu0) on how
     each player's MINUTE SHARE (minutes / team minutes in that game) deviated from the
     team's own trailing-window average (home +, away -). Regressing the error on
     deviations removes team strength, which the rating already carries. Fit incrementally, walk-forward: coefficients used
     for a game come from a refit (every --refit-days days) that saw ONLY games dated
     strictly before that refit.
  3. Lineup value of a team for a game, given its available players A:
         E(A) = sum_{p in A} beta_p * w_p / sum_{p in A} w_p
     where w_p = p's average minute share over the team's last --window games
     (this season). Absent players' minutes are re-spread over those available.
     delta = E(A) - L_hist, with L_hist = the team's own trailing-window average
     lineup value (that is what the team rating has already priced in).
  4. Adjusted forecast: mu = mu0 + gamma * (delta_home - delta_away).

HONEST CAVEAT ABOUT 'A'. ESPN has no historical announced lineups. Here A = players
who actually logged minutes in that game (a hindsight proxy). It is the right
analogue of 'known before tip-off' only for injuries/rest; it also 'knows' coach's
decisions and garbage-time-only cameos. So this backtest is an UPPER-ish bound on
what pre-game information is worth. The live module will use real Out/Day-To-Day
statuses instead, and gamma should be read with that in mind.

TUNING DISCIPLINE. lambda (ridge) and gamma are chosen on --tune-seasons only
(minimum margin RMSE); gamma also has a closed-form OLS value printed as a
cross-check. Sigma for win probabilities is the RMS residual on the tune seasons.
Everything is then reported on --validate-seasons, once.

Usage (from the NBA folder, needs nba_games_*.json AND nba_boxscores_*.json):
    python backtest_nba_lineup_adjustment.py --seasons 2021,2022,2023,2024,2025,2026 ^
        --box-seasons 2022,2023,2024,2025,2026 --tune-seasons 2023,2024 --validate-seasons 2025,2026
"""

import argparse
import datetime as dt
import json
import math
import os
from collections import deque

import numpy as np

from backtest_nba_spread_probability import collect_rows, estimate_sigma
from backtest_nba_win_pct import estimate_home_edge, load_season_games, norm_cdf

DEFAULT_RHO = 0.75
DEFAULT_K = 15.0
LAMBDAS = [0.1, 0.4, 1.5, 6.0, 24.0]
GAMMAS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0]


def load_boxscores(season, directory="."):
    path = os.path.join(directory, f"nba_boxscores_{season}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)["games"]


def _day(s):
    return dt.date.fromisoformat(str(s)[:10])


def _played(players):
    """{pid: minutes} for players who logged minutes."""
    out = {}
    for p in players:
        m = p.get("min")
        if m is not None and m > 0:
            out[str(p["id"])] = float(m)
    return out


def run_pass(rows, boxes, lam, refit_days=14, window=20, min_hist=5, final_fit=False):
    """Chronological pass over rows (each needs game_id, date, season, home_id, away_id, margin, edge).
    Returns (out_rows, final_beta, index, game_counts) where each out_row is a copy of its row plus
    dh, da, D = dh - da (0 when no boxscore / too little history). final_fit=True returns the beta solved from
    ALL games (production use); otherwise beta is the last walk-forward refit (backtest use)."""
    # index every player who ever logs minutes in a boxscore we will see
    index = {}
    for r in rows:
        b = boxes.get(r["game_id"])
        if not b:
            continue
        for side in ("home", "away"):
            for pid in _played(b[side]):
                index.setdefault(pid, len(index))
    n = max(len(index), 1)
    xtx = np.zeros((n, n))
    xty = np.zeros(n)
    beta = np.zeros(n)
    counts = np.zeros(n)
    hist = {}  # (season, team) -> deque of {pid: share}
    last_fit = None
    out = []
    for r in rows:
        d = _day(r["date"])
        if last_fit is None or (d - last_fit).days >= refit_days:
            if last_fit is not None:
                beta = np.linalg.solve(xtx + lam * np.eye(n), xty)
            last_fit = d
        b = boxes.get(r["game_id"])
        o = dict(r)
        o["dh"] = o["da"] = o["D"] = 0.0
        if not b:
            out.append(o)
            continue
        shares, wts = {}, {}
        for side, tid in (("home", r["home_id"]), ("away", r["away_id"])):
            mins = _played(b[side])
            tot = sum(mins.values())
            shares[side] = {pid: m / tot for pid, m in mins.items()} if tot > 0 else {}
            h = hist.get((r["season"], tid))
            if h and len(h) >= min_hist:
                w = {}
                for g in h:
                    for p, s in g.items():
                        w[p] = w.get(p, 0.0) + s / len(h)
                wts[side] = w
        deltas = {"home": 0.0, "away": 0.0}
        for side in ("home", "away"):
            w, avail = wts.get(side), shares[side]
            if not w or not avail:
                continue
            den = sum(w[p] for p in avail if p in w)
            if den <= 0:
                continue
            # expected lineup value given availability, minus the team's own usual (trailing) lineup value
            deltas[side] = (sum(beta[index[p]] * w[p] for p in avail if p in w) / den
                            - sum(beta[index[p]] * v for p, v in w.items()))
        o["dh"], o["da"] = deltas["home"], deltas["away"]
        o["D"] = o["dh"] - o["da"]
        out.append(o)
        # ---- update AFTER predicting: this game's actual shares/result now become history ----
        if "home" in wts and "away" in wts and shares["home"] and shares["away"]:
            # regress the pre-game FORECAST ERROR on how this game's minutes deviated from the team's usual
            # minutes: removes team-level strength (already in the rating) so betas measure players only
            x = {}
            for side, sign in (("home", 1.0), ("away", -1.0)):
                for p in set(shares[side]) | set(wts[side]):
                    x[index[p]] = x.get(index[p], 0.0) + sign * (shares[side].get(p, 0.0) - wts[side].get(p, 0.0))
            idx = np.array(list(x))
            val = np.array([x[i] for i in idx])
            y = r["margin"] - r["mu"]
            xtx[np.ix_(idx, idx)] += np.outer(val, val)
            xty[idx] += y * val
            counts[idx] += 1
        for side, tid in (("home", r["home_id"]), ("away", r["away_id"])):
            if shares[side]:
                hist.setdefault((r["season"], tid), deque(maxlen=window)).append(shares[side])
    if final_fit:
        beta = np.linalg.solve(xtx + lam * np.eye(n), xty)
    return out, beta, index, counts


# ---------------------------------------------------------------- evaluation helpers
def gamma_ols(rows):
    num = sum((r["margin"] - r["mu"]) * r["D"] for r in rows)
    den = sum(r["D"] ** 2 for r in rows)
    return num / den if den > 0 else 0.0


def adj_mu(r, gamma):
    return r["mu"] + gamma * r["D"]


def rmse(rows, gamma):
    return math.sqrt(sum((r["margin"] - adj_mu(r, gamma)) ** 2 for r in rows) / len(rows))


def mae(rows, gamma):
    return sum(abs(r["margin"] - adj_mu(r, gamma)) for r in rows) / len(rows)


def brier(rows, gamma, sigma):
    tot = 0.0
    for r in rows:
        p = norm_cdf(adj_mu(r, gamma) / sigma)
        y = 1.0 if r["margin"] > 0 else 0.0
        tot += (p - y) ** 2
    return tot / len(rows)


def paired_diff(rows, gamma):
    """(mean, standard error) of squared error: baseline minus adjusted. Positive = adjustment helps."""
    d = [(r["margin"] - r["mu"]) ** 2 - (r["margin"] - adj_mu(r, gamma)) ** 2 for r in rows]
    n = len(d)
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1)) if n > 1 else float("nan")
    return m, sd / math.sqrt(n)


def sweep(rows, boxes, tune_seasons, lambdas=LAMBDAS, gammas=GAMMAS, quiet=False, **pass_kw):
    """Tune lambda, gamma on tune seasons only. Returns (best_lam, best_gamma, passes) where
    passes[lam] = (rows, beta, index, counts) for the whole timeline."""
    passes = {}
    best = None
    if not quiet:
        print(f"\n{'=' * 78}\nTUNE seasons {tune_seasons}: margin RMSE by ridge lambda and gamma\n{'=' * 78}")
        print(f"{'lambda':>8} " + " ".join(f"g={g:<5}" for g in gammas) + "  | OLS gamma")
    for lam in lambdas:
        res = run_pass(rows, boxes, lam, **pass_kw)
        passes[lam] = res
        tune_rows = [r for r in res[0] if r["season"] in tune_seasons]
        errs = [(g, rmse(tune_rows, g)) for g in gammas]
        g_best, e_best = min(errs, key=lambda x: x[1])
        if not quiet:
            print(f"{lam:>8} " + " ".join(f"{e:<7.3f}" for _, e in errs) + f"  | {gamma_ols(tune_rows):+.2f}")
        if best is None or e_best < best[2]:
            best = (lam, g_best, e_best)
    lam, g, _ = best
    if not quiet:
        print(f"\nChosen on tune: lambda={lam}, gamma={g}")
        if lam in (lambdas[0], lambdas[-1]):
            print(f"  WARNING: lambda={lam} is at the edge of the grid {lambdas}; surface not bracketed.")
        if g in (gammas[0], gammas[-1]):
            print(f"  WARNING: gamma={g} is at the edge of the grid {gammas}; surface not bracketed.")
    return lam, g, passes


def report_validate(passes, lam, gamma, tune_seasons, validate_seasons, top_names=None):
    out, beta, index, counts = passes[lam]
    tune_rows = [r for r in out if r["season"] in tune_seasons]
    val_rows = [r for r in out if r["season"] in validate_seasons]
    sig_base = estimate_sigma(tune_rows)
    sig_adj = math.sqrt(sum((r["margin"] - adj_mu(r, gamma)) ** 2 for r in tune_rows) / len(tune_rows))
    thr = float(np.quantile([abs(r["D"]) for r in tune_rows if r["D"] != 0.0] or [0.0], 0.75))
    big = [r for r in val_rows if abs(r["D"]) >= thr and thr > 0]
    print(f"\n{'=' * 78}\nVALIDATE seasons {validate_seasons} ({len(val_rows)} games) -- lambda={lam}, gamma={gamma}\n{'=' * 78}")
    print(f"{'':28} {'baseline':>10} {'+lineups':>10}")
    print(f"{'margin RMSE':28} {rmse(val_rows, 0.0):>10.3f} {rmse(val_rows, gamma):>10.3f}")
    print(f"{'margin MAE':28} {mae(val_rows, 0.0):>10.3f} {mae(val_rows, gamma):>10.3f}")
    print(f"{'win-prob Brier (tune sigma)':28} {brier(val_rows, 0.0, sig_base):>10.4f} {brier(val_rows, gamma, sig_adj):>10.4f}")
    print(f"{'sigma (tune RMS residual)':28} {sig_base:>10.2f} {sig_adj:>10.2f}")
    m, se = paired_diff(val_rows, gamma)
    print(f"\nPaired squared-error gain (all validate games): {m:+.2f} +/- {se:.2f} (mean +/- s.e.; > 2 s.e. = real)")
    if big:
        mb, seb = paired_diff(big, gamma)
        print(f"Games with a BIG lineup difference (|D| >= {thr:.2f}, tune 75th pct; n={len(big)}): "
              f"RMSE {rmse(big, 0.0):.3f} -> {rmse(big, gamma):.3f}; paired gain {mb:+.2f} +/- {seb:.2f}")
    print(f"OLS gamma on validate (diagnostic only, not used): {gamma_ols(val_rows):+.2f}  -- near the tuned gamma = stable")
    if top_names:
        inv = {i: p for p, i in index.items()}
        order = [i for i in np.argsort(-beta) if counts[i] >= 60]
        print("\nSanity check -- highest fitted player values (>=60 games, margin pts per 100% of team minutes):")
        for i in order[:12]:
            print(f"   {top_names.get(inv[i], inv[i]):<26} {beta[i]:+7.1f}   ({int(counts[i])} games)")
        print("   ... lowest:")
        for i in order[-5:]:
            print(f"   {top_names.get(inv[i], inv[i]):<26} {beta[i]:+7.1f}   ({int(counts[i])} games)")
        print("   (These should look like real stars at the top. If they look random, the lineup signal is noise.)")


def _years(s):
    return [int(x) for x in s.split(",") if x.strip()]


def main():
    p = argparse.ArgumentParser(description="NBA lineup/availability adjustment backtest")
    p.add_argument("--seasons", required=True, help="All seasons with game files (include the year before the first box season)")
    p.add_argument("--box-seasons", required=True)
    p.add_argument("--tune-seasons", required=True)
    p.add_argument("--validate-seasons", required=True)
    p.add_argument("--rho", type=float, default=DEFAULT_RHO)
    p.add_argument("--k", type=float, default=DEFAULT_K)
    p.add_argument("--refit-days", type=int, default=14)
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--dir", default=".")
    args = p.parse_args()

    gbs = {}
    for s in _years(args.seasons):
        g = load_season_games(s, (2,), args.dir)
        if g is None:
            print(f"[SKIP {s}] nba_games_{s}.json not found.")
            continue
        gbs[s] = g
    boxes, names = {}, {}
    box_seasons = []
    for s in _years(args.box_seasons):
        b = load_boxscores(s, args.dir)
        if b is None:
            print(f"[SKIP {s}] nba_boxscores_{s}.json not found.")
            continue
        boxes.update(b)
        box_seasons.append(s)
        for gm in b.values():
            for side in ("home", "away"):
                for pl in gm[side]:
                    names[str(pl["id"])] = pl.get("name")
    tune, validate = _years(args.tune_seasons), _years(args.validate_seasons)
    edge = estimate_home_edge(gbs, tune)
    rows = collect_rows(gbs, sorted(box_seasons), args.rho, args.k, edge)
    print(f"Home edge (tune seasons): {edge:+.2f}. {len(rows)} games on the timeline, "
          f"{sum(1 for r in rows if r['game_id'] in boxes)} with boxscores. rho={args.rho} k={args.k}")
    lam, gamma, passes = sweep(rows, boxes, tune, refit_days=args.refit_days, window=args.window)
    report_validate(passes, lam, gamma, tune, validate, top_names=names)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: core_nba_moneyline.py (235 lines)
# ======================================================================
_SRC["core_nba_moneyline"] = r'''"""
core_nba_moneyline.py

PRODUCTION live-prediction module for NBA moneyline (win probability). This
is NOT a backtest -- it predicts games that HAVEN'T been played yet, using
every completed regular-season game so far this season plus last season's
final strength as the prior. backtest_nba_win_probability.py did the
validation; this module applies the validated formula/parameters to real
upcoming games, meant to be imported into (or piped from) a platform's own
storage/display layer.

VALIDATED PARAMETERS (see nba-win-probability-backtest-findings.md; tuned on
the 2022-2024 seasons only, scored on 2025-2026 which tuning never saw):
    RHO        = 0.75   carryover of last season's strength
    K          = 15.0   prior weight, in games
    SIGMA      = 13.0   per-game margin sd, points
    HOME_EDGE  = 2.13   home-court points (mean home margin, tune seasons)
Out-of-sample result at these parameters (2,469 games): Brier 0.2088, log
loss 0.6053, vs 0.2123 / 0.6131 for a tuned record-only baseline and
0.2500 / 0.6931 for always-50%. The parameter surface is FLAT (the 8 best of
125 grid points were within 0.0002 Brier), so these are one of many
near-equivalent choices, not a sharp optimum.

FORMULA (identical to the backtest, not re-derived here):
    prior_i    = RHO * (team i's final home-court-adjusted average margin last season)
    rating_i   = (sum of adjusted margins this season + K * prior_i) / (games this season + K)
    P(home wins) = Phi((rating_home - rating_away + HOME_EDGE) / SIGMA)
    (HOME_EDGE forced to 0 for a neutral-site game)

KNOWN LIMITS, shipped as-is:
  * NO injuries / who-is-playing, rest days or back-to-backs, travel, or
    opponent-strength adjustment of past margins. These are the real NBA
    edges a sportsbook prices in -- expect this to beat naive baselines, NOT
    the market. Do not treat it as a market-beating edge without validating
    against real lines.
  * Mild overconfidence in the very top probability bin (81-94% predicted vs
    ~82% actual, ~1.8 standard errors, one bin) was seen in validation --
    treat probabilities above ~85% with some caution.
  * Regular season only (ESPN season type 2). Playoff and preseason games are
    skipped, not predicted -- the model was never validated on them.
  * The home-court edge is an average over 2022-2024; recent seasons showed
    a slightly lower home win rate (validation: predicted 55.8% vs actual
    55.0%). Worth re-estimating periodically.

HOW HISTORY IS BUILT: reads the per-season JSON files written by
nba_collect_games.py (nba_games_{season}.json, in the folder you run from).
By default it first runs that collector incrementally for this season and
last season (only days not yet collected are fetched -- cheap day to day),
so the files are current. Then it predicts every NOT-YET-PLAYED regular-season
game on the requested date.

TRUST GATE: a game comes back trusted=False (null probability, never a
guess) if either team has no prior-season record AND fewer than
MIN_GAMES_NO_PRIOR games this season.

Usage (run from the folder holding the nba_games_*.json files):
    python core_nba_moneyline.py                      -> today's not-yet-played games
    python core_nba_moneyline.py --date 20261022      -> a specific date (YYYYMMDD)
    python core_nba_moneyline.py --out predictions.json --no-refresh

Can also be imported:
    from core_nba_moneyline import predict_upcoming_games
    predictions = predict_upcoming_games("20261022")

Needs internet access (unless --no-refresh with up-to-date files) -- run locally.
"""

import argparse
import datetime
import json
import math
import os

import nba_collect_games as collector

RHO = 0.75                 # VALIDATED -- backtest_nba_win_probability.py --holdout
K = 15.0                   # VALIDATED
SIGMA = 13.0               # VALIDATED
HOME_EDGE = 2.13           # VALIDATED -- mean home margin over the tune seasons 2022-2024
MIN_GAMES_NO_PRIOR = 10    # games needed this season for a team with no prior-season record
REGULAR_SEASON_TYPE = 2    # ESPN season.type for regular season (confirmed via --diagnose)


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def season_for_date(d):
    """Season end-year: games from August onward belong to the season ending next calendar year."""
    return d.year + 1 if d.month >= 8 else d.year


def load_regular_games(season, directory="."):
    from nba_io import load_json_retry
    path = os.path.join(directory, collector.progress_file_for(season))
    progress = load_json_retry(path, None)
    if progress is None:
        return []
    return [g for g in progress["games"].values()
            if g.get("season_type") == REGULAR_SEASON_TYPE
            and g.get("home_score") is not None and g.get("away_score") is not None]


def adjusted_stats(games, home_edge=HOME_EDGE):
    """team -> {"sum_adj", "n"}: home-court-adjusted margin totals."""
    stats = {}
    for g in games:
        margin = g["home_score"] - g["away_score"]
        edge = 0.0 if g.get("neutral_site") else home_edge
        for team, adj in ((g["home_id"], margin - edge), (g["away_id"], -margin + edge)):
            s = stats.setdefault(team, {"sum_adj": 0.0, "n": 0})
            s["sum_adj"] += adj
            s["n"] += 1
    return stats


def build_team_state(prev_games, cur_games, home_edge=HOME_EDGE):
    """team -> {"prior" (None if no prior-season record), "sum_adj", "n"}."""
    prev = adjusted_stats(prev_games, home_edge)
    cur = adjusted_stats(cur_games, home_edge)
    state = {}
    for t in set(prev) | set(cur):
        p = prev.get(t)
        c = cur.get(t, {"sum_adj": 0.0, "n": 0})
        state[t] = {"prior": (p["sum_adj"] / p["n"]) if p and p["n"] else None,
                    "sum_adj": c["sum_adj"], "n": c["n"]}
    return state


def team_rating(state, team, rho=RHO, k=K):
    """Shrunk rating, or None if the team lacks enough history to trust."""
    s = state.get(team)
    if s is None:
        return None
    if s["prior"] is None and s["n"] < MIN_GAMES_NO_PRIOR:
        return None
    prior = rho * (s["prior"] if s["prior"] is not None else 0.0)
    return (s["sum_adj"] + k * prior) / (s["n"] + k)


def win_probability(rating_home, rating_away, neutral_site=False, home_edge=HOME_EDGE, sigma=SIGMA):
    if rating_home is None or rating_away is None:
        return None
    edge = 0.0 if neutral_site else home_edge
    return norm_cdf((rating_home - rating_away + edge) / sigma)


def predict_upcoming_games(game_date, season=None, refresh=True, directory=".", fetch_day=None, collect=None):
    """The main entry point. game_date is 'YYYYMMDD' (or a datetime.date).
    Returns one dict per NOT-YET-PLAYED regular-season game that day, safe to
    json.dump or hand to a platform's storage layer. fetch_day / collect are
    injectable for testing; by default they are the collector's own."""
    if isinstance(game_date, str):
        game_date = datetime.datetime.strptime(game_date, "%Y%m%d").date()
    season = season or season_for_date(game_date)
    fetch_day = fetch_day or collector.get_day
    collect = collect or collector.collect_season

    if refresh:
        collect(season, directory=directory)
        collect(season - 1, directory=directory)

    prev_games = load_regular_games(season - 1, directory)
    cur_games = load_regular_games(season, directory)
    state = build_team_state(prev_games, cur_games)
    return predict_from_events(fetch_day(game_date).get("events") or [], state, season)


def predict_from_events(events, state, season):
    """Pure core of predict_upcoming_games: scoreboard events + a prebuilt team state -> predictions.
    Split out so a long-running service can cache `state` and the scoreboard instead of rebuilding per request."""
    predictions = []
    for ev in events:
        g = collector.parse_event(ev)
        if g is None or g["completed"] or g["season_type"] != REGULAR_SEASON_TYPE:
            continue
        r_h, r_a = team_rating(state, g["home_id"]), team_rating(state, g["away_id"])
        p_home = win_probability(r_h, r_a, neutral_site=g["neutral_site"])
        trusted = p_home is not None
        edge = 0.0 if g["neutral_site"] else HOME_EDGE
        predictions.append({
            "game_id": g["game_id"],
            "date": g["date"],
            "season": season,
            "home_team": g["home_name"], "home_id": g["home_id"],
            "away_team": g["away_name"], "away_id": g["away_id"],
            "neutral_site": g["neutral_site"],
            "trusted": trusted,
            "home_rating": round(r_h, 3) if r_h is not None else None,
            "away_rating": round(r_a, 3) if r_a is not None else None,
            "predicted_margin": round(r_h - r_a + edge, 2) if trusted else None,
            "home_win_probability": round(p_home, 4) if trusted else None,
            "away_win_probability": round(1 - p_home, 4) if trusted else None,
        })
    return predictions


def print_predictions_table(predictions):
    if not predictions:
        print("\nNo not-yet-played regular-season games found for that date.")
        return
    header = f"{'matchup':<44}  {'pred margin':>11}  {'home win%':>9}  {'trusted':>7}"
    print(f"\n{header}")
    print("-" * len(header))
    for p in predictions:
        matchup = f"{p['away_team']} @ {p['home_team']}" + (" (N)" if p["neutral_site"] else "")
        if p["trusted"]:
            print(f"{matchup:<44}  {p['predicted_margin']:>+11.1f}  {p['home_win_probability'] * 100:>8.1f}%  {'yes':>7}")
        else:
            print(f"{matchup:<44}  {'--':>11}  {'--':>9}  {'no':>7}")
    n = sum(1 for p in predictions if p["trusted"])
    print(f"\n{n}/{len(predictions)} game(s) trusted. predicted margin = expected home points minus away points. "
          f"No injury/rest information is used -- see the module docstring.")


def main():
    parser = argparse.ArgumentParser(description="Live NBA moneyline (win probability) for not-yet-played games")
    parser.add_argument("--date", type=str, default=None, help="YYYYMMDD (default: today)")
    parser.add_argument("--season", type=int, default=None, help="Season end-year (default: inferred from --date)")
    parser.add_argument("--out", type=str, default=None, help="Output JSON path (default: nba_predictions_{date}.json)")
    parser.add_argument("--no-refresh", action="store_true", help="Skip the incremental collector run; use files as-is.")
    parser.add_argument("--dir", default=".", help="Folder holding nba_games_*.json (default: current folder)")
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().strftime("%Y%m%d")
    predictions = predict_upcoming_games(date_str, season=args.season, refresh=not args.no_refresh, directory=args.dir)
    out_path = args.out or f"nba_predictions_{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(predictions, f, indent=2)
    print_predictions_table(predictions)
    print(f"\nWrote {len(predictions)} prediction(s) to {out_path}")


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: core_nba_spread.py (456 lines)
# ======================================================================
_SRC["core_nba_spread"] = r'''"""
core_nba_spread.py

PRODUCTION live-prediction module for NBA game margins: spread-cover probability
and win probability, with a LINEUP/AVAILABILITY adjustment from today's injury
feed. Predicts games that HAVEN'T been played yet. Builds on core_nba_moneyline
(team ratings) and backtest_nba_lineup_adjustment (player values).

STATUS: SHADOW MODE. The team-rating part is validated (moneyline Brier 0.2088
out of sample; margin distribution checked, sigma re-estimated on tune seasons
only: 13.84, validate z sd 1.040). The lineup part showed a real but modest gain
in backtest (validate 2025-26, margin RMSE 14.401 -> 14.231, paired gain
+4.88 +/- 1.38) BUT with hindsight availability (who actually played). Live
availability comes from ESPN's injury feed, which is noisier, so the live gain
is expected to be smaller and is UNPROVEN. Neither part has been tested against
real sportsbook lines (ESPN has none historically). Track it this season with
nba_track_lineup_results.py before putting money on it.

MODEL
  mu0   = rating_home - rating_away + home_edge          (core_nba_moneyline)
  delta = for each team: beta-weighted expected lineup value given who is
          available, minus the team's usual lineup value
              E = sum_p beta_p * w_p * q_p / sum_p w_p * q_p ;  usual = sum_p beta_p * w_p
          w_p = p's average minute share over the team's last 20 games this season
          q_p = availability: 0 if the injury feed says Out, 0.5 if Day-To-Day
                (or any other status), 1 otherwise.  (q=0.5 is an approximation to the
                true mixture over play/not-play, NOT validated.)
  mu    = mu0 + GAMMA * (delta_home - delta_away)
  P(home wins) = Phi(mu / SIGMA)
  P(home covers home_spread s) = P(margin > -s) = 1 - Phi((-s - mu) / SIGMA)
          (home_spread is the sportsbook number: -5.5 means home favored by 5.5)
  Player values beta: ridge regression of the pre-game forecast error on minute-share
  deviations, fit on ALL cached boxscores, cached to nba_player_values.json and
  refit when older than --max-age-days.

LIMITS, shipped as-is
  * A team gets NO lineup adjustment until it has 5 boxscores this season (early
    season / new rosters). Players with no fitted value (rookies, new arrivals) count as 0.
  * GAMMA=1.5 is a conservative pick: the tune surface was flat between 1.5 and 2.0
    (and gamma hit the grid edge, which only reflects ridge shrinkage).
  * Each team's adjustment is clipped to +/- MAX_ABS_DELTA points as an unvalidated
    safeguard against a bad injury-feed parse.
  * Individual player values are noisy (e.g. odd names near the bottom/top); only the
    net team-level adjustment has been validated.
  * Push handling: a line equal to the final margin is ignored (not modeled).
  * No rest days / back-to-backs / travel.

Usage (from the NBA folder, with nba_games_*.json and nba_boxscores_*.json):
    python core_nba_spread.py                           -> today's games, no lines
    python core_nba_spread.py --date 20261022 --lines-file lines.json
    lines.json = {"<ESPN game id>": -5.5, "Boston Celtics @ New York Knicks": 3.0}   (home spread)
    python core_nba_spread.py --no-refresh --refit
Needs internet (ESPN scoreboard, summaries, injuries) unless --no-refresh. Run locally.
"""

import argparse
import datetime
import json
import math
import os
import re

import numpy as np
import requests

import backtest_nba_lineup_adjustment as la
import backtest_nba_spread_probability as sp
import core_nba_moneyline as mon
import nba_collect_boxscores as bx
from backtest_nba_win_pct import load_season_games
from nba_io import atomic_json_dump, load_json_retry

SIGMA = 13.84          # VALIDATED: RMS residual on 2022-24 tune seasons; validate z sd 1.040
LAMBDA = 1.5           # ridge strength, tuned on 2023-24
GAMMA = 1.5            # lineup weight (tuned best 2.0 at grid edge; flat 1.5-2.0; conservative)
WINDOW = 20
MIN_HIST = 5
MAX_ABS_DELTA = 8.0    # unvalidated safeguard, points per team
DTD_Q = 0.5            # availability weight for Day-To-Day / unknown statuses
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
VALUES_FILE = "nba_player_values.json"
LOG_FILE = "nba_prediction_log.jsonl"


# ------------------------------------------------------------------ probabilities
def home_covers_probability(mu, home_spread, sigma=SIGMA):
    """P(home margin > -home_spread). home_spread -5.5 = home favored by 5.5."""
    return 1.0 - mon.norm_cdf((-home_spread - mu) / sigma)


# ------------------------------------------------------------------ player values
def _box_seasons(directory):
    out = []
    for name in os.listdir(directory):
        m = re.fullmatch(r"nba_boxscores_(\d{4})\.json", name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _load_all(directory):
    seasons = _box_seasons(directory)
    gbs, boxes, names = {}, {}, {}
    for s in sorted(set(seasons) | {x - 1 for x in seasons}):
        g = load_season_games(s, (2,), directory)
        if g is not None:
            gbs[s] = g
    for s in seasons:
        b = la.load_boxscores(s, directory)
        if b:
            boxes.update(b)
            for gm in b.values():
                for side in ("home", "away"):
                    for pl in gm[side]:
                        names[str(pl["id"])] = pl.get("name")
    return gbs, boxes, names, seasons


def fit_player_values(directory=".", lam=LAMBDA, max_age_days=7, refit=False, today=None):
    """{'beta': {pid: value}, 'names': {pid: name}, 'fitted_through': date, 'fitted_at': iso}. Cached."""
    today = today or datetime.date.today()
    path = os.path.join(directory, VALUES_FILE)
    cached = load_json_retry(path, None) if not refit else None
    if cached is not None:
        age = (today - datetime.date.fromisoformat(cached["fitted_at"][:10])).days
        if age <= max_age_days and cached.get("lambda") == lam:
            return cached
    gbs, boxes, names, seasons = _load_all(directory)
    rows = sp.collect_rows(gbs, seasons, mon.RHO, mon.K, mon.HOME_EDGE)
    if not rows:
        raise RuntimeError("No boxscore seasons with a previous-season games file found; cannot fit player values.")
    # refit_days huge: no intermediate solves needed, we only want the final fit from all games
    out, beta, index, counts = la.run_pass(rows, boxes, lam, refit_days=10 ** 9, window=WINDOW,
                                           min_hist=MIN_HIST, final_fit=True)
    result = {"beta": {pid: float(beta[i]) for pid, i in index.items()},
              "games": {pid: int(counts[i]) for pid, i in index.items()},
              "names": {pid: names.get(pid) for pid in index},
              "lambda": lam,
              "fitted_through": max(str(r["date"])[:10] for r in rows),
              "fitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "n_games": len(rows)}
    atomic_json_dump(path, result)
    return result


# ------------------------------------------------------------------ usual lineups
def usual_minute_shares(season, directory=".", window=WINDOW, min_hist=MIN_HIST):
    """team_id -> {pid: average minute share over the team's last `window` games this season},
    only for teams with >= min_hist boxscores this season. Also returns the game count per team."""
    games = {g["game_id"]: g for g in (load_season_games(season, (2,), directory) or [])}
    boxes = la.load_boxscores(season, directory) or {}
    per_team = {}
    for gid in sorted((x for x in boxes if x in games), key=lambda x: (games[x]["date"], x)):
        for side, tid in (("home", games[gid]["home_id"]), ("away", games[gid]["away_id"])):
            mins = la._played(boxes[gid][side])
            tot = sum(mins.values())
            if tot > 0:
                per_team.setdefault(tid, []).append({p: m / tot for p, m in mins.items()})
    usual, counts = {}, {}
    for tid, hist in per_team.items():
        counts[tid] = len(hist)
        h = hist[-window:]
        if len(h) < min_hist:
            continue
        w = {}
        for g in h:
            for p, s in g.items():
                w[p] = w.get(p, 0.0) + s / len(h)
        usual[tid] = w
    return usual, counts


# ------------------------------------------------------------------ injuries
def _player_id(athlete):
    if athlete.get("id") not in (None, ""):
        return str(athlete["id"])
    for link in athlete.get("links") or []:
        m = re.search(r"/id/(\d+)", str(link.get("href", "")))
        if m:
            return m.group(1)
    return None


def status_to_q(status):
    s = (status or "").strip().lower()
    if s == "out" or s.startswith("out ") or "out for" in s or s in ("suspension", "suspended"):
        return 0.0
    return DTD_Q


def parse_injuries(data, name_to_id=None):
    """ESPN injuries feed -> {player_id: (q, status, name)}. Falls back to matching by display name
    when an entry has no usable id. Entries that can't be identified are counted in the second return."""
    name_to_id = name_to_id or {}
    out, unmatched = {}, 0
    for team in (data.get("injuries") or []):
        for inj in (team.get("injuries") or []):
            ath = inj.get("athlete") or {}
            pid = _player_id(ath) or name_to_id.get(ath.get("displayName"))
            if pid is None:
                unmatched += 1
                continue
            status = inj.get("status")
            q = status_to_q(status)
            if pid not in out or q < out[pid][0]:
                out[pid] = (q, status, ath.get("displayName"))
    return out, unmatched


def fetch_injuries():
    resp = requests.get(INJURIES_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------------ lineup delta
def team_delta(w, beta, injuries, max_abs=MAX_ABS_DELTA):
    """(delta, clipped?, absent list) for a team's usual-share dict w. injuries: {pid: (q, status, name)}."""
    if not w:
        return 0.0, False, []
    q = {p: (injuries[p][0] if p in injuries else 1.0) for p in w}
    den = sum(w[p] * q[p] for p in w)
    if den <= 1e-9:
        return 0.0, False, []
    exp_val = sum(beta.get(p, 0.0) * w[p] * q[p] for p in w) / den
    usual = sum(beta.get(p, 0.0) * w[p] for p in w)
    delta = exp_val - usual
    clipped = abs(delta) > max_abs
    delta = max(-max_abs, min(max_abs, delta))
    absent = sorted(({"id": p, "name": injuries[p][2], "status": injuries[p][1], "usual_share": round(w[p], 3)}
                     for p in w if q[p] < 1.0 and w[p] >= 0.03), key=lambda a: -a["usual_share"])
    return delta, clipped, absent


# ------------------------------------------------------------------ main entry points
def build_context(directory=".", season=None, game_date=None, refit=False, max_age_days=7, today=None):
    """Everything that depends only on the cached files (not on the slate or the injury feed):
    team ratings state, fitted player values, usual minute shares. A long-running service builds this
    once and rebuilds it only when the files change."""
    if season is None:
        season = mon.season_for_date(game_date or datetime.date.today())
    state = mon.build_team_state(mon.load_regular_games(season - 1, directory),
                                 mon.load_regular_games(season, directory))
    values = fit_player_values(directory, refit=refit, max_age_days=max_age_days, today=today)
    usual, counts = usual_minute_shares(season, directory)
    return {"season": season, "state": state, "values": values, "usual": usual, "counts": counts,
            "name_to_id": {n: p for p, n in values["names"].items() if n}}


def predict_from_context(ctx, events, injury_data, lines=None, gamma=GAMMA, sigma=SIGMA):
    """Pure prediction step. events = ESPN scoreboard events; injury_data = raw injuries feed JSON, or None if
    unavailable (-> rating-only, flagged). Returns (results, meta). No file or network access."""
    base = mon.predict_from_events(events, ctx["state"], ctx["season"])
    beta, usual, counts = ctx["values"]["beta"], ctx["usual"], ctx["counts"]
    inj_ok = injury_data is not None
    injuries, unmatched = (parse_injuries(injury_data, ctx["name_to_id"]) if inj_ok else ({}, 0))
    lines = lines or {}

    results = []
    for p in base:
        mu0 = p["predicted_margin"]
        row = dict(p)
        row.update({"predicted_margin_base": mu0, "home_delta": 0.0, "away_delta": 0.0, "lineup_adjustment_applied": False,
                    "lineup_note": None, "home_absent": [], "away_absent": [], "injury_feed_ok": inj_ok})
        if not p["trusted"]:
            row.update({"predicted_margin": None, "fair_home_spread": None, "home_win_probability_base": None})
            results.append(row)
            continue
        w_h, w_a = usual.get(p["home_id"]), usual.get(p["away_id"])
        notes = []
        if inj_ok and w_h and w_a:
            dh, ch, absent_h = team_delta(w_h, beta, injuries)
            da, ca, absent_a = team_delta(w_a, beta, injuries)
            row.update({"home_delta": round(dh, 2), "away_delta": round(da, 2), "lineup_adjustment_applied": True,
                        "home_absent": absent_h, "away_absent": absent_a})
            if ch or ca:
                notes.append("delta clipped at the safeguard (check the injury feed)")
        else:
            if not inj_ok:
                notes.append("injury feed unavailable")
            for side, w, tid in (("home", w_h, p["home_id"]), ("away", w_a, p["away_id"])):
                if not w:
                    notes.append(f"{side} has {counts.get(tid, 0)} boxscore(s) this season (<{MIN_HIST}); no adjustment")
        mu = mu0 + gamma * (row["home_delta"] - row["away_delta"])
        row["predicted_margin"] = round(mu, 2)
        row["fair_home_spread"] = round(-mu, 2)
        row["home_win_probability"] = round(mon.norm_cdf(mu / sigma), 4)
        row["away_win_probability"] = round(1 - row["home_win_probability"], 4)
        row["home_win_probability_base"] = round(mon.norm_cdf(mu0 / sigma), 4)
        row["sigma"] = sigma
        row["gamma"] = gamma
        row["lineup_note"] = "; ".join(notes) or None
        s = lines.get(p["game_id"], lines.get(f"{p['away_team']} @ {p['home_team']}"))
        if s is not None:
            hc = home_covers_probability(mu, s, sigma)
            row.update({"home_spread": s, "home_covers_probability": round(hc, 4),
                        "away_covers_probability": round(1 - hc, 4), "model_edge_points": round(mu + s, 2)})
        results.append(row)
    return results, {"unmatched_injuries": unmatched, "injuries_parsed": len(injuries), "inj_ok": inj_ok,
                     "values_fitted_through": ctx["values"]["fitted_through"]}


def predict_spread_slate(game_date, directory=".", refresh=True, lines=None, fetch_day=None, collect=None,
                         fetch_injuries_fn=None, fetch_summary=None, refit=False, max_age_days=7,
                         gamma=GAMMA, sigma=SIGMA, quiet=True):
    """One-shot (CLI) entry point: optionally refresh data, then build context, fetch the slate and injuries, predict."""
    if isinstance(game_date, str):
        game_date = datetime.datetime.strptime(game_date, "%Y%m%d").date()
    season = mon.season_for_date(game_date)
    fetch_day = fetch_day or mon.collector.get_day
    if refresh:
        kw = {"fetch": fetch_summary} if fetch_summary else {}
        collect = collect or mon.collector.collect_season
        collect(season, directory=directory)
        collect(season - 1, directory=directory)
        bx.collect_season(season, directory, **kw)
    ctx = build_context(directory, season, refit=refit, max_age_days=max_age_days)
    try:
        inj_data = (fetch_injuries_fn or fetch_injuries)()
    except Exception as e:  # network/parse failure: fall back to NO adjustment, loudly
        print(f"[WARN] injuries feed unavailable ({e}); lineup adjustment disabled for this run.")
        inj_data = None
    return predict_from_context(ctx, fetch_day(game_date).get("events") or [], inj_data, lines, gamma, sigma)


def _signature(rec):
    return (rec.get("predicted_margin"), rec.get("home_delta"), rec.get("away_delta"), rec.get("home_spread"),
            rec.get("lineup_adjustment_applied"))


def log_predictions(results, directory=".", now=None, dedupe=False, last_sigs=None, skip_started=False):
    """Append trusted predictions to the tracking log. With dedupe=True a game is only re-logged when its
    prediction (margin / lineup deltas / line) changed since the last logged one (last_sigs: {game_id: signature},
    updated in place -- a service keeps it between calls). skip_started=True skips games whose tip-off has passed
    (so in-progress games are never logged). Returns (path, number of records written)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    path = os.path.join(directory, LOG_FILE)
    keep = ("game_id", "date", "season", "home_team", "away_team", "home_id", "away_id", "trusted", "predicted_margin_base",
            "predicted_margin", "home_delta", "away_delta", "lineup_adjustment_applied", "sigma", "gamma",
            "home_spread", "home_covers_probability", "home_win_probability", "home_win_probability_base")
    lines = []
    for r in results:
        if not r["trusted"]:
            continue
        if skip_started and has_started(r, now):
            continue
        rec = {k: r.get(k) for k in keep}
        sig = _signature(rec)
        if dedupe and last_sigs is not None and last_sigs.get(rec["game_id"]) == sig:
            continue
        rec["logged_at"] = now.isoformat(timespec="seconds")
        lines.append(json.dumps(rec) + "\n")
        if last_sigs is not None:
            last_sigs[rec["game_id"]] = sig
    if lines:
        with open(path, "a") as f:
            f.write("".join(lines))   # one write call per batch
    return path, len(lines)


def tip_off(rec):
    """Tip-off as an aware UTC datetime from the scoreboard date field, or None if it has no time part."""
    d = rec.get("date")
    if not d or "T" not in str(d):
        return None
    try:
        t = datetime.datetime.fromisoformat(str(d).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def has_started(rec, now):
    t = tip_off(rec)
    return t is not None and now >= t


def load_last_signatures(directory="."):
    """Last logged signature per game, so a restarted service doesn't re-log unchanged predictions."""
    path = os.path.join(directory, LOG_FILE)
    sigs = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue   # a partially written last line
                sigs[rec["game_id"]] = _signature(rec)
    return sigs


def print_table(results, meta):
    if not results:
        print("\nNo not-yet-played regular-season games found for that date.")
        return
    print(f"\ninjury feed: {meta['injuries_parsed']} player(s) parsed, {meta['unmatched_injuries']} unidentifiable; "
          f"player values fitted through {meta['values_fitted_through']}")
    head = f"{'matchup':<42} {'base':>6} {'lineup':>7} {'margin':>7} {'home%':>6}  {'line':>6} {'cover%':>7} {'edge':>6}"
    print("\n" + head + "\n" + "-" * len(head))
    for r in results:
        m = f"{r['away_team']} @ {r['home_team']}"[:42]
        if not r["trusted"]:
            print(f"{m:<42} {'--':>6} (untrusted: not enough history)")
            continue
        adj = r["gamma"] * (r["home_delta"] - r["away_delta"])
        line = f"{r['home_spread']:>+6.1f} {r['home_covers_probability'] * 100:>6.1f}% {r['model_edge_points']:>+6.1f}" \
            if "home_spread" in r else f"{'--':>6} {'--':>7} {'--':>6}"
        print(f"{m:<42} {r['predicted_margin_base']:>+6.1f} {adj:>+7.2f} {r['predicted_margin']:>+7.1f} "
              f"{r['home_win_probability'] * 100:>5.1f}%  {line}")
        for side in ("home", "away"):
            for a in r[f"{side}_absent"][:3]:
                print(f"{'':>4}{side} {a['name']} ({a['status']}, usual {a['usual_share'] * 100:.0f}% of minutes)")
        if r["lineup_note"]:
            print(f"{'':>4}note: {r['lineup_note']}")
    print("\nbase = rating-only margin; lineup = adjustment from availability; margin = home minus away. "
          "SHADOW MODE: track before betting (see docstring).")


def _load_lines(path):
    if not path:
        return {}
    if not os.path.exists(path):
        raise SystemExit(f"Lines file '{path}' not found. Lines are optional: omit --lines-file to run without them, or create a "
                         f"JSON file like {{\"Boston Celtics @ New York Knicks\": -5.5}} (home spread; -5.5 = home favored).")
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Live NBA margin/spread/win probability with lineup adjustment (shadow mode)")
    ap.add_argument("--date", default=None, help="YYYYMMDD (default today)")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--lines-file", default=None, help="JSON {game_id or 'Away @ Home': home_spread}")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--refit", action="store_true", help="Force a refit of player values")
    ap.add_argument("--max-age-days", type=int, default=7)
    ap.add_argument("--no-log", action="store_true", help="Do not append to nba_prediction_log.jsonl")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    date_str = args.date or datetime.date.today().strftime("%Y%m%d")
    results, meta = predict_spread_slate(date_str, directory=args.dir, refresh=not args.no_refresh,
                                         lines=_load_lines(args.lines_file), refit=args.refit,
                                         max_age_days=args.max_age_days)
    out = args.out or os.path.join(args.dir, f"nba_spread_predictions_{date_str}.json")
    atomic_json_dump(out, results)
    print_table(results, meta)
    print(f"\nWrote {len(results)} prediction(s) to {out}")
    if not args.no_log:
        path, n = log_predictions(results, args.dir, dedupe=True, last_sigs=load_last_signatures(args.dir), skip_started=True)
        print(f"Appended {n} new/changed prediction(s) to tracking log {path} (used by nba_track_lineup_results.py)")


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: backtest_nba_player_props.py (698 lines)
# ======================================================================
_SRC["backtest_nba_player_props"] = r'''"""
backtest_nba_player_props.py

Walk-forward, no-leakage backtest of NBA player props: POINTS, REBOUNDS, ASSISTS and PRA (their sum).

THE FORMULA (as specified):
    predicted = player's index for the prop  x  opposing team's index allowed to the player's POSITION  x  league average
made concrete, per player-game, in the same shrinkage framework as every other model in this project:

    player index   = (player's stat per minute, shrunk toward the league rate of his position group) / league rate
    opp index      = what the opposing team allows to that position group, vs league (shrunk toward 1), tuned exponent gamma
    league average = league stat-per-minute of the position group
    minutes        = recency-weighted, shrunk expected minutes (the missing link between a RATE and a game total)
    mean           = minutes x league rate x player index x opp index^gamma
    distribution   = Negative Binomial around that mean, dispersion r tuned per prop (counts are over-dispersed)
    PRA            = tested two ways: SUM of the three component means, vs its own direct index.

What is compared (validate seasons only, parameters tuned on tune seasons only):
    model            the formula above
    last-10 average  what a casual line-setter would use (unshrunk trailing 10 games)
    prior only       league rate x projected minutes (no player information)
    ablations        remove each piece (opp index / shrinkage / minute recency / season carry-over / position groups)
Because there are no historical prop LINES here, "proxy lines" (the half-point nearest each player's trailing-10 average)
stand in for a book's line when checking P(over) calibration. That is a stand-in, NOT the market: log real lines before
trusting any edge.

Needs nba_games_{season}.json (nba_collect_games.py) and the v2 nba_boxscores_{season}.json
(python nba_collect_boxscores.py --seasons ... --upgrade, which adds rebounds/assists to old caches).

Usage:
    python backtest_nba_player_props.py --dir . --seasons 2022,2023,2024,2025,2026 --tune-seasons 2023,2024 ^
        --validate-seasons 2025,2026 [--quick] [--out nba_prop_params.json]
    python backtest_nba_player_props.py --dir . --seasons 2022,2023,2024,2025,2026 --diagnose     (position labels etc.)
"""

import argparse
import json
import math
import os
import sys
from collections import deque

import numpy as np
from scipy.special import gammaln
from scipy.stats import nbinom, norm

STATS = ["pts", "reb", "ast", "pra"]
COL = {"pts": 10, "reb": 11, "ast": 12, "pra": 13}
LG0 = {"pts": 0.46, "reb": 0.17, "ast": 0.105, "pra": 0.735}      # per minute, only before any data exists
LG0_MIN = 22.0
THREE = {"PG": "G", "SG": "G", "G": "G", "SF": "F", "PF": "F", "F": "F", "GF": "F", "FC": "C", "C": "C"}
R_GRID = [3, 5, 8, 12, 20, 35, 60, 120, 300]
MIN_PRIOR = 10            # games of history before a prediction counts as "trusted"
MIN_GATE = 15.0           # projected minutes below this are not prop-relevant players

DEFAULTS = {"form": "min", "opp": "resid", "groups": "three", "k_p": 100.0, "rd": 0.97, "k_opp": 3000.0,
            "gamma": 1.0, "carry": 0.6, "decay_m": 0.8, "k_m": 0.5, "window": 20, "kappa": 1.0, "scale": 1.0, "cap0": 99.0, "capc": 0.5}
GRIDS = {"k_p": [1, 3, 10, 30, 100, 300, 800], "rd": [0.90, 0.93, 0.96, 0.98, 0.99, 1.0],
         "k_opp": [500, 1500, 3000, 6000, 12000],
         "gamma": [0.0, 0.5, 1.0, 1.5], "carry": [0.0, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0],
         "opp": ["raw", "resid"], "groups": ["none", "three", "raw"]}
GAME_GRIDS = {"k_p": [2, 4, 8, 16, 32]}
QUICK_GRIDS = {"k_p": [10, 100], "rd": [0.95, 1.0], "k_opp": [1500, 6000],
               "gamma": [0.0, 1.0], "carry": [0.4, 0.8], "opp": ["raw", "resid"], "groups": ["none", "three"]}
MIN_GRIDS = {"decay_m": [0.5, 0.6, 0.7, 0.8, 0.9, 1.0], "k_m": [0.02, 0.05, 0.1, 0.25, 0.5, 1, 2],
             "cap0": [99, 36, 34, 32, 30, 28], "capc": [0.25, 0.5, 0.75]}
TUNE_ORDER = ["k_p", "rd", "kappa", "scale", "k_opp", "gamma", "carry", "opp", "groups"]   # kappa/scale only tuned if a grid is given
PHI_GRID = [1.1, 1.3, 1.6, 2.0, 2.5, 3.0, 4.0, 5.0, 6.5, 8.0, 10.0, 13.0]
Q1_GRID = [-0.7, -0.55, -0.4, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.45]
W_GRID = [1.0, 0.75, 0.5, 0.25, 0.0]       # share of Negative Binomial vs discretised Normal in the count distribution


# ===================================================================== data
def load_rows(directory, seasons):
    """-> (days: list of (date, [row...]) chronological, report dict). A row is
    (date, season, gid, pid, tid, oid, pos, min, starter, home, pts, reb, ast, pra)."""
    by_date = {}
    rep = {"games": 0, "old_format": 0, "missing_game": 0, "rows": 0}
    for season in seasons:
        gp = os.path.join(directory, f"nba_games_{season}.json")
        bp = os.path.join(directory, f"nba_boxscores_{season}.json")
        if not (os.path.exists(gp) and os.path.exists(bp)):
            print(f"[skip {season}] {gp} or {bp} not found")
            continue
        with open(gp) as f:
            games = json.load(f)["games"]
        with open(bp) as f:
            box = json.load(f)["games"]
        for gid, rec in box.items():
            if rec.get("v", 1) < 2:
                rep["old_format"] += 1
                continue
            g = games.get(gid)
            if g is None:
                rep["missing_game"] += 1
                continue
            rep["games"] += 1
            date = str(rec.get("date") or g.get("date"))[:10]
            for side, opp, home in (("home", "away", 1), ("away", "home", 0)):
                tid, oid = str(g[f"{side}_id"]), str(g[f"{opp}_id"])
                for p in rec[side]:
                    m = p.get("min")
                    if p.get("dnp") or not m or m <= 0:
                        continue
                    if p.get("pts") is None or p.get("reb") is None or p.get("ast") is None:
                        continue
                    by_date.setdefault(date, []).append(
                        (date, season, gid, str(p["id"]), tid, oid, p.get("pos"), float(m), int(bool(p.get("starter"))),
                         home, p["pts"], p["reb"], p["ast"], p["pts"] + p["reb"] + p["ast"]))
                    rep["rows"] += 1
    days = [(d, sorted(rows, key=lambda r: (r[2], r[3]))) for d, rows in sorted(by_date.items())]
    return days, rep


# ===================================================================== state
def group_of(label, mode):
    if mode == "none":
        return "ALL"
    t = THREE.get(label or "", None)
    if mode == "three":
        return t or "ALL"
    return label or "NA"          # 'raw'


class PState:
    def __init__(self, P, stat):
        self.P, self.stat = P, stat
        self.season = None
        self.pl = {}        # pid -> [S, M, G]  (decayed)
        self.nprior = {}
        self.hmin = {}
        self.hstat = {}
        self.pos = {}
        self.lg = {}        # group -> [S, M, G]
        self.lgall = [0.0, 0.0, 0.0]
        self.opp = {}       # (tid, group) -> [A, M, G, R, E]

    def rollover(self, season):
        if self.season is not None and season != self.season:
            c = self.P["carry"]
            for d in (self.pl, self.lg, self.opp):
                for v in d.values():
                    for i in range(len(v)):
                        v[i] *= c
            self.lgall = [x * c for x in self.lgall]
        self.season = season

    def lg_min(self):
        return self.lgall[1] / self.lgall[2] if self.lgall[2] > 50 else LG0_MIN

    def lg_rate(self, g):
        v = self.lg.get(g)
        if v is not None and v[1] > 2000:
            return v[0] / v[1]
        if self.lgall[1] > 2000:
            return self.lgall[0] / self.lgall[1]
        return LG0[self.stat]

    def lg_pg(self, g):
        v = self.lg.get(g)
        if v is not None and v[2] > 100:
            return v[0] / v[2]
        if self.lgall[2] > 100:
            return self.lgall[0] / self.lgall[2]
        return LG0[self.stat] * LG0_MIN

    def group(self, pid, label):
        lab = self.pos.get(pid) or label
        g = group_of(lab, self.P["groups"])
        if self.P["groups"] == "raw":
            v = self.lg.get(g)
            if v is None or v[1] < 3000:
                g = group_of(lab, "three")
        return g

    def emin(self, pid):
        h = list(self.hmin.get(pid, ()))[-self.P["window"]:]
        num = den = 0.0
        w = 1.0
        for v in reversed(h):
            num += w * v
            den += w
            w *= self.P["decay_m"]
        lm = self.lg_min()
        em = (num + self.P["k_m"] * lm) / (den + self.P["k_m"])
        c0 = self.P["cap0"]
        if em > c0:
            em -= self.P["capc"] * (em - c0)       # soft cap: heavy-minute players rarely sustain their recent peak (rest, blowouts)
        return em

    def predict(self, row, minutes_only=False):
        pid = row[3]
        em = self.emin(pid)
        if minutes_only:
            return {"emin": em, "nprior": self.nprior.get(pid, 0)}
        P = self.P
        g = self.group(pid, row[6])
        S, M, G = self.pl.get(pid, (0.0, 0.0, 0.0))
        if P["form"] == "min":
            lgr = self.lg_rate(g)
            rate = (S + P["k_p"] * lgr) / (M + P["k_p"])
            pre = rate                                   # expected stat per minute before the opponent
            base = em * lgr * ((rate / lgr) ** P["kappa"]) * P["scale"]
        else:
            lgp = self.lg_pg(g)
            rate = (S + P["k_p"] * lgp) / (G + P["k_p"])
            pre = rate
            base = lgp * ((rate / lgp) ** P["kappa"]) * P["scale"]
        idx = 1.0
        o = self.opp.get((row[5], g))
        if P["gamma"] != 0 and o is not None:
            if P["form"] == "min":
                if P["opp"] == "raw":
                    idx = ((o[0] + P["k_opp"] * lgr) / (o[1] + P["k_opp"])) / lgr
                else:
                    idx = (o[3] + P["k_opp"] * lgr) / (o[4] + P["k_opp"] * lgr)
            else:
                kg = P["k_opp"] / max(self.lg_min(), 1.0)
                if P["opp"] == "raw":
                    idx = ((o[0] + kg * lgp) / (o[2] + kg)) / lgp
                else:
                    idx = (o[3] + kg * lgp) / (o[4] + kg * lgp)
        idx = min(max(idx, 0.5), 2.0)
        mu = base * (idx ** P["gamma"])
        h = list(self.hstat.get(pid, ()))
        t10 = sum(h[-10:]) / len(h[-10:]) if len(h) >= 5 else float("nan")
        savg = S / G if G >= 3 else float("nan")
        prior = (em * self.lg_rate(g)) if P["form"] == "min" else self.lg_pg(g)
        return {"mu": mu, "emin": em, "nprior": self.nprior.get(pid, 0), "t10": t10, "savg": savg, "prior": prior,
                "pre": pre, "g": g, "opp_idx": idx}

    def update(self, row, pr, minutes_only=False):
        pid = row[3]
        m = row[7]
        self.hmin.setdefault(pid, deque(maxlen=20)).append(m)
        self.nprior[pid] = self.nprior.get(pid, 0) + 1
        self.pos[pid] = row[6] or self.pos.get(pid)
        self.lgall[0] += row[COL[self.stat]]
        self.lgall[1] += m
        self.lgall[2] += 1
        if minutes_only:
            return
        y = row[COL[self.stat]]
        self.hstat.setdefault(pid, deque(maxlen=20)).append(y)
        rd = self.P["rd"]
        v = self.pl.setdefault(pid, [0.0, 0.0, 0.0])
        v[0] = v[0] * rd + y
        v[1] = v[1] * rd + m
        v[2] = v[2] * rd + 1
        g = pr["g"]
        lg = self.lg.setdefault(g, [0.0, 0.0, 0.0])
        lg[0] += y
        lg[1] += m
        lg[2] += 1
        o = self.opp.setdefault((row[5], g), [0.0, 0.0, 0.0, 0.0, 0.0])
        o[0] += y
        o[1] += m
        o[2] += 1
        o[3] += y
        o[4] += (m * pr["pre"]) if self.P["form"] == "min" else pr["pre"]


def walk_forward(days, stat, P, stop_date=None, minutes_only=False):
    """Predict every player-game BEFORE its date's games update the state. Returns dict of aligned numpy arrays."""
    st = PState(P, stat)
    keys = ["mu", "emin", "nprior", "t10", "savg", "prior", "opp_idx"]
    cols = {k: [] for k in keys}
    meta = {"season": [], "y": [], "min": [], "date": [], "pid": [], "oid": [], "g": [], "starter": []}
    for date, rows in days:
        if stop_date is not None and date > stop_date:
            break
        st.rollover(rows[0][1])
        preds = [st.predict(r, minutes_only) for r in rows]
        for r, p in zip(rows, preds):
            for k in keys:
                cols[k].append(p.get(k, float("nan")))
            meta["season"].append(r[1])
            meta["y"].append(r[COL[stat]])
            meta["min"].append(r[7])
            meta["date"].append(date)
            meta["pid"].append(r[3])
            meta["oid"].append(r[5])
            meta["g"].append(p.get("g", ""))
            meta["starter"].append(r[8])
        for r, p in zip(rows, preds):
            st.update(r, p, minutes_only)
    out = {k: np.array(v, dtype=float) for k, v in cols.items()}
    out["season"] = np.array(meta["season"])
    out["y"] = np.array(meta["y"], dtype=float)
    out["min"] = np.array(meta["min"], dtype=float)
    out["date"] = np.array(meta["date"])
    out["pid"] = np.array(meta["pid"])
    out["oid"] = np.array(meta["oid"])
    out["g"] = np.array(meta["g"])
    out["starter"] = np.array(meta["starter"])
    return out


# ===================================================================== scoring
def nb_ll(y, mu, r):
    mu = np.maximum(mu, 0.05)
    return (gammaln(y + r) - gammaln(r) - gammaln(y + 1) + r * np.log(r / (r + mu)) + y * np.log(mu / (r + mu)))


def nb_over(line_floor, mu, r):
    mu = np.maximum(mu, 0.05)
    return 1.0 - nbinom.cdf(line_floor, r, r / (r + mu))


def var_of(disp, mu):
    """Variance of the count given its mean. A plain number = classic NB size r (var = mu + mu^2/r);
    {'kind':'nb2','r'}; or {'kind':'pow','phi','q1'}: var = phi * mu * (mu/10)^q1 (variance-to-mean phi at mean 10)."""
    mu = np.maximum(mu, 0.05)
    if not isinstance(disp, dict):
        return mu + mu * mu / float(disp)
    if disp["kind"] == "nb2":
        return mu + mu * mu / float(disp["r"])
    return np.maximum(disp["phi"] * mu * (mu / 10.0) ** disp["q1"], 1.02 * mu)


def r_of(disp, mu):
    mu = np.maximum(mu, 0.05)
    v = var_of(disp, mu)
    return mu * mu / (v - mu)


def _norm_parts(mu, var):
    sd = np.sqrt(var)
    den = np.maximum(1.0 - norm.cdf((-0.5 - mu) / sd), 1e-12)
    return sd, den


def nll_with(y, mu, disp):
    """Per-row negative log-likelihood. Count distribution = w x Negative Binomial + (1-w) x discretised Normal
    (same mean and variance); the Normal share lets the shape be less right-skewed than a pure NB (points)."""
    mu = np.maximum(mu, 0.05)
    var = var_of(disp, mu)
    w = disp.get("w", 1.0) if isinstance(disp, dict) else 1.0
    ll_nb = nb_ll(y, mu, mu * mu / (var - mu))
    if w >= 1.0:
        return -ll_nb
    sd, den = _norm_parts(mu, var)
    pn = (norm.cdf((y + 0.5 - mu) / sd) - norm.cdf((y - 0.5 - mu) / sd)) / den
    ll_n = np.log(np.maximum(pn, 1e-300))
    if w <= 0.0:
        return -ll_n
    return -np.logaddexp(np.log(w) + ll_nb, np.log(1 - w) + ll_n)


def over_with(fl, mu, disp):
    """P(count > fl) under the same mixture."""
    mu = np.maximum(mu, 0.05)
    var = var_of(disp, mu)
    w = disp.get("w", 1.0) if isinstance(disp, dict) else 1.0
    p_nb = nb_over(fl, mu, mu * mu / (var - mu))
    if w >= 1.0:
        return p_nb
    sd, den = _norm_parts(mu, var)
    p_n = (1.0 - norm.cdf((fl + 0.5 - mu) / sd)) / den
    return w * p_nb + (1 - w) * p_n


def fit_dispersion(y, mu):
    """Best spread/shape on these rows: classic NB vs (variance-power, NB/Normal mix). -> dict with both fits and the winner."""
    mu = np.maximum(mu, 0.05)
    nb = min(((float(nll_with(y, mu, {"kind": "nb2", "r": r}).mean()), r) for r in R_GRID))
    best = None
    for w in W_GRID:
        for phi in PHI_GRID:
            for q1 in Q1_GRID:
                d = {"kind": "pow", "phi": phi, "q1": q1, "w": w}
                v = float(nll_with(y, mu, d).mean())
                if best is None or v < best[0]:
                    best = (v, d)
    nb2 = {"kind": "nb2", "r": nb[1]}
    win = best[1] if best[0] < nb[0] - 1e-4 else nb2
    return {"nb2": nb2, "nb2_nll": nb[0], "pow": best[1], "pow_nll": best[0], "use": win}


def gate(preds, seasons=None):
    m = (preds["nprior"] >= MIN_PRIOR) & (preds["emin"] >= MIN_GATE)
    if seasons is not None:
        m &= np.isin(preds["season"], seasons)
    return m


def profile_nll(y, mu):
    """Best classic-NB dispersion on these rows: -> (mean NLL, r). Used while tuning the mean."""
    best = None
    for r in R_GRID:
        v = float(nll_with(y, mu, {"kind": "nb2", "r": r}).mean())
        if best is None or v < best[0]:
            best = (v, r)
    return best


def paired(a, b):
    d = a - b
    return float(d.mean()), float(d.std(ddof=1) / math.sqrt(len(d)))


def tune_minutes(days, tune_seasons, base, grids, quiet=False):
    stop = max(r[0] for d, rows in days for r in rows[:1] if r[1] in tune_seasons)
    P = dict(base)

    def obj(Q):
        pr = walk_forward(days, "pts", Q, stop, minutes_only=True)
        m = gate(pr, tune_seasons) | ((pr["nprior"] >= MIN_PRIOR) & np.isin(pr["season"], tune_seasons))
        return float(np.sqrt(((pr["emin"][m] - pr["min"][m]) ** 2).mean()))     # RMSE: a prop needs the MEAN right, not the median
    best = obj(P)
    if not quiet:
        print(f"  minutes start: RMSE {best:.4f} min (decay_m={P['decay_m']} k_m={P['k_m']})")
    for _ in range(2):
        for key, grid in grids.items():
            vals = {v: obj(dict(P, **{key: v})) for v in grid}
            v = min(vals, key=vals.get)
            P[key] = v
            best = vals[v]
            if not quiet:
                print(f"  minutes {key:8} -> {v}   RMSE {best:.4f}  (grid {min(vals.values()):.4f}..{max(vals.values()):.4f})")
    return P, best


def tune_stat(days, tune_seasons, stat, base, grids, passes=2, quiet=False):
    stop = max(r[0] for d, rows in days for r in rows[:1] if r[1] in tune_seasons)
    P = dict(base)
    cache = {}

    def obj(Q):
        key = tuple(sorted(Q.items()))
        if key not in cache:
            pr = walk_forward(days, stat, Q, stop)
            m = gate(pr, tune_seasons)
            cache[key] = profile_nll(pr["y"][m], pr["mu"][m])
        return cache[key]
    cur = obj(P)
    if not quiet:
        print(f"  {stat} start: NLL {cur[0]:.5f} (r={cur[1]})  " + " ".join(f"{k}={P[k]}" for k in TUNE_ORDER))
    edge_notes = []
    for ps in range(passes):
        for key in TUNE_ORDER:
            if key not in grids:
                continue
            vals = {v: obj(dict(P, **{key: v})) for v in grids[key]}
            v = min(vals, key=lambda x: vals[x][0])
            P[key] = v
            cur = vals[v]
            if not quiet:
                lo, hi = min(x[0] for x in vals.values()), max(x[0] for x in vals.values())
                print(f"  {stat} pass {ps + 1} {key:6} -> {v}   NLL {cur[0]:.5f}  (grid {lo:.5f}..{hi:.5f})")
            g = grids[key]
            if ps == passes - 1 and isinstance(v, (int, float)) and len(g) > 2 and v in (g[0], g[-1]) and (max(x[0] for x in vals.values()) - min(x[0] for x in vals.values())) > 0.0005:
                edge_notes.append(f"{key}={v} is at the grid edge")
    if edge_notes and not quiet:
        print(f"  WARNING ({stat}): " + "; ".join(edge_notes) + " -- widen the grid before trusting that value")
    return P, cur[1], cur[0]


# ===================================================================== reports
def disp_text(d):
    d = d["use"] if isinstance(d, dict) and "use" in d else d
    if not isinstance(d, dict):
        return f"NB r={d}"
    if d["kind"] == "nb2":
        return f"NB r={d['r']}"
    return f"variance = {d['phi']} x mean x (mean/10)^{d['q1']}, shape = {d.get('w', 1.0):.2f} NB + {1 - d.get('w', 1.0):.2f} Normal"


def calib_buckets(preds, m, title="", n_buckets=6):
    mu, y = preds["mu"][m], preds["y"][m]
    order = np.argsort(mu)
    size = len(order) // n_buckets
    print(f"\n{title}\n{'predicted mean':>18} {'n':>6} {'avg pred':>9} {'avg actual':>10} {'bias':>7} {'var/mean':>9}")
    for b in range(n_buckets):
        idx = order[b * size:(b + 1) * size] if b < n_buckets - 1 else order[b * size:]
        a = y[idx].mean()
        print(f"{mu[idx[0]]:>8.2f} - {mu[idx[-1]]:>6.2f} {len(idx):>6} {mu[idx].mean():>9.2f} {a:>10.2f} {a - mu[idx].mean():>+7.2f} "
              f"{y[idx].var(ddof=1) / max(a, 1e-9):>9.2f}")
    for s_ in sorted(set(preds["season"][m].tolist())):
        ms = m & (preds["season"] == s_)
        print(f"  season {s_}: bias {preds['y'][ms].mean() - preds['mu'][ms].mean():+.3f} on a mean of {preds['y'][ms].mean():.2f} (n={int(ms.sum())})")


def minutes_report(preds, m):
    em, mn = preds["emin"][m], preds["min"][m]
    order = np.argsort(em)
    n_b = 6
    size = len(order) // n_b
    print(f"\nMINUTES: projected vs actual (shared by every prop), MAE {np.abs(em - mn).mean():.2f} min, RMSE {np.sqrt(((em - mn) ** 2).mean()):.2f}")
    print(f"{'projected min':>18} {'n':>6} {'avg proj':>9} {'avg actual':>10} {'bias':>7}")
    for b in range(n_b):
        idx = order[b * size:(b + 1) * size] if b < n_b - 1 else order[b * size:]
        print(f"{em[idx[0]]:>8.1f} - {em[idx[-1]]:>6.1f} {len(idx):>6} {em[idx].mean():>9.2f} {mn[idx].mean():>10.2f} {mn[idx].mean() - em[idx].mean():>+7.2f}")


def proxy_line_report(preds, m, disp, stat):
    ok = m & ~np.isnan(preds["t10"])
    y, mu, t10 = preds["y"][ok], preds["mu"][ok], preds["t10"][ok]
    line = np.floor(t10) + 0.5
    fl = np.floor(line)
    over = (y > line).astype(float)
    p_model = np.clip(over_with(fl, mu, disp), 1e-4, 1 - 1e-4)
    p_base = np.clip(over_with(fl, t10, disp), 1e-4, 1 - 1e-4)
    ll = lambda p: -(over * np.log(p) + (1 - over) * np.log(1 - p))
    d, se = paired(ll(p_base), ll(p_model))
    print(f"\n--- {stat.upper()} on PROXY lines (half-point nearest each player's trailing-10 average; NOT market lines), n={len(y)}")
    print(f"  over rate {over.mean() * 100:.1f}%;  log loss: last-10 baseline {ll(p_base).mean():.4f}  model {ll(p_model).mean():.4f}"
          f"  -> model gain {d:+.4f} +/- {se:.4f} ({'real' if d > 2 * se else 'not clearly better'})")
    print(f"  {'model P(over)':>16} {'n':>6} {'predicted':>10} {'actual over':>12}")
    edges = [0, 0.30, 0.40, 0.50, 0.60, 0.70, 1.01]
    for lo, hi in zip(edges[:-1], edges[1:]):
        s = (p_model >= lo) & (p_model < hi)
        if s.sum() >= 30:
            print(f"  {f'{lo:.2f}-{min(hi, 1):.2f}':>16} {int(s.sum()):>6} {p_model[s].mean() * 100:>9.1f}% {over[s].mean() * 100:>11.1f}%")


def evaluate_stat(days, stat, P, disp, val_seasons):
    preds = walk_forward(days, stat, P)
    m = gate(preds, val_seasons)
    y, mu = preds["y"][m], preds["mu"][m]
    out = {"preds": preds, "mask": m, "nll_model": nll_with(y, mu, disp)}
    ok = m & ~np.isnan(preds["t10"])
    out["nll_t10"] = nll_with(preds["y"][ok], preds["t10"][ok], disp)
    out["nll_model_on_t10rows"] = nll_with(preds["y"][ok], preds["mu"][ok], disp)
    out["nll_prior"] = nll_with(y, preds["prior"][m], disp)
    out["mae"] = {"model": float(np.abs(y - mu).mean()), "t10": float(np.abs(preds["y"][ok] - preds["t10"][ok]).mean()),
                  "prior": float(np.abs(y - preds["prior"][m]).mean()), "model_on_t10rows": float(np.abs(preds["y"][ok] - preds["mu"][ok]).mean())}
    return out


def ablations(days, stat, P, disp, val_seasons, quiet=False):
    base = walk_forward(days, stat, P)
    m = gate(base, val_seasons)
    y = base["y"][m]
    ll0 = nll_with(y, base["mu"][m], disp)
    rows = []
    tests = [("opposing-team index (gamma -> 0)", {"gamma": 0.0}),
             ("player shrinkage (k_p -> tiny = raw rate)", {"k_p": 1e-6}),
             ("minutes recency (decay_m -> 1.0)", {"decay_m": 1.0}),
             ("season carry-over (carry -> 0)", {"carry": 0.0}),
             ("in-season rate recency (rd -> 1.0)", {"rd": 1.0}),
             ("star stretch (kappa -> 1.0)", {"kappa": 1.0}),
             ("level scale (scale -> 1.0)", {"scale": 1.0}),
             ("position groups (-> one group)", {"groups": "none"}),
             (f"opp index mode ({'raw' if P['opp'] == 'resid' else 'resid'} instead of {P['opp']})", {"opp": "raw" if P["opp"] == "resid" else "resid"}),
             (f"form ({'game' if P['form'] == 'min' else 'min'} instead of {P['form']})",
              {"form": "game" if P["form"] == "min" else "min", "k_p": 8.0 if P["form"] == "min" else 100.0})]
    for name, change in tests:
        Q = dict(P, **change)
        pr = walk_forward(days, stat, Q)
        ll1 = nll_with(y, pr["mu"][m], disp)
        d, se = paired(ll1, ll0)
        rows.append((name, d, se))
    if not quiet:
        print(f"\nWHAT EACH PIECE IS WORTH for {stat.upper()} (validate; NLL increase when the piece is removed; positive = piece helps)")
        for name, d, se in rows:
            tag = "piece helps" if d > 2 * se else ("piece HURTS" if d < -2 * se else "no clear value")
            print(f"  {name:48} {d:+.5f} +/- {se:.5f}   ({tag})")
    return rows


def opp_table(days, stat, P, top=5):
    st = PState(P, stat)
    for date, rows in days:
        st.rollover(rows[0][1])
        preds = [st.predict(r) for r in rows]
        for r, p in zip(rows, preds):
            st.update(r, p)
    grp = sorted({k[1] for k in st.opp})
    print(f"\nOPPONENT INDEX (allowed to position group, {stat.upper()}, current state; >1 = allows more than league; team ids as ESPN numbers)")
    for g in grp:
        lgr = st.lg_rate(g)
        items = []
        for (t, gg), o in st.opp.items():
            if gg != g or o[1] < 500:
                continue
            idx = ((o[0] + P["k_opp"] * lgr) / (o[1] + P["k_opp"])) / lgr if P["opp"] == "raw" else (o[3] + P["k_opp"] * lgr) / (o[4] + P["k_opp"] * lgr)
            items.append((idx, t))
        items.sort()
        if items:
            print(f"  {g:>4}: lowest " + ", ".join(f"{t}:{i:.3f}" for i, t in items[:top]) + " | highest " + ", ".join(f"{t}:{i:.3f}" for i, t in items[-top:][::-1]))


def diagnose(days):
    from collections import Counter
    labels = Counter()
    per_player = {}
    mins = Counter()
    for date, rows in days:
        for r in rows:
            labels[r[6]] += 1
            per_player.setdefault(r[3], set()).add(r[6])
            mins[r[6]] += r[7]
    print("\nPOSITION LABELS seen in the boxscores (player-games, minutes) and how the 'three' grouping maps them:")
    for lab, n in labels.most_common():
        print(f"  {str(lab):>6}: {n:>7} player-games {mins[lab]:>10.0f} min  -> {THREE.get(lab or '', 'ALL (unmapped)')}")
    multi = sum(1 for s in per_player.values() if len(s) > 1)
    print(f"  {multi}/{len(per_player)} players carry more than one distinct label across games"
          + ("  (labels vary by game: positions are read from each player's last seen label)" if multi else "  (labels are stable per player)"))
    n = sum(len(rows) for d, rows in days)
    print(f"  {n} player-games over {len(days)} game dates; first {days[0][0]} last {days[-1][0]}")


def run(days, tune_seasons, val_seasons, quick=False, stats=None, quiet=False):
    stats = stats or STATS
    grids = QUICK_GRIDS if quick else GRIDS
    print("=" * 78 + "\nMINUTES MODEL (shared by every prop)\n" + "=" * 78)
    mp, mae = tune_minutes(days, tune_seasons, DEFAULTS, {"decay_m": [0.7, 0.9], "k_m": [0.5, 2], "cap0": [99, 32]} if quick else MIN_GRIDS)
    results = {}
    for stat in stats:
        print("\n" + "=" * 78 + f"\nTUNING {stat.upper()} on seasons {tune_seasons} (profile NB log-likelihood)\n" + "=" * 78)
        base = dict(DEFAULTS, decay_m=mp["decay_m"], k_m=mp["k_m"], cap0=mp["cap0"], capc=mp["capc"])
        P, r, nll = tune_stat(days, tune_seasons, stat, base, grids, quiet=quiet)
        tp = walk_forward(days, stat, P, max(d for d, rows in days if rows[0][1] in tune_seasons))
        tm = gate(tp, tune_seasons)
        disp = fit_dispersion(tp["y"][tm], tp["mu"][tm])
        results[stat] = {"params": P, "disp": disp, "tune_nll": nll}
        print(f"  {stat} final: " + " ".join(f"{k}={P[k]}" for k in TUNE_ORDER if k in grids) + f" decay_m={P['decay_m']} k_m={P['k_m']} cap0={P['cap0']} capc={P['capc']}")
        print(f"  {stat} spread of counts on tune rows: classic NB r={disp['nb2']['r']} NLL {disp['nb2_nll']:.5f} | "
              f"variance-power mix (phi={disp['pow']['phi']}, q1={disp['pow']['q1']}, NB share {disp['pow'].get('w', 1.0):.2f}) NLL {disp['pow_nll']:.5f} -> using {disp_text(disp)}")
    print("\n" + "=" * 78 + f"\nVALIDATE seasons {val_seasons}: results by prop\n" + "=" * 78)
    evals = {}
    shown_minutes = False
    for stat in stats:
        P, disp = results[stat]["params"], results[stat]["disp"]["use"]
        ev = evaluate_stat(days, stat, P, disp, val_seasons)
        evals[stat] = ev
        m, preds = ev["mask"], ev["preds"]
        if not shown_minutes:
            minutes_report(preds, m)
            shown_minutes = True
        n = int(m.sum())
        print(f"\n##### {stat.upper()}  (n={n} trusted player-games with >= {MIN_PRIOR} prior games and projected >= {MIN_GATE:.0f} min; {disp_text(disp)})")
        nm = ev["nll_model_on_t10rows"]
        d1, s1 = paired(ev["nll_t10"], nm)
        print(f"  mean per game: actual {preds['y'][m].mean():.2f}   model {preds['mu'][m].mean():.2f}   bias {preds['y'][m].mean() - preds['mu'][m].mean():+.3f}")
        print(f"  MAE:  model {ev['mae']['model']:.3f} | last-10 avg {ev['mae']['t10']:.3f} (same rows: model {ev['mae']['model_on_t10rows']:.3f}) | prior only {ev['mae']['prior']:.3f}")
        print(f"  NLL (lower = better): model {float(ev['nll_model'].mean()):.4f} | prior only {float(ev['nll_prior'].mean()):.4f} | last-10 avg {float(ev['nll_t10'].mean()):.4f}"
              f" (model on the same rows {float(nm.mean()):.4f}; gain {d1:+.4f} +/- {s1:.4f})")
        calib_buckets(preds, m, title=f"{stat.upper()} calibration of the MEAN by predicted-mean bucket")
        proxy_line_report(preds, m, disp, stat)
        ablations(days, stat, P, disp, val_seasons)
        if stat != "pra":
            opp_table(days, stat, P)
    if all(s in results for s in ("pts", "reb", "ast", "pra")):
        print("\n" + "=" * 78 + "\nPRA: SUM of the three component means vs its own DIRECT index\n" + "=" * 78)
        mu_sum = evals["pts"]["preds"]["mu"] + evals["reb"]["preds"]["mu"] + evals["ast"]["preds"]["mu"]
        pra = evals["pra"]["preds"]
        tmask = gate(pra, tune_seasons)
        disp_sum = fit_dispersion(pra["y"][tmask], mu_sum[tmask])["use"]
        vm = gate(pra, val_seasons)
        ll_sum = nll_with(pra["y"][vm], mu_sum[vm], disp_sum)
        ll_dir = nll_with(pra["y"][vm], pra["mu"][vm], results["pra"]["disp"]["use"])
        d, se = paired(ll_dir, ll_sum)
        print(f"  NLL sum-of-parts {float(ll_sum.mean()):.4f} ({disp_text(disp_sum)}) | direct {float(ll_dir.mean()):.4f} ({disp_text(results['pra']['disp'])}) "
              f"-> sum-of-parts advantage {d:+.4f} +/- {se:.4f}   MAE sum {np.abs(pra['y'][vm] - mu_sum[vm]).mean():.3f} direct {np.abs(pra['y'][vm] - pra['mu'][vm]).mean():.3f}")
        results["pra_sum"] = {"disp": disp_sum}
        print("  (PRA components are correlated -- a big game tends to lift all three -- so PRA's spread is fit on PRA itself, not added up.)")
    return {"minutes": {k: mp[k] for k in ("decay_m", "k_m", "cap0", "capc")},
            "props": {k: {"params": v["params"], "disp": v["disp"]["use"]} for k, v in results.items() if k in STATS},
            "pra_sum_disp": results.get("pra_sum", {}).get("disp")}


def main():
    ap = argparse.ArgumentParser(description="NBA player props (points/rebounds/assists/PRA) walk-forward backtest")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--seasons", required=True)
    ap.add_argument("--tune-seasons", default=None)
    ap.add_argument("--validate-seasons", default=None)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--stats", default=None, help="comma list of pts,reb,ast,pra (default all)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    seasons = [int(x) for x in args.seasons.split(",")]
    days, rep = load_rows(args.dir, seasons)
    print(f"{rep['rows']} player-games from {rep['games']} games loaded; {rep['old_format']} boxscore(s) in the OLD format skipped"
          + (" -- run: python nba_collect_boxscores.py --seasons " + args.seasons + " --upgrade" if rep["old_format"] else ""))
    if not days:
        sys.exit("no usable data")
    if rep["old_format"] > 0.05 * (rep["games"] + rep["old_format"]):
        sys.exit("more than 5% of boxscores lack rebounds/assists (old format). Run the --upgrade command above first.")
    if args.diagnose:
        diagnose(days)
        return
    if not (args.tune_seasons and args.validate_seasons):
        sys.exit("--tune-seasons and --validate-seasons are required (seasons before the first tune season are warm-up)")
    tune = [int(x) for x in args.tune_seasons.split(",")]
    val = [int(x) for x in args.validate_seasons.split(",")]
    res = run(days, tune, val, quick=args.quick, stats=args.stats.split(",") if args.stats else None)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: core_nba_player_props.py (412 lines)
# ======================================================================
_SRC["core_nba_player_props"] = r'''"""
core_nba_player_props.py

PRODUCTION live-prediction module for NBA player props: POINTS, REBOUNDS, ASSISTS and PRA
(points+rebounds+assists), with the probability of going over a sportsbook line.
Predicts games that HAVEN'T been played yet. Builds on backtest_nba_player_props (same model code).

STATUS: SHADOW MODE. Validated out of sample (tune 2023-24, validate 2025-26, 131k real player-games)
against a last-10-game-average baseline: NLL gain +0.025 (PTS) .. +0.037 (AST), calibrated on
proxy half-point lines. It has NOT been tested against real sportsbook prop lines (no historical
lines exist in the data). Track it with nba_track_prop_results.py before putting money on it.

MODEL (per prop)
  mu = E_min x league_rate(position group) x player_index x opp_index^gamma
    E_min        recency-weighted, shrunk, soft-capped minutes projection
    player_index shrunk recency-weighted per-minute rate vs the league rate for his position group
    opp_index    what the opposing team allowed to that position group (G/F/C) vs expectation
  PRA mean = pts mean + reb mean + ast mean (tied with a direct PRA fit in the backtest)
  Count distribution: negative binomial / discretised Normal mix fitted on the tune seasons
  (parameters in nba_prop_params.json, written by backtest_nba_player_props.py --out).

WHO IS PREDICTED  Players whose latest team is playing, who appeared in >= RECENT_MIN of that team's last
  RECENT_WINDOW games, have >= 10 prior games and project >= 15 minutes. Injury feed: 'Out' players are
  skipped; Day-To-Day players ARE predicted but flagged (the mean is conditional on playing).

LIMITS, shipped as-is
  * The mean is conditional on the player playing his usual role. NOT modeled: teammates being out (usage
    goes UP for the others -- the model under-predicts then), rest/back-to-backs, home/away, blowout risk,
    minutes restrictions after injury, lineup/starter news after the injury feed.
  * No market comparison yet. Line + odds you supply are logged so the tracker can answer that.
  * Integer lines: P(push) is reported; over/under probabilities exclude it.

Usage (from the NBA folder, with nba_games_*.json, nba_boxscores_*.json (v2) and nba_prop_params.json):
    python core_nba_player_props.py                       -> today's slate, no lines
    python core_nba_player_props.py --date 20261022 --lines-file prop_lines.json
    lines file: {"Jayson Tatum|pts": 27.5, "Jayson Tatum|pra": {"line": 41.5, "over": -115, "under": -105}}
    key = "<player name or ESPN id>|<pts|reb|ast|pra>"
Needs internet (ESPN scoreboard, injuries, boxscores) unless --no-refresh. Run locally.
"""

import argparse
import datetime
import json
import math
import os
import re

import numpy as np

import backtest_nba_player_props as bp
import core_nba_moneyline as mon
import core_nba_spread as cs
import nba_collect_boxscores as bx
from nba_io import atomic_json_dump

PARAMS_FILE = "nba_prop_params.json"
LOG_FILE = "nba_prop_log.jsonl"
HISTORY_SEASONS = 3          # this season + 2 prior (prior seasons feed the carry-over)
RECENT_WINDOW = 10           # team games looked at to decide who is on the roster
RECENT_MIN = 3               # appearances within that window
MAX_LINE = 90
STATS = bp.STATS


# ------------------------------------------------------------------ parameters
def load_params(path):
    """nba_prop_params.json -> {'minutes': {...}, 'props': {stat: {'params','disp'}}, 'pra_sum_disp'}."""
    with open(path) as f:
        res = json.load(f)
    for k in ("minutes", "props"):
        if k not in res:
            raise SystemExit(f"{path} has no '{k}'. Re-run: python backtest_nba_player_props.py ... --out {PARAMS_FILE}")
    for s in STATS:
        if s not in res["props"]:
            raise SystemExit(f"{path} lacks '{s}'. Re-run the backtest with all four props.")
    return res


def stat_params(res, stat):
    p = dict(bp.DEFAULTS)
    p.update(res["props"][stat]["params"])
    p.update(res["minutes"])        # real files carry the minutes keys in both places; the shared minutes block wins
    return p


# ------------------------------------------------------------------ probabilities
def pmf(y, mu, disp):
    return float(np.exp(-bp.nll_with(np.array([float(y)]), np.array([float(mu)]), disp))[0])


def over_under(line, mu, disp):
    """(P(over), P(under), P(push)) for a line. Half-point line: no push."""
    mu = float(mu)
    fl = math.floor(line)
    if abs(line - fl) > 1e-9:
        po = float(bp.over_with(np.array([fl]), np.array([mu]), disp)[0])
        return po, 1.0 - po, 0.0
    push = pmf(fl, mu, disp)
    po = float(bp.over_with(np.array([fl]), np.array([mu]), disp)[0])
    return po, max(0.0, 1.0 - po - push), push


def fair_line(mu, disp):
    """Half-point line whose P(over) is closest to 50%."""
    best = None
    for k in range(0, MAX_LINE):
        po = float(bp.over_with(np.array([k]), np.array([float(mu)]), disp)[0])
        d = abs(po - 0.5)
        if best is None or d < best[0]:
            best = (d, k + 0.5)
        if po < 0.5:
            break
    return best[1]


def american_to_decimal(o):
    o = float(o)
    return 1 + (o / 100 if o > 0 else 100 / -o)


def ev_per_unit(p, odds):
    return p * (american_to_decimal(odds) - 1) - (1 - p)


# ------------------------------------------------------------------ context (depends only on cached files)
def _box_seasons(directory):
    out = []
    for n in os.listdir(directory):
        m = re.fullmatch(r"nba_boxscores_(\d{4})\.json", n)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def build_state(days, stat, P, season):
    """Replay every game date through PState (predict then update, exactly like the backtest), then roll over
    to `season` if the data stop before it."""
    st = bp.PState(P, stat)
    for date, rows in days:
        st.rollover(rows[0][1])
        preds = [st.predict(r) for r in rows]
        for r, p in zip(rows, preds):
            st.update(r, p)
    if st.season is not None and season > st.season:
        st.rollover(season)
    return st


def build_context(directory=".", season=None, params=None, game_date=None, pra_direct=False):
    """Build once, reuse until the files change (a service caches this)."""
    if season is None:
        season = mon.season_for_date(game_date or datetime.date.today())
    params = params or load_params(os.path.join(directory, PARAMS_FILE))
    have = _box_seasons(directory)
    seasons = [s for s in have if season - HISTORY_SEASONS < s <= season]
    days, rep = bp.load_rows(directory, seasons)
    if not days:
        raise SystemExit("no usable v2 boxscores found; run nba_collect_boxscores.py --upgrade")
    # names / roster from the raw boxscores of the current and previous season
    names, last_tid, last_pos = {}, {}, {}
    seen, gdate = {}, {}
    for date, rows in days:
        for r in rows:
            seen.setdefault((r[4], r[2]), set()).add(r[3])
            last_tid[r[3]] = r[4]
            if r[6]:
                last_pos[r[3]] = r[6]
            gdate[r[2]] = date
    per_team = {}
    for (tid, gid), pids in seen.items():
        per_team.setdefault(tid, []).append((gdate[gid], gid, pids))
    recent = {}
    for tid, lst in per_team.items():
        lst.sort()
        cnt = {}
        for _, _, pids in lst[-RECENT_WINDOW:]:
            for p in pids:
                cnt[p] = cnt.get(p, 0) + 1
        recent[tid] = cnt
    for s in seasons[-2:]:
        bpth = os.path.join(directory, bx.boxscore_file_for(s))
        with open(bpth) as f:
            for rec in json.load(f)["games"].values():
                for side in ("home", "away"):
                    for p in rec.get(side) or []:
                        if p.get("name"):
                            names[str(p["id"])] = p["name"]
    states = {}
    for stat in STATS:
        if stat == "pra":
            continue
        states[stat] = build_state(days, stat, stat_params(params, stat), season)
    if pra_direct:
        states["pra"] = build_state(days, "pra", stat_params(params, "pra"), season)
    return {"season": season, "params": params, "states": states, "names": names, "last_tid": last_tid,
            "last_pos": last_pos, "recent": recent, "last_date": days[-1][0],
            "name_to_id": {n: p for p, n in names.items()}}


# ------------------------------------------------------------------ prediction
def _disp(ctx, stat, pra_mode):
    p = ctx["params"]
    if stat == "pra" and pra_mode == "sum" and p.get("pra_sum_disp"):
        return p["pra_sum_disp"]
    return p["props"][stat]["disp"]


def player_lines(lines, name, pid):
    """{stat: (line, over_odds, under_odds)} for one player from a lines dict keyed 'name|stat' or 'id|stat'."""
    out = {}
    for stat in STATS:
        v = lines.get(f"{name}|{stat}", lines.get(f"{pid}|{stat}"))
        if v is None:
            continue
        if isinstance(v, dict):
            out[stat] = (float(v["line"]), v.get("over"), v.get("under"))
        else:
            out[stat] = (float(v), None, None)
    return out


def predict_from_context(ctx, events, injury_data, lines=None, pra_mode="sum"):
    """Pure prediction step. -> (rows, meta). One row per (player, prop). No file or network access."""
    lines = lines or {}
    inj_ok = injury_data is not None
    injuries, unmatched = (cs.parse_injuries(injury_data, ctx["name_to_id"]) if inj_ok else ({}, 0))
    rows_out, skipped_out = [], []
    d_stats = [s for s in STATS if s != "pra"]
    for ev in events:
        g = mon.collector.parse_event(ev)
        if g is None or g["completed"] or g["season_type"] != mon.REGULAR_SEASON_TYPE:
            continue
        gdate = str(g["date"])[:10]
        for side, tid, oid, home, tname, oname in (("home", g["home_id"], g["away_id"], 1, g["home_name"], g["away_name"]),
                                                   ("away", g["away_id"], g["home_id"], 0, g["away_name"], g["home_name"])):
            cnt = ctx["recent"].get(tid, {})
            team_out = []
            for pid, n in cnt.items():
                if n < RECENT_MIN or ctx["last_tid"].get(pid) != tid:
                    continue
                if pid in injuries and injuries[pid][0] <= 0.0:
                    team_out.append({"id": pid, "name": ctx["names"].get(pid, pid), "status": injuries[pid][1]})
            for pid, n in sorted(cnt.items()):
                if n < RECENT_MIN or ctx["last_tid"].get(pid) != tid:
                    continue
                inj = injuries.get(pid)
                if inj is not None and inj[0] <= 0.0:
                    continue
                row = (gdate, ctx["season"], g["game_id"], pid, tid, oid, ctx["last_pos"].get(pid), 0.0, 0, home, 0, 0, 0, 0)
                pr = {s: ctx["states"][s].predict(row) for s in ctx["states"]}
                ref = pr[d_stats[0]]
                if ref["nprior"] < bp.MIN_PRIOR or ref["emin"] < bp.MIN_GATE:
                    continue
                mus = {s: pr[s]["mu"] for s in d_stats}
                if pra_mode == "direct" and "pra" in pr:
                    mus["pra"] = pr["pra"]["mu"]
                else:
                    mus["pra"] = mus["pts"] + mus["reb"] + mus["ast"]
                name = ctx["names"].get(pid, pid)
                pl = player_lines(lines, name, pid)
                for stat in STATS:
                    mu, disp = mus[stat], _disp(ctx, stat, pra_mode)
                    t10 = (sum(pr[s]["t10"] for s in d_stats) if stat == "pra" and stat not in pr else pr[stat]["t10"])
                    rec = {"game_id": g["game_id"], "date": g["date"], "season": ctx["season"], "player_id": pid, "player": name,
                           "team_id": tid, "team": tname, "opp_id": oid, "opp": oname, "home": home, "stat": stat,
                           "mu": round(mu, 3), "emin": round(ref["emin"], 2), "last10": None if t10 != t10 else round(t10, 2),
                           "opp_index": round(pr[stat]["opp_idx"], 3) if stat in pr else None,
                           "group": ref["g"], "n_prior": int(ref["nprior"]), "fair_line": fair_line(mu, disp), "disp": disp,
                           "status": inj[1] if inj else None, "teammates_out": [o for o in team_out if o["id"] != pid],
                           "pra_mode": pra_mode if stat == "pra" else None}
                    if stat in pl:
                        line, oo, uo = pl[stat]
                        po, pu, pp = over_under(line, mu, disp)
                        rec.update({"line": line, "p_over": round(po, 4), "p_under": round(pu, 4), "p_push": round(pp, 4),
                                    "over_odds": oo, "under_odds": uo})
                        if oo is not None:
                            rec["ev_over"] = round(ev_per_unit(po, oo), 4)
                        if uo is not None:
                            rec["ev_under"] = round(ev_per_unit(pu, uo), 4)
                    rows_out.append(rec)
    return rows_out, {"injuries_parsed": len(injuries), "unmatched_injuries": unmatched, "inj_ok": inj_ok,
                      "data_through": ctx["last_date"]}


def predict_props_slate(game_date, directory=".", refresh=True, lines=None, fetch_day=None, fetch_injuries_fn=None,
                        fetch_summary=None, collect=None, params=None, pra_mode="sum"):
    if isinstance(game_date, str):
        game_date = datetime.datetime.strptime(game_date, "%Y%m%d").date()
    season = mon.season_for_date(game_date)
    fetch_day = fetch_day or mon.collector.get_day
    if refresh:
        kw = {"fetch": fetch_summary} if fetch_summary else {}
        (collect or mon.collector.collect_season)(season, directory=directory)
        bx.collect_season(season, directory, **kw)
    ctx = build_context(directory, season, params, pra_direct=(pra_mode == "direct"))
    try:
        inj = (fetch_injuries_fn or cs.fetch_injuries)()
    except Exception as e:
        print(f"[WARN] injuries feed unavailable ({e}); players flagged Out may appear in the output.")
        inj = None
    rows, meta = predict_from_context(ctx, fetch_day(game_date).get("events") or [], inj, lines, pra_mode)
    if ctx["last_date"] >= game_date.isoformat():
        meta["warning"] = f"cached data run through {ctx['last_date']}, which is not before {game_date}; predictions may include games already played"
    return rows, meta


# ------------------------------------------------------------------ logging
def _signature(rec):
    return (rec.get("mu"), rec.get("line"), rec.get("over_odds"), rec.get("under_odds"), rec.get("status"))


def load_last_signatures(directory="."):
    path = os.path.join(directory, LOG_FILE)
    sigs = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sigs[(r["game_id"], r["player_id"], r["stat"])] = _signature(r)
    return sigs


def log_predictions(rows, directory=".", now=None, dedupe=True, last_sigs=None, skip_started=True):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    path = os.path.join(directory, LOG_FILE)
    out = []
    for r in rows:
        if skip_started and cs.has_started(r, now):
            continue
        key = (r["game_id"], r["player_id"], r["stat"])
        sig = _signature(r)
        if dedupe and last_sigs is not None and last_sigs.get(key) == sig:
            continue
        rec = dict(r)
        rec["logged_at"] = now.isoformat(timespec="seconds")
        out.append(json.dumps(rec) + "\n")
        if last_sigs is not None:
            last_sigs[key] = sig
    if out:
        with open(path, "a") as f:
            f.write("".join(out))
    return path, len(out)


# ------------------------------------------------------------------ output
def print_table(rows, meta, min_edge=None):
    if meta.get("warning"):
        print(f"\n[WARN] {meta['warning']}")
    if not rows:
        print("\nNo not-yet-played regular-season games / trusted players found for that date.")
        return
    print(f"\ndata through {meta['data_through']}; injury feed: {meta['injuries_parsed']} parsed, {meta['unmatched_injuries']} unidentifiable")
    head = f"{'player':<24}{'team':<14}{'stat':<5}{'proj':>6}{'min':>5}{'last10':>7}{'fair':>6}  {'line':>6}{'over%':>7}{'under%':>7}{'EVo':>7}{'EVu':>7}  note"
    print("\n" + head + "\n" + "-" * len(head))
    by_game = {}
    for r in rows:
        by_game.setdefault(r["game_id"], []).append(r)
    for gid, rs in by_game.items():
        print(f"\n{rs[0]['team'] if rs[0]['home'] == 0 else rs[0]['opp']} @ {rs[0]['opp'] if rs[0]['home'] == 0 else rs[0]['team']}")
        for r in sorted(rs, key=lambda x: (x["team_id"], -x["emin"], x["player"], STATS.index(x["stat"]))):
            def f(k, fmt):
                return fmt.format(r[k]) if r.get(k) is not None else "--"
            def pct(v):
                return "--" if v is None else f"{v * 100:.1f}%"
            note = []
            if r["status"]:
                note.append(f"{r['status']} (conditional on playing)")
            if r["teammates_out"] and r["stat"] == "pts":
                note.append("teammates out: " + ", ".join(o["name"] for o in r["teammates_out"][:3]) + " (usage up, NOT modeled)")
            print(f"{r['player'][:23]:<24}{str(r['team'])[:13]:<14}{r['stat']:<5}{r['mu']:>6.1f}{r['emin']:>5.0f}{f('last10', '{:.1f}'):>7}{r['fair_line']:>6.1f}  "
                  f"{f('line', '{:.1f}'):>6}{pct(r.get('p_over')):>7}{pct(r.get('p_under')):>7}{f('ev_over', '{:+.3f}'):>7}{f('ev_under', '{:+.3f}'):>7}  {'; '.join(note)}")
    print("\nproj = expected stat given he plays his usual role; fair = half-point line with P(over)~50%. SHADOW MODE: track before betting.")


def load_lines(path):
    if not path:
        return {}
    if not os.path.exists(path):
        raise SystemExit(f"Lines file '{path}' not found. Lines are optional: omit --lines-file, or create JSON like "
                         "{\"Jayson Tatum|pts\": 27.5, \"Jayson Tatum|pra\": {\"line\": 41.5, \"over\": -115, \"under\": -105}}")
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Live NBA player props (PTS/REB/AST/PRA), shadow mode")
    ap.add_argument("--date", default=None, help="YYYYMMDD (default today)")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--lines-file", default=None)
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--pra", choices=["sum", "direct"], default="sum")
    ap.add_argument("--only", default=None, help="show only players whose name contains this text")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    date_str = args.date or datetime.date.today().strftime("%Y%m%d")
    rows, meta = predict_props_slate(date_str, args.dir, refresh=not args.no_refresh, lines=load_lines(args.lines_file), pra_mode=args.pra)
    out = args.out or os.path.join(args.dir, f"nba_prop_predictions_{date_str}.json")
    atomic_json_dump(out, rows)
    shown = [r for r in rows if not args.only or args.only.lower() in r["player"].lower()]
    print_table(shown, meta)
    print(f"\nWrote {len(rows)} prediction row(s) to {out}")
    if not args.no_log:
        path, n = log_predictions(rows, args.dir, last_sigs=load_last_signatures(args.dir))
        print(f"Appended {n} new/changed row(s) to {path} (used by nba_track_prop_results.py)")


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: nba_track_lineup_results.py (199 lines)
# ======================================================================
_SRC["nba_track_lineup_results"] = r'''"""
nba_track_lineup_results.py

Shadow-mode scorecard. core_nba_spread.py appends every pre-game prediction to
nba_prediction_log.jsonl. This script joins them with the final scores in
nba_games_{season}.json (refresh those first: python nba_collect_games.py --seasons 2027)
and answers the question that matters before any money goes down:
    does the lineup adjustment actually improve live predictions?

For each game it uses the LAST logged prediction made before tip-off (so a late
injury update counts, a post-game re-run does not; if tip-off time can't be parsed
it falls back to the last logged prediction). Reports, for completed games:
  * margin RMSE / MAE and win-probability Brier: rating-only vs rating+lineups
  * paired squared-error gain +/- s.e. (> 2 s.e. = real) and the same on games where
    the lineup adjustment was large (|home_delta - away_delta| * gamma >= --big)
  * if lines were logged: cover-probability calibration and hit rate by model-edge bucket
Needs ~150+ games before the numbers mean anything; the output says how many.

Usage:
    python nba_track_lineup_results.py --season 2027 [--big 1.0] [--dir .]
"""

import argparse
import datetime
import json
import math
import os

from backtest_nba_win_pct import norm_cdf

LOG_FILE = "nba_prediction_log.jsonl"


def _parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_log(directory="."):
    path = os.path.join(directory, LOG_FILE)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def pick_pregame_predictions(log, games):
    """{game_id: record}: last record logged before tip-off (fallback: last record)."""
    best = {}
    for rec in log:
        g = games.get(rec["game_id"])
        if g is None:
            continue
        tip = _parse_ts(g.get("date")) if "T" in str(g.get("date")) else None
        logged = _parse_ts(rec.get("logged_at"))
        if tip is not None and logged is not None:
            if tip.tzinfo is None:
                tip = tip.replace(tzinfo=datetime.timezone.utc)
            if logged.tzinfo is None:
                logged = logged.replace(tzinfo=datetime.timezone.utc)
            if logged >= tip:
                continue
        prev = best.get(rec["game_id"])
        if prev is None or str(rec["logged_at"]) >= str(prev["logged_at"]):
            best[rec["game_id"]] = rec
    return best


def score(records, games, big=1.0):
    rows = []
    for gid, rec in records.items():
        g = games[gid]
        if g.get("home_score") is None or g.get("away_score") is None or not g.get("completed", True):
            continue
        rows.append({**rec, "margin": g["home_score"] - g["away_score"]})
    return rows


def _paired(rows):
    d = [(r["margin"] - r["predicted_margin_base"]) ** 2 - (r["margin"] - r["predicted_margin"]) ** 2 for r in rows]
    n = len(d)
    if n < 2:
        return float("nan"), float("nan")
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1))
    return m, sd / math.sqrt(n)


def _rmse(rows, key):
    return math.sqrt(sum((r["margin"] - r[key]) ** 2 for r in rows) / len(rows))


def _mae(rows, key):
    return sum(abs(r["margin"] - r[key]) for r in rows) / len(rows)


def _brier(rows, key):
    return sum((norm_cdf(r[key] / r["sigma"]) - (1.0 if r["margin"] > 0 else 0.0)) ** 2 for r in rows) / len(rows)


def summarize(rows, big=1.0):
    """The scorecard as a JSON-safe dict (for a website); report() prints the same numbers."""
    n = len(rows)
    out = {"n_games": n, "enough_data": n >= 150, "big_threshold": big}
    if n == 0:
        return out
    m, se = _paired(rows)
    adj = [r for r in rows if abs(r["predicted_margin"] - r["predicted_margin_base"]) >= big]
    out.update({
        "rmse": {"rating_only": _rmse(rows, "predicted_margin_base"), "with_lineups": _rmse(rows, "predicted_margin")},
        "mae": {"rating_only": _mae(rows, "predicted_margin_base"), "with_lineups": _mae(rows, "predicted_margin")},
        "brier": {"rating_only": _brier(rows, "predicted_margin_base"), "with_lineups": _brier(rows, "predicted_margin")},
        "paired_gain": {"mean": m, "se": se, "significant": bool(se == se and m > 2 * se)},
        "lineup_applied_games": sum(1 for r in rows if r.get("lineup_adjustment_applied")),
        "big_adjustment_games": len(adj),
    })
    if len(adj) >= 2:
        m2, se2 = _paired(adj)
        out["big_adjustment"] = {"rmse_rating_only": _rmse(adj, "predicted_margin_base"),
                                 "rmse_with_lineups": _rmse(adj, "predicted_margin"), "paired_gain": m2, "se": se2}
    lined = [r for r in rows if r.get("home_spread") is not None and r["margin"] != -r["home_spread"]]
    if lined:
        out["cover"] = {
            "n": len(lined),
            "predicted": sum(r["home_covers_probability"] for r in lined) / len(lined),
            "actual": sum(1 for r in lined if r["margin"] > -r["home_spread"]) / len(lined),
            "brier": sum((r["home_covers_probability"] - (1.0 if r["margin"] > -r["home_spread"] else 0.0)) ** 2
                         for r in lined) / len(lined)}
    return out


def report(rows, big=1.0):
    n = len(rows)
    print(f"\n{n} completed game(s) with a pre-game logged prediction.")
    if n == 0:
        return
    if n < 150:
        print(f"  (only {n}: far too few to judge; differences below are mostly noise until ~150+)")
    print(f"\n{'':30} {'rating only':>12} {'+ lineups':>10}")
    print(f"{'margin RMSE':30} {_rmse(rows, 'predicted_margin_base'):>12.3f} {_rmse(rows, 'predicted_margin'):>10.3f}")
    print(f"{'margin MAE':30} {_mae(rows, 'predicted_margin_base'):>12.3f} {_mae(rows, 'predicted_margin'):>10.3f}")
    print(f"{'win-prob Brier':30} {_brier(rows, 'predicted_margin_base'):>12.4f} {_brier(rows, 'predicted_margin'):>10.4f}")
    m, se = _paired(rows)
    print(f"\nPaired squared-error gain, all games: {m:+.2f} +/- {se:.2f} (positive = lineups help; > 2 s.e. = real)")
    adj = [r for r in rows if abs(r["predicted_margin"] - r["predicted_margin_base"]) >= big]
    print(f"Games where the lineup adjustment was >= {big:.1f} pt (n={len(adj)}):", end=" ")
    if len(adj) >= 2:
        m2, se2 = _paired(adj)
        print(f"RMSE {_rmse(adj, 'predicted_margin_base'):.3f} -> {_rmse(adj, 'predicted_margin'):.3f}; paired gain {m2:+.2f} +/- {se2:.2f}")
    else:
        print("too few")
    applied = sum(1 for r in rows if r.get("lineup_adjustment_applied"))
    print(f"Lineup adjustment was applied (>=5 boxscores each side + injury feed up) in {applied}/{n} games.")

    lined = [r for r in rows if r.get("home_spread") is not None]
    if lined:
        print(f"\n{len(lined)} game(s) had a logged line.")
        pushes = [r for r in lined if r["margin"] == -r["home_spread"]]
        lined = [r for r in lined if r["margin"] != -r["home_spread"]]
        if pushes:
            print(f"  ({len(pushes)} push(es) excluded)")
        if lined:
            pred = sum(r["home_covers_probability"] for r in lined) / len(lined)
            act = sum(1 for r in lined if r["margin"] > -r["home_spread"]) / len(lined)
            br = sum((r["home_covers_probability"] - (1.0 if r["margin"] > -r["home_spread"] else 0.0)) ** 2 for r in lined) / len(lined)
            print(f"  home cover: predicted {pred * 100:.1f}% vs actual {act * 100:.1f}%; Brier {br:.4f} (0.2500 = no information)")
            print(f"  {'model edge (pts)':>18} {'n':>5} {'picked side covers':>20}")
            buckets = [(0, 1), (1, 2), (2, 4), (4, 99)]
            for lo, hi in buckets:
                sel = [r for r in lined if lo <= abs(r["predicted_margin"] + r["home_spread"]) < hi]
                if not sel:
                    continue
                wins = sum(1 for r in sel
                           if (r["margin"] > -r["home_spread"]) == (r["predicted_margin"] + r["home_spread"] > 0))
                print(f"  {f'{lo}-{hi}' if hi < 99 else f'{lo}+':>18} {len(sel):>5} {wins / len(sel) * 100:>19.1f}%")
            print("  (Needs hundreds of games and a break-even above ~52.4% at -110 before it means anything.)")


def main():
    ap = argparse.ArgumentParser(description="Scorecard for shadow-mode NBA lineup predictions")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--big", type=float, default=1.0)
    ap.add_argument("--dir", default=".")
    args = ap.parse_args()
    path = os.path.join(args.dir, f"nba_games_{args.season}.json")
    if not os.path.exists(path):
        print(f"{path} not found; run nba_collect_games.py --seasons {args.season} first.")
        return
    with open(path) as f:
        games = json.load(f)["games"]
    log = [r for r in load_log(args.dir) if r.get("season") == args.season]
    recs = pick_pregame_predictions(log, games)
    report(score(recs, games, args.big), args.big)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: nba_track_prop_results.py (195 lines)
# ======================================================================
_SRC["nba_track_prop_results"] = r'''"""
nba_track_prop_results.py

Shadow-mode scorecard for NBA player props. core_nba_player_props.py appends every pre-game
projection to nba_prop_log.jsonl. This script joins the LAST projection logged before tip-off with the
actual box score (refresh first: python nba_collect_boxscores.py --seasons 2027) and reports, per prop:
  * mean error (bias), MAE, and log-likelihood of the model vs the player's last-10 average
  * minutes projection error
  * if lines were logged: P(over) calibration by bucket, Brier/log-loss, hit rate by model edge, and, if
    odds were logged, the flat-stake ROI of betting every side with EV > --min-ev
Players who did not play (no minutes) are VOIDED, as a sportsbook would void them; they are counted.
Needs ~300+ player-games per prop (more with lines) before the numbers mean anything.

Usage:  python nba_track_prop_results.py --season 2027 [--min-ev 0.03] [--dir .]
"""

import argparse
import datetime
import json
import math
import os

import numpy as np

import backtest_nba_player_props as bp

LOG_FILE = "nba_prop_log.jsonl"


def _ts(s):
    try:
        t = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def load_log(directory="."):
    path = os.path.join(directory, LOG_FILE)
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass            # partially written last line
    return out


def load_actuals(directory, season):
    """{(game_id, player_id): {'min','pts','reb','ast','pra'}} for players who played."""
    path = os.path.join(directory, f"nba_boxscores_{season}.json")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found")
    with open(path) as f:
        box = json.load(f)["games"]
    out, finished = {}, set()
    for gid, rec in box.items():
        finished.add(gid)
        for side in ("home", "away"):
            for p in rec.get(side) or []:
                m = p.get("min")
                if p.get("dnp") or not m or m <= 0 or p.get("pts") is None or p.get("reb") is None or p.get("ast") is None:
                    continue
                out[(gid, str(p["id"]))] = {"min": float(m), "pts": p["pts"], "reb": p["reb"], "ast": p["ast"],
                                            "pra": p["pts"] + p["reb"] + p["ast"]}
    return out, finished


def pick_pregame(log):
    """{(game, player, stat): record}: last record logged before tip-off (fallback: last logged)."""
    best = {}
    for r in log:
        key = (r["game_id"], r["player_id"], r["stat"])
        tip, lg = _ts(r.get("date")) if "T" in str(r.get("date")) else None, _ts(r.get("logged_at"))
        if tip is not None and lg is not None and lg >= tip:
            continue
        prev = best.get(key)
        if prev is None or str(r.get("logged_at")) >= str(prev.get("logged_at")):
            best[key] = r
    return best


def join(log, actuals, finished):
    rows, voided, pending = [], 0, 0
    for key, r in pick_pregame(log).items():
        gid, pid, stat = key
        if gid not in finished:
            pending += 1
            continue
        a = actuals.get((gid, pid))
        if a is None:
            voided += 1
            continue
        rows.append((r, a))
    return rows, voided, pending


def _brier(p, y):
    return float(np.mean((p - y) ** 2))


def _logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def report(rows, voided, pending, min_ev=0.03):
    print(f"{len(rows)} graded player-prop(s); {voided} voided (did not play); {pending} awaiting final box scores.")
    if not rows:
        return
    for stat in bp.STATS:
        rs = [(r, a) for r, a in rows if r["stat"] == stat]
        if not rs:
            continue
        mu = np.array([r["mu"] for r, a in rs])
        y = np.array([a[stat] for r, a in rs], dtype=float)
        emin = np.array([r["emin"] for r, a in rs])
        mins = np.array([a["min"] for r, a in rs])
        t10 = np.array([np.nan if r.get("last10") is None else r["last10"] for r, a in rs], dtype=float)
        print(f"\n##### {stat.upper()}  n={len(rs)}")
        print(f"  mean actual {y.mean():.2f} vs projected {mu.mean():.2f}   bias {y.mean() - mu.mean():+.3f}   MAE {np.abs(y - mu).mean():.3f}"
              f"   minutes: projected {emin.mean():.1f} actual {mins.mean():.1f} (MAE {np.abs(mins - emin).mean():.2f})")
        ok = ~np.isnan(t10)
        if ok.sum() > 30:
            print(f"  vs last-10 avg on {int(ok.sum())} rows: MAE model {np.abs(y - mu)[ok].mean():.3f} / last-10 {np.abs(y - t10)[ok].mean():.3f}")
        # likelihood under the logged distribution
        ll = np.array([float(bp.nll_with(np.array([yy]), np.array([m]), r["disp"])[0]) for (r, a), yy, m in zip(rs, y, mu)])
        print(f"  mean NLL under the logged count distribution: {ll.mean():.4f}")
        st = [(r, a) for r, a in rs if r.get("line") is not None]
        stat_tag = f"  with lines: n={len(st)}"
        if len(st) < 30:
            print(stat_tag + " (too few for calibration yet)")
            continue
        yy = np.array([a[stat] for r, a in st], dtype=float)
        line = np.array([r["line"] for r, a in st])
        po = np.array([r["p_over"] for r, a in st])
        pu = np.array([r["p_under"] for r, a in st])
        keep = yy != line                                       # pushes are refunded
        over = (yy > line).astype(float)
        print(stat_tag + f", pushes {int((~keep).sum())}; over rate actual {over[keep].mean():.3f} vs model {po[keep].mean():.3f};"
              f" Brier {_brier(po[keep], over[keep]):.4f}, log-loss {_logloss(po[keep], over[keep]):.4f} (coin flip 0.6931)")
        # does the model beat a no-skill 'always 50%' and the line's own price (when odds given)?
        edges = np.array([[0.0, 0.45], [0.45, 0.55], [0.55, 0.65], [0.65, 1.01]])
        print("  P(over) bucket     n   model  actual")
        for lo, hi in ((0, .35), (.35, .45), (.45, .55), (.55, .65), (.65, 1.01)):
            m = keep & (po >= lo) & (po < hi)
            if m.sum() >= 5:
                print(f"    {lo:.2f}-{min(hi, 1):.2f}   {int(m.sum()):>5}   {po[m].mean():.3f}   {over[m].mean():.3f}")
        # model's chosen side: hit rate by confidence
        side_p = np.maximum(po, pu)
        pick_over = po >= pu
        hit = np.where(pick_over, over, 1 - over)
        print("  model's pick (more likely side): hit rate by its probability")
        for lo, hi in ((.5, .55), (.55, .6), (.6, .7), (.7, 1.01)):
            m = keep & (side_p >= lo) & (side_p < hi)
            if m.sum() >= 5:
                print(f"    {lo:.2f}-{min(hi, 1):.2f}   {int(m.sum()):>5}   model {side_p[m].mean():.3f}   hit {hit[m].mean():.3f}")
        # ROI where odds were logged
        bets = []
        for (r, a), yv in zip(st, yy):
            for side in ("over", "under"):
                ev, odds = r.get(f"ev_{side}"), r.get(f"{side}_odds")
                if ev is None or odds is None or ev < min_ev or yv == r["line"]:
                    continue
                won = (yv > r["line"]) if side == "over" else (yv < r["line"])
                dec = 1 + (odds / 100 if odds > 0 else 100 / -odds)
                bets.append((ev, (dec - 1) if won else -1.0))
        if bets:
            pnl = np.array([b[1] for b in bets])
            print(f"  bets with model EV >= {min_ev:+.2f}: {len(bets)}, flat-stake ROI {pnl.mean() * 100:+.1f}% "
                  f"(+/- {pnl.std(ddof=1) / math.sqrt(len(pnl)) * 100:.1f}% s.e.), avg model EV {np.mean([b[0] for b in bets]) * 100:+.1f}%")
        else:
            print("  (no logged odds with positive model EV yet; add odds to the lines file to get an ROI check)")
    print("\nA real edge needs ROI clearly above 0 by 2+ standard errors over several hundred bets; calibration alone does not prove one.")


def main():
    ap = argparse.ArgumentParser(description="Scorecard for logged NBA player-prop projections")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--min-ev", type=float, default=0.03)
    args = ap.parse_args()
    log = [r for r in load_log(args.dir) if r.get("season") == args.season]
    if not log:
        raise SystemExit(f"no logged projections for season {args.season} in {os.path.join(args.dir, LOG_FILE)}")
    actuals, finished = load_actuals(args.dir, args.season)
    rows, voided, pending = join(log, actuals, finished)
    report(rows, voided, pending, args.min_ev)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: nba_service.py (414 lines)
# ======================================================================
_SRC["nba_service"] = r'''"""
nba_service.py

The WEBSITE-FACING layer over the NBA models. Built for a page that reloads on its
own: every call is cheap, never raises, never prints, is safe to hit from many
threads at once, and returns plain JSON-serializable dicts.

TWO PATHS, deliberately separate
  READ path (called on every page load; milliseconds):  get_slate(), get_scorecard(), health()
      - reads only the cached files; never downloads boxscores, never refits.
      - the ESPN scoreboard and injuries feed are cached in memory (default 60 s / 300 s) so
        N visitors reloading = ~1 upstream call per TTL, not N.
      - heavy file-derived state (ratings, player values, usual minutes) is built once and rebuilt
        only when a data file's modification time changes.
      - if ESPN is unreachable it serves the last good data flagged status="stale" (or rating-only
        if only the injury feed is down) instead of failing.
  WRITE path (run on a schedule, e.g. every 30 min; seconds to minutes):  refresh_data()
      - collects new games + boxscores, refits player values when older than 7 days.
      - guarded by a lock file so two overlapping jobs/workers never run at once
        (the second returns status="busy"). Files are written atomically, so readers never see half a file.
      - start_background_refresh(interval_s) runs it on a daemon thread if you don't have a scheduler.

SLATE RESPONSE (schema_version 1)
  {schema_version, generated_at (UTC ISO), date (YYYY-MM-DD), season, status: "ok"|"stale"|"degraded"|"error",
   warnings: [str], data: {scoreboard_age_s, injuries_age_s, injuries_ok, injuries_parsed, injuries_unidentifiable,
   player_values_fitted_through}, model: {sigma, gamma, lambda, shadow_mode: true},
   games: [ per game: game_id, date, tip_off, started, home_team/away_team (+ids), trusted, predicted_margin_base,
            home_delta, away_delta, lineup_adjustment_applied, lineup_note, predicted_margin, fair_home_spread,
            home_win_probability, away_win_probability, home_win_probability_base, home_absent[], away_absent[],
            and when a line is known: home_spread, home_covers_probability, away_covers_probability, model_edge_points ]}
  status meaning: ok = everything fresh; degraded = injury feed down so margins are rating-only;
  stale = upstream scoreboard down, serving last good data; error = nothing usable (games = []).
  An untrusted game has null probabilities -- render it as "not enough history", never as 50%.

LINES. Pass lines={game_id or "Away @ Home": home_spread} to get_slate(), or drop a nba_lines.json
(same shape) next to the data files and it is picked up automatically (re-read when it changes).

TRACKING LOG. get_slate() appends to nba_prediction_log.jsonl only when a game's prediction CHANGED
(margin / lineup deltas / line) and only before tip-off, so page reloads don't flood or pollute it.

Reference HTTP server (stdlib only), if you want something running before your own wiring is done:
    python nba_service.py --serve --port 8051 --refresh-minutes 30 [--cors]
      GET /api/nba/slate?date=YYYYMMDD     GET /api/nba/scorecard?season=2027     GET /health
"""

import argparse
import datetime
import json
import math
import os
import threading
import time

import core_nba_moneyline as mon
import core_nba_spread as cs
import nba_collect_boxscores as bx
import nba_track_lineup_results as tr
from nba_io import FileLock, LockBusy, load_json_retry

SCHEMA_VERSION = 1
LINES_FILE = "nba_lines.json"
REFRESH_LOCK = "nba_refresh.lock"
MAX_INJURY_STALENESS_S = 3600     # an injury list older than this is NOT trusted; fall back to rating-only


def clean(obj):
    """Make JSON-safe: NaN/inf -> null (browsers' JSON.parse rejects NaN), numpy scalars -> python."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        return clean(obj.item())
    return obj


def _stat_key(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _default_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date()   # ESPN slates follow US Eastern dates
    except Exception:
        return datetime.date.today()


class NBAService:
    def __init__(self, directory=".", scoreboard_ttl=60, injuries_ttl=300, now_fn=None, today_fn=None,
                 fetch_day=None, fetch_injuries_fn=None, collect_games=None, collect_boxes=None,
                 log_predictions=True):
        self.dir = directory
        self.scoreboard_ttl, self.injuries_ttl = scoreboard_ttl, injuries_ttl
        self._now_fn = now_fn or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._today_fn = today_fn or _default_today
        self._fetch_day = fetch_day or mon.collector.get_day
        self._fetch_inj = fetch_injuries_fn or cs.fetch_injuries
        self._collect_games = collect_games or mon.collector.collect_season
        self._collect_boxes = collect_boxes or bx.collect_season
        self._log = log_predictions
        self._lock = threading.RLock()
        self._ctx = {}            # season -> (key, ctx)
        self._board = {}          # date_str -> (fetched_at_epoch, events)
        self._inj = None          # (fetched_at_epoch, raw)
        self._last_good = {}      # date_str -> last good slate dict
        self._sigs = None
        self._lines_cache = (None, {})
        self._score_cache = {}
        self._refresh_info = {"last": None}
        self._stop = threading.Event()
        self.upstream_calls = {"scoreboard": 0, "injuries": 0}   # for monitoring/tests

    # ----------------------------------------------------------------- clocks
    def _now(self):
        return self._now_fn()

    def _epoch(self):
        return self._now().timestamp()

    # ----------------------------------------------------------------- cached state
    def _context(self, season):
        paths = [os.path.join(self.dir, f"nba_games_{season}.json"), os.path.join(self.dir, f"nba_games_{season - 1}.json"),
                 os.path.join(self.dir, f"nba_boxscores_{season}.json"), os.path.join(self.dir, cs.VALUES_FILE)]
        key = _stat_key(*paths)
        with self._lock:
            hit = self._ctx.get(season)
            if hit and hit[0] == key:
                return hit[1]
            ctx = cs.build_context(self.dir, season, max_age_days=10 ** 6)   # read path never refits stale values (refresh_data does); only a MISSING file is fitted once
            # the build can itself write the values file, so re-stat after
            self._ctx[season] = (_stat_key(*paths), ctx)
            return ctx

    def _scoreboard(self, date, warnings):
        ds = date.strftime("%Y%m%d")
        with self._lock:
            hit = self._board.get(ds)
            if hit and self._epoch() - hit[0] < self.scoreboard_ttl:
                return hit[1], self._epoch() - hit[0]
            try:
                self.upstream_calls["scoreboard"] += 1
                events = self._fetch_day(date).get("events") or []
                self._board[ds] = (self._epoch(), events)
                return events, 0.0
            except Exception as e:
                if hit:
                    warnings.append(f"scoreboard unreachable ({type(e).__name__}); showing data from {int(self._epoch() - hit[0])}s ago")
                    return hit[1], self._epoch() - hit[0]
                warnings.append(f"scoreboard unreachable ({type(e).__name__}) and no cached copy")
                return None, None

    def _injuries(self, warnings):
        with self._lock:
            if self._inj and self._epoch() - self._inj[0] < self.injuries_ttl:
                return self._inj[1], self._epoch() - self._inj[0]
            try:
                self.upstream_calls["injuries"] += 1
                raw = self._fetch_inj()
                self._inj = (self._epoch(), raw)
                return raw, 0.0
            except Exception as e:
                if self._inj and self._epoch() - self._inj[0] < MAX_INJURY_STALENESS_S:
                    warnings.append(f"injury feed unreachable ({type(e).__name__}); using a list {int(self._epoch() - self._inj[0])}s old")
                    return self._inj[1], self._epoch() - self._inj[0]
                warnings.append(f"injury feed unreachable ({type(e).__name__}); margins are rating-only")
                return None, None

    def _lines(self):
        path = os.path.join(self.dir, LINES_FILE)
        key = _stat_key(path)
        with self._lock:
            if self._lines_cache[0] == key:
                return self._lines_cache[1]
            try:
                data = load_json_retry(path, {}) or {}
            except Exception:
                data = {}
            self._lines_cache = (key, data)
            return data

    # ----------------------------------------------------------------- READ path
    def get_slate(self, date=None, lines=None):
        """date: None (today, US Eastern), 'YYYYMMDD', 'YYYY-MM-DD' or datetime.date. Never raises."""
        warnings = []
        try:
            d = self._parse_date(date)
        except Exception:
            return clean({"schema_version": SCHEMA_VERSION, "generated_at": self._now().isoformat(timespec="seconds"),
                          "date": None, "season": None, "status": "error", "warnings": [f"bad date: {date!r}"], "games": []})
        ds = d.isoformat()
        season = mon.season_for_date(d)
        base = {"schema_version": SCHEMA_VERSION, "generated_at": self._now().isoformat(timespec="seconds"),
                "date": ds, "season": season,
                "model": {"sigma": cs.SIGMA, "gamma": cs.GAMMA, "lambda": cs.LAMBDA, "shadow_mode": True}}
        try:
            ctx = self._context(season)
        except Exception as e:
            return self._fallback(ds, base, warnings, f"model data unavailable ({type(e).__name__}: {e})")
        events, board_age = self._scoreboard(d, warnings)
        if events is None:
            return self._fallback(ds, base, warnings, None)
        inj, inj_age = self._injuries(warnings)
        try:
            use_lines = lines if lines is not None else self._lines()
            results, meta = cs.predict_from_context(ctx, events, inj, use_lines)
        except Exception as e:
            return self._fallback(ds, base, warnings, f"prediction failed ({type(e).__name__}: {e})")
        now = self._now()
        for r in results:
            t = cs.tip_off(r)
            r["tip_off"] = t.isoformat(timespec="minutes") if t else None
            r["started"] = bool(t and now >= t)
        if self._log:
            try:
                with self._lock:
                    if self._sigs is None:
                        self._sigs = cs.load_last_signatures(self.dir)
                    cs.log_predictions(results, self.dir, now=now, dedupe=True, last_sigs=self._sigs, skip_started=True)
            except Exception as e:
                warnings.append(f"tracking log write failed ({type(e).__name__})")
        stale = board_age is not None and board_age > self.scoreboard_ttl
        status = "stale" if stale else ("degraded" if not meta["inj_ok"] else "ok")
        out = dict(base)
        out.update({"status": status, "warnings": warnings, "games": results,
                    "data": {"scoreboard_age_s": round(board_age, 1) if board_age is not None else None,
                             "injuries_age_s": round(inj_age, 1) if inj_age is not None else None,
                             "injuries_ok": meta["inj_ok"], "injuries_parsed": meta["injuries_parsed"],
                             "injuries_unidentifiable": meta["unmatched_injuries"],
                             "player_values_fitted_through": meta["values_fitted_through"]}})
        out = clean(out)
        with self._lock:
            self._last_good[ds] = out
        return out

    def _fallback(self, ds, base, warnings, reason):
        if reason:
            warnings.append(reason)
        with self._lock:
            prev = self._last_good.get(ds)
        if prev:
            out = dict(prev)
            out.update({"status": "stale", "warnings": warnings + ["serving the last good result"],
                        "generated_at": base["generated_at"]})
            return out
        out = dict(base)
        out.update({"status": "error", "warnings": warnings, "games": []})
        return clean(out)

    @staticmethod
    def _parse_date(date):
        if date is None:
            raise TypeError
        if isinstance(date, datetime.datetime):
            return date.date()
        if isinstance(date, datetime.date):
            return date
        s = str(date)
        return datetime.datetime.strptime(s, "%Y-%m-%d" if "-" in s else "%Y%m%d").date()

    def get_slate_today(self, lines=None):
        return self.get_slate(self._today_fn(), lines)

    def get_scorecard(self, season=None):
        """Shadow-mode scorecard as a dict (see nba_track_lineup_results.summarize). Cached until a file changes."""
        try:
            season = season or mon.season_for_date(self._today_fn())
            gpath = os.path.join(self.dir, f"nba_games_{season}.json")
            lpath = os.path.join(self.dir, tr.LOG_FILE)
            key = _stat_key(gpath, lpath)
            with self._lock:
                hit = self._score_cache.get(season)
                if hit and hit[0] == key:
                    return hit[1]
            games = (load_json_retry(gpath, {"games": {}}) or {"games": {}})["games"]
            log = [r for r in tr.load_log(self.dir) if r.get("season") == season]
            out = {"schema_version": SCHEMA_VERSION, "season": season, "status": "ok",
                   "generated_at": self._now().isoformat(timespec="seconds"),
                   "scorecard": tr.summarize(tr.score(tr.pick_pregame_predictions(log, games), games))}
            out = clean(out)
            with self._lock:
                self._score_cache[season] = (key, out)
            return out
        except Exception as e:
            return {"schema_version": SCHEMA_VERSION, "status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def health(self):
        season = mon.season_for_date(self._today_fn())
        def age(path):
            try:
                return round(self._epoch() - os.stat(os.path.join(self.dir, path)).st_mtime, 0)
            except OSError:
                return None
        return clean({"status": "ok", "now": self._now().isoformat(timespec="seconds"), "season": season,
                      "file_age_s": {"games": age(f"nba_games_{season}.json"), "boxscores": age(f"nba_boxscores_{season}.json"),
                                     "player_values": age(cs.VALUES_FILE), "prediction_log": age(cs.LOG_FILE)},
                      "last_refresh": self._refresh_info["last"], "upstream_calls": dict(self.upstream_calls)})

    # ----------------------------------------------------------------- WRITE path
    def refresh_data(self, season=None, refit=False):
        """Collect new games + boxscores and refresh player values. Never raises; returns a status dict."""
        started = time.time()
        lock = FileLock(os.path.join(self.dir, REFRESH_LOCK), timeout=0.0, stale_after=1800)
        try:
            lock.__enter__()
        except LockBusy:
            return {"status": "busy"}
        try:
            season = season or mon.season_for_date(self._today_fn())
            self._collect_games(season, directory=self.dir, quiet=True)
            self._collect_games(season - 1, directory=self.dir, quiet=True)
            self._collect_boxes(season, self.dir, quiet=True)
            values = cs.fit_player_values(self.dir, refit=refit)
            info = {"status": "ok", "season": season, "values_fitted_through": values["fitted_through"],
                    "finished_at": self._now().isoformat(timespec="seconds"), "duration_s": round(time.time() - started, 1)}
        except Exception as e:
            info = {"status": "error", "error": f"{type(e).__name__}: {e}",
                    "finished_at": self._now().isoformat(timespec="seconds")}
        finally:
            lock.release()
        self._refresh_info["last"] = info
        return info

    def start_background_refresh(self, interval_s=1800, first_delay_s=0):
        """Daemon thread that calls refresh_data() every interval_s. Call stop() to end it."""
        self._stop.clear()

        def loop():
            if first_delay_s:
                self._stop.wait(first_delay_s)
            while not self._stop.is_set():
                self.refresh_data()
                self._stop.wait(interval_s)
        t = threading.Thread(target=loop, name="nba-refresh", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()


# ===================================================================== reference HTTP server
def serve(service, host="127.0.0.1", port=8051, cors=False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/nba/slate":
                self._send(service.get_slate(q["date"][0] if "date" in q else None))
            elif u.path == "/api/nba/scorecard":
                self._send(service.get_scorecard(int(q["season"][0]) if "season" in q else None))
            elif u.path in ("/health", "/api/nba/health"):
                self._send(service.health())
            else:
                self._send({"status": "error", "warnings": ["not found"]}, 404)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/api/nba/slate, /api/nba/scorecard, /health). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="NBA model service (website-facing layer)")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8051)
    ap.add_argument("--cors", action="store_true")
    ap.add_argument("--refresh-minutes", type=int, default=30, help="Background data refresh interval (0 = off)")
    ap.add_argument("--refresh-once", action="store_true", help="Run one data refresh and exit (for a scheduler)")
    args = ap.parse_args()
    svc = NBAService(args.dir)
    if args.refresh_once:
        print(json.dumps(svc.refresh_data(), indent=2))
        return
    if args.serve:
        if args.refresh_minutes > 0:
            svc.start_background_refresh(args.refresh_minutes * 60)
        serve(svc, args.host, args.port, args.cors)
    else:
        print(json.dumps(svc.get_slate_today(), indent=2))


if __name__ == "__main__":
    main()
'''


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        module.__file__ = f"<nba_platform:{name}.py>"
        exec(compile(_SRC[name], module.__file__, "exec"), module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _SRC:
            return importlib.util.spec_from_loader(fullname, _Loader())
        return None


def _install():
    """The embedded copies win over any same-named .py files lying around, so behaviour never depends on stale local copies."""
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
    for n in _ORDER:
        sys.modules.pop(n, None)


_install()
import backtest_nba_player_props as bp          # noqa: E402
import core_nba_moneyline as mon                # noqa: E402
import core_nba_player_props as cp              # noqa: E402
import core_nba_spread as cs                    # noqa: E402
import nba_collect_boxscores as bx              # noqa: E402
import nba_service as svc_mod                   # noqa: E402
import nba_track_prop_results as ptr            # noqa: E402
from nba_io import FileLock, LockBusy, atomic_json_dump, load_json_retry   # noqa: E402

PROP_LINES_FILE = "nba_prop_lines.json"
SCHEMA_VERSION = 1
clean = svc_mod.clean
_stat_key = svc_mod._stat_key


# ===================================================================== platform layer
class NBAPlatform(svc_mod.NBAService):
    """Game layer (NBAService) + player-props layer, one object, one refresh, one set of caches."""

    def __init__(self, directory=".", prop_params_path=None, pra_mode="sum", **kw):
        super().__init__(directory, **kw)
        self.prop_params_path = prop_params_path or os.path.join(directory, cp.PARAMS_FILE)
        self.pra_mode = pra_mode
        self._pctx = {}                       # season -> (key, ctx)
        self._plock = threading.RLock()       # props context build must not block game reads
        self._psigs = None
        self._plines = (None, {})
        self._pgood = {}
        self._pscore = {}

    # ------------------------------------------------------------- props context
    def _props_key(self, season):
        paths = [self.prop_params_path] + [os.path.join(self.dir, bx.boxscore_file_for(s)) for s in range(season - cp.HISTORY_SEASONS + 1, season + 1)]
        return _stat_key(*paths)

    def _props_context(self, season, force=False):
        key = self._props_key(season)
        with self._plock:
            hit = self._pctx.get(season)
            if hit and hit[0] == key and not force:
                return hit[1]
            if not os.path.exists(self.prop_params_path):
                raise FileNotFoundError(f"{os.path.basename(self.prop_params_path)} not found; run the props backtest with --out (see the docstring)")
            ctx = cp.build_context(self.dir, season, cp.load_params(self.prop_params_path), pra_direct=(self.pra_mode == "direct"))
            self._pctx[season] = (key, ctx)
            return ctx

    def _prop_lines(self):
        path = os.path.join(self.dir, PROP_LINES_FILE)
        key = _stat_key(path)
        with self._plock:
            if self._plines[0] == key:
                return self._plines[1]
            try:
                data = load_json_retry(path, {}) or {}
            except Exception:
                data = {}
            self._plines = (key, data)
            return data

    # ------------------------------------------------------------- READ path: props
    def props_rows(self, date=None, lines=None):
        """-> (rows, meta, warnings, status). Raw one-row-per-(player, stat) records; used by get_props and the CLI table."""
        warnings = []
        d = self._parse_date(date) if date is not None else self._today_fn()
        season = mon.season_for_date(d)
        ctx = self._props_context(season)
        events, board_age = self._scoreboard(d, warnings)
        if events is None:
            raise RuntimeError("scoreboard unreachable and no cached copy")
        inj, inj_age = self._injuries(warnings)
        rows, meta = cp.predict_from_context(ctx, events, inj, lines if lines is not None else self._prop_lines(), self.pra_mode)
        now = self._now()
        for r in rows:
            t = cs.tip_off(r)
            r["tip_off"] = t.isoformat(timespec="minutes") if t else None
            r["started"] = bool(t and now >= t)
        meta.update({"season": season, "date": d.isoformat(), "scoreboard_age_s": board_age, "injuries_age_s": inj_age})
        if ctx["last_date"] >= d.isoformat():
            warnings.append(f"cached box scores already run through {ctx['last_date']}; projections for {d.isoformat()} may include games already played")
        elif (datetime.date.fromisoformat(d.isoformat()) - datetime.date.fromisoformat(ctx["last_date"])).days > 10:
            warnings.append(f"box scores are {(d - datetime.date.fromisoformat(ctx['last_date'])).days} days old; run refresh")
        if self._log:
            try:
                with self._plock:
                    if self._psigs is None:
                        self._psigs = cp.load_last_signatures(self.dir)
                    cp.log_predictions(rows, self.dir, now=now, last_sigs=self._psigs, skip_started=True)
            except Exception as e:
                warnings.append(f"prop log write failed ({type(e).__name__})")
        status = "degraded" if not meta["inj_ok"] else "ok"
        if not meta["inj_ok"]:
            warnings.append("injury feed unavailable: players who are OUT may be included")
        return rows, meta, warnings, status

    def get_props(self, date=None, lines=None):
        """One entry per player (games grouped), each with pts/reb/ast/pra blocks. Never raises."""
        base = {"schema_version": SCHEMA_VERSION, "generated_at": self._now().isoformat(timespec="seconds"),
                "model": {"shadow_mode": True, "pra_mode": self.pra_mode,
                          "note": "projection assumes the player plays his usual role; teammate-out usage boosts are NOT modeled"}}
        try:
            rows, meta, warnings, status = self.props_rows(date, lines)
        except Exception as e:
            ds = None
            try:
                ds = self._parse_date(date).isoformat() if date is not None else self._today_fn().isoformat()
            except Exception:
                pass
            with self._plock:
                prev = self._pgood.get(ds)
            if prev:
                out = dict(prev)
                out.update({"status": "stale", "generated_at": base["generated_at"],
                            "warnings": [f"{type(e).__name__}: {e}", "serving the last good result"]})
                return out
            return clean(dict(base, date=ds, status="error", warnings=[f"{type(e).__name__}: {e}"], players=[]))
        players = {}
        for r in rows:
            k = (r["game_id"], r["player_id"])
            p = players.get(k)
            if p is None:
                p = players[k] = {"game_id": r["game_id"], "tip_off": r["tip_off"], "started": r["started"], "player_id": r["player_id"],
                                  "player": r["player"], "team": r["team"], "team_id": r["team_id"], "opp": r["opp"], "opp_id": r["opp_id"],
                                  "home": bool(r["home"]), "position_group": r["group"], "projected_minutes": r["emin"],
                                  "games_of_history": r["n_prior"], "status": r["status"], "teammates_out": r["teammates_out"], "props": {}}
            blk = {k2: r.get(k2) for k2 in ("mu", "last10", "fair_line", "opp_index", "line", "p_over", "p_under", "p_push",
                                            "over_odds", "under_odds", "ev_over", "ev_under")}
            blk["projection"] = blk.pop("mu")
            p["props"][r["stat"]] = blk
        plist = sorted(players.values(), key=lambda p: (p["tip_off"] or "", p["team"], -p["projected_minutes"]))
        out = clean(dict(base, date=meta["date"], season=meta["season"], status=status, warnings=warnings, players=plist,
                         data={"box_scores_through": meta["data_through"], "scoreboard_age_s": meta["scoreboard_age_s"],
                               "injuries_age_s": meta["injuries_age_s"], "injuries_ok": meta["inj_ok"],
                               "injuries_parsed": meta["injuries_parsed"], "injuries_unidentifiable": meta["unmatched_injuries"]}))
        with self._plock:
            self._pgood[meta["date"]] = out
        return out

    def get_props_scorecard(self, season=None):
        """Shadow-mode props scorecard as a dict: per prop bias / MAE / minutes error / line calibration."""
        try:
            season = season or mon.season_for_date(self._today_fn())
            bpath = os.path.join(self.dir, f"nba_boxscores_{season}.json")
            lpath = os.path.join(self.dir, ptr.LOG_FILE)
            key = _stat_key(bpath, lpath)
            with self._plock:
                hit = self._pscore.get(season)
                if hit and hit[0] == key:
                    return hit[1]
            log = [r for r in ptr.load_log(self.dir) if r.get("season") == season]
            actuals, finished = ptr.load_actuals(self.dir, season)
            rows, voided, pending = ptr.join(log, actuals, finished)
            out = clean({"schema_version": SCHEMA_VERSION, "season": season, "status": "ok", "graded": len(rows), "voided_dnp": voided,
                         "awaiting_results": pending, "generated_at": self._now().isoformat(timespec="seconds"),
                         "by_stat": summarize_props(rows),
                         "note": "needs several hundred graded props per stat (more with lines) before conclusions"})
            with self._plock:
                self._pscore[season] = (key, out)
            return out
        except Exception as e:
            return {"schema_version": SCHEMA_VERSION, "status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    # ------------------------------------------------------------- WRITE path
    def refresh_data(self, season=None, refit=False):
        info = super().refresh_data(season, refit)
        if info.get("status") != "busy":      # props do not depend on the player-value fit: a game-side failure must not freeze them
            try:
                t0 = time.time()
                self._props_context(info.get("season") or season or mon.season_for_date(self._today_fn()), force=True)
                info["props_context_s"] = round(time.time() - t0, 1)
            except Exception as e:
                info["props_error"] = f"{type(e).__name__}: {e}"
        self._refresh_info["last"] = info
        return info

    def health(self):
        h = super().health()
        season = h.get("season")
        h["props"] = {"params_file": os.path.exists(self.prop_params_path),
                      "context_cached": season in self._pctx,
                      "box_scores_through": (self._pctx[season][1]["last_date"] if season in self._pctx else None)}
        return clean(h)


def summarize_props(rows):
    """per-stat dict from joined (log record, actual) pairs."""
    import numpy as np
    out = {}
    for stat in bp.STATS:
        rs = [(r, a) for r, a in rows if r["stat"] == stat]
        if not rs:
            continue
        mu = np.array([r["mu"] for r, a in rs])
        y = np.array([a[stat] for r, a in rs], dtype=float)
        e = {"n": len(rs), "mean_actual": float(y.mean()), "mean_projected": float(mu.mean()), "bias": float((y - mu).mean()),
             "mae": float(np.abs(y - mu).mean()),
             "minutes_mae": float(np.mean([abs(a["min"] - r["emin"]) for r, a in rs]))}
        t10 = [(r["last10"], yy, m) for (r, a), yy, m in zip(rs, y, mu) if r.get("last10") is not None]
        if len(t10) > 30:
            e["mae_last10_baseline"] = float(np.mean([abs(yy - t) for t, yy, m in t10]))
            e["mae_model_same_rows"] = float(np.mean([abs(yy - m) for t, yy, m in t10]))
        wl = [(r, a) for r, a in rs if r.get("line") is not None and a[stat] != r["line"]]
        e["with_lines"] = len(wl)
        if len(wl) >= 30:
            po = np.array([r["p_over"] for r, a in wl])
            ov = np.array([float(a[stat] > r["line"]) for r, a in wl])
            e["lines"] = {"over_rate_actual": float(ov.mean()), "over_rate_model": float(po.mean()),
                          "brier": float(np.mean((po - ov) ** 2)), "brier_coin_flip": 0.25}
            bets = []
            for r, a in wl:
                for side in ("over", "under"):
                    ev, odds = r.get(f"ev_{side}"), r.get(f"{side}_odds")
                    if ev is None or odds is None or ev < 0.03:
                        continue
                    won = (a[stat] > r["line"]) if side == "over" else (a[stat] < r["line"])
                    bets.append((1 + (odds / 100 if odds > 0 else 100 / -odds)) - 1 if won else -1.0)
            if bets:
                b = np.array(bets)
                e["lines"]["bets_ev_ge_3pct"] = {"n": len(b), "flat_roi": float(b.mean()),
                                                 "se": float(b.std(ddof=1) / math.sqrt(len(b))) if len(b) > 1 else None}
        out[stat] = e
    return out


# ===================================================================== HTTP
def serve(platform, host="127.0.0.1", port=8051, cors=False):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            season = int(q["season"]) if q.get("season", "").isdigit() else None
            routes = {"/api/nba/slate": lambda: platform.get_slate(q.get("date")),
                      "/api/nba/props": lambda: platform.get_props(q.get("date")),
                      "/api/nba/scorecard": lambda: platform.get_scorecard(season),
                      "/api/nba/props/scorecard": lambda: platform.get_props_scorecard(season),
                      "/health": platform.health, "/api/nba/health": platform.health}
            fn = routes.get(u.path)
            if fn is None:
                self._send({"status": "error", "warnings": ["not found"]}, 404)
            else:
                self._send(fn())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/api/nba/slate, /props, /scorecard, /props/scorecard, /health). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ===================================================================== CLI
def _print_slate(slate):
    print(f"\nGAMES {slate.get('date')}  [status {slate.get('status')}]")
    for w in slate.get("warnings") or []:
        print(f"  [warn] {w}")
    games = slate.get("games") or []
    if not games:
        print("  no not-yet-played regular-season games found")
        return
    print(f"  {'matchup':<42}{'margin':>8}{'home%':>8}   line  cover%")
    for g in games:
        m = f"{g['away_team']} @ {g['home_team']}"[:41]
        if not g.get("trusted"):
            print(f"  {m:<42}  (not enough history)")
            continue
        ln = f"{g['home_spread']:+.1f} {g['home_covers_probability'] * 100:5.1f}%" if g.get("home_spread") is not None else "  --      --"
        print(f"  {m:<42}{g['predicted_margin']:>+8.1f}{g['home_win_probability'] * 100:>7.1f}%   {ln}")
        for side in ("home", "away"):
            for a in (g.get(f"{side}_absent") or [])[:3]:
                print(f"      {side} {a['name']} ({a['status']})")


def _print_scorecards(season, p):
    for title, fn in (("GAME SCORECARD", p.get_scorecard), ("PROPS SCORECARD", p.get_props_scorecard)):
        print(f"\n{title} season {season}")
        print(json.dumps(fn(season), indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="NBA platform: models, props, data, tracking, service -- one file")
    ap.add_argument("--dir", default=".", help="folder with the nba_*.json files (default: current)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("daily", "slate", "props"):
        s = sub.add_parser(name)
        s.add_argument("--date", default=None, help="YYYYMMDD (default: today, US Eastern)")
        s.add_argument("--no-refresh", action="store_true")
        s.add_argument("--no-log", action="store_true")
        s.add_argument("--only", default=None, help="props: show players whose name contains this text")
        s.add_argument("--pra", choices=["sum", "direct"], default="sum")
    sub.add_parser("refresh")
    t = sub.add_parser("track")
    t.add_argument("--season", type=int, default=None)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8051)
    sv.add_argument("--cors", action="store_true")
    sv.add_argument("--refresh-minutes", type=int, default=30)
    r = sub.add_parser("run", help="run an embedded module's own command line")
    r.add_argument("module")
    r.add_argument("rest", nargs=argparse.REMAINDER)
    u = sub.add_parser("unpack")
    u.add_argument("outdir")
    for sp in sub.choices.values():          # allow --dir after the command too
        if sp is not r:
            sp.add_argument("--dir", default=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.cmd == "run":
        if args.module not in _SRC:
            sys.exit(f"unknown module '{args.module}'. Available: {', '.join(_ORDER)}")
        mod = sys.modules.get(args.module) or importlib.import_module(args.module)
        if not hasattr(mod, "main"):
            sys.exit(f"{args.module} has no command line")
        sys.argv = [args.module] + [a for a in args.rest if a != "--"]
        return mod.main()
    if args.cmd == "unpack":
        os.makedirs(args.outdir, exist_ok=True)
        for n in _ORDER:
            with open(os.path.join(args.outdir, n + ".py"), "w") as f:
                f.write(_SRC[n])
        print(f"wrote {len(_ORDER)} modules to {args.outdir}")
        return
    if args.cmd is None:
        ap.print_help()
        return

    p = NBAPlatform(args.dir, pra_mode=getattr(args, "pra", "sum"), log_predictions=not getattr(args, "no_log", False))
    if args.cmd == "refresh":
        print(json.dumps(p.refresh_data(), indent=2))
    elif args.cmd == "track":
        _print_scorecards(args.season or mon.season_for_date(p._today_fn()), p)
    elif args.cmd == "serve":
        if args.refresh_minutes > 0:
            p.start_background_refresh(args.refresh_minutes * 60)
        serve(p, args.host, args.port, args.cors)
    else:
        if not args.no_refresh:
            print(json.dumps(p.refresh_data(), indent=2))
        if args.cmd in ("daily", "slate"):
            _print_slate(p.get_slate(args.date))
        if args.cmd in ("daily", "props"):
            try:
                rows, meta, warnings, status = p.props_rows(args.date)
                for w in warnings:
                    print(f"  [warn] {w}")
                shown = [r for r in rows if not args.only or args.only.lower() in r["player"].lower()]
                cp.print_table(shown, meta)
            except Exception as e:
                print(f"\nPROPS unavailable: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
