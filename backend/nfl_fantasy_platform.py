#!/usr/bin/env python
"""
nfl_fantasy_platform.py  --  ONE FILE for the NFL fantasy model: data refresh, weekly projections (QB / RB / WR / TE / K / DEF),
your lineup, waiver ranking, per-league scoring and roster slots, and a web service for a live dashboard (Railway).

Embedded, unchanged and individually tested:
  nfl_fantasy_projections   the model (shrunk player points x opponent points-allowed-to-position x average, plus the small Vegas and
                            usage factors), Yahoo scoring from a league file, walk-forward backtest, lineup and waiver logic
  nfl_io                    atomic JSON writes, tolerant reads, cross-process file lock
plus the platform layer below: one class, FantasyPlatform.

LEAGUES: two are built in (Family Fantasy Football League 501858, 18 teams; Otuhiva Family League 572561, 12 teams).  On first run
they are written to <data dir>/nfl_league_<id>.json; edit those files (or drop in more nfl_league_<id>.json files) to change scoring or
roster slots.  Each league has its own tuned parameter file nfl_fantasy_params_<id>.json (falls back to nfl_fantasy_params.json).

DATA: nflverse (public GitHub releases, no key): weekly player + team stats, schedule with Vegas lines and scores, injury reports.
Files are cached in <data dir>/nflfp_data/.  Reads NEVER touch the network; refresh does (default every 30 minutes; the files only change
after games, mostly Tuesday).  Yahoo rosters / free agents are not readable without a Yahoo login: you give the platform your roster
(and optionally the players owned elsewhere) with POST /api/fantasy/roster and it ranks pickups and sets the best lineup from that.

------------------------------------------------------------------ WIRING (library)
    from nfl_fantasy_platform import FantasyPlatform
    fp = FantasyPlatform("/data")
    fp.refresh_data()                                # WRITE path: re-download current-season files. Never raises.
    fp.get_projections("572561", week=None, pos=None)    # cached, no network.  week=None -> next week with games
    fp.get_lineup("572561")                          # best start/bench from the saved roster
    fp.get_waivers("572561", top=15)                 # non-rostered players ranked by this week's lineup gain
    fp.search_players("allen")                       # names for a roster pick-list
    fp.set_roster("572561", ["Josh Allen", ...], taken=[...])
    fp.get_leagues(); fp.health()
  Every read returns a JSON-safe dict with "status" ("ok" | "error") and "warnings" and never raises.

------------------------------------------------------------------ HTTP (stdlib only) -- local or Railway
    python nfl_fantasy_platform.py serve [--dir PATH] [--port N] [--host H] [--refresh-minutes 30] [--cors] [--allow-write] [--bootstrap]
      GET  /api/fantasy/leagues
      GET  /api/fantasy/projections?league=572561&week=5&pos=QB
      GET  /api/fantasy/lineup?league=572561&week=5          GET /api/fantasy/waivers?league=572561&top=15
      GET  /api/fantasy/players?q=allen&league=572561        GET /health
      POST /api/fantasy/roster   {"league": "572561", "players": ["Josh Allen", ...], "taken": ["..."]}   (needs --allow-write)
      POST /api/fantasy/refresh  {"what": "data" | "params"}                                             (needs --allow-write)
  Environment variables: PORT (Railway sets it; binds 0.0.0.0), FANTASY_DATA_DIR (put on a Railway volume, e.g. /data),
    FANTASY_API_TOKEN (if set, POSTs need  Authorization: Bearer <token>), FANTASY_REFRESH_MINUTES, FANTASY_ALLOW_WRITE=1,
    FANTASY_CORS=1, FANTASY_BOOTSTRAP=1.

------------------------------------------------------------------ RAILWAY, step by step
  1. Put this file (alone) in a repo, add a Volume mounted at /data.
  2. Start command:   python nfl_fantasy_platform.py serve --bootstrap
     Variables:       FANTASY_DATA_DIR=/data  FANTASY_API_TOKEN=<long random string>  FANTASY_ALLOW_WRITE=1  FANTASY_CORS=1 (if a browser calls it)
  3. First boot: --bootstrap downloads 2018-now (about 20 small files) and tunes each league (~1-2 minutes each).  /health shows
     "bootstrap": running; until it finishes the projections use default parameters and carry an UNVALIDATED warning.
  4. Health check path: /health.   The dashboard calls /api/fantasy/projections, /lineup, /waivers.
  5. Each preseason (or if scoring changes): POST /api/fantasy/refresh {"what":"params"} or `python nfl_fantasy_platform.py bootstrap`.

------------------------------------------------------------------ COMMAND LINE
    python nfl_fantasy_platform.py projections --league 572561 [--week N] [--pos QB] [--top 15] [--no-refresh]
    python nfl_fantasy_platform.py lineup | waivers --league 572561 [--roster my.txt]    (--roster saves it first)
    python nfl_fantasy_platform.py refresh | bootstrap [--league ID] | leagues
    python nfl_fantasy_platform.py run nfl_fantasy_projections backtest --league nfl_league_572561.json
    python nfl_fantasy_platform.py unpack OUTDIR

STATUS: validated against RESULTS only (walk-forward 2019-2026): beats a last-3-games average by ~12 squared points and a position
average by ~11 on the top-ranked players; the opponent adjustment helps QBs clearly and little elsewhere; the Vegas and usage factors
are small refinements; K / DEF are close to unpredictable week to week.  Projections are conditional on the player playing.
NOT COVERED: playoffs/bye logic beyond "team not playing = not on the sheet", rookies with no NFL history, Yahoo login, trades,
"Extra Point Returned" scoring, defence yardage tiers.
Needs: python 3.9+ and nothing else (openpyxl only for the command-line xlsx).
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
_ORDER = ['nfl_io', 'nfl_fantasy_projections']
_SRC = {}
# ======================================================================
# embedded module: nfl_io.py (96 lines)
# ======================================================================
_SRC["nfl_io"] = r'''"""
nfl_io.py -- small file helpers so the fantasy data files are safe to read while a
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
# embedded module: nfl_fantasy_projections.py (859 lines)
# ======================================================================
_SRC["nfl_fantasy_projections"] = r'''"""
nfl_fantasy_projections.py  --  weekly fantasy projections for QB / RB / WR / TE under Yahoo PPR scoring.  Stdlib only.

FORMULA (the same shrunk index-multiplication used everywhere else in this project)
    projection = player_points_index x opponent_points_allowed_to_position_index x average_points
               = shrunk player average fantasy points per game  x  (defense's points allowed to the position / league average)^gamma
    player index   = player's own average points per game they played, shrunk toward the position's league average (k_p pseudo-games),
                     recent games weighted more (per-game decay), last season carried over at a reduced weight
    opponent index = points the defense allowed to that POSITION per game (all players of the position added together),
                     shrunk toward the league average (k_o pseudo-games), last season carried over at a reduced weight
    home advantage = optional small multiplier (tuned; the backtest decides whether it is worth having)
  Projections are conditional on the player playing (no injury forecast).  A player's games are the games he appeared in
  (a pass attempt, carry or target); players ruled Out on the injury report are removed from the sheet.

SCORING (Yahoo default PPR; edit SCORING or pass --scoring-file to change):
    passing yards 0.04/yd, passing TD 4, interception -1, rushing + receiving yards 0.1/yd, rushing + receiving TD 6,
    reception 1, fumble lost -2, 2-point conversion 2, kick/punt return TD 6, fumble-recovery TD 6.   No yardage bonuses.

DATA: nflverse (public, no key): weekly player stats  github.com/nflverse/nflverse-data  releases/stats_player/stats_player_week_YYYY.csv
      schedule + Vegas lines (releases/schedules/games.csv) and weekly injury reports (releases/injuries/injuries_YYYY.csv).
      Files are cached in  <dir>/nflfp_data/ ; the current season is re-downloaded when older than 6 hours.

COMMANDS (run in your NFL folder)
    python nfl_fantasy_projections.py backtest [--first 2018 --last 2026]     # walk-forward tune + validate, writes nfl_fantasy_params.json
    python nfl_fantasy_projections.py project  [--season 2026 --week 5]        # writes nfl_projections_wkN.csv (+ .xlsx if openpyxl is installed)
    python nfl_fantasy_projections.py check                                    # verifies the Yahoo scoring code against nflverse's own totals
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.request

BASE = "https://github.com/nflverse/nflverse-data/releases/download"
URL_WEEK = BASE + "/stats_player/stats_player_week_{y}.csv"
URL_GAMES = BASE + "/schedules/games.csv"
URL_INJ = BASE + "/injuries/injuries_{y}.csv"
URL_TEAM = BASE + "/stats_team/stats_team_week_{y}.csv"
POSITIONS = ("QB", "RB", "WR", "TE")
EXTRA = ("K", "DEF")
ALLPOS = POSITIONS + EXTRA
GROUPS = {"skill": POSITIONS, "kd": EXTRA}
POS_MAP = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE", "K": "K"}
PARAMS_NAME = "nfl_fantasy_params.json"
LEAGUE_TAG = ""

SCORING = dict(pass_yd=0.04, pass_td=4.0, intc=-1.0, rush_yd=0.1, rush_td=6.0, rec=1.0, rec_yd=0.1, rec_td=6.0,
               fumble_lost=-2.0, two_pt=2.0, st_td=6.0, fum_rec_td=6.0,
               k_fg_u40=3.0, k_fg_40=4.0, k_fg_50=5.0, k_pat=1.0, k_pat_miss=0.0, k_fg_miss=0.0,
               d_sack=1.0, d_int=2.0, d_fum=2.0, d_td=6.0, d_safety=2.0, d_block=2.0,
               d_pa=[[0, 10], [6, 7], [13, 4], [20, 1], [27, 0], [34, -1], [999, -4]])   # Yahoo default D/ST: (max points allowed, pts)
SCORING_NFLVERSE = dict(SCORING, intc=-2.0)             # what nflverse's own fantasy_points_ppr column uses (used by `check`)

DEFAULTS = dict(k_p=3.0, rd=0.9, carry=0.5, k_o=8.0, rdo=1.0, carry_o=0.3, gamma=1.0, hfa=0.0, carry_lg=0.5, beta=0.0, delta=0.0)
TOP_K = {"QB": 24, "RB": 40, "WR": 60, "TE": 24, "K": 20, "DEF": 20}      # evaluation set: the model's top-K per position each week (not chosen on results)
MEANINGFUL = {"QB": lambda r: r["att"] >= 10, "RB": lambda r: r["car"] + r["tgt"] >= 8,
              "WR": lambda r: r["tgt"] >= 3, "TE": lambda r: r["tgt"] >= 2, "K": lambda r: True, "DEF": lambda r: True}       # roles used for the position prior (past games only)


# ============================================================================================ data
def _num(x):
    try:
        return float(x) if x not in ("", "NA", None) else 0.0
    except ValueError:
        return 0.0


def fantasy_points(r, sc=SCORING):
    """Fantasy points from a raw nflverse weekly row (dict of strings or numbers)."""
    g = lambda k: _num(r.get(k))
    fl = g("sack_fumbles_lost") + g("rushing_fumbles_lost") + g("receiving_fumbles_lost")
    return (g("passing_yards") * sc["pass_yd"] + g("passing_tds") * sc["pass_td"] + g("passing_interceptions") * sc["intc"]
            + g("rushing_yards") * sc["rush_yd"] + g("rushing_tds") * sc["rush_td"]
            + g("receptions") * sc["rec"] + g("receiving_yards") * sc["rec_yd"] + g("receiving_tds") * sc["rec_td"]
            + fl * sc["fumble_lost"]
            + (g("passing_2pt_conversions") + g("rushing_2pt_conversions") + g("receiving_2pt_conversions")) * sc["two_pt"]
            + g("special_teams_tds") * sc["st_td"] + g("fumble_recovery_tds") * sc["fum_rec_td"])


def kicker_points(r, sc=SCORING):
    g = lambda k: _num(r.get(k))
    return ((g("fg_made_0_19") + g("fg_made_20_29") + g("fg_made_30_39")) * sc["k_fg_u40"] + g("fg_made_40_49") * sc["k_fg_40"]
            + (g("fg_made_50_59") + g("fg_made_60_")) * sc["k_fg_50"] + g("pat_made") * sc["k_pat"] + max(0.0, g("pat_att") - g("pat_made")) * sc["k_pat_miss"] + g("fg_missed") * sc["k_fg_miss"])


def defense_points(r, points_allowed, sc=SCORING):
    g = lambda k: _num(r.get(k))
    pts = (g("def_sacks") * sc["d_sack"] + g("def_interceptions") * sc["d_int"] + g("fumble_recovery_opp") * sc["d_fum"]
           + (g("def_tds") + g("special_teams_tds")) * sc["d_td"] + g("def_safeties") * sc["d_safety"]
           + (g("def_punt_blocks") + g("def_pat_blocks") + g("def_fg_blocks")) * sc["d_block"])
    for cap, v in sc["d_pa"]:
        if points_allowed <= cap:
            return pts + v
    return pts


def download(url, path, max_age_h=None, timeout=120):
    if os.path.exists(path) and (max_age_h is None or time.time() - os.stat(path).st_mtime < max_age_h * 3600):
        return path
    req = urllib.request.Request(url, headers={"User-Agent": "sports-hub-fantasy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def data_dir(directory):
    return os.path.join(directory, "nflfp_data")


def current_season(today=None):
    d = today or dt.date.today()
    return d.year if d.month >= 3 else d.year - 1


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_games(directory, fetch=download):
    p = fetch(URL_GAMES, os.path.join(data_dir(directory), "games.csv"), max_age_h=6)
    return read_csv(p)


def load_weekly(directory, seasons, fetch=download, cur=None, scoring=SCORING):
    """-> list of player-game dicts (REG season; QB/RB/WR/TE with at least one attempt / carry / target; K with a kick; DEF = team defence),
    oldest first.  Each row carries the team's Vegas implied points (impl) and the opponent's (oimpl) when the schedule has them."""
    cur = cur if cur is not None else current_season()
    games = {g["game_id"]: g for g in load_games(directory, fetch)}
    out = []

    def lines(g, team):
        hp, ap = implied_points(g) if g else (None, None)
        return (hp, ap) if (g and g["home_team"] == team) else (ap, hp)

    for y in seasons:
        age = 6 if y >= cur else None
        p = fetch(URL_WEEK.format(y=y), os.path.join(data_dir(directory), f"week_{y}.csv"), max_age_h=age)
        for r in read_csv(p):
            if r.get("season_type") != "REG" or r.get("position") not in POS_MAP:
                continue
            att, car, tgt = _num(r.get("attempts")), _num(r.get("carries")), _num(r.get("targets"))
            pos = POS_MAP[r["position"]]
            if pos == "K":
                if _num(r.get("fg_att")) + _num(r.get("pat_att")) <= 0:
                    continue
                pts = kicker_points(r, scoring)
            else:
                if att + car + tgt <= 0:
                    continue
                pts = fantasy_points(r, scoring)
            g = games.get(r.get("game_id"))
            im, om = lines(g, r["team"])
            out.append({"season": int(r["season"]), "week": int(r["week"]), "pid": r["player_id"],
                        "name": r.get("player_display_name") or r.get("player_name"), "pos": pos,
                        "team": r["team"], "opp": r["opponent_team"], "home": bool(g and g["home_team"] == r["team"]),
                        "pts": pts, "att": att, "car": car, "tgt": tgt, "impl": im, "oimpl": om,
                        "rec": _num(r.get("receptions")), "ryds": _num(r.get("rushing_yards")),
                        "pyds": _num(r.get("passing_yards")), "recyds": _num(r.get("receiving_yards")),
                        "ppr_nflverse": _num(r.get("fantasy_points_ppr"))})
        try:
            tp = fetch(URL_TEAM.format(y=y), os.path.join(data_dir(directory), f"team_{y}.csv"), max_age_h=age)
            trows = read_csv(tp)
        except Exception:
            trows = []
        for r in trows:
            g = games.get(r.get("game_id"))
            if r.get("season_type") != "REG" or not g or g.get("home_score") in (None, "", "NA"):
                continue
            home = g["home_team"] == r["team"]
            pa = _num(g["away_score"] if home else g["home_score"])
            im, om = lines(g, r["team"])
            out.append({"season": int(r["season"]), "week": int(r["week"]), "pid": "DEF_" + r["team"], "name": r["team"] + " D/ST", "pos": "DEF",
                        "team": r["team"], "opp": r["opponent_team"], "home": home, "pts": defense_points(r, pa, scoring),
                        "att": 0, "car": 0, "tgt": 0, "impl": im, "oimpl": om, "rec": 0, "ryds": 0, "pyds": 0, "recyds": 0, "ppr_nflverse": 0.0})
    out.sort(key=lambda r: (r["season"], r["week"]))
    return out


# ============================================================================================ model
class Walker:
    """Chronological state: per-player decayed point sums, per-(defense, position) allowed points, league means.
    advance(key) applies the season carry-over; predict(...) reads the state; update(rows) adds a finished week."""

    def __init__(self, params):
        self.p = dict(DEFAULTS, **params)
        self.pl = {}                                   # pid -> [S, N, count, last3 list, pos, team, last (season, week)]
        self.op = {}                                   # (def, pos) -> [S, N]
        self.lp = {pos: [0.0, 0.0] for pos in ALLPOS}     # league per-appearance points (meaningful roles): sum, n
        self.lo = {pos: [0.0, 0.0] for pos in ALLPOS}
        self.ti = {}                                         # team -> [sum, n] of its own implied points (decayed): the team's usual scoring level
        self.li = [0.0, 0.0]                                 # league mean team implied points: sum, n     # league per-team-game position totals: sum, n
        self.season = None

    def new_season(self, season):
        if self.season is not None and season != self.season:
            c, co, cl = self.p["carry"], self.p["carry_o"], self.p["carry_lg"]
            for v in self.pl.values():
                for i in (0, 1, 7, 8, 9):
                    v[i] *= c
            self.li[0] *= cl
            self.li[1] *= cl
            for t in self.ti.values():
                t[0] *= c
                t[1] *= c
            for v in self.op.values():
                v[0] *= co
                v[1] *= co
            for d in (self.lp, self.lo):
                for v in d.values():
                    v[0] *= cl
                    v[1] *= cl
        self.season = season

    def league_prior(self, pos):
        s, n = self.lp[pos]
        return s / n if n > 0 else None

    def opp_index(self, defense, pos):
        s, n = self.lo[pos]
        if n <= 0:
            return 1.0, None
        lo = s / n
        so, no = self.op.get((defense, pos), (0.0, 0.0))
        if lo <= 0:
            return 1.0, lo
        return ((so + self.p["k_o"] * lo) / (no + self.p["k_o"])) / lo, lo

    def predict(self, pid, pos, defense, home, impl=None, oimpl=None, opp_team=None):
        v = self.pl.get(pid)
        m = self.league_prior(pos)
        if v is None or m is None or v[1] <= 0:
            return None
        rate = (v[0] + self.p["k_p"] * m) / (v[1] + self.p["k_p"])
        oi, _ = self.opp_index(defense, pos)
        h = (1.0 + self.p["hfa"]) if home else 1.0 / (1.0 + self.p["hfa"])
        base = rate * (oi ** self.p["gamma"]) * h
        role = 1.0
        if v[9] > 0 and v[1] > 0:                                    # recent opportunities (att + carries + targets) vs the player's longer-run level
            slow = v[9] / v[1]
            fast = (v[7] + slow) / (v[8] + 1.0)
            role = min(1.3, max(0.7, fast / slow)) ** self.p["delta"]
        veg = 1.0
        if self.li[1] > 0:
            lg = self.li[0] / self.li[1]
            # this game's expected scoring relative to the team's own usual level (the player's average already contains that level)
            def usual(team):
                s_, n_ = self.ti.get(team, (0.0, 0.0))
                return (s_ + 4.0 * lg) / (n_ + 4.0)
            if pos == "DEF" and oimpl:
                veg = (usual(opp_team) / oimpl) ** self.p["beta"] if opp_team else (lg / oimpl) ** self.p["beta"]   # defence: opponent's expected scoring
            elif pos != "DEF" and impl:
                veg = (impl / usual(v[5])) ** self.p["beta"]
        mu = base * role * veg
        last3 = v[3][-3:]
        return {"mu": mu, "rate": rate, "opp_idx": oi, "pos_mean": m, "last3": sum(last3) / len(last3) if last3 else None,
                "n_eff": v[1], "games": v[2], "role": role, "veg": veg, "mu_nobeta": mu / veg if veg else mu, "mu_norole": mu / role,
                "mu_noopp": rate * h * role * veg}

    def update(self, rows):
        rd, rdo = self.p["rd"], self.p["rdo"]
        for r in rows:
            v = self.pl.get(r["pid"])
            if v is None:
                v = self.pl[r["pid"]] = [0.0, 0.0, 0, [], r["pos"], r["team"], None, 0.0, 0.0, 0.0]
            v[0] = v[0] * rd + r["pts"]
            v[1] = v[1] * rd + 1.0
            v[2] += 1
            v[3] = (v[3] + [r["pts"]])[-3:]
            v[4], v[5], v[6] = r["pos"], r["team"], (r["season"], r["week"])
            opps = r["att"] + r["car"] + r["tgt"]
            v[7] = v[7] * 0.5 + opps
            v[8] = v[8] * 0.5 + 1.0
            v[9] = v[9] * rd + opps
            if MEANINGFUL[r["pos"]](r):
                self.lp[r["pos"]][0] += r["pts"]
                self.lp[r["pos"]][1] += 1.0
        tot = {}
        for r in rows:
            if (r["team"], r["opp"]) not in tot and r.get("impl"):
                self.li[0] += r["impl"]
                self.li[1] += 1.0
                t_ = self.ti.setdefault(r["team"], [0.0, 0.0])
                t_[0] = t_[0] * 0.9 + r["impl"]
                t_[1] = t_[1] * 0.9 + 1.0
            tot[(r["team"], r["opp"])] = tot.get((r["team"], r["opp"]), {pos: 0.0 for pos in ALLPOS})
            tot[(r["team"], r["opp"])][r["pos"]] += r["pts"]
        for (off, dfn), by in tot.items():
            for pos in ALLPOS:
                o = self.op.setdefault((dfn, pos), [0.0, 0.0])
                o[0] = o[0] * rdo + by[pos]
                o[1] = o[1] * rdo + 1.0
                self.lo[pos][0] += by[pos]
                self.lo[pos][1] += 1.0


def weeks_of(rows):
    out, cur, key = [], [], None
    for r in rows:
        k = (r["season"], r["week"])
        if k != key and cur:
            out.append((key, cur))
            cur = []
        key = k
        cur.append(r)
    if cur:
        out.append((key, cur))
    return out


def walk_forward(rows, params):
    """Records for every player-game with prior history; each prediction uses only strictly earlier weeks."""
    w = Walker(params)
    recs = []
    for (season, week), wk in weeks_of(rows):
        w.new_season(season)
        for r in wk:
            pr = w.predict(r["pid"], r["pos"], r["opp"], r["home"], r.get("impl"), r.get("oimpl"), r["opp"])
            if pr:
                recs.append(dict(pr, season=season, week=week, pid=r["pid"], name=r["name"], pos=r["pos"], team=r["team"], opp=r["opp"],
                                 home=r["home"], actual=r["pts"]))
        w.update(wk)
    mark_top(recs)
    return recs


def mark_top(recs):
    by = {}
    for r in recs:
        by.setdefault((r["season"], r["week"], r["pos"]), []).append(r)
    for (s, wk, pos), lst in by.items():
        lst.sort(key=lambda r: -r["mu"])
        for i, r in enumerate(lst):
            r["top"] = i < TOP_K[pos]
            r["rank"] = i + 1


# ============================================================================================ evaluation
def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def errors(recs, key):
    e = [(r[key] - r["actual"]) for r in recs if r.get(key) is not None]
    n = len(e)
    if not n:
        return {"n": 0, "mae": float("nan"), "rmse": float("nan"), "bias": float("nan")}
    return {"n": n, "mae": sum(abs(x) for x in e) / n, "rmse": math.sqrt(sum(x * x for x in e) / n), "bias": sum(e) / n}


def spearman(a, b):
    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = _mean(ra), _mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


def weekly_rank_corr(recs, key):
    by = {}
    for r in recs:
        if r.get(key) is not None:
            by.setdefault((r["season"], r["week"], r["pos"]), []).append(r)
    cs = [spearman([r[key] for r in lst], [r["actual"] for r in lst]) for lst in by.values() if len(lst) >= 8]
    return _mean(c for c in cs if c == c)


def paired_gain(recs, key_a, key_b):
    """mean squared-error improvement of b over a (positive = b better), with standard error."""
    d = [(r[key_a] - r["actual"]) ** 2 - (r[key_b] - r["actual"]) ** 2 for r in recs if r.get(key_a) is not None and r.get(key_b) is not None]
    n = len(d)
    if n < 2:
        return float("nan"), float("nan")
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1))
    return m, sd / math.sqrt(n)


def split(recs, first_eval_week=3):
    """eval rows = model top-K, from week `first_eval_week` on, 2019+.  tune = up to the second-last complete season; validate = the rest."""
    ev = [r for r in recs if r["top"] and r["season"] >= 2019 and r["week"] >= first_eval_week]
    seasons = sorted({r["season"] for r in ev})
    cut = seasons[-2] if len(seasons) >= 3 else seasons[-1]              # validate = last 2 seasons (including the live one)
    tune = [r for r in ev if r["season"] < cut]
    val = [r for r in ev if r["season"] >= cut]
    return tune, val, f"tune {seasons[0]}-{cut - 1}, validate {cut}-{seasons[-1]}"


def objective(rows, params):
    recs = walk_forward(rows, params)
    tune, val, desc = split(recs)
    return errors(tune, "mu")["rmse"], recs


GRIDS = {"skill": {"k_p": [0.5, 1, 2, 3, 5, 8, 12], "rd": [1.0, 0.95, 0.9, 0.85, 0.8, 0.7], "carry": [0.0, 0.25, 0.5, 0.75, 1.0],
                   "k_o": [2, 4, 8, 16, 32, 64, 128], "rdo": [1.0, 0.95, 0.9, 0.85, 0.8], "carry_o": [0.0, 0.25, 0.5, 0.75],
                   "gamma": [0.0, 0.5, 0.75, 1.0, 1.25, 1.5], "hfa": [0.0, 0.02, 0.04, 0.06],
                   "beta": [0.0, 0.25, 0.5, 0.75, 1.0, 1.5], "delta": [0.0, 0.25, 0.5, 0.75, 1.0]},
         "kd": {"k_p": [1, 2, 3, 5, 8, 12, 20, 40], "rd": [1.0, 0.95, 0.9, 0.85, 0.8], "carry": [0.0, 0.25, 0.5, 0.75, 1.0],
                "k_o": [8, 16, 32, 64, 128, 256], "rdo": [1.0, 0.95, 0.9, 0.85, 0.8], "carry_o": [0.0, 0.25, 0.5, 0.75],
                "gamma": [0.0, 0.5, 1.0, 1.5], "hfa": [0.0, 0.02, 0.04], "beta": [0.0, 0.25, 0.5, 0.75, 1.0, 1.5]}}
GRID = GRIDS["skill"]


def tune(rows, start=None, passes=2, out=print, grid=None):
    grid = grid or GRID
    p = dict(DEFAULTS, **(start or {}))
    best, _ = objective(rows, p)
    out(f"start RMSE (tune set) {best:.4f}")
    for ps in range(passes):
        changed = False
        for name, vals in grid.items():
            for v in vals:
                if v == p[name]:
                    continue
                trial = dict(p, **{name: v})
                sc, _ = objective(rows, trial)
                if sc < best - 1e-6:
                    best, p, changed = sc, trial, True
            out(f"  pass {ps + 1}: {name} = {p[name]:g}   RMSE {best:.4f}")
        if not changed:
            break
    return p, best


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = q * (len(xs) - 1)
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def band_ratios(recs):
    """Empirical quantiles of actual / projection by position (multiplicative floor / ceiling)."""
    out = {}
    for pos in ALLPOS:
        rr = [r["actual"] / r["mu"] for r in recs if r["pos"] == pos and r["mu"] > 0.5]
        out[pos] = {q: quantile(rr, q) for q in (0.1, 0.2, 0.5, 0.8, 0.9)} if len(rr) >= 30 else {0.1: 0.2, 0.2: 0.4, 0.5: 0.95, 0.8: 1.5, 0.9: 1.9}
    return out


def _report(recs, positions, grp, p, grid, out):
    tune_set, val, desc = split(recs)
    out(f"\n#### {grp.upper()} group ({', '.join(positions)}) ####\nChosen: " + ", ".join(f"{k}={v:g}" for k, v in p.items() if k in grid))
    for nm, v in p.items():
        if nm in grid and v in (grid[nm][0], grid[nm][-1]) and nm not in ("hfa", "gamma", "beta", "delta", "rd", "carry"):
            out(f"  WARNING: {nm}={v:g} sits on the edge of its grid ({grid[nm][0]:g}..{grid[nm][-1]:g}).")
    for r in recs:
        r["pos_mean_b"] = r["pos_mean"]
    out(f"Evaluation = each week's top-{ {k: TOP_K[k] for k in positions} } by projection per position, week 3 onward.  {desc}.")
    abl = (("mu_noopp", "opponent index"), ("mu_nobeta", "Vegas team total"), ("mu_norole", "usage/role factor"))
    for label, rs in (("TUNE", tune_set), ("VALIDATE (never used for tuning)", val)):
        out("\n" + "=" * 96 + f"\n{label}: {len(rs)} player-games\n" + "=" * 96)
        out(f"{'position':<9}{'n':>6}{'model RMSE':>12}{'player-avg':>12}{'last-3':>9}{'pos-avg':>9}{'MAE model':>11}{'MAE last3':>11}{'bias':>7}{'rank corr':>11}{'corr last3':>11}")
        for pos in tuple(positions) + ("ALL",):
            sub = [r for r in rs if pos == "ALL" or r["pos"] == pos]
            if not sub:
                continue
            m, pa, l3, pm = (errors(sub, k) for k in ("mu", "rate", "last3", "pos_mean_b"))
            out(f"{pos:<9}{m['n']:>6}{m['rmse']:>12.3f}{pa['rmse']:>12.3f}{l3['rmse']:>9.3f}{pm['rmse']:>9.3f}{m['mae']:>11.3f}{l3['mae']:>11.3f}"
                f"{m['bias']:>+7.2f}{weekly_rank_corr(sub, 'mu'):>11.3f}{weekly_rank_corr(sub, 'last3'):>11.3f}")
        g2, se2 = paired_gain(rs, "last3", "mu")
        out(f"model vs last-3 average: squared-error gain {g2:+.3f} +/- {se2:.3f}")
        for key, nm in abl:
            if (key == "mu_nobeta" and p["beta"] == 0) or (key == "mu_norole" and p["delta"] == 0) or (key == "mu_noopp" and p["gamma"] == 0):
                out(f"{nm}: switched off by tuning (parameter = 0)")
                continue
            g, se = paired_gain(rs, key, "mu")
            out(f"{nm}: squared-error gain from including it = {g:+.3f} +/- {se:.3f}  ({g / se if se else float('nan'):+.1f} s.e.)   by position: "
                + ", ".join(f"{pos} {paired_gain([r for r in rs if r['pos'] == pos], key, 'mu')[0]:+.2f}"
                            f" ({paired_gain([r for r in rs if r['pos'] == pos], key, 'mu')[0] / (paired_gain([r for r in rs if r['pos'] == pos], key, 'mu')[1] or float('nan')):+.1f})"
                            for pos in positions))
    out("\nCalibration on validate (mean actual vs mean projection, by projection bucket)")
    for pos in positions:
        sub = sorted([r for r in val if r["pos"] == pos], key=lambda r: r["mu"])
        if len(sub) < 40:
            continue
        cuts = [sub[int(len(sub) * i / 4):int(len(sub) * (i + 1) / 4)] for i in range(4)]
        out(f"  {pos}: " + "   ".join(f"proj {_mean(r['mu'] for r in c):.1f} -> actual {_mean(r['actual'] for r in c):.1f}" for c in cuts))
    bands = band_ratios(tune_set)
    cov = {pos: _mean(1.0 if bands[pos][0.2] * r["mu"] <= r["actual"] <= bands[pos][0.8] * r["mu"] else 0.0
                      for r in val if r["pos"] == pos) for pos in positions}
    out("Floor / ceiling = 20th / 80th percentile of actual/projection by position (from tune games).  Share of validate games inside: "
        + ", ".join(f"{p_} {c:.0%}" for p_, c in cov.items()) + "  (target 60%)")
    return bands, val, desc


def backtest(directory, first=2018, last=None, out=print, write_params=True, rows=None, do_tune=True, fixed=None, scoring=SCORING, params_name=None):
    last = last or current_season()
    rows = rows if rows is not None else load_weekly(directory, range(first, last + 1), scoring=scoring)
    out(f"{len(rows)} player/team-games, {first}-{last}  (Yahoo scoring; each prediction uses only earlier weeks)")
    all_params, all_bands, all_val, descs, recs_all = {}, {}, [], {}, []
    for grp, positions in GROUPS.items():
        sub = [r for r in rows if r["pos"] in positions]
        if not sub:
            continue
        grid = GRIDS[grp]
        out(f"\n=== tuning {grp} ({len(sub)} rows) ===")
        if do_tune:
            p, _ = tune(sub, fixed, out=out, grid=grid)
        else:
            p = dict(DEFAULTS, **(fixed or {}))
        recs = walk_forward(sub, p)
        bands, val, desc = _report(recs, positions, grp, p, grid, out)
        all_params[grp], all_bands[grp], descs[grp] = p, bands, desc
        all_val += val
        recs_all += recs
    if write_params:
        merged_bands = {pos: b for g in all_bands.values() for pos, b in g.items()}
        pname = params_name or PARAMS_NAME
        with open(os.path.join(directory, pname), "w") as f:
            json.dump({"params": all_params.get("skill", dict(DEFAULTS)), "params_kd": all_params.get("kd", dict(DEFAULTS)),
                       "bands": {pos: {str(q): v for q, v in d.items()} for pos, d in merged_bands.items()}, "validated_on": descs,
                       "scoring": scoring, "first": first, "last": last,
                       "validate_rmse": {pos: errors([r for r in all_val if r["pos"] == pos], "mu")["rmse"] for pos in ALLPOS}}, f, indent=1)
        out(f"\nSaved {pname}")
    return all_params, recs_all


# ============================================================================================ this week's sheet
def load_params(directory, name=None):
    """-> (params_by_group, bands, validated)"""
    path = os.path.join(directory, name or PARAMS_NAME)
    if not os.path.exists(path):
        path = os.path.join(directory, "nfl_fantasy_params.json")       # league without its own backtest: fall back to the default one
    if os.path.exists(path):
        with open(path) as fh:
            d = json.load(fh)
        bands = {pos: {float(q): v for q, v in b.items()} for pos, b in d["bands"].items()}
        for pos in ALLPOS:
            bands.setdefault(pos, band_ratios([])[pos])
        return {"skill": dict(DEFAULTS, **d["params"]), "kd": dict(DEFAULTS, **d.get("params_kd", {}))}, bands, True
    return {"skill": dict(DEFAULTS), "kd": dict(DEFAULTS)}, band_ratios([]), False


def implied_points(g):
    try:
        tot, sp = float(g["total_line"]), float(g["spread_line"])
    except (TypeError, ValueError, KeyError):
        return None, None
    return (tot + sp) / 2.0, (tot - sp) / 2.0              # (home, away); spread_line > 0 means the home team is favoured


def project(directory, season, week, scoring=SCORING, fetch=download, out=print, first=2018, today=None, params_name=None):
    params, bands, validated = load_params(directory, params_name)
    seasons = list(range(first, season + 1))
    rows = load_weekly(directory, seasons, fetch, cur=season, scoring=scoring)
    games = load_games(directory, fetch)
    wk_games = [g for g in games if g["season"] == str(season) and g["week"] == str(week) and g["game_type"] == "REG"]
    if not wk_games:
        raise SystemExit(f"No regular-season games found for {season} week {week} in the schedule.")
    hist = [r for r in rows if (r["season"], r["week"]) < (season, week)]
    played = {(r["pid"]): r for r in rows if (r["season"], r["week"]) == (season, week)}
    walkers = {}
    for grp, positions in GROUPS.items():
        wg = Walker(params[grp])
        for (s_, k), wkrows in weeks_of([r for r in hist if r["pos"] in positions]):
            wg.new_season(s_)
            wg.update(wkrows)
        wg.new_season(season)
        walkers[grp] = wg
    wof = {pos: walkers[g] for g, ps in GROUPS.items() for pos in ps}
    inj = {}
    try:
        for r in read_csv(fetch(URL_INJ.format(y=season), os.path.join(data_dir(directory), f"injuries_{season}.csv"), max_age_h=3)):
            if r.get("week") == str(week) and r.get("gsis_id"):
                inj[r["gsis_id"]] = (r.get("report_status") or "").strip() or ""
    except Exception as e:
        out(f"note: injury report unavailable ({type(e).__name__}); nobody is filtered as Out")
    team_game = {}
    for g in wk_games:
        hp, ap = implied_points(g)
        team_game[g["home_team"]] = dict(opp=g["away_team"], home=True, impl=hp, oimpl=ap, date=g["gameday"], spread=g.get("spread_line"))
        team_game[g["away_team"]] = dict(opp=g["home_team"], home=False, impl=ap, oimpl=hp, date=g["gameday"], spread=g.get("spread_line"))
    # soft-matchup ranking: 1 = most points allowed to the position
    ranks = {}
    for pos in ALLPOS:
        idx = sorted(((wof[pos].opp_index(t, pos)[0], t) for t in team_game), reverse=True)
        ranks[pos] = {t: (i + 1, v) for i, (v, t) in enumerate(idx)}
    cand = {}
    plist = [(pid, v) for wg in walkers.values() for pid, v in wg.pl.items()]
    for pid, v in plist:
        last = v[6]
        recent = last and ((last[0] == season and week - last[1] <= 5) or (last[0] == season - 1 and week <= 2) or (last[0] == season and last[1] < week))
        if not last or last[0] < season - 1 or not recent:
            continue
        cand[pid] = v
    sheet = []
    for pid, v in cand.items():
        team = v[5]
        tg = team_game.get(team)
        if tg is None:
            continue
        pr = wof[v[4]].predict(pid, v[4], tg["opp"], tg["home"], tg["impl"], tg["oimpl"], tg["opp"])
        if pr is None or pr["mu"] < 0.8:
            continue
        pos = v[4]
        b = bands[pos]
        status = inj.get(pid, "")
        sheet.append({"pos": pos, "player": next((r["name"] for r in reversed(hist) if r["pid"] == pid), pid), "team": team,
                      "opp": ("vs " if tg["home"] else "@ ") + tg["opp"], "date": tg["date"], "proj": pr["mu"],
                      "floor": pr["mu"] * b[0.2], "ceil": pr["mu"] * b[0.8], "rate": pr["rate"], "opp_idx": pr["opp_idx"],
                      "opp_rank": ranks[pos][tg["opp"]][0], "last3": pr["last3"], "games": pr["games"], "status": status,
                      "team_impl_pts": tg["impl"], "played_actual": played[pid]["pts"] if pid in played else None, "pid": pid})
    for pos in ALLPOS:
        lst = sorted((s for s in sheet if s["pos"] == pos), key=lambda s: -s["proj"])
        for i, s in enumerate(lst):
            s["pos_rank"] = i + 1
    sheet.sort(key=lambda s: (-s["proj"]))
    return sheet, validated, params


COLUMNS = [("pos_rank", "Pos rank"), ("pos", "Pos"), ("player", "Player"), ("team", "Team"), ("opp", "Opp"), ("date", "Date"),
           ("proj", "Proj (PPR)"), ("floor", "Floor 20%"), ("ceil", "Ceiling 80%"), ("rate", "Player avg (shrunk)"), ("opp_idx", "Opp index"),
           ("opp_rank", "Opp softness rank (1=softest)"), ("last3", "Last 3 avg"), ("games", "Games"), ("status", "Injury status"),
           ("team_impl_pts", "Vegas team total"), ("played_actual", "Already played: actual")]


def write_sheet(sheet, directory, season, week, tag=None):
    path = os.path.join(directory, f"nfl_projections{LEAGUE_TAG if tag is None else tag}_{season}_wk{week}.csv")
    fmt = lambda k, v: ("" if v is None else (f"{v:.1f}" if k in ("proj", "floor", "ceil", "rate", "last3", "team_impl_pts", "played_actual") else
                                              f"{v:.3f}" if k == "opp_idx" else v))
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow([c[1] for c in COLUMNS])
        for s in sheet:
            wr.writerow([fmt(k, s.get(k)) for k, _ in COLUMNS])
    x = None
    try:
        import openpyxl
        from openpyxl.styles import Font
        wb = openpyxl.Workbook()
        for pos in ("ALL",) + ALLPOS:
            ws = wb.active if pos == "ALL" else wb.create_sheet(pos)
            ws.title = pos
            ws.append([c[1] for c in COLUMNS])
            for c in ws[1]:
                c.font = Font(bold=True)
            for s in sheet:
                if pos == "ALL" or s["pos"] == pos:
                    ws.append([s.get(k) if not isinstance(s.get(k), float) else round(s[k], 3 if k == "opp_idx" else 1) for k, _ in COLUMNS])
            ws.freeze_panes = "D2"
            for col, wdt in zip("ABCDEFGHIJKLMNOPQ", (9, 5, 24, 6, 9, 11, 11, 10, 11, 15, 10, 14, 10, 7, 12, 12, 14)):
                ws.column_dimensions[col].width = wdt
        x = os.path.join(directory, f"nfl_projections{LEAGUE_TAG if tag is None else tag}_{season}_wk{week}.xlsx")
        wb.save(x)
    except ImportError:
        pass
    return path, x


def print_sheet(sheet, top=12, out=print):
    for pos in ALLPOS:
        out(f"\n{pos}  (top {top})")
        out(f"{'#':>3} {'player':<24}{'team':<5}{'opp':<8}{'proj':>6}{'floor':>7}{'ceil':>7}{'avg':>6}{'oppIdx':>8}{'oppRk':>6}  status")
        for s in [s for s in sheet if s["pos"] == pos][:top]:
            tag = s["status"] or ""
            if s["played_actual"] is not None:
                tag = f"PLAYED {s['played_actual']:.1f}"
            out(f"{s['pos_rank']:>3} {s['player'][:23]:<24}{s['team']:<5}{s['opp']:<8}{s['proj']:>6.1f}{s['floor']:>7.1f}{s['ceil']:>7.1f}{s['rate']:>6.1f}"
                f"{s['opp_idx']:>8.2f}{s['opp_rank']:>6}  {tag}")


# ============================================================================================ checks + CLI
def check(directory, seasons, out=print):
    """Our points code with nflverse's scoring settings must reproduce nflverse's own fantasy_points_ppr."""
    rows = [r for r in load_weekly(directory, seasons, scoring=SCORING_NFLVERSE) if r["pos"] in POSITIONS]
    diffs = [abs(r["pts"] - r["ppr_nflverse"]) for r in rows]
    bad = sum(1 for d in diffs if d > 0.01)
    out(f"{len(rows)} player-games; our points (nflverse settings) match nflverse fantasy_points_ppr within 0.01 on {len(rows) - bad} "
        f"({1 - bad / max(len(rows), 1):.2%}); mean abs diff {_mean(diffs):.4f}")
    rows_y = [r for r in load_weekly(directory, seasons, scoring=SCORING) if r["pos"] in POSITIONS]
    out(f"Yahoo scoring differs from nflverse only by interceptions (-1 vs -2): average Yahoo - nflverse = {_mean(a['pts'] - b['pts'] for a, b in zip(rows_y, rows)):+.4f} points per player-game")
    return bad


DEFAULT_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1}      # Family league 501858: QB, 2 WR, 2 RB, TE, 2 W/R/T, K, DEF
FLEX_POS = ("RB", "WR", "TE")


def next_week(directory, season, fetch=download, today=None):
    """First regular-season week with a game today or later; after the last game, the last week; 1 if the season is not in the schedule."""
    games = [g for g in load_games(directory, fetch) if g["season"] == str(season) and g["game_type"] == "REG"]
    today = today or dt.date.today().isoformat()
    wks = sorted({int(g["week"]) for g in games if g["gameday"] >= today})
    if wks:
        return wks[0]
    return max((int(g["week"]) for g in games), default=1)


def best_lineup(pool, slots):
    start, used = [], set()
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        for r in sorted((r for r in pool if r["pos"] == pos), key=lambda r: -r["proj"])[:slots.get(pos, 0)]:
            start.append((pos, r))
            used.add(id(r))
    for r in sorted((r for r in pool if r["pos"] in FLEX_POS and id(r) not in used), key=lambda r: -r["proj"])[:slots.get("FLEX", 0)]:
        start.append(("FLEX", r))
    return start


def _match(sheet, names):
    def norm(x):
        return "".join(ch for ch in x.lower() if ch.isalnum())
    by = {}
    for r in sheet:
        by.setdefault(norm(r["player"]), r)
        if r["pos"] == "DEF":
            by.setdefault(norm(r["team"]), r)
    mine, missing = [], []
    for n in names:
        r = by.get(norm(n)) or next((v for k, v in by.items() if norm(n) in k and len(norm(n)) >= 5), None)
        (mine if r else missing).append(r or n)
    return mine, missing


def waivers(sheet, names, taken, slots, top=12, out=print):
    """Rank non-rostered players by the gain in THIS WEEK's projected lineup total if added (the displaced starter moves to the bench).
    Availability is unknown (no Yahoo login): pass --taken for players owned elsewhere, or check the top names in Yahoo."""
    mine, missing = _match(sheet, names)
    mine = [r for r in mine if r["status"].lower() not in ("out", "ir", "pup", "suspended")]
    have = {id(r) for r in mine}
    tk, _ = _match(sheet, taken or [])
    blocked = have | {id(r) for r in tk}
    base_start = best_lineup(mine, slots)
    base = sum(r["proj"] for _, r in base_start)
    weakest = {pos: min((r for sl, r in base_start if sl == pos), key=lambda r: r["proj"], default=None) for pos in ("QB", "RB", "WR", "TE", "K", "DEF", "FLEX")}
    rows = []
    for r in sheet:
        if id(r) in blocked or r["status"].lower() in ("out", "ir", "pup", "suspended") or r["played_actual"] is not None:
            continue
        new = sum(x["proj"] for _, x in best_lineup(mine + [r], slots))
        rows.append((new - base, r))
    rows.sort(key=lambda t: (-t[0], -t[1]["proj"]))
    out(f"Current lineup projects {base:.1f}.  Gain = change in projected lineup total this week if the player is added.")
    out(f"{'gain':>6}  {'pos':<4}{'player':<24}{'team':<5}{'opp':<8}{'proj':>6}{'avg':>6}  note")
    shown = 0
    for g, r in rows:
        if g <= 0.05 or shown >= top:
            break
        slot = "FLEX" if r["pos"] in FLEX_POS and (weakest.get(r["pos"]) is None or r["proj"] <= weakest[r["pos"]]["proj"]) else r["pos"]
        out(f"{g:>+6.1f}  {r['pos']:<4}{r['player']:<24}{r['team']:<5}{r['opp']:<8}{r['proj']:>6.1f}{r['rate']:>6.1f}  would start at {slot}")
        shown += 1
    if shown == 0:
        out("  nobody outside the roster would improve this week's lineup.")
    out("\nBest non-rostered players per position by projection (for depth / bye or injury cover):")
    for pos in ALLPOS:
        c = [r for _, r in rows if r["pos"] == pos][:3]
        c.sort(key=lambda r: -r["proj"])
        out(f"  {pos:<4}" + "   ".join(f"{r['player']} {r['proj']:.1f}" for r in c))
    if missing:
        out("\nnot on the sheet (bye week, no recent games, or misspelled): " + ", ".join(missing))
    return rows


def lineup(sheet, names, slots, out=print):
    """Best starting lineup for the slots from a list of roster names (greedy is optimal: FLEX pool is a superset of RB/WR/TE)."""
    mine, missing = _match(sheet, names)
    out_ = [r for r in mine if r["status"].lower() in ("out", "ir", "pup", "suspended")]
    pool = [r for r in mine if r not in out_]
    start = best_lineup(pool, slots)
    used = {id(r) for _, r in start}
    out("\nSTART")
    for slot, r in start:
        done = f"  [played: {r['played_actual']:.1f}]" if r["played_actual"] is not None else ""
        out(f"  {slot:<5}{r['player']:<24}{r['team']:<5}{r['opp']:<8}{r['proj']:>6.1f}  ({r['floor']:.1f}-{r['ceil']:.1f}){('  ' + r['status']) if r['status'] else ''}{done}")
    out(f"  projected total {sum(r['proj'] for _, r in start):.1f}")
    out("BENCH")
    for r in sorted((r for r in pool if id(r) not in used), key=lambda r: -r["proj"]):
        out(f"  {r['pos']:<5}{r['player']:<24}{r['team']:<5}{r['opp']:<8}{r['proj']:>6.1f}")
    for r in out_:
        out(f"  OUT  {r['player']} ({r['status']})")
    if missing:
        out("not found on the sheet (no recent NFL games or misspelled): " + ", ".join(missing))
    return start


def main(argv=None):
    ap = argparse.ArgumentParser(description="NFL fantasy projections (Yahoo PPR)")
    ap.add_argument("command", choices=["backtest", "project", "check", "lineup", "waivers"])
    ap.add_argument("--dir", default=".")
    ap.add_argument("--season", type=int)
    ap.add_argument("--week", type=int)
    ap.add_argument("--first", type=int, default=2018)
    ap.add_argument("--last", type=int)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--league", help="league JSON (scoring overrides + roster slots), e.g. nfl_league_501858.json")
    ap.add_argument("--roster", help="text file, one player per line (K/DEF as e.g. \"MIN D/ST\"), for the lineup command")
    ap.add_argument("--taken", help="optional text file of players NOT available (owned elsewhere), one per line, for waivers")
    ap.add_argument("--scoring-file", help="JSON with any of: " + ", ".join(SCORING))
    a = ap.parse_args(argv)
    sc = dict(SCORING)
    if a.scoring_file:
        sc.update(json.load(open(a.scoring_file)))
    slots = dict(DEFAULT_SLOTS)
    if a.league:
        lg = json.load(open(a.league))
        sc.update(lg.get("scoring", {}))
        slots = lg.get("slots", slots)
        if lg.get("id"):
            global PARAMS_NAME, LEAGUE_TAG
            LEAGUE_TAG = f"_{lg['id']}"
            PARAMS_NAME = f"nfl_fantasy_params_{lg['id']}.json"
        print(f"League: {lg.get('name', a.league)}  slots {slots}")
    season = a.season or current_season()
    if a.command == "check":
        check(a.dir, range(a.first, (a.last or season) + 1))
    elif a.command == "backtest":
        backtest(a.dir, a.first, a.last or season, scoring=sc)
    elif a.command == "lineup":
        if not a.roster:
            raise SystemExit("lineup needs --roster file.txt")
        sheet, validated, params = project(a.dir, season, a.week or next_week(a.dir, season), sc)
        lineup(sheet, [l.strip() for l in open(a.roster) if l.strip() and not l.startswith("#")], slots)
    elif a.command == "waivers":
        if not a.roster:
            raise SystemExit("waivers needs --roster file.txt")
        sheet, validated, params = project(a.dir, season, a.week or next_week(a.dir, season), sc)
        rd = lambda p: [l.strip() for l in open(p) if l.strip() and not l.startswith("#")]
        waivers(sheet, rd(a.roster), rd(a.taken) if a.taken else [], slots, top=a.top)
    else:
        if a.week is None:
            a.week = next_week(a.dir, season)
        sheet, validated, params = project(a.dir, season, a.week, sc)
        if not validated:
            print(f"WARNING: no {PARAMS_NAME}: using default parameters. Run `backtest` first.")
        csv_path, xlsx = write_sheet(sheet, a.dir, season, a.week)
        print_sheet(sheet, a.top)
        print(f"\nWrote {csv_path}" + (f" and {xlsx}" if xlsx else "  (install openpyxl for an .xlsx with one tab per position)"))


if __name__ == "__main__":
    main()
'''
_DEFAULT_LEAGUES = [{'name': 'Family Fantasy Football League', 'id': 501858, 'platform': 'Yahoo', 'teams': 18, 'format': 'H2H, 6 playoff teams (weeks 15-17)', 'scoring': {'pass_yd': 0.04, 'pass_td': 4, 'intc': -1, 'rush_yd': 0.1, 'rush_td': 6, 'rec': 1, 'rec_yd': 0.1, 'rec_td': 6, 'fumble_lost': -2, 'two_pt': 2, 'st_td': 6, 'fum_rec_td': 6, 'k_fg_u40': 3, 'k_fg_40': 4, 'k_fg_50': 5, 'k_pat': 1, 'k_fg_miss': 0, 'd_sack': 1, 'd_int': 2, 'd_fum': 2, 'd_td': 6, 'd_safety': 2, 'd_block': 2, 'd_pa': [[0, 10], [6, 7], [13, 4], [20, 1], [27, 0], [34, -1], [999, -4]], 'k_pat_miss': 0}, 'slots': {'QB': 1, 'RB': 2, 'WR': 2, 'TE': 1, 'FLEX': 2, 'K': 1, 'DEF': 1}, 'bench': 6, 'ir': 2, 'notes': 'Extra Point Returned (2 pts) not modelled; no yardage tiers for defense.'}, {'name': 'Otuhiva Family League', 'id': 572561, 'platform': 'Yahoo', 'teams': 12, 'format': 'H2H, 6 playoff teams (weeks 15-17)', 'scoring': {'pass_yd': 0.04, 'pass_td': 6, 'intc': -2, 'rush_yd': 0.1, 'rush_td': 6, 'rec': 1, 'rec_yd': 0.1, 'rec_td': 6, 'fumble_lost': -2, 'two_pt': 2, 'st_td': 6, 'fum_rec_td': 6, 'k_fg_u40': 3, 'k_fg_40': 4, 'k_fg_50': 5, 'k_pat': 1, 'k_fg_miss': 0, 'd_sack': 1, 'd_int': 2, 'd_fum': 2, 'd_td': 6, 'd_safety': 2, 'd_block': 2, 'd_pa': [[0, 10], [6, 7], [13, 4], [20, 1], [27, 0], [34, -1], [999, -4]], 'k_pat_miss': -1}, 'slots': {'QB': 1, 'RB': 2, 'WR': 3, 'TE': 1, 'FLEX': 2, 'K': 1, 'DEF': 1}, 'bench': 6, 'ir': 1, 'notes': 'Points allowed 21-27 not shown in pasted settings; assumed 0. Extra Point Returned (2) not modelled.'}]


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        module.__file__ = f"<nfl_fantasy_platform:{name}.py>"
        exec(compile(_SRC[name], module.__file__, "exec"), module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _SRC:
            return importlib.util.spec_from_loader(fullname, _Loader())
        return None


def _install():
    """The embedded copies win over any same-named .py files lying around."""
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
    for n in _ORDER:
        sys.modules.pop(n, None)


_install()
import nfl_fantasy_projections as fpm                                          # noqa: E402
from nfl_io import FileLock, LockBusy, atomic_json_dump, load_json_retry       # noqa: E402

ROSTER_FILE = "nfl_rosters.json"
STATE_FILE = "nfl_fantasy_refresh_state.json"
REFRESH_LOCK = "nfl_fantasy_refresh.lock"
BOOT_FILE = "nfl_fantasy_bootstrap_state.json"
OUT_STATUS = ("out", "ir", "pup", "suspended")
MAX_NAMES = 80


def clean(obj):
    """JSON-safe: NaN/inf -> null, tuples -> lists."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _stat_key(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


def _quiet(*_a, **_k):
    pass


def _row(r):
    """Sheet row -> JSON-friendly dict."""
    keys = ("pos", "pos_rank", "player", "team", "opp", "date", "proj", "floor", "ceil", "rate", "opp_idx", "opp_rank", "last3", "games",
            "status", "team_impl_pts", "played_actual", "pid")
    return {k: r.get(k) for k in keys}


class FantasyPlatform:
    def __init__(self, directory=".", now_fn=None, fetch=None, min_refresh_interval_s=300):
        self.dir = directory
        self._now_fn = now_fn or (lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
        self._fetch = fetch or fpm.download                 # used ONLY by refresh / bootstrap
        self.min_refresh_interval_s = min_refresh_interval_s
        self._lock = threading.RLock()
        self._cache = {}                                    # (league, season, week) -> (key, sheet, validated)
        self._inputs = {}
        self._boot = {"status": "idle"}
        self._refresh_info = {"last": None}
        self._stop = threading.Event()
        self._leagues_ready = False

    # ----------------------------------------------------------------- helpers
    def _now(self):
        return self._now_fn()

    def _p(self, name):
        return os.path.join(self.dir, name)

    def _offline(self, url, path, **_kw):
        """fetch replacement for READ paths: the cached file or an error, never the network."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"{os.path.basename(path)} is not downloaded yet (run refresh / bootstrap)")
        return path

    def season(self):
        return fpm.current_season(self._now().date())

    def ensure_leagues(self):
        """Write the built-in league files if absent."""
        if self._leagues_ready:
            return
        os.makedirs(self.dir, exist_ok=True)
        for lg in _DEFAULT_LEAGUES:
            path = self._p(f"nfl_league_{lg['id']}.json")
            if not os.path.exists(path):
                atomic_json_dump(path, lg)
        self._leagues_ready = True

    def leagues(self):
        self.ensure_leagues()
        out = {}
        for f in sorted(os.listdir(self.dir)):
            if f.startswith("nfl_league_") and f.endswith(".json"):
                d = load_json_retry(self._p(f), {}) or {}
                if d.get("id") and isinstance(d.get("scoring"), dict):
                    out[str(d["id"])] = d
        return out

    def _league(self, league_id):
        lgs = self.leagues()
        if league_id is None and len(lgs) == 1:
            league_id = next(iter(lgs))
        if str(league_id) not in lgs:
            raise KeyError(f"unknown league {league_id!r}; available: {', '.join(lgs) or 'none'}")
        d = lgs[str(league_id)]
        sc = dict(fpm.SCORING)
        sc.update(d["scoring"])
        return d, sc, d.get("slots") or dict(fpm.DEFAULT_SLOTS)

    @staticmethod
    def _params_name(league):
        return f"nfl_fantasy_params_{league['id']}.json"

    def _read_rosters(self):
        path = self._p(ROSTER_FILE)
        key = _stat_key(path)
        hit = self._inputs.get("rosters")
        if hit and hit[0] == key:
            return hit[1]
        data = load_json_retry(path, {}) if os.path.exists(path) else {}
        data = data if isinstance(data, dict) else {}
        self._inputs["rosters"] = (key, data)
        return data

    # ----------------------------------------------------------------- READ path (never touches the network)
    def _sheet(self, league_id, week=None):
        """-> (league dict, slots, week, season, sheet, validated, warnings)"""
        warnings = []
        if league_id in (None, ""):
            lgs = self.leagues()
            if len(lgs) > 1:
                league_id = sorted(lgs)[0]
                warnings.append(f"no league given: showing league {league_id} (pass ?league=ID)")
        lg, sc, slots = self._league(league_id)
        season = self.season()
        week = int(week) if week not in (None, "") else fpm.next_week(self.dir, season, self._offline, today=self._now().date().isoformat())
        pname = self._params_name(lg)
        d = fpm.data_dir(self.dir)
        files = [os.path.join(d, "games.csv"), os.path.join(d, f"week_{season}.csv"), os.path.join(d, f"team_{season}.csv"),
                 os.path.join(d, f"injuries_{season}.csv"), self._p(pname), self._p("nfl_fantasy_params.json"), self._p(f"nfl_league_{lg['id']}.json")]
        key = _stat_key(*files)
        ck = (str(lg["id"]), season, week)
        hit = self._cache.get(ck)
        if hit and hit[0] == key:
            sheet, validated = hit[1], hit[2]
        else:
            sheet, validated, _ = fpm.project(self.dir, season, week, sc, fetch=self._offline, out=_quiet, params_name=pname)
            self._cache = {k: v for k, v in self._cache.items() if v[0] == key or k != ck}
            self._cache[ck] = (key, sheet, validated)
        if not validated:
            warnings.append("UNVALIDATED: no tuned parameter file yet (using defaults); run bootstrap")
        elif not os.path.exists(self._p(pname)):
            warnings.append("this league has no parameter file of its own yet: using the default tuning (fine when its scoring matches Yahoo PPR; run bootstrap)")
        return lg, slots, week, season, sheet, validated, warnings

    def get_projections(self, league=None, week=None, pos=None):
        try:
            with self._lock:
                lg, slots, week, season, sheet, validated, warnings = self._sheet(league, week)
            rows = [_row(r) for r in sheet if not pos or r["pos"] == str(pos).upper()]
            return clean({"status": "ok", "league": str(lg["id"]), "league_name": lg.get("name"), "season": season, "week": week,
                          "generated_at": self._now().isoformat(timespec="seconds") + "Z", "params_validated": validated,
                          "warnings": warnings, "count": len(rows), "players": rows,
                          "note": "projections are conditional on the player playing; floor / ceiling = 20th / 80th percentile of past outcomes"})
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "players": []}

    def search_players(self, q, league=None, week=None, limit=15):
        try:
            with self._lock:
                lg, slots, week, season, sheet, validated, warnings = self._sheet(league, week)
            n = fpm_norm(q)
            hits = [r for r in sheet if n and (n in fpm_norm(r["player"]) or (r["pos"] == "DEF" and n == fpm_norm(r["team"])))]
            hits.sort(key=lambda r: -r["proj"])
            return clean({"status": "ok", "query": q, "players": [{k: r[k] for k in ("player", "pos", "team", "proj", "status")} for r in hits[:limit]],
                          "warnings": warnings})
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "players": []}

    def _roster(self, league_id):
        r = self._read_rosters().get(str(league_id)) or {}
        return list(r.get("players") or []), list(r.get("taken") or [])

    def get_lineup(self, league=None, week=None, players=None):
        try:
            with self._lock:
                lg, slots, week, season, sheet, validated, warnings = self._sheet(league, week)
                names = players if players is not None else self._roster(lg["id"])[0]
            if not names:
                return {"status": "ok", "league": str(lg["id"]), "week": week, "warnings": warnings + ["no roster saved for this league: POST /api/fantasy/roster"],
                        "start": [], "bench": [], "out": [], "missing": []}
            mine, missing = fpm._match(sheet, names)
            out_ = [r for r in mine if (r["status"] or "").lower() in OUT_STATUS]
            pool = [r for r in mine if r not in out_]
            start = fpm.best_lineup(pool, slots)
            used = {id(r) for _, r in start}
            bench = sorted((r for r in pool if id(r) not in used), key=lambda r: -r["proj"])
            unfilled = {s: n - sum(1 for sl, _ in start if sl == s) for s, n in slots.items()}
            unfilled = {s: n for s, n in unfilled.items() if n > 0}
            if unfilled:
                warnings.append("empty slots (no eligible rostered player on the sheet): " + ", ".join(f"{s} x{n}" for s, n in unfilled.items()))
            if missing:
                warnings.append("not on this week's sheet (bye, no recent games, or misspelled): " + ", ".join(missing))
            tot = lambda k: sum(r[k] for _, r in start)
            return clean({"status": "ok", "league": str(lg["id"]), "league_name": lg.get("name"), "season": season, "week": week,
                          "slots": slots, "start": [dict(_row(r), slot=sl) for sl, r in start], "bench": [_row(r) for r in bench],
                          "out": [_row(r) for r in out_], "missing": missing, "projected_total": tot("proj"),
                          "floor_total": tot("floor"), "ceiling_total": tot("ceil"), "warnings": warnings,
                          "note": "floor / ceiling totals add each starter's own bands, so the true range for the sum is narrower"})
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "start": []}

    def get_waivers(self, league=None, week=None, top=15, players=None, taken=None):
        try:
            with self._lock:
                lg, slots, week, season, sheet, validated, warnings = self._sheet(league, week)
                names, tk = self._roster(lg["id"])
                names = players if players is not None else names
                tk = taken if taken is not None else tk
            if not names:
                return {"status": "ok", "league": str(lg["id"]), "week": week, "warnings": warnings + ["no roster saved for this league: POST /api/fantasy/roster"],
                        "adds": []}
            lines = []
            rows = fpm.waivers(sheet, names, tk, slots, top=int(top), out=lines.append)
            adds = [dict(_row(r), gain=g) for g, r in rows if g > 0.05][:int(top)]
            per_pos = {}
            for pos in fpm.ALLPOS:
                per_pos[pos] = [_row(r) for _, r in rows if r["pos"] == pos][:3]
            base = None
            for ln in lines:
                if ln.startswith("Current lineup projects"):
                    base = float(ln.split("projects")[1].split(".  ")[0])
            warnings.append("availability is not checked: confirm the player is free in Yahoo (or save the owned-elsewhere list in 'taken')")
            return clean({"status": "ok", "league": str(lg["id"]), "league_name": lg.get("name"), "season": season, "week": week,
                          "current_lineup_projection": base, "adds": adds, "best_by_position": per_pos, "warnings": warnings,
                          "note": "gain = change in THIS week's projected lineup total if the player is added (the displaced starter moves to the bench)"})
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "adds": []}

    def get_leagues(self):
        try:
            out = []
            for lid, d in self.leagues().items():
                own = os.path.exists(self._p(self._params_name(d)))
                has = own or os.path.exists(self._p("nfl_fantasy_params.json"))
                roster, taken = self._roster(lid)
                out.append({"id": lid, "name": d.get("name"), "teams": d.get("teams"), "format": d.get("format"), "slots": d.get("slots"),
                            "tuned_parameters": has, "own_parameters": own, "roster_size": len(roster), "taken_size": len(taken), "notes": d.get("notes")})
            return {"status": "ok", "leagues": out, "warnings": []}
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "leagues": []}

    def health(self):
        def age(path):
            try:
                return round(time.time() - os.stat(path).st_mtime, 0)
            except OSError:
                return None
        season = self.season()
        d = fpm.data_dir(self.dir)
        state = load_json_retry(self._p(STATE_FILE), {}) or {}
        return clean({"status": "ok", "now": self._now().isoformat(timespec="seconds") + "Z", "season": season, "bootstrap": self._boot,
                      "file_age_s": {"schedule": age(os.path.join(d, "games.csv")), "player_stats": age(os.path.join(d, f"week_{season}.csv")),
                                     "team_stats": age(os.path.join(d, f"team_{season}.csv")), "injuries": age(os.path.join(d, f"injuries_{season}.csv"))},
                      "leagues": [{"id": l["id"], "tuned_parameters": l["tuned_parameters"]} for l in self.get_leagues().get("leagues", [])],
                      "last_refresh": self._refresh_info["last"] or state.get("last_refresh")})

    # ----------------------------------------------------------------- inputs
    def set_roster(self, league, players, taken=None):
        problems = []
        try:
            lg, _, _ = self._league(league)
        except KeyError as e:
            return {"status": "error", "warnings": [str(e).strip("'\"")]}

        def check(lst, what):
            if not isinstance(lst, list) or len(lst) > MAX_NAMES or not all(isinstance(x, str) and 0 < len(x.strip()) <= 60 for x in lst):
                problems.append(f"{what} must be a list of at most {MAX_NAMES} names (strings up to 60 characters)")
        check(players, "players")
        if taken is not None:
            check(taken, "taken")
        if problems:
            return {"status": "error", "warnings": problems}
        with self._lock:
            data = dict(self._read_rosters())
            entry = {"players": [x.strip() for x in players], "taken": [x.strip() for x in (taken if taken is not None else (data.get(str(lg["id"])) or {}).get("taken", []))],
                     "updated": self._now().isoformat(timespec="seconds") + "Z"}
            data[str(lg["id"])] = entry
            atomic_json_dump(self._p(ROSTER_FILE), data)
        return {"status": "ok", "league": str(lg["id"]), "players": len(entry["players"]), "taken": len(entry["taken"])}

    # ----------------------------------------------------------------- WRITE path
    def refresh_data(self, force=False):
        """Re-download the current-season files (schedule, player + team stats, injuries). Never raises."""
        started = time.time()
        lock = FileLock(self._p(REFRESH_LOCK), timeout=0.0, stale_after=1800)
        try:
            lock.__enter__()
        except LockBusy:
            return {"status": "busy"}
        info = {}
        try:
            state = load_json_retry(self._p(STATE_FILE), {}) or {}
            last = (state.get("last_refresh") or {}).get("finished_at_ts")
            if not force and last and time.time() - last < self.min_refresh_interval_s:
                return {"status": "skipped", "reason": "refreshed moments ago"}
            season = self.season()
            d = fpm.data_dir(self.dir)
            errors, got = [], []
            plan = [(fpm.URL_GAMES, "games.csv"), (fpm.URL_WEEK.format(y=season), f"week_{season}.csv"),
                    (fpm.URL_TEAM.format(y=season), f"team_{season}.csv"), (fpm.URL_INJ.format(y=season), f"injuries_{season}.csv")]
            for url, name in plan:
                try:
                    self._fetch(url, os.path.join(d, name), max_age_h=0)
                    got.append(name)
                except Exception as e:                       # one missing file (e.g. injuries before week 1) must not stop the rest
                    errors.append(f"{name}: {type(e).__name__}: {e}")
            critical = {"games.csv", f"week_{season}.csv"}
            info = {"status": "ok" if critical <= set(got) else "error", "season": season, "files": got, "errors": errors,
                    "finished_at": self._now().isoformat(timespec="seconds") + "Z", "finished_at_ts": time.time(),
                    "duration_s": round(time.time() - started, 1)}
            atomic_json_dump(self._p(STATE_FILE), {"last_refresh": info})
        except Exception as e:
            info = {"status": "error", "error": f"{type(e).__name__}: {e}", "finished_at": self._now().isoformat(timespec="seconds") + "Z"}
        finally:
            lock.release()
        self._refresh_info["last"] = info
        return info

    def bootstrap(self, league=None, first=2018, log=print, retune=True):
        """Download 2018..now and tune each league (own scoring).  Resumable: completed downloads are kept."""
        self._boot = {"status": "running", "started_at": self._now().isoformat(timespec="seconds") + "Z", "step": "starting"}
        try:
            self.ensure_leagues()
            season = self.season()
            d = fpm.data_dir(self.dir)
            self._boot["step"] = "schedule"
            self._fetch(fpm.URL_GAMES, os.path.join(d, "games.csv"), max_age_h=0)
            for y in range(first, season + 1):
                self._boot["step"] = f"{y}: player + team stats"
                log(self._boot["step"])
                age = 0 if y >= season else None
                self._fetch(fpm.URL_WEEK.format(y=y), os.path.join(d, f"week_{y}.csv"), max_age_h=age)
                try:
                    self._fetch(fpm.URL_TEAM.format(y=y), os.path.join(d, f"team_{y}.csv"), max_age_h=age)
                except Exception as e:
                    log(f"  team stats {y} unavailable ({e})")
            try:
                self._fetch(fpm.URL_INJ.format(y=season), os.path.join(d, f"injuries_{season}.csv"), max_age_h=0)
            except Exception:
                pass
            tuned = []
            if retune:
                for lid, lg in self.leagues().items():
                    if league and str(league) != lid:
                        continue
                    self._boot["step"] = f"tuning league {lid}"
                    log(self._boot["step"])
                    sc = dict(fpm.SCORING)
                    sc.update(lg["scoring"])
                    fpm.backtest(self.dir, first, season, out=_quiet, scoring=sc, params_name=self._params_name(lg))
                    tuned.append(lid)
            with self._lock:
                self._cache.clear()
            self._boot = {"status": "done", "finished_at": self._now().isoformat(timespec="seconds") + "Z", "tuned": tuned}
        except Exception as e:
            self._boot = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        atomic_json_dump(self._p(BOOT_FILE), self._boot)
        return self._boot

    def needs_bootstrap(self):
        self.ensure_leagues()
        d = fpm.data_dir(self.dir)
        return (not os.path.exists(os.path.join(d, "games.csv"))
                or any(not os.path.exists(self._p(self._params_name(l))) for l in self.leagues().values()))

    def start_background_refresh(self, interval_s=1800, bootstrap=False):
        self._stop.clear()

        def loop():
            if bootstrap and self.needs_bootstrap():
                self.bootstrap()
            while not self._stop.is_set():
                self.refresh_data()
                self._stop.wait(interval_s)
        t = threading.Thread(target=loop, name="fantasy-refresh", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()


def fpm_norm(x):
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


# ===================================================================== HTTP
def serve(platform, host="127.0.0.1", port=8054, cors=False, allow_write=False, token=None):
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
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send({})

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                top = int(q.get("top", 15))
            except ValueError:
                top = 15
            routes = {"/api/fantasy/leagues": platform.get_leagues,
                      "/api/fantasy/projections": lambda: platform.get_projections(q.get("league"), q.get("week"), q.get("pos")),
                      "/api/fantasy/lineup": lambda: platform.get_lineup(q.get("league"), q.get("week")),
                      "/api/fantasy/waivers": lambda: platform.get_waivers(q.get("league"), q.get("week"), top),
                      "/api/fantasy/players": lambda: platform.search_players(q.get("q", ""), q.get("league"), q.get("week")),
                      "/health": platform.health, "/api/fantasy/health": platform.health}
            fn = routes.get(u.path)
            if fn is None:
                self._send({"status": "error", "warnings": ["not found"]}, 404)
            else:
                self._send(fn())

        def _authorised(self):
            return (not token) or self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in ("/api/fantasy/roster", "/api/fantasy/refresh"):
                return self._send({"status": "error", "warnings": ["not found"]}, 404)
            if not allow_write:
                return self._send({"status": "error", "warnings": ["writes are disabled (start with --allow-write)"]}, 403)
            if not self._authorised():
                return self._send({"status": "error", "warnings": ["missing or wrong bearer token"]}, 401)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 200000:
                    return self._send({"status": "error", "warnings": ["body too large"]}, 413)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send({"status": "error", "warnings": ["body is not a JSON object"]}, 400)
            if u.path == "/api/fantasy/refresh":
                what = body.get("what", "data")
                if what not in ("data", "params"):
                    return self._send({"status": "error", "warnings": ["what must be 'data' or 'params'"]}, 400)
                if what == "params":
                    threading.Thread(target=platform.bootstrap, kwargs={"league": body.get("league")}, daemon=True).start()
                    return self._send({"status": "started", "note": "tuning runs in the background; watch /health"})
                return self._send(platform.refresh_data(force=True))
            res = platform.set_roster(body.get("league"), body.get("players"), body.get("taken"))
            self._send(res, 200 if res["status"] == "ok" else 400)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/api/fantasy/leagues, /projections, /lineup, /waivers, /players, /health"
          f"{', POST /api/fantasy/roster, POST /api/fantasy/refresh' if allow_write else ''}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


# ===================================================================== CLI
def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _print_sheet(res, top):
    print(f"\n{res.get('league_name')}  season {res.get('season')} week {res.get('week')}  [status {res.get('status')}]")
    for w in res.get("warnings") or []:
        print(f"  [warn] {w}")
    by = {}
    for r in res.get("players", []):
        by.setdefault(r["pos"], []).append(r)
    for pos, rows in by.items():
        print(f"\n{pos} (top {top})")
        for r in rows[:top]:
            tag = r["status"] or (f"PLAYED {r['played_actual']:.1f}" if r["played_actual"] is not None else "")
            print(f"  {r['pos_rank']:>3} {r['player'][:23]:<24}{r['team']:<5}{r['opp']:<8}{r['proj']:>6.1f} ({r['floor']:.1f}-{r['ceil']:.1f}) {tag}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NFL fantasy platform: model, data, leagues, lineup, waivers, service -- one file")
    ap.add_argument("--dir", default=os.environ.get("FANTASY_DATA_DIR", "."), help="data folder (default: $FANTASY_DATA_DIR or current)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("projections", "lineup", "waivers"):
        s = sub.add_parser(name)
        s.add_argument("--league", default=None)
        s.add_argument("--week", type=int, default=None)
        s.add_argument("--pos", default=None)
        s.add_argument("--top", type=int, default=15)
        s.add_argument("--roster", default=None, help="text file, one name per line: saved for the league before running")
        s.add_argument("--taken", default=None, help="text file of players owned elsewhere (waivers)")
        s.add_argument("--no-refresh", action="store_true")
    sub.add_parser("refresh")
    sub.add_parser("leagues")
    bs = sub.add_parser("bootstrap")
    bs.add_argument("--league", default=None)
    bs.add_argument("--first", type=int, default=2018)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--cors", action="store_true")
    sv.add_argument("--allow-write", action="store_true")
    sv.add_argument("--bootstrap", action="store_true")
    sv.add_argument("--refresh-minutes", type=int, default=int(os.environ.get("FANTASY_REFRESH_MINUTES", "30")))
    r = sub.add_parser("run", help="run the embedded model's own command line")
    r.add_argument("module")
    r.add_argument("rest", nargs=argparse.REMAINDER)
    u = sub.add_parser("unpack")
    u.add_argument("outdir")
    for sp_ in sub.choices.values():
        if sp_ is not r:
            sp_.add_argument("--dir", default=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.cmd == "run":
        if args.module not in _SRC:
            sys.exit(f"unknown module '{args.module}'. Available: {', '.join(_ORDER)}")
        mod = sys.modules.get(args.module) or importlib.import_module(args.module)
        rest = [a for a in args.rest if a != "--"]
        if "--dir" not in rest and args.module == "nfl_fantasy_projections":
            rest += ["--dir", args.dir]
        return mod.main(rest)
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
    os.makedirs(args.dir, exist_ok=True)
    p = FantasyPlatform(args.dir)
    p.ensure_leagues()
    if args.cmd == "refresh":
        print(json.dumps(p.refresh_data(force=True), indent=2))
    elif args.cmd == "leagues":
        print(json.dumps(p.get_leagues(), indent=2))
    elif args.cmd == "bootstrap":
        print(json.dumps(p.bootstrap(args.league, args.first), indent=2))
    elif args.cmd == "serve":
        port = args.port or int(os.environ.get("PORT", "8054"))
        host = args.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        allow = args.allow_write or _env_flag("FANTASY_ALLOW_WRITE")
        token = os.environ.get("FANTASY_API_TOKEN") or None
        if allow and not token and host != "127.0.0.1":
            print("WARNING: writes are enabled on a public address without FANTASY_API_TOKEN: anyone who can reach it can change your roster.", flush=True)
        if args.refresh_minutes > 0:
            p.start_background_refresh(args.refresh_minutes * 60, bootstrap=args.bootstrap or _env_flag("FANTASY_BOOTSTRAP"))
        serve(p, host, port, args.cors or _env_flag("FANTASY_CORS"), allow, token)
    else:
        lid = args.league
        if not args.no_refresh:
            print(json.dumps(p.refresh_data(), indent=2))
        rd = lambda path: [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]
        if args.roster:
            print(json.dumps(p.set_roster(lid, rd(args.roster), rd(args.taken) if args.taken else None)))
        if args.cmd == "projections":
            _print_sheet(p.get_projections(lid, args.week, args.pos), args.top)
        elif args.cmd == "lineup":
            print(json.dumps(p.get_lineup(lid, args.week), indent=1))
        else:
            print(json.dumps(p.get_waivers(lid, args.week, args.top), indent=1))


if __name__ == "__main__":
    main()
