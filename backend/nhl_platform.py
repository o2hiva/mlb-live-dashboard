#!/usr/bin/env python
"""
nhl_platform.py  --  ONE FILE for the NHL goals model: data collection, predictions, Kalshi prices, logging, tracking, web service.

Embedded, unchanged and individually tested:
  nhl_goals_model   team-goal and game-total over/under/PUSH probabilities, goalie + back-to-back adjustments, overtime and
                    shootout process, push-aware Kelly, bet log, profit tracker, backtest, lineup check
  nhl_period_model  goals in each period (1st / 2nd / 3rd) on their own: team and total probabilities, P(goal), who leads
                    after the period, goals through two periods; same pricing / logging / tracking
  kalshi_lines      Kalshi's public NHL goal-total markets (game, team, period ladders) -> fee-inclusive lines (read-only, no key)
  nhl_io            atomic JSON writes, tolerant reads, cross-process file lock
plus the platform layer below: one class, NHLPlatform, with the read / write paths a website needs.

KALSHI RULES BUILT IN (from Kalshi's own rule text): full-game and team totals count regulation + overtime AND credit the
shootout winner one goal; period totals are regulation only.  Lines from Kalshi are priced and settled that way.  Quotes
older than 8 hours, or for another date, are ignored.  Typed sportsbook lines and Kalshi lines for the same market are
priced side by side; at most ONE bet per market is suggested (best EV).

STATUS: validated against RESULTS only (3 seasons, 3,936 games): beats a league-average guess by about +0.009 log loss per
team-game, +0.002 per game total, +0.003 per team-period -- thin edges.  Kalshi charges roughly a 9-cent spread plus fees on
totals, so few bets clear the EV threshold.  Run in shadow mode until the scorecard shows 100+ priced bets.

------------------------------------------------------------------ WIRING (library)
    from nhl_platform import NHLPlatform
    nhl = NHLPlatform("/data")
    nhl.refresh_data()                 # WRITE path: results, schedule, new boxscores (+ period goals). Never raises.
    nhl.refresh_kalshi()               # WRITE path: Kalshi prices -> nhl_lines_kalshi.json. Never raises.
    nhl.get_slate(date=None)           # game / team / total predictions + priced lines (cheap, cached, no network)
    nhl.get_periods(date=None)         # per-period predictions + priced period lines
    nhl.get_scorecard(); nhl.health(); nhl.kalshi_status()
    nhl.set_inputs(lines={...}, starters={...})     # YOUR typed lines / confirmed goalies, written atomically
  Every read returns a JSON-safe dict with "status" ("ok" | "error") and "warnings" and never raises.

------------------------------------------------------------------ HTTP (stdlib only) -- local or Railway
    python nhl_platform.py serve [--dir PATH] [--port N] [--host H] [--refresh-minutes 30] [--kalshi-minutes 10]
                                 [--cors] [--allow-write] [--bootstrap]
      GET  /api/nhl/slate?date=2026-10-10       GET /api/nhl/periods?date=...      GET /api/nhl/scorecard
      GET  /api/nhl/kalshi                       GET /health   (also /api/nhl/health)
      POST /api/nhl/inputs   {"lines": {...}, "starters": {...}}          (needs --allow-write)
      POST /api/nhl/refresh  {"what": "data" | "kalshi"}                  (needs --allow-write)
  Environment variables (used when the flag is not given):  PORT (Railway sets it; the server then binds 0.0.0.0),
    NHL_DATA_DIR (put this on a Railway volume, e.g. /data), NHL_API_TOKEN (if set, POSTs need  Authorization: Bearer <token>),
    NHL_REFRESH_MINUTES, NHL_KALSHI_MINUTES, NHL_ALLOW_WRITE=1, NHL_CORS=1, NHL_BOOTSTRAP=1.

------------------------------------------------------------------ RAILWAY, step by step
  1. Put this file (alone) in a repo, add a Volume mounted at /data.
  2. Start command:   python nhl_platform.py serve --bootstrap
     Variables:       NHL_DATA_DIR=/data   NHL_API_TOKEN=<long random string>   NHL_ALLOW_WRITE=1   NHL_CORS=1 (if a browser calls it)
  3. First boot with an empty volume: --bootstrap downloads three seasons (about 8,000 calls, 20-40 minutes), then tunes both models
     and writes the parameter files.  Until it finishes, /health shows "bootstrap": running and slates carry UNVALIDATED warnings.
     It is resumable: if the service restarts, it carries on.  (Faster alternative: run `bootstrap` once on your PC, copy the
     nhl_*.json files into the volume.)
  4. Health check path: /health.     The dashboard calls /api/nhl/slate and /api/nhl/periods.
  5. Monthly: `python nhl_platform.py bootstrap` (or run it in a Railway shell) re-tunes both models on the newest data.

------------------------------------------------------------------ COMMAND LINE (run from the NHL folder)
    python nhl_platform.py daily [--date D] [--no-refresh]      refresh data + Kalshi, print game and period slates, log predictions
    python nhl_platform.py slate | periods | refresh | kalshi | track | bootstrap [--seasons A,B,C]
    python nhl_platform.py run nhl_goals_model backtest --seasons 20232024,20242025,20252026
    python nhl_platform.py run nhl_period_model backtest --seasons 20232024,20242025,20252026
    python nhl_platform.py run kalshi_lines rules
    python nhl_platform.py unpack OUTDIR                        write the embedded modules out as .py files

API: api-web.nhle.com (public, no key) and api.elections.kalshi.com (public market data, read-only).
FILES (in the data dir): nhl_goals_{season}.json (schedule + results), nhl_goals_box_{season}.json (goalies, skaters, period goals),
  nhl_goals_params.json / nhl_period_params.json (tuned), nhl_goals_log.jsonl (the prediction / bet log), nhl_lines.json and
  nhl_starters.json (YOUR inputs), nhl_lines_kalshi.json (fetched), nhl_refresh_state.json.
  lines:    {"TOR@BOS|total": {"line": 6.0, "over": -110, "under": -110}, "TOR@BOS|home": 2.5, "TOR@BOS|p1total": 1.5,
             "TOR@BOS|total": [ladder of several lines as a list]}       markets: total, home, away, p1/p2/p3 + total/home/away
  starters: {"BOS": "Swayman", "TOR": "Stolarz"}
NOT COVERED: playoffs, skater lineups / injuries, automatic starter detection, Kalshi winner / spread / overtime / player markets.
Needs: python 3.9+ and nothing else.
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
_ORDER = ['nhl_io', 'nhl_goals_model', 'nhl_period_model', 'kalshi_lines']
_SRC = {}
# ======================================================================
# embedded module: nhl_io.py (96 lines)
# ======================================================================
_SRC["nhl_io"] = r'''"""
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
# embedded module: nhl_goals_model.py (1857 lines)
# ======================================================================
_SRC["nhl_goals_model"] = r'''"""
nhl_goals_model.py  --  NHL team-goals and game-total model, with market pricing, Kelly staking and profit tracking.

ONE FILE, standard library only (no numpy / pandas / requests).

FORMULA (same shrunk index-multiplication pattern as the rest of this project)
    goals_home = home_scoring_index * away_allowed_index * league_avg_goals * (1 + hfa)
    goals_away = away_scoring_index * home_allowed_index * league_avg_goals / (1 + hfa)
    scoring_index = shrunk goals-for per game / league avg     (shrunk toward league avg with k pseudo-games)
    allowed_index = shrunk goals-against per game / league avg
    game total mean = goals_home + goals_away
  The means are fitted on REGULATION (60-minute) goals.  Regulation scores are independent Poisson / Negative Binomial /
  Binomial counts (shape chosen by the backtest).  If the teams are tied after 60 minutes, overtime ends the game with
  probability q_ot (one extra goal, to the home team with probability w_home); otherwise it goes to a shootout (tie, no
  goal counted).  q_ot and w_home are measured from the data.  This is why odd totals (5, 7, 9) are more common than
  a plain Poisson model says: every OT goal turns an even tied total into an odd one.

WHAT COUNTS AS A GOAL: regulation + overtime goals only.  A shootout adds one phantom goal to the winner's final score;
  sportsbooks settle totals WITHOUT it, so it is removed (games whose period type is SO count as a tie at the end of OT).

GOALIES AND REST (v3)
  * Each team's goals-against is divided by the quality of the goalie who played (shrunk save%), so team defence means
    skaters + system, not whoever was in net.  Tonight's opponent goal mean is multiplied by tonight's goalie factor.
  * Starter KNOWN (nhl_starters.json, e.g. {"BOS": "Swayman"}): that goalie is used.  Starter UNKNOWN: a mixture of the team's
    No.1 and No.2 goalies, weighted by how often the No.1 really starts (measured, and much lower on the second night of a
    back-to-back).  The backtest reports both cases and switches the mixture off if it does not help out of sample.
  * Back-to-back / long rest: Poisson regression of regulation goals on four rest flags (own/opponent back-to-back, own/opponent
    4+ days off) with the model's own mean as offset; coefficients and z-scores are printed.

COMMANDS (run in your NHL folder; data files are created next to the script)
    python nhl_goals_model.py probe    --season 20252026          # one-time: confirm the API fields this script reads
    python nhl_goals_model.py probe-pregame --date 2026-10-08     # one-time: what does the API publish before puck drop?
    python nhl_goals_model.py update                              # the daily job: results, boxscores, today's predictions logged
    python nhl_goals_model.py lineups-check --seasons 20232024,20242025,20252026   # best-case test: do skater lineups move goals?
    python nhl_goals_model.py boxscores --seasons 20232024,20242025,20252026   # goalie + skater lines per game (~4,000 calls once)
    python nhl_goals_model.py refresh  --season 20252026          # 32 schedule calls; resumable, safe to re-run daily
    python nhl_goals_model.py backtest --seasons 20232024,20242025,20252026   # tunes + validates, writes nhl_goals_params.json
    python nhl_goals_model.py slate    [--date 2026-10-10] [--lines-file nhl_lines.json] [--bankroll 1000]
    python nhl_goals_model.py track    [--bankroll 1000]          # settles logged bets, profit / ROI / calibration

LINES FILE (nhl_lines.json, you type the book's numbers; keys are AWAY@HOME|market, market = total | home | away)
    {"TOR@BOS|total": {"line": 6.0, "over": -110, "under": -110},
     "TOR@BOS|home":  {"line": 2.5, "over": -130, "under": 105},
     "TOR@BOS|away":  2.5}                       <- a bare number = line only, no odds (probabilities, no staking)

STAKING: push-aware Kelly, f = (b*p_win - p_loss) / (b*(p_win + p_loss)), times --kelly (default 0.25), capped at
  --max-stake-pct of bankroll (default 3%), only when EV per unit >= --min-ev (default 3%).  The model has not been
  validated against sportsbook prices (no historical lines), so treat the first ~100 logged bets as a test, not a bankroll plan.
"""

import argparse
import datetime as dt
import itertools
import json
import math
import os
import sys
import time
import urllib.request

API_BASE = "https://api-web.nhle.com/v1"
NHL_TEAMS = [
    "ANA", "UTA", "BOS", "BUF", "CGY", "CAR", "CHI", "COL", "CBJ", "DAL", "DET", "EDM", "FLA", "LAK", "MIN", "MTL",
    "NSH", "NJD", "NYI", "NYR", "OTT", "PHI", "PIT", "SJS", "SEA", "STL", "TBL", "TOR", "VAN", "VGK", "WSH", "WPG",
]
REGULAR_SEASON = 2
FINAL_STATES = ("OFF", "FINAL")
UPCOMING_STATES = ("FUT", "PRE", "LIVE", "CRIT")
LG_STATIC = 3.00           # fallback goals per team-game before any data exists
POOL_PSEUDO = 60.0         # team-games of prior weight on the league average (smooths the early season)
MAX_TEAM_GOALS = 20
MAX_TOTAL_GOALS = 34
DEFAULTS = dict(k=25.0, carry=0.25, hfa=0.03, r_team=None, q_ot=0.5, w_home=0.5, min_games=3,
                gamma_g=0.5, n0_g=800.0, rest={})
GOALIE_CARRY = 0.6          # weight on last season's shots/saves when judging a goalie
SV_PRIOR = 0.905            # league save% prior, with SV_PRIOR_SHOTS pseudo shots
SV_PRIOR_SHOTS = 3000.0
START_WINDOW = 25           # a team's last N starts decide who its No.1 / No.2 goalie is
MIN_STARTS_KNOWN = 8
REST_KEYS = ["own_b2b", "opp_b2b", "own_long", "opp_long"]
BOX_PREFIX = "nhl_goals_box_"   # UNVALIDATED until backtest has run
LOG_NAME = "nhl_goals_log.jsonl"
PARAMS_NAME = "nhl_goals_params.json"


# =========================================================================================== small utilities
def _utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class FileLock:
    """Cross-process mutex via an exclusive lock file; a lock left by a crashed job is broken after stale_after seconds."""

    def __init__(self, path, timeout=0.0, stale_after=900.0):
        self.path, self.timeout, self.stale_after, self._fd = path, timeout, stale_after, None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale_after:
                        os.unlink(self.path)
                        continue
                except OSError:
                    continue
                if time.time() >= deadline:
                    raise TimeoutError(self.path)
                time.sleep(0.05)

    def __exit__(self, *a):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def season_code_for(date_str):
    d = dt.date.fromisoformat(date_str)
    start = d.year if d.month >= 8 else d.year - 1
    return f"{start}{start + 1}"


def data_file(directory, season):
    return os.path.join(directory, f"nhl_goals_{season}.json")


def fetch_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nhl-goals-model"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:      # network errors are retried, then re-raised
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


# =========================================================================================== odds / Kelly
def american_to_decimal(o):
    o = float(o)
    return 1.0 + (o / 100.0 if o > 0 else 100.0 / -o)


def novig_two_way(dec_a, dec_b):
    ia, ib = 1.0 / dec_a, 1.0 / dec_b
    s = ia + ib
    return ia / s, ib / s


def kelly_fraction(p_win, p_loss, dec):
    """Full-Kelly fraction with a push (stake returned) as the third outcome."""
    b = dec - 1.0
    if b <= 0 or (p_win + p_loss) <= 0:
        return 0.0
    return max(0.0, (b * p_win - p_loss) / (b * (p_win + p_loss)))


# =========================================================================================== distributions
def goal_pmf(mu, r=None, nmax=MAX_TEAM_GOALS):
    """P(X = 0..nmax) for a regulation goal count with mean mu.
    r None -> Poisson (var = mu);  r > 0 -> Negative Binomial (var = mu + mu^2/r, wider);
    r < 0 -> Binomial with n = -r trials (var = mu (1 - mu/n), narrower).  Renormalised."""
    mu = max(float(mu), 0.05)
    out = []
    if r is None or abs(r) >= 1e6:
        p = math.exp(-mu)
        out.append(p)
        for k in range(1, nmax + 1):
            p *= mu / k
            out.append(p)
    elif r > 0:
        out.append(math.exp(r * math.log(r / (r + mu))))
        for k in range(1, nmax + 1):
            out.append(out[-1] * (k - 1 + r) / k * (mu / (r + mu)))
    else:
        n = int(round(-r))
        pr = min(mu / n, 0.999)
        out.append((1 - pr) ** n)
        for k in range(1, nmax + 1):
            out.append(out[-1] * (n - k + 1) / k * pr / (1 - pr) if k <= n else 0.0)
    s = sum(out)
    return [x / s for x in out]


def game_dists(mu_h, mu_a, params, so_goal=False):
    """-> (home_goals_pmf, away_goals_pmf, total_pmf) for FINAL goals (regulation + overtime, no shootout goal).
    so_goal=True counts the shootout winner as one extra goal (50/50 who wins it) - how Kalshi settles its goal markets."""
    shape = params.get("r_team")
    q, w = params.get("q_ot", 0.5), params.get("w_home", 0.5)
    ph, pa = goal_pmf(mu_h, shape), goal_pmf(mu_a, shape)
    n = len(ph)
    H, A, T = [0.0] * (n + 1), [0.0] * (n + 1), [0.0] * (2 * n + 2)
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            pr = x * y
            if pr < 1e-13:
                continue
            if i == j:
                stay, hw, aw = pr * (1 - q), pr * q * w, pr * q * (1 - w)
                if so_goal:
                    H[i] += aw + stay * 0.5
                    A[j] += hw + stay * 0.5
                    H[i + 1] += stay * 0.5
                    A[j + 1] += stay * 0.5
                    T[i + j + 1] += stay
                else:
                    H[i] += stay + aw
                    A[j] += stay + hw
                    T[i + j] += stay
                H[i + 1] += hw
                A[j + 1] += aw
                T[i + j + 1] += hw + aw
            else:
                H[i] += pr
                A[j] += pr
                T[i + j] += pr
    H, A, T = H[:MAX_TEAM_GOALS + 1], A[:MAX_TEAM_GOALS + 1], T[:MAX_TOTAL_GOALS + 1]
    return [x / sum(H) for x in H], [x / sum(A) for x in A], [x / sum(T) for x in T]


def total_pmf(mu_h, mu_a, params):
    return game_dists(mu_h, mu_a, params)[2]


def regulation_goals(g):
    """Final scores -> 60-minute scores (the overtime winner's extra goal removed)."""
    hg, ag = g["hg"], g["ag"]
    if g.get("period") == "OT" and hg != ag:
        return (hg - 1, ag) if hg > ag else (hg, ag - 1)
    return hg, ag


def over_under_push(pmf, line):
    over = sum(p for k, p in enumerate(pmf) if k > line)
    under = sum(p for k, p in enumerate(pmf) if k < line)
    push = pmf[int(line)] if float(line).is_integer() and int(line) < len(pmf) else 0.0
    return over, under, push


def fair_line(pmf):
    """Half-point line whose P(over) is closest to 50%."""
    best = None
    for i in range(0, len(pmf) - 1):
        line = i + 0.5
        o = over_under_push(pmf, line)[0]
        if best is None or abs(o - 0.5) < best[0]:
            best = (abs(o - 0.5), line)
    return best[1]


def pmf_nll(pmf, k):
    return -math.log(max(pmf[min(int(k), len(pmf) - 1)], 1e-12))


# =========================================================================================== data: parse + refresh
def adjust_for_shootout(hg, ag, period):
    """Final scores include one phantom shootout goal for the winner; sportsbooks do not count it."""
    if period == "SO" and hg != ag:
        return (hg - 1, ag) if hg > ag else (hg, ag - 1)
    return hg, ag


def parse_schedule(schedule):
    """-> (finished {id: game}, upcoming [game]).  Only regular-season games.  Uses fields confirmed by nhl_probe_api.py
    (id, gameType, gameState, gameDate, homeTeam.abbrev, awayTeam.abbrev); score / gameOutcome are checked by `probe`."""
    finished, upcoming = {}, []
    for g in schedule.get("games", []):
        if g.get("gameType") != REGULAR_SEASON:
            continue
        home = (g.get("homeTeam") or {})
        away = (g.get("awayTeam") or {})
        rec = {"id": g.get("id"), "date": g.get("gameDate"), "start_utc": g.get("startTimeUTC"),
               "home": home.get("abbrev"), "away": away.get("abbrev")}
        state = g.get("gameState")
        if state in FINAL_STATES:
            rec["hs"], rec["as"] = home.get("score"), away.get("score")
            rec["period"] = (g.get("gameOutcome") or {}).get("lastPeriodType")
            finished[str(rec["id"])] = rec
        elif state in UPCOMING_STATES:
            upcoming.append(rec)
    return finished, upcoming


def _finalise_game(rec):
    hg, ag = adjust_for_shootout(int(rec["hs"]), int(rec["as"]), rec.get("period"))
    so = None
    if rec.get("period") == "SO" and int(rec["hs"]) != int(rec["as"]):
        so = "home" if int(rec["hs"]) > int(rec["as"]) else "away"          # who won the shootout (Kalshi credits that team a goal)
    return {"id": rec["id"], "date": rec["date"], "start_utc": rec.get("start_utc"), "home": rec["home"],
            "away": rec["away"], "hg": hg, "ag": ag, "period": rec.get("period"), "so": so}


def refresh_season(directory, season, fetch=fetch_json, log=print):
    """Pulls all 32 club schedules (1 call each), keeps finished games (with scores) and the upcoming list.
    Games whose schedule entry lacks a score or period type are completed from the boxscore.  Never loses stored data."""
    path = data_file(directory, season)
    store = _read_json(path, {"season": season, "games": {}, "upcoming": []})
    finished_all, upcoming_all, errors = {}, {}, 0
    for i, team in enumerate(NHL_TEAMS, 1):
        try:
            sched = fetch(f"{API_BASE}/club-schedule-season/{team}/{season}")
        except Exception as e:
            errors += 1
            log(f"  [{i}/{len(NHL_TEAMS)}] {team}: {e}")
            continue
        fin, up = parse_schedule(sched)
        finished_all.update(fin)
        for u in up:
            upcoming_all[str(u["id"])] = u
    fetched_box = 0
    for gid, rec in finished_all.items():
        if gid in store["games"] and store["games"][gid].get("period") is not None:
            continue
        if rec.get("hs") is None or rec.get("as") is None or rec.get("period") is None:
            try:
                box = fetch(f"{API_BASE}/gamecenter/{gid}/boxscore")
                rec["hs"] = (box.get("homeTeam") or {}).get("score", rec.get("hs"))
                rec["as"] = (box.get("awayTeam") or {}).get("score", rec.get("as"))
                rec["period"] = ((box.get("gameOutcome") or {}).get("lastPeriodType")
                                 or (box.get("periodDescriptor") or {}).get("periodType") or rec.get("period"))
                fetched_box += 1
            except Exception as e:
                log(f"  boxscore {gid}: {e}")
        if rec.get("hs") is None or rec.get("as") is None:
            continue
        store["games"][gid] = _finalise_game(rec)
    store["upcoming"] = [u for u in upcoming_all.values() if str(u["id"]) not in store["games"]]
    store["fetched_at"] = _utcnow().isoformat() + "Z"
    if errors < len(NHL_TEAMS):         # never overwrite with an all-failed pull
        _atomic_json(path, store)
    unknown = sum(1 for g in store["games"].values() if g.get("period") is None)
    return {"season": season, "games": len(store["games"]), "upcoming": len(store["upcoming"]),
            "schedule_errors": errors, "boxscore_calls": fetched_box, "games_without_period_type": unknown}


def probe(season, team="TOR", fetch=fetch_json, out=print):
    """Shows whether the fields this script depends on are present in the real API response."""
    sched = fetch(f"{API_BASE}/club-schedule-season/{team}/{season}")
    games = [g for g in sched.get("games", []) if g.get("gameType") == REGULAR_SEASON and g.get("gameState") in FINAL_STATES]
    out(f"{len(sched.get('games', []))} games in {team}'s schedule, {len(games)} finished regular-season games")
    if not games:
        out("No finished regular-season game yet for this season - try --season with last season's code.")
        return
    g = games[-1]
    box = fetch(f"{API_BASE}/gamecenter/{g['id']}/boxscore")
    checks = [("schedule homeTeam.score", (g.get("homeTeam") or {}).get("score")),
              ("schedule awayTeam.score", (g.get("awayTeam") or {}).get("score")),
              ("schedule gameOutcome.lastPeriodType", (g.get("gameOutcome") or {}).get("lastPeriodType")),
              ("schedule startTimeUTC", g.get("startTimeUTC")),
              ("boxscore homeTeam.score", (box.get("homeTeam") or {}).get("score")),
              ("boxscore awayTeam.score", (box.get("awayTeam") or {}).get("score")),
              ("boxscore gameOutcome.lastPeriodType", (box.get("gameOutcome") or {}).get("lastPeriodType")),
              ("boxscore periodDescriptor.periodType", (box.get("periodDescriptor") or {}).get("periodType"))]
    for name, val in checks:
        out(f"  {'OK       ' if val is not None else 'NOT FOUND'} {name} = {val}")
    land = {}
    try:
        land = fetch(f"{API_BASE}/gamecenter/{g['id']}/landing")
    except Exception as e:
        out(f"  (landing page not available: {e})")
    out("\nGoals by period (needed for the period model):")
    for label, data in (("boxscore", box), ("landing", land)):
        per = parse_periods(data)
        out(f"  {'OK       ' if per else 'NOT FOUND'} {label}: {per}")
        if not per:
            for p_, v in _find_keys(data, ("linescore", "byperiod", "scoring", "periodby"))[:8]:
                out(f"      saw {p_} = {str(v)[:80]}")
    if sum((box.get('homeTeam') or {}).get('score', 0) for _ in [0]) is not None:
        out(f"  (official score this game: home {(box.get('homeTeam') or {}).get('score')}, away {(box.get('awayTeam') or {}).get('score')})")
    pb = box.get("playerByGameStats") or {}
    home = pb.get("homeTeam") or {}
    out("\nPlayer data inside the boxscore (needed for goalies and the lineup model):")
    for grp in ("goalies", "forwards", "defense"):
        lst = home.get(grp) or []
        out(f"  {'OK       ' if lst else 'NOT FOUND'} playerByGameStats.homeTeam.{grp}: {len(lst)} players")
        if lst:
            out(f"      fields: {', '.join(sorted(lst[0].keys()))}")
    out("\nPaste this output back if anything says NOT FOUND (the script falls back to the boxscore automatically).")


def box_file(directory, season):
    return os.path.join(directory, f"{BOX_PREFIX}{season}.json")


def _toi_seconds(t):
    try:
        m_, s_ = str(t).split(":")
        return int(m_) * 60 + int(s_)
    except (ValueError, AttributeError):
        return 0


def _team_abbrev(x):
    if isinstance(x, dict):
        return x.get("default") or x.get("abbrev")
    return x


def parse_periods(d):
    """Goals by REGULATION period (1-3) for both teams, from whichever of the known response shapes is present:
       linescore.byPeriod[{periodDescriptor.number, home, away}]   (top level or under `summary`)
       scoring[{periodDescriptor.number, goals:[{teamAbbrev}]}]    (top level or under `summary`)
    -> {"home": [p1, p2, p3], "away": [p1, p2, p3]} or None.  `periods-check` verifies it against the real final scores."""
    if not isinstance(d, dict):
        return None
    for root in (d, d.get("summary") if isinstance(d.get("summary"), dict) else {}):
        ls = root.get("linescore")
        rows = ls.get("byPeriod") if isinstance(ls, dict) else None
        if isinstance(rows, list):
            out = {"home": [0, 0, 0], "away": [0, 0, 0]}
            seen = set()
            for r in rows:
                n = (r.get("periodDescriptor") or {}).get("number", r.get("period"))
                if isinstance(n, int) and 1 <= n <= 3:
                    out["home"][n - 1], out["away"][n - 1] = int(r.get("home") or 0), int(r.get("away") or 0)
                    seen.add(n)
            if seen == {1, 2, 3}:
                return out
    home = ((d.get("homeTeam") or {}).get("abbrev"))
    away = ((d.get("awayTeam") or {}).get("abbrev"))
    for root in (d, d.get("summary") if isinstance(d.get("summary"), dict) else {}):
        sc = root.get("scoring")
        if isinstance(sc, list) and sc and home and away:
            out = {"home": [0, 0, 0], "away": [0, 0, 0]}
            seen = set()
            for per in sc:
                n = (per.get("periodDescriptor") or {}).get("number")
                if isinstance(n, int) and 1 <= n <= 3:
                    seen.add(n)
                    for goal in per.get("goals") or []:
                        t = _team_abbrev(goal.get("teamAbbrev"))
                        if t == home:
                            out["home"][n - 1] += 1
                        elif t == away:
                            out["away"][n - 1] += 1
            if seen:
                return out
    return None


def extract_box(box):
    """Boxscore -> goalie lines and skater lines for both sides.  Goalie fields (playerId, name.default, shotsAgainst, saves,
    toi, decision) are the ones already confirmed by the NHL collectors; skater fields are read tolerantly (see `probe`)."""
    pb = box.get("playerByGameStats") or {}
    out = {"gl": {}, "sk": {}}
    for side, key in (("home", "homeTeam"), ("away", "awayTeam")):
        st = pb.get(key) or {}
        out["gl"][side] = [{"id": g.get("playerId"), "name": (g.get("name") or {}).get("default"),
                            "sa": g.get("shotsAgainst") or 0, "sv": g.get("saves") or 0,
                            "toi": _toi_seconds(g.get("toi")), "dec": g.get("decision")} for g in st.get("goalies", [])]
        sk = []
        for grp in ("forwards", "defense"):
            for p in st.get(grp, []):
                sk.append({"id": p.get("playerId"), "name": (p.get("name") or {}).get("default"), "pos": p.get("position"),
                           "toi": _toi_seconds(p.get("toi")), "g": p.get("goals") or 0, "a": p.get("assists") or 0,
                           "sog": p.get("sog", p.get("shots")) or 0})
        out["sk"][side] = sk
    out["per"] = parse_periods(box)
    return out


def _merge_write(path, entries, season):
    """Re-read the file under a lock and add our entries, so two jobs (daily task + dashboard refresh) cannot lose each other's work."""
    lock = FileLock(path + ".lock", timeout=60.0, stale_after=300)
    with lock:
        cur = _read_json(path, {"season": season, "box": {}}) or {"season": season, "box": {}}
        cur["box"].update(entries)
        _atomic_json(path, cur)
        return len(cur["box"])


def collect_boxscores(directory, season, fetch=fetch_json, workers=4, limit=None, log=print, fill_periods=False):
    """Downloads each finished game's boxscore once (goalies, skaters, goals by period).  Resumable: saved every 100 games.
    fill_periods=True also re-downloads games saved BEFORE period data was collected.  If the boxscore has no period
    scoring, the game's landing page is tried; a game whose period goals do not add up to its regulation score is stored
    as per=False (so it is not fetched again)."""
    import concurrent.futures as cf
    games = (_read_json(data_file(directory, season), {}) or {}).get("games", {})
    path = box_file(directory, season)
    store = _read_json(path, {"season": season, "box": {}})
    need = [gid for gid in games if gid not in store["box"] or (fill_periods and "per" not in store["box"][gid])]
    if limit:
        need = need[:limit]
    done = errors = bad_periods = 0
    pending = {}
    if need:
        log(f"{season}: {len(need)} boxscore(s) to download ({len(store['box'])} already saved)")

    def one(gid):
        old = store["box"].get(gid)
        if old is not None and "gl" in old:                  # already have goalies/skaters: only the period goals are missing
            rec = {k: v for k, v in old.items() if k not in ("per", "per_bad")}
            rec["per"] = None
        else:
            rec = extract_box(fetch(f"{API_BASE}/gamecenter/{gid}/boxscore"))
        if rec["per"] is None:
            try:
                rec["per"] = parse_periods(fetch(f"{API_BASE}/gamecenter/{gid}/landing"))
            except Exception:
                rec["per"] = None
        g = games[gid]
        rg = regulation_goals(g)
        if rec["per"] is not None and (sum(rec["per"]["home"]), sum(rec["per"]["away"])) != rg:
            rec["per"] = False                      # inconsistent with the official score: do not trust
            rec["per_bad"] = True
        elif rec["per"] is None:
            rec["per"] = False
        return gid, rec

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, gid) for gid in need]
        for f in cf.as_completed(futs):
            try:
                gid, rec = f.result()
                pending[gid] = rec
                bad_periods += 1 if rec.get("per_bad") else 0
                done += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    log(f"  boxscore error: {e}")
            if done and done % 100 == 0 and pending:
                _merge_write(path, pending, season)
                pending = {}
                log(f"  {done}/{len(need)}")
    total = len(store["box"])
    if pending:
        total = _merge_write(path, pending, season)
    return {"season": season, "downloaded": done, "errors": errors, "total_saved": total, "period_mismatch": bad_periods}


def rest_flags(gap):
    """gap = days since the team's previous game.  -> (back_to_back, long_rest)."""
    return (1.0 if gap == 1 else 0.0), (1.0 if gap is not None and gap >= 4 else 0.0)


def _attach_rest(games, upcoming):
    last = {}
    for g in sorted(games + upcoming, key=lambda x: (x["date"], x["id"])):
        d = dt.date.fromisoformat(g["date"])
        for side, t in (("h", g["home"]), ("a", g["away"])):
            g["rest_" + side] = (d - last[t]).days if t in last else None
        last[g["home"]] = d
        last[g["away"]] = d


def _find_keys(obj, words, path="", out=None, depth=0):
    out = [] if out is None else out
    if depth > 7:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if any(w in k.lower() for w in words):
                out.append((p, v if not isinstance(v, (dict, list)) else f"<{type(v).__name__} len {len(v)}>"))
            _find_keys(v, words, p, out, depth + 1)
    elif isinstance(obj, list) and obj:
        _find_keys(obj[0], words, path + "[0]", out, depth + 1)
    return out


def _dump(obj, path, out, depth):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:14]:
            if isinstance(v, (dict, list)):
                out(f"   {path}.{k}: <{type(v).__name__} len {len(v)}>")
                _dump(v, f"{path}.{k}", out, depth + 1)
            else:
                out(f"   {path}.{k} = {str(v)[:70]}")
    elif isinstance(obj, list) and obj:
        _dump(obj[0], path + "[0]", out, depth + 1)


def probe_pregame(directory, season, date, fetch=fetch_json, out=print):
    """One-time: what does the NHL API publish BEFORE puck drop (starting goalies, lineups, scratches, injuries)?"""
    _, upcoming = load_games(directory, season)
    today = [u for u in upcoming if u["date"] == date] or upcoming[:1]
    if not today:
        out("No upcoming game found in the saved schedule - run `refresh` first.")
        return
    u = today[0]
    out(f"Looking at {u['away']} @ {u['home']} on {u['date']} (game {u['id']})")
    for ep in ("landing", "right-rail", "boxscore"):
        try:
            data = fetch(f"{API_BASE}/gamecenter/{u['id']}/{ep}")
        except Exception as e:
            out(f"\n{ep}: not available ({e})")
            continue
        out(f"\n{ep}: top-level keys = {', '.join(sorted(data.keys()))}")
        if ep == "landing" and isinstance(data.get("matchup"), dict):
            out("   --- matchup subtree (values trimmed) ---")
            _dump(data["matchup"], "matchup", out, 0)
        hits = _find_keys(data, ("goalie", "starter", "lineup", "scratch", "injur", "probable", "roster"))
        for p_, v in hits[:30]:
            out(f"   {p_} = {str(v)[:90]}")
        if not hits:
            out("   (no goalie / starter / lineup / scratch / injury fields)")
    out("\nPaste this back: it tells me whether starting goalies and lineups can be read automatically before the game.")


def load_games(directory, season):
    d = _read_json(data_file(directory, season), None)
    if not d:
        return [], []
    box = (_read_json(box_file(directory, season), {}) or {}).get("box", {})
    games = sorted(d["games"].values(), key=lambda g: (g["date"], g["id"]))
    upcoming = [dict(u) for u in d.get("upcoming", [])]
    for g in games:
        b = box.get(str(g["id"]))
        if b:
            g["gl"], g["sk"] = b["gl"], b["sk"]
            if b.get("per"):
                g["per"] = b["per"]
    _attach_rest(games, upcoming)
    return games, upcoming


# =========================================================================================== the model state
class State:
    """Running team goals for/against (regulation), goalie save%, who starts in goal, with last-season carry-over."""

    def __init__(self, params, prev=None):
        self.p = params
        self.teams = {}
        self.prev = prev.final_counts() if prev else {}
        self.prior_lg = prev.league_avg() if prev else LG_STATIC
        self.pool_sum = 0.0
        self.pool_n = 0
        self.goalies = {}                       # id -> [shots against, saves] this season
        self.prev_goalies = prev.all_goalies() if prev else {}
        self.names = dict(prev.names) if prev else {}
        self.starts = {t: list(v)[-START_WINDOW:] for t, v in prev.starts.items()} if prev else {}   # team -> [(goalie id, b2b)]
        self.sv_shots, self.sv_saves = 0.0, 0.0
        self.sv_prior = prev.league_sv() if prev else SV_PRIOR
        self.sp = {"n": [0, 0], "g1": [0, 0]}   # No.1 goalie start counts: [normal, back-to-back]
        if prev:
            self.sp = {"n": list(prev.sp["n"]), "g1": list(prev.sp["g1"])}

    # ---- goalies
    def league_sv(self):
        return (self.sv_saves + SV_PRIOR_SHOTS * self.sv_prior) / (self.sv_shots + SV_PRIOR_SHOTS)

    def all_goalies(self):
        out = {k: list(v) for k, v in self.prev_goalies.items()}
        for k, v in self.goalies.items():
            o = out.setdefault(k, [0.0, 0.0])
            o[0] += v[0]
            o[1] += v[1]
        return out

    def gfac(self, gid):
        """Multiplier on the opposing team's goals when this goalie plays (1.0 = league-average goalie)."""
        gamma = self.p.get("gamma_g", 0.0)
        if gamma == 0 or gid is None:
            return 1.0
        sa, sv = self.goalies.get(gid, (0.0, 0.0))
        psa, psv = self.prev_goalies.get(gid, (0.0, 0.0))
        sa += GOALIE_CARRY * psa
        sv += GOALIE_CARRY * psv
        lg = self.league_sv()
        n0 = self.p.get("n0_g", 800.0)
        sv_shr = (sv + n0 * lg) / (sa + n0)
        return max(0.5, ((1.0 - sv_shr) / (1.0 - lg))) ** gamma

    def _game_factor(self, goalies):
        if not goalies:
            return 1.0
        w = sum(max(g["toi"], 1) for g in goalies)
        return sum(max(g["toi"], 1) * self.gfac(g["id"]) for g in goalies) / w

    def top_goalies(self, team):
        cnt = {}
        for gid, _ in self.starts.get(team, [])[-START_WINDOW:]:
            cnt[gid] = cnt.get(gid, 0) + 1
        return sorted(cnt.items(), key=lambda x: -x[1])

    def _p_no1(self, team, b2b):
        lst = self.starts.get(team, [])[-START_WINDOW:]
        top = self.top_goalies(team)
        pooled_n = (self.sp["g1"][0] + 40 * 0.8) / (self.sp["n"][0] + 40)
        pooled_b = (self.sp["g1"][1] + 25 * 0.4) / (self.sp["n"][1] + 25)
        normal = [s for s in lst if not s[1]]
        n1 = sum(1 for s in normal if s[0] == top[0][0])
        p_team = (n1 + 5 * pooled_n) / (len(normal) + 5)
        return min(0.97, p_team * (pooled_b / pooled_n)) if b2b else min(0.97, p_team)

    def scenarios(self, team, b2b, confirmed=None):
        """-> [(weight, factor, name)] for the goalie this team will use."""
        if confirmed:
            low = confirmed.lower()
            for gid, nm in self.names.items():
                if nm and (low == nm.lower() or low in nm.lower() or nm.lower().split(". ")[-1] == low.split()[-1]):
                    if any(gid == s[0] for s in self.starts.get(team, [])):
                        return [(1.0, self.gfac(gid), nm)]
            return [(1.0, 1.0, confirmed)]
        top = self.top_goalies(team)
        if not top or sum(c for _, c in top) < MIN_STARTS_KNOWN:
            return [(1.0, 1.0, None)]
        p = self._p_no1(team, b2b)
        g1 = top[0][0]
        if len(top) > 1:
            g2 = top[1][0]
            return [(p, self.gfac(g1), self.names.get(g1)), (1 - p, self.gfac(g2), self.names.get(g2))]
        return [(p, self.gfac(g1), self.names.get(g1)), (1 - p, 1.0, None)]

    # ---- teams
    def add_game(self, game):
        hg, ag = regulation_goals(game)
        home, away = game["home"], game["away"]
        gl = game.get("gl") or {}
        fh, fa = self._game_factor(gl.get("home")), self._game_factor(gl.get("away"))   # BEFORE this game's goalie stats
        # who started, versus who we expected (for the start-probability estimates)
        for side, team, gap in (("home", home, game.get("rest_h")), ("away", away, game.get("rest_a"))):
            g_list = gl.get(side) or []
            if not g_list:
                continue
            starter = max(g_list, key=lambda g: g["toi"])["id"]
            b2b = 1 if gap == 1 else 0
            top = self.top_goalies(team)
            if top and sum(c for _, c in top) >= MIN_STARTS_KNOWN:
                self.sp["n"][b2b] += 1
                self.sp["g1"][b2b] += 1 if starter == top[0][0] else 0
            self.starts.setdefault(team, []).append((starter, b2b))
            self.starts[team] = self.starts[team][-START_WINDOW:]
            for g in g_list:
                if g["id"] is not None:
                    gg = self.goalies.setdefault(g["id"], [0.0, 0.0])
                    gg[0] += g["sa"]
                    gg[1] += g["sv"]
                    self.names[g["id"]] = g["name"] or self.names.get(g["id"])
                    self.sv_shots += g["sa"]
                    self.sv_saves += g["sv"]
        for t, gf, ga in ((home, hg, ag / fh), (away, ag, hg / fa)):
            c = self.teams.setdefault(t, [0, 0.0, 0.0])
            c[0] += 1
            c[1] += gf
            c[2] += ga
        self.pool_sum += hg + ag
        self.pool_n += 2

    def league_avg(self):
        return (self.pool_sum + POOL_PSEUDO * self.prior_lg) / (self.pool_n + POOL_PSEUDO)

    def games_played(self, t):
        return self.teams.get(t, [0])[0]

    def _rates(self, t, lg):
        n, gf, ga = self.teams.get(t, [0, 0.0, 0.0])
        pn, pgf, pga = self.prev.get(t, (0, 0.0, 0.0))
        c = self.p["carry"]
        n_eff, gf_eff, ga_eff = n + c * pn, gf + c * pgf, ga + c * pga
        k = self.p["k"]
        return (gf_eff + k * lg) / (n_eff + k), (ga_eff + k * lg) / (n_eff + k)

    def means(self, home, away):
        """Team-only means (before tonight's goalie and rest adjustments)."""
        lg = self.league_avg()
        h_for, h_against = self._rates(home, lg)
        a_for, a_against = self._rates(away, lg)
        h = 1.0 + self.p["hfa"]
        mu_h = (h_for / lg) * (a_against / lg) * lg * h
        mu_a = (a_for / lg) * (h_against / lg) * lg / h
        return mu_h, mu_a, lg

    def trusted(self, home, away):
        m = self.p["min_games"]
        return self.games_played(home) >= m and self.games_played(away) >= m

    def final_counts(self):
        return {t: (n, gf, ga) for t, (n, gf, ga) in self.teams.items()}


def rest_x(gap_own, gap_opp):
    ob, ol = rest_flags(gap_own)
    pb, pl = rest_flags(gap_opp)
    return [ob, pb, ol, pl]


def row_combos(r, params, use_lg=False, mode="expected"):
    """-> [(weight, mu_home, mu_away)] over the possible goalies (both teams) with the rest adjustment applied."""
    beta = (params.get("rest") or {})
    bv = [beta.get(k, 0.0) for k in REST_KEYS]
    adj_h = math.exp(sum(b * x for b, x in zip(bv, rest_x(r.get("rest_h"), r.get("rest_a")))))
    adj_a = math.exp(sum(b * x for b, x in zip(bv, rest_x(r.get("rest_a"), r.get("rest_h")))))
    if use_lg:
        return [(1.0, r["lg"], r["lg"])]
    neutral = [(1.0, 1.0, None)]
    if mode == "actual":                      # the goalie who really played (stands in for a confirmed starter)
        sh, sa_ = r.get("scn_act_h") or neutral, r.get("scn_act_a") or neutral
    elif mode == "expected" and params.get("expected_goalie", True):
        sh, sa_ = r.get("scn_h") or neutral, r.get("scn_a") or neutral
    else:
        sh, sa_ = neutral, neutral
    out = []
    for wh, fh, _ in sh:                       # home goalie -> away team's scoring
        for wa, fa, _ in sa_:                  # away goalie -> home team's scoring
            out.append((wh * wa, r["mu_h0"] * fa * adj_h, r["mu_a0"] * fh * adj_a))
    return out


def mix_dists(combos, params, so_goal=False):
    H = A = T = None
    for w, mh, ma in combos:
        h, a, t = game_dists(mh, ma, params, so_goal)
        if H is None:
            H, A, T = [w * x for x in h], [w * x for x in a], [w * x for x in t]
        else:
            H = [u + w * x for u, x in zip(H, h)]
            A = [u + w * x for u, x in zip(A, a)]
            T = [u + w * x for u, x in zip(T, t)]
    return H, A, T


def _actual_scn(st, g, side):
    gl = (g.get("gl") or {}).get(side)
    if not gl:
        return [(1.0, 1.0, None)]
    top = max(gl, key=lambda x: x["toi"])
    return [(1.0, st._game_factor(gl), top.get("name"))]


def walk_forward(seasons, params):
    """seasons = [(season_code, [game, ...]), ...] oldest first.  Predictions use only strictly earlier dates."""
    rows, prev_state = [], None
    for season, games in seasons:
        st = State(params, prev_state)
        by_date = itertools.groupby(sorted(games, key=lambda g: (g["date"], g["id"])), key=lambda g: g["date"])
        for _, day in by_date:
            day = list(day)
            for g in day:
                mu_h, mu_a, lg = st.means(g["home"], g["away"])
                rg_h, rg_a = regulation_goals(g)
                rows.append({"season": season, "date": g["date"], "home": g["home"], "away": g["away"],
                             "hg": g["hg"], "ag": g["ag"], "rg_h": rg_h, "rg_a": rg_a, "period": g.get("period"),
                             "rest_h": g.get("rest_h"), "rest_a": g.get("rest_a"),
                             "scn_h": st.scenarios(g["home"], g.get("rest_h") == 1),
                             "scn_a": st.scenarios(g["away"], g.get("rest_a") == 1),
                             "scn_act_h": _actual_scn(st, g, "home"), "scn_act_a": _actual_scn(st, g, "away"),
                             "mu_h0": mu_h, "mu_a0": mu_a, "lg": lg, "trusted": st.trusted(g["home"], g["away"])})
            for g in day:
                st.add_game(g)
        prev_state = st
    return rows


# =========================================================================================== prediction (live)
def load_params(directory):
    saved = _read_json(os.path.join(directory, PARAMS_NAME), None)
    if saved and "params" in saved:
        p = dict(DEFAULTS)
        p.update(saved["params"])
        return p, True
    return dict(DEFAULTS), False


def build_live_state(directory, season, params):
    start = int(season[:4])
    prev_season = f"{start - 1}{start}"
    prev_games, _ = load_games(directory, prev_season)
    ps = None
    if prev_games:
        ps = State(params)
        for g in prev_games:
            ps.add_game(g)
    games, upcoming = load_games(directory, season)
    st = State(params, ps)
    for g in games:
        st.add_game(g)
    return st, games, upcoming, bool(prev_games)


def predict_game(state, game, params, starters=None):
    """game = an upcoming-game record (home, away, rest_h, rest_a).  starters = {"BOS": "Swayman"} for confirmed goalies."""
    home, away = game["home"], game["away"]
    starters = starters or {}
    mu_h0, mu_a0, lg = state.means(home, away)
    scn_h = state.scenarios(home, game.get("rest_h") == 1, starters.get(home))
    scn_a = state.scenarios(away, game.get("rest_a") == 1, starters.get(away))
    row = {"mu_h0": mu_h0, "mu_a0": mu_a0, "lg": lg, "scn_h": scn_h, "scn_a": scn_a,
           "rest_h": game.get("rest_h"), "rest_a": game.get("rest_a")}
    combos = row_combos(row, params)
    hp, ap, tp = mix_dists(combos, params)
    pmf_k = dict(zip(("home", "away", "total"), mix_dists(combos, params, so_goal=True)))        # Kalshi counts the shootout goal
    mu_h = sum(k * x for k, x in enumerate(hp))
    mu_a = sum(k * x for k, x in enumerate(ap))
    def fmt(scn, confirmed):
        return [{"name": n, "p": w, "factor": f, "confirmed": bool(confirmed)} for w, f, n in scn]
    return {"home": home, "away": away, "mu_home": mu_h, "mu_away": mu_a, "mu_total": mu_h + mu_a, "league_avg": lg,
            "trusted": state.trusted(home, away), "home_games": state.games_played(home),
            "away_games": state.games_played(away), "pmf": {"home": hp, "away": ap, "total": tp}, "pmf_k": pmf_k,
            "fair_line": {"home": fair_line(hp), "away": fair_line(ap), "total": fair_line(tp)},
            "goalies": {"home": fmt(scn_h, starters.get(home)), "away": fmt(scn_a, starters.get(away))},
            "rest": {"home_b2b": game.get("rest_h") == 1, "away_b2b": game.get("rest_a") == 1,
                     "home_days": game.get("rest_h"), "away_days": game.get("rest_a")}}


def price_line(pmf, line, over_odds=None, under_odds=None):
    po, pu, pp = over_under_push(pmf, float(line))
    out = {"line": float(line), "p_over": po, "p_under": pu, "p_push": pp}
    if over_odds is not None and under_odds is not None:
        do, du = american_to_decimal(over_odds), american_to_decimal(under_odds)
        nv_o, nv_u = novig_two_way(do, du)
        cond_o = po / (po + pu) if (po + pu) > 0 else 0.5
        out.update({"over_odds": over_odds, "under_odds": under_odds,
                    "ev_over": po * (do - 1) - pu, "ev_under": pu * (du - 1) - po,
                    "market_novig_over": nv_o, "edge_over": cond_o - nv_o, "edge_under": (1 - cond_o) - nv_u,
                    "_dec": (do, du)})
    return out


def pick_bet(priced, bankroll, kelly, min_ev, max_stake_pct):
    """Best side by EV; returns bet dict or None."""
    if "ev_over" not in priced:
        return None
    side = "over" if priced["ev_over"] >= priced["ev_under"] else "under"
    ev = priced["ev_" + side]
    if ev < min_ev:
        return None
    do, du = priced["_dec"]
    dec = do if side == "over" else du
    pw, pl = (priced["p_over"], priced["p_under"]) if side == "over" else (priced["p_under"], priced["p_over"])
    f = kelly_fraction(pw, pl, dec)
    stake = round(bankroll * min(max_stake_pct, kelly * f), 2)
    if stake <= 0:
        return None
    return {"side": side, "odds": priced[side + "_odds"], "dec": dec, "ev": ev, "kelly_full": f, "stake": stake}


# =========================================================================================== log + settle + track
def load_log(directory):
    path = os.path.join(directory, LOG_NAME)
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def write_log(directory, records):
    path = os.path.join(directory, LOG_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, path)


def log_records(directory, new_records, now):
    """Dedupe on game|market|line.  A re-run before puck drop REPLACES the earlier record (updated odds / stake);
    records for games that have already started are never touched."""
    existing = {r["key"]: r for r in load_log(directory)}
    added = replaced = skipped = 0
    for r in new_records:
        start = r.get("start_utc")
        if start and now >= dt.datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None):
            skipped += 1
            continue
        if r["key"] in existing:
            replaced += 1
        else:
            added += 1
        existing[r["key"]] = r
    write_log(directory, list(existing.values()))
    return {"added": added, "replaced": replaced, "skipped_started": skipped}


def settle_outcome(goals, line):
    if float(line).is_integer() and goals == line:
        return "push"
    return "over" if goals > line else "under"


def bet_profit(bet, outcome):
    if outcome == "push":
        return 0.0
    return bet["stake"] * (bet["dec"] - 1.0) if outcome == bet["side"] else -bet["stake"]


def current_bankroll(directory, start_bankroll):
    games = _all_results(directory)
    profit = 0.0
    for r in load_log(directory):
        b = r.get("bet")
        g = games.get(str(r["game_id"]))
        goals = _goals_for(r["market"], g, r.get("src")) if g else None
        if b and goals is not None:
            profit += bet_profit(b, settle_outcome(goals, r["line"]))
    return start_bankroll + profit


def _all_results(directory):
    out = {}
    for fn in os.listdir(directory):
        if fn.startswith("nhl_goals_") and fn.endswith(".json") and fn != PARAMS_NAME and "_box_" not in fn:
            d = _read_json(os.path.join(directory, fn), {})
            games = d.get("games", {})
            box = (_read_json(os.path.join(directory, fn.replace("nhl_goals_", BOX_PREFIX, 1)), {}) or {}).get("box", {}) \
                if os.path.exists(os.path.join(directory, fn.replace("nhl_goals_", BOX_PREFIX, 1))) else {}
            for gid, g in games.items():
                per = (box.get(gid) or {}).get("per")
                if per:
                    g = dict(g, per=per)
                out[gid] = g
    return out


def _goals_for(market, g, src=None):
    """Goals the market is about, or None if the result is not available (e.g. no period data).
    src="kalshi": the shootout winner is credited one goal (Kalshi's rule); sportsbooks do not count it."""
    if market in ("total", "home", "away"):
        hg, ag = g["hg"], g["ag"]
        if src == "kalshi" and g.get("period") == "SO":
            if market == "total":
                return hg + ag + 1
            if g.get("so") not in ("home", "away"):
                return None                      # shootout winner not recorded for this old game
            if g["so"] == market:
                return (hg if market == "home" else ag) + 1
        return {"total": hg + ag, "home": hg, "away": ag}[market]
    if len(market) >= 3 and market[0] == "p" and market[1] in "123":       # p1total, p2home, p3away, t2total (through 2 periods)
        per = g.get("per")
        if not per:
            return None
        i = int(market[1]) - 1
        what = market[2:]
        return {"total": per["home"][i] + per["away"][i], "home": per["home"][i], "away": per["away"][i]}.get(what)
    return None


def track_report(directory, start_bankroll=1000.0):
    games = _all_results(directory)
    settled = []
    for r in load_log(directory):
        g = games.get(str(r["game_id"]))
        if g is None:
            continue
        goals = _goals_for(r["market"], g, r.get("src"))
        if goals is None:
            continue
        settled.append((r, goals, settle_outcome(goals, r["line"])))
    rep = {"logged": len(load_log(directory)), "settled_lines": len(settled)}
    if not settled:
        rep["note"] = "Nothing settled yet - log a slate, wait for the games to finish, `refresh`, then run track again."
        return rep
    # --- probability quality (all logged lines, odds or not)
    brier, ll, n = 0.0, 0.0, 0
    buckets = {}
    by_market_mu = {}
    for r, goals, out in settled:
        by_market_mu.setdefault(r["market"], []).append((r["mu"], goals))
        if out == "push":
            continue
        po = r["p_over"] / (r["p_over"] + r["p_under"])
        y = 1.0 if out == "over" else 0.0
        brier += (po - y) ** 2
        ll += -math.log(max(po if y else 1 - po, 1e-9))
        n += 1
        b = min(int(po * 10), 9)
        buckets.setdefault(b, []).append((po, y))
    rep["probability_quality"] = {"n_non_push": n, "brier": brier / n if n else None, "log_loss": ll / n if n else None,
                                  "calibration": [{"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(v),
                                                   "model": sum(x for x, _ in v) / len(v), "actual": sum(y for _, y in v) / len(v)}
                                                  for b, v in sorted(buckets.items())]}
    rep["mean_goals_check"] = {m: {"n": len(v), "predicted": sum(a for a, _ in v) / len(v), "actual": sum(b for _, b in v) / len(v)}
                               for m, v in by_market_mu.items()}
    # --- profit
    bets = sorted([(r, goals, out) for r, goals, out in settled if r.get("bet")], key=lambda x: (x[0]["date"], x[0]["key"]))
    if not bets:
        rep["profit"] = {"bets": 0, "note": "No bets logged with odds yet (add odds in the lines file)."}
        return rep
    cum, peak, dd, curve = 0.0, 0.0, 0.0, []
    stats = {}
    staked = 0.0
    wins = losses = pushes = 0
    for r, goals, out in bets:
        b = r["bet"]
        p = bet_profit(b, out)
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append(round(cum, 2))
        staked += b["stake"]
        wins += out == b["side"]
        losses += out not in (b["side"], "push")
        pushes += out == "push"
        s = stats.setdefault(r["market"], {"bets": 0, "staked": 0.0, "profit": 0.0})
        s["bets"] += 1
        s["staked"] += b["stake"]
        s["profit"] += p
    edge_buckets = {}
    for r, goals, out in bets:
        e = r["bet"]["ev"]
        k = "EV>=8%" if e >= 0.08 else ("EV 5-8%" if e >= 0.05 else "EV 3-5%" if e >= 0.03 else "EV<3%")
        s = edge_buckets.setdefault(k, {"bets": 0, "staked": 0.0, "profit": 0.0})
        s["bets"] += 1
        s["staked"] += r["bet"]["stake"]
        s["profit"] += bet_profit(r["bet"], out)
    for d in list(stats.values()) + list(edge_buckets.values()):
        d["roi"] = d["profit"] / d["staked"] if d["staked"] else None
    rep["profit"] = {"bets": len(bets), "wins": wins, "losses": losses, "pushes": pushes, "staked": staked,
                     "profit": cum, "roi": cum / staked if staked else None, "max_drawdown": dd,
                     "bankroll_start": start_bankroll, "bankroll_now": start_bankroll + cum,
                     "by_market": stats, "by_ev_bucket": edge_buckets, "curve_tail": curve[-10:],
                     "avg_expected_ev": sum(b[0]["bet"]["ev"] for b in bets) / len(bets)}
    return rep


def print_track(rep, out=print):
    out("=" * 72 + "\nNHL GOALS MODEL - TRACKER\n" + "=" * 72)
    out(f"lines logged: {rep['logged']}   settled: {rep['settled_lines']}")
    if "note" in rep:
        out(rep["note"])
        return
    q = rep["probability_quality"]
    out(f"\nProbability quality on settled lines (pushes excluded, n={q['n_non_push']}): "
        f"Brier {q['brier']:.4f} (0.2500 = coin flip), log loss {q['log_loss']:.4f} (0.6931 = coin flip)")
    out("  P(over) bin      n   model  actual")
    for c in q["calibration"]:
        out(f"  {c['bin']:>9} {c['n']:>6} {c['model']:>7.1%} {c['actual']:>7.1%}")
    out("\nMean goals, predicted vs actual:")
    for m, d in rep["mean_goals_check"].items():
        out(f"  {m:>6}: n={d['n']:>4}  predicted {d['predicted']:.2f}  actual {d['actual']:.2f}")
    p = rep["profit"]
    out("\nBETS")
    if not p["bets"]:
        out("  " + p["note"])
        return
    out(f"  bets {p['bets']} (W {p['wins']} / L {p['losses']} / push {p['pushes']})  staked {p['staked']:.2f}  "
        f"profit {p['profit']:+.2f}  ROI {p['roi']:+.1%}  (model expected {p['avg_expected_ev']:+.1%})")
    out(f"  bankroll {p['bankroll_start']:.2f} -> {p['bankroll_now']:.2f}   max drawdown {p['max_drawdown']:.2f}")
    for name, d in p["by_market"].items():
        out(f"  {name:>6}: {d['bets']:>3} bets  profit {d['profit']:+8.2f}  ROI {d['roi']:+.1%}")
    for name, d in sorted(p["by_ev_bucket"].items()):
        out(f"  {name:>8}: {d['bets']:>3} bets  profit {d['profit']:+8.2f}  ROI {d['roi']:+.1%}")
    if p["bets"] < 100:
        out(f"\n  Only {p['bets']} bets so far - ROI swings by +/-10% or more on samples this small; judge after 100+.")


# =========================================================================================== slate
def _line_entry(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return {"line": float(value)}
    if isinstance(value, dict) and "line" in value:
        return value
    return None


MAX_AUTO_AGE_HOURS = 8.0       # automatically fetched prices (Kalshi) older than this are ignored


def _line_entries(value, date=None, now=None):
    """One posted line, or a LADDER of lines (list), -> list of entries [{"line","over","under"}...].
    Automatically fetched entries carry "date" (game date) and "fetched_at"; ones for another date, or stale, are dropped."""
    vals = value if isinstance(value, list) else [value]
    out = []
    for v in vals:
        e = _line_entry(v)
        if e is None:
            continue
        if date is not None and e.get("date") and e["date"] != date:
            continue
        if now is not None and e.get("fetched_at"):
            try:
                age = now - dt.datetime.fromisoformat(str(e["fetched_at"]).rstrip("Z"))
                if age > dt.timedelta(hours=MAX_AUTO_AGE_HOURS):
                    continue
            except ValueError:
                pass
        out.append(e)
    return out


def _is_auto(value):
    vals = value if isinstance(value, list) else [value]
    return bool(vals) and all(isinstance(v, dict) and v.get("src") for v in vals)


def merge_lines(*dicts):
    """Combine lines dicts.  Same key in several -> ONE ladder holding every entry (typed sportsbook lines and fetched
    Kalshi lines are priced side by side; at most one bet per market is suggested)."""
    out = {}
    for d in dicts:
        for k, v in (d or {}).items():
            ents = _line_entries(v)
            if ents:
                out.setdefault(k, []).extend(ents)
    return {k: (v[0] if len(v) == 1 and not v[0].get("src") and set(v[0]) <= {"line", "over", "under"} else v) for k, v in out.items()}


def load_lines(directory, names):
    """Merge one or more lines files (comma-separated names).  Missing files are skipped."""
    return merge_lines(*[_read_json(os.path.join(directory, nm), {}) or {}
                         for nm in [x.strip() for x in str(names).split(",") if x.strip()]
                         if os.path.exists(os.path.join(directory, nm))])


def price_ladder(pmf, ents, bankroll, kelly, min_ev, max_stake_pct, trusted, pmf_auto=None):
    """Prices every rung of a ladder.  At most ONE bet per market (the best-EV rung): rungs of a ladder are strongly
    correlated, so staking several would over-bet.  -> (priced_rungs, best_index)"""
    rungs = []
    for ent in ents:
        pm_ = pmf_auto if (pmf_auto is not None and ent.get("src") == "kalshi") else pmf
        pr = price_line(pm_, ent["line"], ent.get("over"), ent.get("under"))
        bet = pick_bet(pr, bankroll, kelly, min_ev, max_stake_pct) if trusted else None
        rungs.append({"ent": ent, "pr": pr, "bet": bet})
    best = None
    for i, r in enumerate(rungs):
        if r["bet"] and (best is None or r["bet"]["ev"] > rungs[best]["bet"]["ev"]):
            best = i
    for i, r in enumerate(rungs):
        if i != best:
            r["bet"] = None
    if best is None and rungs:
        best = min(range(len(rungs)), key=lambda i: abs(rungs[i]["pr"]["p_over"] - 0.5))      # show the rung nearest 50/50
    return rungs, best


def _goalie_tag(scn):
    if len(scn) == 1 and scn[0]["confirmed"]:
        return f"{scn[0]['name']} (confirmed)"
    if len(scn) == 1 and scn[0]["name"] is None:
        return "unknown"
    return " / ".join(f"{x['name'] or '?'} {x['p']:.0%}" for x in scn)


def slate(directory, date, lines=None, bankroll=1000.0, kelly=0.25, min_ev=0.03, max_stake_pct=0.03,
          now=None, log=True, season=None, starters=None, log_model_lines=False, live=None):
    now = now or _utcnow()
    season = season or season_code_for(date)
    params, validated = load_params(directory)
    st, games, upcoming, have_prev = live or build_live_state(directory, season, params)
    warnings = []
    if not validated:
        warnings.append("No nhl_goals_params.json: using UNVALIDATED default parameters. Run `backtest` first.")
    if not games and not have_prev:
        warnings.append("No data for this season yet: predictions are league-average. Run `refresh` (and the previous season for carry-over).")
    today = [u for u in upcoming if u["date"] == date]
    lines = lines or {}
    used_keys, records, out_games = set(), [], []
    for u in sorted(today, key=lambda x: x.get("start_utc") or ""):
        pred = predict_game(st, u, params, starters)
        entry = {"game_id": u["id"], "date": date, "home": u["home"], "away": u["away"], "start_utc": u.get("start_utc"),
                 "mu_home": pred["mu_home"], "mu_away": pred["mu_away"], "mu_total": pred["mu_total"],
                 "trusted": pred["trusted"], "fair_line": pred["fair_line"], "goalies": pred["goalies"],
                 "rest": pred["rest"], "markets": {}}
        for market in ("total", "home", "away"):
            pmf = pred["pmf"][market]
            mu = {"total": pred["mu_total"], "home": pred["mu_home"], "away": pred["mu_away"]}[market]
            grid = [4.5, 5.5, 6.5] if market == "total" else [1.5, 2.5, 3.5]
            mk = {"mu": mu, "fair_line": pred["fair_line"][market],
                  "grid": {str(l): over_under_push(pmf, l)[0] for l in grid},
                  "pmf": [round(x, 5) for x in pmf[:12 if market != "total" else 16]], "priced": None, "bet": None}
            key = f"{u['away']}@{u['home']}|{market}"
            ents = _line_entries(lines.get(key), date, now)
            if ents:
                used_keys.add(key)
                rungs, best = price_ladder(pmf, ents, bankroll, kelly, min_ev, max_stake_pct, pred["trusted"], pred["pmf_k"][market])
                mk["priced"] = {k: v for k, v in rungs[best]["pr"].items() if k != "_dec"}
                mk["bet"] = rungs[best]["bet"]
                if len(rungs) > 1:
                    mk["ladder"] = [dict({k: v for k, v in r["pr"].items() if k != "_dec"}, bet=r["bet"]) for r in rungs]
                for r in rungs:
                    pr, ent, bet = r["pr"], r["ent"], r["bet"]
                    records.append({"key": f"{u['id']}|{market}|{pr['line']}", "ts": now.isoformat(), "date": date,
                                    "game_id": u["id"], "home": u["home"], "away": u["away"], "start_utc": u.get("start_utc"),
                                    "market": market, "line": pr["line"], "mu": mu, "p_over": pr["p_over"],
                                    "p_under": pr["p_under"], "p_push": pr["p_push"], "over_odds": ent.get("over"),
                                    "under_odds": ent.get("under"), "trusted": pred["trusted"], "bet": bet, "src": ent.get("src"),
                                    "goalie_home": _goalie_tag(pred["goalies"]["home"]),
                                    "goalie_away": _goalie_tag(pred["goalies"]["away"]),
                                    "b2b_home": pred["rest"]["home_b2b"], "b2b_away": pred["rest"]["away_b2b"]})
            elif log_model_lines:
                # no posted line typed in: still record the model's own fair line so calibration keeps accumulating
                fl = pred["fair_line"][market]
                pr = price_line(pmf, fl)
                records.append({"key": f"{u['id']}|{market}|{fl}", "ts": now.isoformat(), "date": date,
                                "game_id": u["id"], "home": u["home"], "away": u["away"], "start_utc": u.get("start_utc"),
                                "market": market, "line": fl, "mu": mu, "p_over": pr["p_over"], "p_under": pr["p_under"],
                                "p_push": pr["p_push"], "over_odds": None, "under_odds": None, "trusted": pred["trusted"],
                                "bet": None, "source": "model",
                                "goalie_home": _goalie_tag(pred["goalies"]["home"]),
                                "goalie_away": _goalie_tag(pred["goalies"]["away"]),
                                "b2b_home": pred["rest"]["home_b2b"], "b2b_away": pred["rest"]["away_b2b"]})
            entry["markets"][market] = mk
        out_games.append(entry)
    unmatched = [k for k in lines if k not in used_keys and not _is_auto(lines[k])]
    if unmatched:
        warnings.append(f"Lines not matched to a game on {date}: {', '.join(unmatched)} (key format AWAY@HOME|total|home|away)")
    if not any(g["goalies"]["home"][0]["name"] or g["goalies"]["away"][0]["name"] for g in out_games) and out_games:
        warnings.append("No goalie history yet (run `boxscores`): goalie and back-to-back goalie effects are off.")
    untrusted = [f"{g['away']}@{g['home']}" for g in out_games if not g["trusted"]]
    if untrusted:
        warnings.append(f"Fewer than {params['min_games']} games of data for a team, no bets suggested: {', '.join(untrusted)}")
    if not today:
        warnings.append(f"No upcoming games found on {date}. Run `refresh`, or the date is off-season.")
    summary = {"date": date, "season": season, "params": params, "validated": validated, "bankroll": bankroll,
               "games": out_games, "warnings": warnings}
    if log and records:
        summary["log"] = log_records(directory, records, now)
    return summary


def print_slate(s, out=print):
    out(f"NHL goals slate {s['date']}  (params {'validated' if s['validated'] else 'UNVALIDATED defaults'}, bankroll {s['bankroll']:.2f})")
    out("-" * 100)
    out(f"{'game':<11}{'home':>6}{'away':>6}{'total':>7}{'fair':>6}  {'P(o5.5)':>8}{'P(o6.5)':>8}  {'H o2.5':>7}{'A o2.5':>7}  rest")
    for g in s["games"]:
        t, h, a = g["markets"]["total"], g["markets"]["home"], g["markets"]["away"]
        flag = "" if g["trusted"] else " (thin data)"
        rest = ("B2B:" + ",".join(x for x, f in ((g["home"], g["rest"]["home_b2b"]), (g["away"], g["rest"]["away_b2b"])) if f)
                if g["rest"]["home_b2b"] or g["rest"]["away_b2b"] else "")
        out(f"{g['away'] + '@' + g['home']:<11}{g['mu_home']:>6.2f}{g['mu_away']:>6.2f}{g['mu_total']:>7.2f}{t['fair_line']:>6.1f}  "
            f"{t['grid']['5.5']:>8.1%}{t['grid']['6.5']:>8.1%}  {h['grid']['2.5']:>7.1%}{a['grid']['2.5']:>7.1%}  {rest}{flag}")
        out(f"{'':<11}goalies  {g['home']}: {_goalie_tag(g['goalies']['home'])}   |   {g['away']}: {_goalie_tag(g['goalies']['away'])}")
    priced = [(g, m, mk) for g in s["games"] for m, mk in g["markets"].items() if mk["priced"]]
    if priced:
        out("\nPRICED LINES")
        out(f"{'game':<11}{'mkt':<6}{'line':>5}{'P(over)':>9}{'P(under)':>9}{'P(push)':>8}   bet")
        for g, m, mk in priced:
            for p in (mk.get("ladder") or [dict(mk["priced"], bet=mk["bet"])]):
                b = p["bet"]
                tail = (f"{b['side'].upper()} {b['odds']:+d}  EV {b['ev']:+.1%}  stake {b['stake']:.2f}  (full Kelly {b['kelly_full']:.1%})"
                        if b else ("no bet" + (f"  (best EV {max(p['ev_over'], p['ev_under']):+.1%})" if "ev_over" in p else "")))
                out(f"{g['away'] + '@' + g['home']:<11}{m:<6}{p['line']:>5.1f}{p['p_over']:>9.1%}{p['p_under']:>9.1%}{p['p_push']:>8.1%}   {tail}")
        total_stake = sum(mk["bet"]["stake"] for _, _, mk in priced if mk["bet"])
        out(f"\nTotal suggested stake today: {total_stake:.2f}")
    for w in s["warnings"]:
        out("WARNING: " + w)
    if "log" in s:
        out(f"Logged: {s['log']}")


# =========================================================================================== backtest
def _pois_ll(mu, k):
    mu = max(mu, 0.05)
    return k * math.log(mu) - mu - math.lgamma(k + 1)


def _reg_nll_mix(rows, params, shape=None, mode="expected"):
    """Regulation team-goals NLL per team-game, averaging over the possible goalies (the way the live model does)."""
    tot, n = 0.0, 0
    for r in rows:
        if not r["trusted"]:
            continue
        combos = row_combos(r, params, mode=mode)
        for idx, key in ((1, "rg_h"), (2, "rg_a")):
            y = r[key]
            if shape is None:
                like = sum(c[0] * math.exp(_pois_ll(c[idx], y)) for c in combos)
            else:
                like = sum(c[0] * goal_pmf(c[idx], shape)[min(y, MAX_TEAM_GOALS)] for c in combos)
            tot -= math.log(max(like, 1e-12))
            n += 1
    return tot / max(n, 1)


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[piv] = M[piv], M[i]
        for r in range(n):
            if r != i:
                f = M[r][i] / M[i][i]
                for c in range(i, n + 1):
                    M[r][c] -= f * M[i][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_rest(rows, params, ridge=5.0):
    """Poisson regression of regulation goals on rest flags with the model's own mean as offset.  -> (coefs, std errors, n)."""
    base = dict(params, rest={})
    obs = []
    for r in rows:
        if not r["trusted"]:
            continue
        combos = row_combos(r, base, mode="actual")
        mh = sum(c[0] * c[1] for c in combos)
        ma = sum(c[0] * c[2] for c in combos)
        obs.append((rest_x(r["rest_h"], r["rest_a"]), r["rg_h"], math.log(mh)))
        obs.append((rest_x(r["rest_a"], r["rest_h"]), r["rg_a"], math.log(ma)))
    beta, se = poisson_glm(obs, ridge)
    return dict(zip(REST_KEYS, beta)), dict(zip(REST_KEYS, se)), len(obs)


def poisson_glm(obs, ridge=5.0):
    """obs = [(feature list, y, log-mean offset)].  Ridge-penalised Poisson regression.  -> (coefs, std errors)."""
    p = len(obs[0][0])
    beta = [0.0] * p
    H = None
    for _ in range(30):
        grad = [-ridge * b for b in beta]
        H = [[ridge if i == j else 0.0 for j in range(p)] for i in range(p)]
        for x, y, off in obs:
            mu = math.exp(off + sum(b * v for b, v in zip(beta, x)))
            for i in range(p):
                if x[i]:
                    grad[i] += x[i] * (y - mu)
                    for j in range(p):
                        if x[j]:
                            H[i][j] += x[i] * x[j] * mu
        step = _solve(H, grad)
        beta = [b + s_ for b, s_ in zip(beta, step)]
        if max(abs(v) for v in step) < 1e-9:
            break
    se = []
    for i in range(p):
        e = [1.0 if j == i else 0.0 for j in range(p)]
        se.append(math.sqrt(max(_solve(H, e)[i], 0.0)))
    return beta, se


def _final_nlls(rows, params, use_lg=False, mode="expected"):
    """-> (team-goals NLL per team-game, total NLL per game, [dists]) on trusted rows, FINAL goals."""
    tn, gn, n, dists = 0.0, 0.0, 0, []
    for r in rows:
        if not r["trusted"]:
            continue
        H, A, T = mix_dists(row_combos(r, params, use_lg, mode), params)
        dists.append((r, H, A, T))
        tn += pmf_nll(H, r["hg"]) + pmf_nll(A, r["ag"])
        gn += pmf_nll(T, r["hg"] + r["ag"])
        n += 1
    return tn / max(2 * n, 1), gn / max(n, 1), dists


def _split(rows, seasons):
    if len(seasons) >= 2:
        last = seasons[-1][0]
        return [r for r in rows if r["season"] != last], [r for r in rows if r["season"] == last], f"validate = {last}"
    dates = sorted({r["date"] for r in rows})
    cut = dates[int(len(dates) * 0.6)]
    return [r for r in rows if r["date"] < cut], [r for r in rows if r["date"] >= cut], f"validate = games on/after {cut}"


SHAPES = [None, 200.0, 100.0, 60.0, 40.0, 20.0, 10.0, -60.0, -40.0, -28.0, -20.0, -14.0]


def backtest(directory, season_codes, out=print, write_params=True):
    seasons = []
    for s in season_codes:
        games, _ = load_games(directory, s)
        if games:
            seasons.append((s, games))
        else:
            out(f"(no data for {s}: run `refresh --season {s}` first)")
    if not seasons:
        out("Nothing to backtest.")
        return None
    n_games = sum(len(g) for _, g in seasons)
    has_goalies = sum(1 for _, gs in seasons for g in gs if g.get("gl")) > 0.5 * n_games
    out(f"{n_games} games from {len(seasons)} season(s); walk-forward, no future information used.")
    out("Goalie data: " + ("found" if has_goalies else "MISSING - run `boxscores --season S` for each season; goalie effect skipped") + "\n")
    k_grid, carry_grid, hfa_grid = [8, 15, 25, 40, 70], ([0.0, 0.2, 0.4, 0.6] if len(seasons) > 1 else [0.0]), [0.0, 0.02, 0.04, 0.06]
    best = None
    for k, carry, hfa in itertools.product(k_grid, carry_grid, hfa_grid):
        p = dict(DEFAULTS, k=float(k), carry=carry, hfa=hfa, r_team=None, gamma_g=0.0, rest={})
        rows = walk_forward(seasons, p)
        tune, _, _ = _split(rows, seasons)
        score = _reg_nll_mix(tune, p)
        if best is None or score < best[0]:
            best = (score, p)
    p_team = best[1]
    # ---- stage 2: goalie quality
    p = dict(p_team)
    rows = walk_forward(seasons, p)
    tune, val, desc = _split(rows, seasons)
    out(f"Tuned on {len(tune)} games, {desc} ({len(val)} games).")
    g_grid = [(g, n0) for g in (0.5, 1.0, 1.5) for n0 in (300.0, 800.0, 2000.0)]
    p["expected_goalie"] = True
    if has_goalies:
        # judged with the goalie who really played = what you get once the starter is confirmed
        g_best = (_reg_nll_mix(tune, dict(p_team, gamma_g=0.0), mode="actual"), 0.0, p["n0_g"])
        for gam, n0 in g_grid:
            pg = dict(p_team, gamma_g=gam, n0_g=n0)
            r_ = walk_forward(seasons, pg)
            t_, _, _ = _split(r_, seasons)
            sc = _reg_nll_mix(t_, pg, mode="actual")
            if sc < g_best[0] - 1e-4:
                g_best = (sc, gam, n0)
        p["gamma_g"], p["n0_g"] = g_best[1], g_best[2]
        rows = walk_forward(seasons, p)
        tune, val, desc = _split(rows, seasons)
        # with the starter NOT known, is the expected-goalie mixture better than ignoring the goalie?
        if p["gamma_g"] > 0:
            p["expected_goalie"] = _reg_nll_mix(tune, p, mode="expected") < _reg_nll_mix(tune, dict(p, expected_goalie=False), mode="expected") - 1e-4
    else:
        p["gamma_g"] = 0.0
    # ---- stage 3: rest / back-to-back
    coefs, ses, n_obs = fit_rest(tune, p)
    p["rest"] = coefs
    # ---- stage 4: count shape and overtime process
    shape_best = (None, _reg_nll_mix(tune, p, None))
    for sh in SHAPES[1:]:
        sc = _reg_nll_mix(tune, p, sh)
        if sc < shape_best[1] - 1e-5:
            shape_best = (sh, sc)
    p["r_team"] = shape_best[0]
    ties = [r for r in tune if r["rg_h"] == r["rg_a"]]
    ot = [r for r in ties if r.get("period") == "OT"]
    if len(ties) >= 30:
        p["q_ot"] = len(ot) / len(ties)
        if len(ot) >= 15:
            p["w_home"] = sum(1 for r in ot if r["hg"] > r["ag"]) / len(ot)
    shape_txt = ("Poisson" if p["r_team"] is None else f"Negative Binomial r={p['r_team']:g} (wider)" if p["r_team"] > 0
                 else f"Binomial n={-p['r_team']:g} (narrower than Poisson)")
    out(f"Chosen: shrinkage k={p['k']:g}, carry-over={p['carry']:g}, home-ice factor={p['hfa']:g}, regulation goal shape = {shape_txt}")
    if has_goalies:
        out(f"Goalie effect: strength gamma={p['gamma_g']:g} (0 = no goalie effect), save% prior weight {p['n0_g']:g} shots; "
            f"when the starter is not known the model {'uses the expected-goalie mixture' if p['expected_goalie'] else 'IGNORES the goalie (the mixture did not help)'}")
    out("Rest effects on a team's regulation goals (multiplier = exp(coef)):")
    labels = {"own_b2b": "team on a back-to-back", "opp_b2b": "opponent on a back-to-back",
              "own_long": "team off 4+ days", "opp_long": "opponent off 4+ days"}
    for key in REST_KEYS:
        z = coefs[key] / ses[key] if ses[key] else 0.0
        out(f"   {labels[key]:<28} x{math.exp(coefs[key]):.3f}   (coef {coefs[key]:+.3f}, s.e. {ses[key]:.3f}, z {z:+.1f})")
    b2b_n = sum(1 for r in tune if r["trusted"] and r.get("rest_h") == 1) + sum(1 for r in tune if r["trusted"] and r.get("rest_a") == 1)
    out(f"   ({b2b_n} back-to-back team-games in the tuning set; coefficients are shrunk toward 0 with a ridge penalty)")
    out(f"Overtime: {len(ties)} of {len(tune)} tuning games were tied after 60 min ({len(ties) / max(len(tune), 1):.1%}); "
        f"{p['q_ot']:.1%} of those ended in OT (rest shootout); home team won {p['w_home']:.1%} of OT endings.")
    for name, val_, grid in (("k", p["k"], k_grid), ("carry", p["carry"], carry_grid), ("hfa", p["hfa"], hfa_grid)):
        if len(grid) > 1 and val_ in (grid[0], grid[-1]):
            out(f"  WARNING: {name}={val_:g} sits on the edge of its grid ({grid[0]:g}..{grid[-1]:g}); the true optimum may be outside it.")
    if has_goalies and p["gamma_g"] == 1.5:
        out("  WARNING: goalie gamma sits at the top of its grid (1.5).")
    # ---- validation, with ablations
    out("\n" + "=" * 72 + "\nVALIDATION (games the tuning never saw)\n" + "=" * 72)
    p_off = dict(p, gamma_g=0.0, rest={})
    rows_off = walk_forward(seasons, p_off)
    _, v_off, _ = _split(rows_off, seasons)
    p_g = dict(p, rest={})
    def nl(rr, pp, mode):
        return _reg_nll_mix(rr, pp, mode=mode)
    abl = [("team strength only", nl(v_off, p_off, "expected")),
           ("+ rest / back-to-back", nl(v_off, dict(p_off, rest=p["rest"]), "expected")),
           ("+ goalies, starter unknown", nl(val, p_g, "expected")),
           ("+ goalies, starter confirmed", nl(val, p_g, "actual")),
           ("+ goalies + rest, unknown", nl(val, p, "expected")),
           ("+ goalies + rest, confirmed", nl(val, p, "actual"))]
    out("Regulation team-goals log loss per team-game (lower is better):")
    for label, v_ in abl:
        out(f"   {label:<32}{v_:.4f}   vs team-only {abl[0][1] - v_:+.4f}")
    tn, gn, vd = _final_nlls(val, p)
    nn, ng, _ = _final_nlls(val, dict(p, r_team=None, rest={}), use_lg=True)
    vt = [r for r, *_ in vd]
    out(f"\n{len(vt)} validate games with both teams trusted (>= {p['min_games']} games played)")
    out(f"Final team-goals log loss per team-game: model {tn:.4f}   league-average baseline {nn:.4f}   gain {nn - tn:+.4f}")
    out(f"Final game-total log loss per game:      model {gn:.4f}   league-average baseline {ng:.4f}   gain {ng - gn:+.4f}")
    if vt:
        mh = sum(r["hg"] - sum(k * x for k, x in enumerate(H)) for r, H, A, T in vd) / len(vd)
        ma = sum(r["ag"] - sum(k * x for k, x in enumerate(A)) for r, H, A, T in vd) / len(vd)
        out(f"Mean error (actual - predicted): home {mh:+.3f}, away {ma:+.3f}, total {mh + ma:+.3f} goals")
    out("\nCalibration of P(over) on validate games (model vs what happened)")
    out(f"{'market':<14}{'line':>5}{'n':>6}{'model':>8}{'actual':>8}{'z':>6}   {'model push':>10}{'actual push':>12}")
    for market, lines_ in (("game total", [4.5, 5.5, 6.5, 7.5]), ("team goals", [1.5, 2.5, 3.5]), ("game total", [5.0, 6.0, 7.0])):
        for line in lines_:
            ps, ys, pushes_m, pushes_a = [], [], 0.0, 0
            for r, H, A, T in vd:
                if market == "game total":
                    po, pu, pp_ = over_under_push(T, line)
                    goals = r["hg"] + r["ag"]
                    ps.append(po / (po + pu) if (po + pu) else 0.5)
                    ys.append(1.0 if goals > line else 0.0 if goals < line else None)
                    pushes_m += pp_
                    pushes_a += goals == line
                else:
                    for pm, goals in ((H, r["hg"]), (A, r["ag"])):
                        ps.append(over_under_push(pm, line)[0])
                        ys.append(1.0 if goals > line else 0.0)
            pairs = [(a_, b_) for a_, b_ in zip(ps, ys) if b_ is not None]
            if not pairs:
                continue
            n = len(pairs)
            m_, a_ = sum(x for x, _ in pairs) / n, sum(y for _, y in pairs) / n
            se = math.sqrt(max(m_ * (1 - m_), 1e-9) / n)
            push_txt = f"{pushes_m / len(vt):>10.1%}{pushes_a / len(vt):>12.1%}" if market == "game total" and float(line).is_integer() else ""
            out(f"{market:<14}{line:>5.1f}{n:>6}{m_:>8.1%}{a_:>8.1%}{(a_ - m_) / se:>6.1f}   {push_txt}")
    out("\n|z| beyond about 2 means the probability is off by more than chance. Integer lines use push-excluded over rates.")
    out("No sportsbook prices are involved here: this checks the MODEL against results. Market edge can only be measured\n"
        "by logging real posted lines with `slate` and reading the `track` report.")
    diagnostics(rows, vd, p, out)
    if write_params:
        _atomic_json(os.path.join(directory, PARAMS_NAME),
                     {"params": p, "validated_on": desc, "seasons": [s for s, _ in seasons],
                      "validate_log_loss_gain_team": nn - tn, "validate_log_loss_gain_total": ng - gn,
                      "rest_se": ses, "written": _utcnow().isoformat() + "Z"})
        out(f"\nSaved {PARAMS_NAME} (the slate command reads it automatically).")
    return p


def lineup_check(directory, season_codes, out=print, m_shrink=20.0, window=10):
    """CEILING test for a skater-lineup effect.  For every game it measures how strong the lineup that really dressed was,
    relative to that team's own recent lineups (player strength = shrunk points per game from earlier games only), and asks
    whether goals follow.  Because it uses the lineup that actually played, it is the best case for a lineup model: if the
    gain here is ~0, confirmed lineups would not help either."""
    seasons = []
    for sc in season_codes:
        g, _ = load_games(directory, sc)
        if g and sum(1 for x in g if x.get("sk", {}).get("home")) > 0.5 * len(g):
            seasons.append((sc, g))
    if not seasons:
        out("No skater data found: run `boxscores --seasons ...` first.")
        return None
    params, validated = load_params(directory)
    rows = walk_forward(seasons, params)
    feats, pl = [], {}                  # pl: player id -> [games, points]
    hist = {}                           # team -> recent [(F strength, D strength)]
    pool = {"F": [0, 0.0], "D": [0, 0.0]}
    def val(pid, grp):
        gp, pts = pl.get(pid, (0, 0.0))
        lg = (pool[grp][1] + 10) / max(pool[grp][0], 1) if pool[grp][0] else (0.55 if grp == "F" else 0.25)
        return (pts + m_shrink * lg) / (gp + m_shrink)
    for season, games in seasons:
        for _, day in itertools.groupby(sorted(games, key=lambda g: (g["date"], g["id"])), key=lambda g: g["date"]):
            day = list(day)
            todo = []
            for g in day:
                f = {}
                for side, team in (("home", g["home"]), ("away", g["away"])):
                    sk = g["sk"][side]
                    fs = sum(val(p["id"], "F") for p in sk if p["pos"] != "D")
                    ds = sum(val(p["id"], "D") for p in sk if p["pos"] == "D")
                    h = hist.get(team, [])
                    if len(h) >= 5:
                        bf, bd = sum(x[0] for x in h) / len(h), sum(x[1] for x in h) / len(h)
                        f[side] = (math.log(fs / bf), math.log(ds / bd))
                    else:
                        f[side] = None
                    todo.append((team, fs, ds))
                feats.append(f)
                for side in ("home", "away"):
                    for p in g["sk"][side]:
                        grp = "D" if p["pos"] == "D" else "F"
                        gg = pl.setdefault(p["id"], [0, 0.0])
                        gg[0] += 1
                        gg[1] += p["g"] + p["a"]
                        pool[grp][0] += 1
                        pool[grp][1] += p["g"] + p["a"]
            for team, fs, ds in todo:
                hist.setdefault(team, []).append((fs, ds))
                hist[team] = hist[team][-window:]
    tune, val_rows, desc = _split(rows, seasons)
    ids = {id(r): i for i, r in enumerate(rows)}
    def build(rs):
        obs = []
        for r in rs:
            f = feats[ids[id(r)]]
            if not r["trusted"] or f["home"] is None or f["away"] is None:
                continue
            combos = row_combos(r, params, mode="actual")
            mh, ma = sum(c[0] * c[1] for c in combos), sum(c[0] * c[2] for c in combos)
            # own forward strength -> own goals;  opposing defence strength -> own goals
            obs.append(([f["home"][0], f["away"][1]], r["rg_h"], math.log(mh)))
            obs.append(([f["away"][0], f["home"][1]], r["rg_a"], math.log(ma)))
        return obs
    ot, ov = build(tune), build(val_rows)
    beta, se = poisson_glm(ot, ridge=20.0)
    out(f"Lineup ceiling test ({desc}); {len(ot)} tuning team-games, {len(ov)} validation team-games")
    names = ["own forwards' strength vs usual", "opposing defence strength vs usual"]
    for n_, b_, s_ in zip(names, beta, se):
        out(f"   {n_:<38} coef {b_:+.3f}  (s.e. {s_:.3f}, z {b_ / s_ if s_ else 0:+.1f})   [+1.0 = goals move one-for-one with lineup strength]")
    def nll(obs, b):
        t = 0.0
        for x, y, off in obs:
            t -= _pois_ll(math.exp(off + sum(bb * xx for bb, xx in zip(b, x))), y)
        return t / len(obs)
    base_v, with_v = nll(ov, [0.0, 0.0]), nll(ov, beta)
    out(f"   validation log loss per team-game: without lineup {base_v:.4f}   with lineup {with_v:.4f}   gain {base_v - with_v:+.4f}")
    sd_f = (sum(x[0][0] ** 2 for x in ov) / len(ov)) ** 0.5
    out(f"   typical lineup-strength swing (std of log ratio): forwards {sd_f:.3f}  ->  about {abs(beta[0]) * sd_f * 100:.1f}% of a team's goals")
    out("   Because this uses the lineup that actually dressed, it is the BEST case. A gain near 0 means skater lineups are not worth modelling.")
    return {"beta": beta, "se": se, "gain": base_v - with_v}



def diagnostics(rows, vd, p, out=print):
    """Where is the model off?  Level by season / part of season, odd-vs-even totals, spread."""
    out("\n" + "=" * 72 + "\nDIAGNOSTICS: level and shape\n" + "=" * 72)
    out("By season (trusted games): level of the average total, and how often the total is an ODD number")
    out(f"{'season':<10}{'n':>6}{'actual avg':>12}{'model avg':>11}{'error':>8}{'odd: actual':>13}{'model':>8}")
    for s in sorted({r["season"] for r in rows}):
        rs = [r for r in rows if r["season"] == s and r["trusted"]]
        if not rs:
            continue
        a = sum(r["hg"] + r["ag"] for r in rs) / len(rs)
        odd_a = sum((r["hg"] + r["ag"]) % 2 for r in rs) / len(rs)
        m_, odd_m = 0.0, 0.0
        for r in rs:
            T = mix_dists(row_combos(r, p), p)[2]
            m_ += sum(k * x for k, x in enumerate(T))
            odd_m += sum(x for k, x in enumerate(T) if k % 2)
        m_ /= len(rs)
        odd_m /= len(rs)
        out(f"{s:<10}{len(rs):>6}{a:>12.3f}{m_:>11.3f}{a - m_:>+8.3f}{odd_a:>13.1%}{odd_m:>8.1%}")
    vt = sorted([x for x in vd], key=lambda x: x[0]["date"])
    if len(vt) >= 40:
        out("\nValidation season in fifths (an error that fades = early-season bias; one that persists = the season's scoring level):")
        out(f"{'part':<8}{'n':>6}{'actual':>9}{'model':>8}{'error':>8}")
        step = len(vt) / 5.0
        for i in range(5):
            rs = vt[int(i * step):int((i + 1) * step)]
            a = sum(r["hg"] + r["ag"] for r, *_ in rs) / len(rs)
            m_ = sum(sum(k * x for k, x in enumerate(T)) for _, _, _, T in rs) / len(rs)
            out(f"{i + 1}/5{'':<4}{len(rs):>6}{a:>9.3f}{m_:>8.3f}{a - m_:>+8.3f}")
    if vt:
        n = len(vt)
        out("\nShape: how often the final total lands on each value (validation games)")
        out(f"{'goals':>6}{'actual':>9}{'model':>8}{'z':>6}")
        for g in range(1, 12):
            act = sum(1 for r, *_ in vt if r["hg"] + r["ag"] == g) / n
            mod = sum(T[g] for _, _, _, T in vt) / n
            se = math.sqrt(max(mod * (1 - mod), 1e-9) / n)
            out(f"{g:>6}{act:>9.1%}{mod:>8.1%}{(act - mod) / se:>6.1f}")
        vr = sum((r["hg"] + r["ag"] - sum(k * x for k, x in enumerate(T))) ** 2 for r, _, _, T in vt) / n
        mv = sum(sum((k - sum(j * x for j, x in enumerate(T))) ** 2 * x for k, x in enumerate(T)) for _, _, _, T in vt) / n
        out(f"\nSpread: actual mean squared error of the total {vr:.2f} vs model-implied variance {mv:.2f}  (ratio {vr / mv:.3f}; "
            f"1.0 = right, >1 = reality wider than the model, <1 = narrower)")


def daily_update(directory, date, season, lines=None, starters=None, fetch=fetch_json, log=print, now=None):
    """One call for the scheduler: pull the schedule + results, download new boxscores (this fills in the whole season so far
    the first time and only new games afterwards), then log today's model predictions."""
    out = {"date": date, "season": season}
    out["refresh"] = refresh_season(directory, season, fetch=fetch, log=log)
    out["boxscores"] = collect_boxscores(directory, season, fetch=fetch, log=log)
    prev = f"{int(season[:4]) - 1}{int(season[:4])}"
    if os.path.exists(data_file(directory, prev)):
        out["boxscores_prev"] = collect_boxscores(directory, prev, fetch=fetch, log=log, limit=2000)
    s = slate(directory, date, lines or {}, current_bankroll(directory, 1000.0), starters=starters, log=True, season=season,
              log_model_lines=True, now=now)
    out["games_today"] = len(s["games"])
    out["logged"] = s.get("log")
    out["warnings"] = s["warnings"]
    return out


# =========================================================================================== CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="NHL goals model: team goals, game totals, Kelly staking, profit tracking")
    ap.add_argument("command", choices=["probe", "probe-pregame", "refresh", "boxscores", "update", "backtest", "lineups-check", "slate", "track"])
    ap.add_argument("--dir", default=".")
    ap.add_argument("--season", help="8-digit NHL season code, e.g. 20252026 (default: from today's date)")
    ap.add_argument("--seasons", help="comma list for backtest, oldest first")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, US Eastern-ish)")
    ap.add_argument("--lines-file", default="nhl_lines.json,nhl_lines_kalshi.json", help="comma-separated; later files win per key")
    ap.add_argument("--starters-file", default="nhl_starters.json", help='{"BOS": "Swayman", "TOR": "Stolarz"} confirmed goalies')
    ap.add_argument("--fill-periods", action="store_true", help="boxscores: also re-download games saved before period data existed")
    ap.add_argument("--limit", type=int, default=None, help="boxscores: stop after this many downloads")
    ap.add_argument("--bankroll", type=float, default=1000.0, help="STARTING bankroll; realised profit from settled bets is added automatically")
    ap.add_argument("--kelly", type=float, default=0.25)
    ap.add_argument("--min-ev", type=float, default=0.03)
    ap.add_argument("--max-stake-pct", type=float, default=0.03)
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = a.dir
    today = (_utcnow() - dt.timedelta(hours=7)).date().isoformat()
    date = a.date or today
    season = a.season or season_code_for(date)

    if a.command == "probe":
        probe(season)
    elif a.command == "probe-pregame":
        probe_pregame(d, season, date)
    elif a.command == "refresh":
        print(refresh_season(d, season))
    elif a.command == "boxscores":
        for sc in [x.strip() for x in (a.seasons or season).split(",") if x.strip()]:
            print(collect_boxscores(d, sc, limit=a.limit, fill_periods=a.fill_periods))
    elif a.command == "backtest":
        codes = [s.strip() for s in (a.seasons or season).split(",") if s.strip()]
        backtest(d, codes)
    elif a.command == "update":
        sp = os.path.join(d, a.starters_file)
        res = daily_update(d, date, season, load_lines(d, a.lines_file),
                           _read_json(sp, {}) if os.path.exists(sp) else {})
        print(f"[{_utcnow().isoformat()}Z] update {season}: games saved {res['refresh']['games']}, boxscores saved "
              f"{res['boxscores']['total_saved']} (+{res['boxscores']['downloaded']} new), games today {res['games_today']}, "
              f"logged {res['logged']}")
        for w in res["warnings"]:
            print("  note:", w)
    elif a.command == "lineups-check":
        lineup_check(d, [x.strip() for x in (a.seasons or season).split(",") if x.strip()])
    elif a.command == "slate":
        if not a.no_refresh:
            store = _read_json(data_file(d, season), {})
            fresh = False
            if store.get("fetched_at"):
                age = _utcnow() - dt.datetime.fromisoformat(store["fetched_at"].rstrip("Z"))
                fresh = age < dt.timedelta(hours=3)
            if not fresh:
                try:
                    print(refresh_season(d, season), file=sys.stderr)
                    print(collect_boxscores(d, season, limit=400), file=sys.stderr)
                except Exception as e:
                    print(f"refresh failed ({e}); using cached data", file=sys.stderr)
        lines = load_lines(d, a.lines_file)
        bankroll = current_bankroll(d, a.bankroll)
        sp = os.path.join(d, a.starters_file)
        starters = _read_json(sp, {}) if os.path.exists(sp) else {}
        s = slate(d, date, lines, bankroll, a.kelly, a.min_ev, a.max_stake_pct, log=not a.no_log, season=season, starters=starters)
        if a.json:
            print(json.dumps(s, default=str))
        else:
            print_slate(s)
    elif a.command == "track":
        if not a.no_refresh:
            try:
                print(refresh_season(d, season), file=sys.stderr)
                print(collect_boxscores(d, season, limit=400), file=sys.stderr)
            except Exception as e:
                print(f"refresh failed ({e}); settling from cached results", file=sys.stderr)
        rep = track_report(d, a.bankroll)
        if a.json:
            print(json.dumps(rep, default=str))
        else:
            print_track(rep)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: nhl_period_model.py (511 lines)
# ======================================================================
_SRC["nhl_period_model"] = r'''"""
nhl_period_model.py  --  goals per PERIOD (1st, 2nd, 3rd), each period modelled on its own.  Needs nhl_goals_model.py next to it.

FORMULA (the same shrunk index-multiplication pattern, one period at a time)
    goals_home(p) = home_scoring_index(p) * away_allowed_index(p) * league_avg_goals(p) * (1 + hfa)
    goals_away(p) = away_scoring_index(p) * home_allowed_index(p) * league_avg_goals(p) / (1 + hfa)
    scoring_index(p) = shrunk (team's goals scored in period p per game) / league_avg_goals(p)
    allowed_index(p) = shrunk (team's goals allowed in period p per game) / league_avg_goals(p)
  A single period is only ~1 goal per team, so a team's period history is noisy.  The backtest therefore also tests shrinking
  each team's period rate toward the team's OWN full-game rate (times the league's share of goals in that period) instead of
  toward the league average, and keeps whichever scores better on games it did not tune on.
  Regulation periods only (overtime and shootouts are not part of periods 1-3).  Periods are treated as independent; the
  backtest prints the measured correlation between periods so you can see how true that is.

PER GAME AND PERIOD YOU GET
    goals distribution for each team and for the period total; P(over/under/push) at any line; P(goal in the period);
    period result (home leading / tied / away leading at the end of the period); the same for the first two periods combined.

COMMANDS (run in your NHL folder)
    python nhl_goals_model.py probe --season 20252026                                  # shows whether goals-by-period are in the API
    python nhl_goals_model.py boxscores --seasons 20232024,20242025,20252026 --fill-periods    # one-time backfill (~4,000 calls)
    python nhl_period_model.py check    --seasons 20232024,20242025,20252026           # do period goals add up to the real scores?
    python nhl_period_model.py backtest --seasons 20232024,20242025,20252026           # tune, validate, write nhl_period_params.json
    python nhl_period_model.py slate    [--date 2026-10-09] [--lines-file nhl_lines.json]
  New games get period data automatically from `nhl_goals_model.py update`.

LINES FILE keys (same file as the game model):  "TOR@BOS|p1total": {"line": 1.5, "over": -140, "under": 115},
  "TOR@BOS|p1home": 0.5, "TOR@BOS|p2away": 0.5, ...   (p1/p2/p3 + total/home/away).  Staking, logging and the tracker are the same
  as the game model (push-aware Kelly, capped, EV threshold) and period bets settle from the stored period goals.
"""

import argparse
import itertools
import json
import math
import os
import sys

import nhl_goals_model as base

PARAMS_NAME = "nhl_period_params.json"
STATIC_LG = [0.93, 1.02, 1.03]          # goals per team per period before any data
PDEFAULTS = dict(k=60.0, k_game=40.0, hfa=0.03, carry=0.3, to_game=True, shapes=[None, None, None], min_games=3)
KGRID = [20, 40, 80, 160, 320, 640, 1280, 5120, 1e6]      # 1e6 = no team-specific period tendencies at all
HGRID = [0.0, 0.02, 0.04, 0.06, 0.08]
MAX_P = 9                                # max goals per team in a period
MAX_PT = 14


def convolve(a, b, nmax):
    """pmf of X+Y for independent pmfs a, b, truncated at nmax (mass beyond is dropped, then renormalised)."""
    out = [0.0] * (nmax + 1)
    for i, x in enumerate(a):
        if x < 1e-15:
            continue
        for j, y in enumerate(b):
            if i + j > nmax:
                break
            out[i + j] += x * y
    s = sum(out)
    return [v / s for v in out]


class PState:
    """Running per-team, per-period goals scored / allowed (regulation), with last-season carry-over."""

    def __init__(self, params, prev=None):
        self.p = params
        self.teams = {}                                            # team -> [n, gf[3], ga[3]]
        self.prev = prev.final_counts() if prev else {}
        self.prior_lg = prev.league_avg() if prev else list(STATIC_LG)
        self.pool = [0.0, 0.0, 0.0]
        self.pool_n = 0

    def add_game(self, game):
        per = game["per"]
        for t, gf, ga in ((game["home"], per["home"], per["away"]), (game["away"], per["away"], per["home"])):
            c = self.teams.setdefault(t, [0, [0.0] * 3, [0.0] * 3])
            c[0] += 1
            for i in range(3):
                c[1][i] += gf[i]
                c[2][i] += ga[i]
        for i in range(3):
            self.pool[i] += per["home"][i] + per["away"][i]
        self.pool_n += 2

    def league_avg(self):
        w = base.POOL_PSEUDO
        return [(self.pool[i] + w * self.prior_lg[i]) / (self.pool_n + w) for i in range(3)]

    def final_counts(self):
        return {t: (c[0], list(c[1]), list(c[2])) for t, c in self.teams.items()}

    def games_played(self, t):
        return self.teams.get(t, [0])[0]

    def trusted(self, home, away):
        m = self.p["min_games"]
        return self.games_played(home) >= m and self.games_played(away) >= m

    def _eff(self, t):
        n, gf, ga = self.teams.get(t, [0, [0.0] * 3, [0.0] * 3])
        pn, pgf, pga = self.prev.get(t, (0, [0.0] * 3, [0.0] * 3))
        c = self.p["carry"]
        return n + c * pn, [gf[i] + c * pgf[i] for i in range(3)], [ga[i] + c * pga[i] for i in range(3)]

    def _rates(self, t, lg):
        n, gf, ga = self._eff(t)
        k = self.p["k"]
        lg_game = sum(lg)
        if self.p["to_game"]:
            kg = self.p["k_game"]
            f_idx = ((sum(gf) + kg * lg_game) / (n + kg)) / lg_game        # team's whole-game scoring index
            a_idx = ((sum(ga) + kg * lg_game) / (n + kg)) / lg_game
        else:
            f_idx = a_idx = 1.0
        f = [(gf[i] + k * lg[i] * f_idx) / (n + k) for i in range(3)]
        a = [(ga[i] + k * lg[i] * a_idx) / (n + k) for i in range(3)]
        return f, a

    def means(self, home, away):
        """-> (mu_home[3], mu_away[3], league_avg[3])"""
        lg = self.league_avg()
        hf, ha = self._rates(home, lg)
        af, aa = self._rates(away, lg)
        h = 1.0 + self.p["hfa"]
        mu_h = [(hf[i] / lg[i]) * (aa[i] / lg[i]) * lg[i] * h for i in range(3)]
        mu_a = [(af[i] / lg[i]) * (ha[i] / lg[i]) * lg[i] / h for i in range(3)]
        return mu_h, mu_a, lg


def walk_forward(seasons, params):
    """Predictions for each game use only games on strictly earlier dates.  Games without period data are skipped."""
    rows, prev = [], None
    for season, games in seasons:
        st = PState(params, prev)
        games = [g for g in games if g.get("per")]
        for _, day in itertools.groupby(sorted(games, key=lambda g: (g["date"], g["id"])), key=lambda g: g["date"]):
            day = list(day)
            for g in day:
                mu_h, mu_a, lg = st.means(g["home"], g["away"])
                rows.append({"season": season, "date": g["date"], "home": g["home"], "away": g["away"], "per": g["per"],
                             "mu_h": mu_h, "mu_a": mu_a, "lg": lg, "trusted": st.trusted(g["home"], g["away"]),
                             "rest_h": g.get("rest_h"), "rest_a": g.get("rest_a")})
            for g in day:
                st.add_game(g)
        prev = st
    return rows


# ------------------------------------------------------------------ distributions
def period_dists(mu_h, mu_a, shape=None):
    """-> dict for ONE period: home pmf, away pmf, total pmf, and P(home ahead / tied / away ahead)."""
    ph, pa = base.goal_pmf(mu_h, shape, MAX_P), base.goal_pmf(mu_a, shape, MAX_P)
    tot = convolve(ph, pa, MAX_PT)
    win = sum(ph[i] * pa[j] for i in range(len(ph)) for j in range(len(pa)) if i > j)
    tie = sum(ph[i] * pa[i] for i in range(len(ph)))
    return {"home": ph, "away": pa, "total": tot, "result": {"home_leads": win, "tied": tie, "away_leads": max(0.0, 1 - win - tie)}}


def game_periods(mu_h, mu_a, shapes):
    ps = [period_dists(mu_h[i], mu_a[i], shapes[i]) for i in range(3)]
    h12 = convolve(ps[0]["home"], ps[1]["home"], 2 * MAX_P)
    a12 = convolve(ps[0]["away"], ps[1]["away"], 2 * MAX_P)
    win = sum(h12[i] * a12[j] for i in range(len(h12)) for j in range(len(a12)) if i > j)
    tie = sum(h12[i] * a12[i] for i in range(len(h12)))
    thru2 = {"home": h12, "away": a12, "total": convolve(h12, a12, 4 * MAX_P),
             "result": {"home_leads": win, "tied": tie, "away_leads": max(0.0, 1 - win - tie)}}
    return ps, thru2


def _pois(mu, k):
    return k * math.log(max(mu, 0.05)) - max(mu, 0.05) - math.lgamma(k + 1)


def _nll_poisson(rows, period=None):
    tot, n = 0.0, 0
    for r in rows:
        if not r["trusted"]:
            continue
        for i in (range(3) if period is None else [period]):
            tot -= _pois(r["mu_h"][i], r["per"]["home"][i]) + _pois(r["mu_a"][i], r["per"]["away"][i])
            n += 2
    return tot / max(n, 1)


def _nll_shape(rows, i, shape):
    tot, n = 0.0, 0
    for r in rows:
        if not r["trusted"]:
            continue
        for mu, y in ((r["mu_h"][i], r["per"]["home"][i]), (r["mu_a"][i], r["per"]["away"][i])):
            pm = base.goal_pmf(mu, shape, MAX_P)
            tot += base.pmf_nll(pm, y)
            n += 1
    return tot / max(n, 1)


def _split(rows, seasons):
    return base._split(rows, seasons)


def load_period_seasons(directory, season_codes, out=print):
    seasons = []
    for sc in season_codes:
        g, _ = base.load_games(directory, sc)
        have = [x for x in g if x.get("per")]
        if have:
            seasons.append((sc, have))
        out(f"{sc}: {len(have)} of {len(g)} games have period data")
    return seasons


def check(directory, season_codes, out=print):
    """Do the stored period goals match the official regulation scores?  (collect_boxscores already rejects mismatches.)"""
    for sc in season_codes:
        games, _ = base.load_games(directory, sc)
        box = (base._read_json(base.box_file(directory, sc), {}) or {}).get("box", {})
        ok = bad = missing = 0
        for g in games:
            b = box.get(str(g["id"]))
            if not b or "per" not in b:
                missing += 1
            elif b["per"] is False:
                bad += 1
            else:
                ok += 1
        out(f"{sc}: {ok} games with verified period goals, {bad} rejected (did not add up / not published), "
            f"{missing} not collected yet (run boxscores --fill-periods)")


# ------------------------------------------------------------------ backtest
def backtest(directory, season_codes, out=print, write_params=True):
    seasons = load_period_seasons(directory, season_codes, out)
    if not seasons:
        out("No period data. Run `python nhl_goals_model.py probe` then `boxscores --fill-periods`.")
        return None
    out(f"\nwalk-forward over {sum(len(g) for _, g in seasons)} games; no future information used.\n")
    best = None
    grid = list(itertools.product(KGRID, [False, True], HGRID,
                                  [0.0, 0.3] if len(seasons) > 1 else [0.0]))
    for k, to_game, hfa, carry in grid:
        p = dict(PDEFAULTS, k=float(k), to_game=to_game, hfa=hfa, carry=carry)
        tune, _, _ = _split(walk_forward(seasons, p), seasons)
        sc = _nll_poisson(tune)
        if best is None or sc < best[0]:
            best = (sc, p)
    p = best[1]
    rows = walk_forward(seasons, p)
    tune, val, desc = _split(rows, seasons)
    shapes = []
    for i in range(3):
        sb = (None, _nll_shape(tune, i, None))
        for sh in base.SHAPES[1:]:
            sc = _nll_shape(tune, i, sh)
            if sc < sb[1] - 1e-5:
                sb = (sh, sc)
        shapes.append(sb[0])
    p["shapes"] = shapes
    name = lambda sh: "Poisson" if sh is None else (f"NegBin r={sh:g}" if sh > 0 else f"Binomial n={-sh:g}")
    out(f"Tuned on {len(tune)} games, {desc} ({len(val)} games).")
    out(f"Chosen: shrinkage k={p['k']:g} pseudo-games, shrink toward the team's own full-game rate = {p['to_game']}, "
        f"home-ice factor={p['hfa']:g}, last-season carry-over={p['carry']:g}")
    out("Goal-count shape by period: " + ", ".join(f"P{i + 1} {name(s)}" for i, s in enumerate(shapes)))
    if p["k"] >= 1e6:
        out("  Team-specific period tendencies add nothing here: each period = league share of goals x the team's FULL-GAME index.")
    for nm, v, g_ in (("k", p["k"], KGRID[:-1]), ("hfa", p["hfa"], HGRID)):
        if v in (g_[0], g_[-1]):
            out(f"  WARNING: {nm}={v:g} sits on the edge of its grid ({g_[0]:g}..{g_[-1]:g}).")
    # ablations on validation
    out("\n" + "=" * 72 + "\nVALIDATION (games the tuning never saw)\n" + "=" * 72)
    vt = [r for r in val if r["trusted"]]
    out(f"{len(vt)} validate games with both teams trusted")
    p_lg = dict(p, to_game=not p["to_game"])
    alt = _split(walk_forward(seasons, p_lg), seasons)[1]
    out("Period goals log loss per team-period (lower is better), Poisson:")
    out(f"{'':<34}{'P1':>9}{'P2':>9}{'P3':>9}{'all':>9}")
    def line(label, rs, key=None):
        vals = [_nll_poisson(rs, i) for i in range(3)] + [_nll_poisson(rs)]
        out(f"{label:<34}" + "".join(f"{v:>9.4f}" for v in vals))
        return vals
    naive = [dict(r, mu_h=list(r["lg"]), mu_a=list(r["lg"])) for r in val]
    v_n = line("league-average baseline", naive)
    v_m = line(f"model (to_game={p['to_game']})", val)
    v_a = line(f"model (to_game={not p['to_game']})", alt)
    out(f"{'model gain over baseline':<34}" + "".join(f"{a - b:>+9.4f}" for a, b in zip(v_n, v_m)))
    out("\nMean goals per period (both teams), validate games: model vs actual")
    for i in range(3):
        mm = sum(r["mu_h"][i] + r["mu_a"][i] for r in vt) / len(vt)
        aa = sum(r["per"]["home"][i] + r["per"]["away"][i] for r in vt) / len(vt)
        out(f"  P{i + 1}: model {mm:.3f}  actual {aa:.3f}  (diff {aa - mm:+.3f})")
    # calibration
    out("\nCalibration on validate games (model P(over) vs what happened)")
    out(f"{'market':<20}{'line':>5}{'n':>6}{'model':>8}{'actual':>8}{'z':>6}")
    ds = [(r, game_periods(r["mu_h"], r["mu_a"], shapes)) for r in vt]
    for i in range(3):
        for mk, line_, getp, gety in (
                (f"P{i + 1} team goals", 0.5, lambda d, i=i: [d[0][i]["home"], d[0][i]["away"]], lambda r, i=i: [r["per"]["home"][i], r["per"]["away"][i]]),
                (f"P{i + 1} total goals", 1.5, lambda d, i=i: [d[0][i]["total"]], lambda r, i=i: [r["per"]["home"][i] + r["per"]["away"][i]]),
                (f"P{i + 1} total goals", 2.5, lambda d, i=i: [d[0][i]["total"]], lambda r, i=i: [r["per"]["home"][i] + r["per"]["away"][i]])):
            ps, ys = [], []
            for r, d in ds:
                for pm, y in zip(getp(d), gety(r)):
                    ps.append(base.over_under_push(pm, line_)[0])
                    ys.append(1.0 if y > line_ else 0.0)
            n = len(ps); m_, a_ = sum(ps) / n, sum(ys) / n
            se = math.sqrt(max(m_ * (1 - m_), 1e-9) / n)
            out(f"{mk:<20}{line_:>5.1f}{n:>6}{m_:>8.1%}{a_:>8.1%}{(a_ - m_) / se:>6.1f}")
    for line_ in (2.5, 3.5):
        ps = [base.over_under_push(d[1]["total"], line_)[0] for r, d in ds]
        ys = [1.0 if sum(r["per"]["home"][:2]) + sum(r["per"]["away"][:2]) > line_ else 0.0 for r, d in ds]
        n = len(ps); m_, a_ = sum(ps) / n, sum(ys) / n
        out(f"{'goals thru 2 periods':<20}{line_:>5.1f}{n:>6}{m_:>8.1%}{a_:>8.1%}{(a_ - m_) / math.sqrt(max(m_ * (1 - m_), 1e-9) / n):>6.1f}")
    out("\nPeriod result (who leads after the period): model vs actual")
    out(f"{'':<8}{'home leads':>20}{'tied':>20}{'away leads':>20}")
    for i in range(3):
        mod = [sum(d[0][i]["result"][k] for _, d in ds) / len(ds) for k in ("home_leads", "tied", "away_leads")]
        act = [0.0, 0.0, 0.0]
        for r, _ in ds:
            h, a = r["per"]["home"][i], r["per"]["away"][i]
            act[0 if h > a else 1 if h == a else 2] += 1 / len(ds)
        out(f"{'P' + str(i + 1):<8}" + "".join(f"{m_:>11.1%} / {a_:<6.1%}" for m_, a_ in zip(mod, act)))
    out("  (each cell: model / actual)")
    out("\nHow often a period has 0, 1, 2, 3+ goals (both teams), model vs actual")
    for i in range(3):
        mod = [sum(d[0][i]["total"][k] for _, d in ds) / len(ds) for k in range(3)]
        mod.append(1 - sum(mod))
        act = [sum(1 for r, _ in ds if min(r["per"]["home"][i] + r["per"]["away"][i], 3) == k) / len(ds) for k in range(4)]
        out(f"  P{i + 1}: " + "  ".join(f"{k}{'+' if k == 3 else ''}: {m_:.1%}/{a_:.1%}" for k, (m_, a_) in enumerate(zip(mod, act))))
    # independence across periods
    def resid(r, i):
        return r["per"]["home"][i] + r["per"]["away"][i] - r["mu_h"][i] - r["mu_a"][i]
    cors = []
    for i, j in ((0, 1), (1, 2), (0, 2)):
        xs, ys = [resid(r, i) for r in vt], [resid(r, j) for r in vt]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        c = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
        cors.append((i + 1, j + 1, c, c * math.sqrt(len(xs))))
    out("\nIndependence check (correlation of period total-goal errors; z beyond +-2 means periods are NOT independent):")
    out("  " + "   ".join(f"P{a}-P{b}: {c:+.3f} (z {z:+.1f})" for a, b, c, z in cors))
    out("\nNo sportsbook prices are involved: this checks the model against results. Log real period lines with `slate`.")
    if write_params:
        base._atomic_json(os.path.join(directory, PARAMS_NAME), {"params": p, "validated_on": desc, "seasons": [s for s, _ in seasons],
                                                                 "gain_over_baseline": [a - b for a, b in zip(v_n, v_m)]})
        out(f"Saved {PARAMS_NAME}")
    return p


# ------------------------------------------------------------------ live slate
def load_params(directory):
    saved = base._read_json(os.path.join(directory, PARAMS_NAME), None)
    if saved and "params" in saved:
        return dict(PDEFAULTS, **saved["params"]), True
    return dict(PDEFAULTS), False


def build_state(directory, season, params):
    start = int(season[:4])
    prev_games, _ = base.load_games(directory, f"{start - 1}{start}")
    ps = None
    prev_games = [g for g in prev_games if g.get("per")]
    if prev_games:
        ps = PState(params)
        for g in prev_games:
            ps.add_game(g)
    games, upcoming = base.load_games(directory, season)
    st = PState(params, ps)
    n = 0
    for g in games:
        if g.get("per"):
            st.add_game(g)
            n += 1
    return st, n, upcoming


MARKETS = [f"p{i}{w}" for i in (1, 2, 3) for w in ("total", "home", "away")]
STD_LINES = {"total": 1.5, "home": 0.5, "away": 0.5}


def slate(directory, date, lines=None, bankroll=1000.0, kelly=0.25, min_ev=0.03, max_stake_pct=0.03, now=None, log=True,
          season=None, log_model_lines=False, live=None):
    now = now or base._utcnow()
    season = season or base.season_code_for(date)
    params, validated = load_params(directory)
    st, n_games, upcoming = live or build_state(directory, season, params)
    lines = lines or {}
    warnings, records, games_out, used = [], [], [], set()
    if not validated:
        warnings.append(f"No {PARAMS_NAME}: using UNVALIDATED default parameters. Run `backtest` first.")
    if n_games == 0:
        warnings.append("No games with period data this season yet: predictions are league-average (run `boxscores --fill-periods`).")
    today = [u for u in upcoming if u["date"] == date]
    if not today:
        warnings.append(f"No upcoming games found on {date}.")
    for u in sorted(today, key=lambda x: x.get("start_utc") or ""):
        mu_h, mu_a, lg = st.means(u["home"], u["away"])
        ps, t2 = game_periods(mu_h, mu_a, params["shapes"])
        trusted = st.trusted(u["home"], u["away"])
        entry = {"game_id": u["id"], "date": date, "home": u["home"], "away": u["away"], "start_utc": u.get("start_utc"),
                 "trusted": trusted, "periods": [], "through_2": {
                     "mu_total": sum(mu_h[:2]) + sum(mu_a[:2]),
                     "p_over": {str(l): base.over_under_push(t2["total"], l)[0] for l in (2.5, 3.5)},
                     "result": t2["result"]}}
        for i in range(3):
            d = ps[i]
            pe = {"period": i + 1, "mu_home": mu_h[i], "mu_away": mu_a[i], "mu_total": mu_h[i] + mu_a[i],
                  "league_avg": lg[i], "fair_line_total": base.fair_line(d["total"]),
                  "p_goal_in_period": 1 - d["total"][0],
                  "p_over": {"total_1.5": base.over_under_push(d["total"], 1.5)[0], "total_2.5": base.over_under_push(d["total"], 2.5)[0],
                             "home_0.5": base.over_under_push(d["home"], 0.5)[0], "away_0.5": base.over_under_push(d["away"], 0.5)[0],
                             "home_1.5": base.over_under_push(d["home"], 1.5)[0], "away_1.5": base.over_under_push(d["away"], 1.5)[0]},
                  "both_score": (1 - d["home"][0]) * (1 - d["away"][0]), "result": d["result"],
                  "pmf": {"home": [round(x, 5) for x in d["home"][:6]], "away": [round(x, 5) for x in d["away"][:6]],
                          "total": [round(x, 5) for x in d["total"][:8]]}, "markets": {}}
            for what in ("total", "home", "away"):
                market = f"p{i + 1}{what}"
                pm = d[what]
                mu = pe["mu_" + what]
                key = f"{u['away']}@{u['home']}|{market}"
                rec_base = {"ts": now.isoformat(), "date": date, "game_id": u["id"], "home": u["home"], "away": u["away"],
                            "start_utc": u.get("start_utc"), "market": market, "mu": mu, "trusted": trusted}
                ents = base._line_entries(lines.get(key), date, now)
                if ents:
                    used.add(key)
                    rungs, best = base.price_ladder(pm, ents, bankroll, kelly, min_ev, max_stake_pct, trusted)
                    strip = lambda pr: {k: v for k, v in pr.items() if k != "_dec"}
                    pe["markets"][what] = {"priced": strip(rungs[best]["pr"]), "bet": rungs[best]["bet"]}
                    if len(rungs) > 1:
                        pe["markets"][what]["ladder"] = [dict(strip(r["pr"]), bet=r["bet"]) for r in rungs]
                    for r in rungs:
                        pr, ent = r["pr"], r["ent"]
                        records.append(dict(rec_base, key=f"{u['id']}|{market}|{pr['line']}", line=pr["line"], p_over=pr["p_over"],
                                            p_under=pr["p_under"], p_push=pr["p_push"], over_odds=ent.get("over"),
                                            under_odds=ent.get("under"), bet=r["bet"]))
                elif log_model_lines:
                    ln = STD_LINES[what]
                    pr = base.price_line(pm, ln)
                    records.append(dict(rec_base, key=f"{u['id']}|{market}|{ln}", line=ln, p_over=pr["p_over"], p_under=pr["p_under"],
                                        p_push=pr["p_push"], over_odds=None, under_odds=None, bet=None, source="model"))
            entry["periods"].append(pe)
        games_out.append(entry)
    unmatched = [k for k in lines if k not in used and k.split("|")[-1] in MARKETS and not base._is_auto(lines[k])]
    if unmatched:
        warnings.append(f"Period lines not matched to a game on {date}: {', '.join(unmatched)}")
    out = {"date": date, "season": season, "validated": validated, "params": params, "bankroll": bankroll, "games": games_out,
           "warnings": warnings}
    if log and records:
        out["log"] = base.log_records(directory, records, now)
    return out


def print_slate(s, out=print):
    out(f"NHL goals by period, {s['date']}  (params {'validated' if s['validated'] else 'UNVALIDATED defaults'})")
    for g in s["games"]:
        out("-" * 100)
        out(f"{g['away']} @ {g['home']}" + ("" if g["trusted"] else "   (thin data)"))
        out(f"  {'period':<8}{'home':>6}{'away':>6}{'total':>7}{'P(goal)':>9}{'T o1.5':>8}{'H o.5':>7}{'A o.5':>7}"
            f"{'both':>7}   home leads / tied / away leads")
        for pe in g["periods"]:
            r = pe["result"]
            out(f"  P{pe['period']:<7}{pe['mu_home']:>6.2f}{pe['mu_away']:>6.2f}{pe['mu_total']:>7.2f}{pe['p_goal_in_period']:>9.1%}"
                f"{pe['p_over']['total_1.5']:>8.1%}{pe['p_over']['home_0.5']:>7.1%}{pe['p_over']['away_0.5']:>7.1%}{pe['both_score']:>7.1%}"
                f"   {r['home_leads']:.1%} / {r['tied']:.1%} / {r['away_leads']:.1%}")
        t2 = g["through_2"]
        out(f"  after 2  total {t2['mu_total']:.2f}   P(over 2.5) {t2['p_over']['2.5']:.1%}  P(over 3.5) {t2['p_over']['3.5']:.1%}   "
            f"home leads / tied / away leads {t2['result']['home_leads']:.1%} / {t2['result']['tied']:.1%} / {t2['result']['away_leads']:.1%}")
        for pe in g["periods"]:
            for what, mk in pe["markets"].items():
                for p in (mk.get("ladder") or [dict(mk["priced"], bet=mk["bet"])]):
                    b = p["bet"]
                    tail = (f"{b['side'].upper()} {b['odds']:+d} EV {b['ev']:+.1%} stake {b['stake']:.2f}" if b else "no bet")
                    out(f"  priced P{pe['period']} {what:<6} line {p['line']:.1f}: over {p['p_over']:.1%} under {p['p_under']:.1%} push {p['p_push']:.1%}  -> {tail}")
    for w in s["warnings"]:
        out("WARNING: " + w)
    if "log" in s:
        out(f"Logged: {s['log']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NHL goals per period")
    ap.add_argument("command", choices=["check", "backtest", "slate"])
    ap.add_argument("--dir", default=".")
    ap.add_argument("--seasons")
    ap.add_argument("--date")
    ap.add_argument("--season")
    ap.add_argument("--lines-file", default="nhl_lines.json,nhl_lines_kalshi.json")
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--kelly", type=float, default=0.25)
    ap.add_argument("--min-ev", type=float, default=0.03)
    ap.add_argument("--max-stake-pct", type=float, default=0.03)
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    date = a.date or (base._utcnow() - base.dt.timedelta(hours=8)).date().isoformat()
    season = a.season or base.season_code_for(date)
    codes = [x.strip() for x in (a.seasons or season).split(",") if x.strip()]
    if a.command == "check":
        check(a.dir, codes)
    elif a.command == "backtest":
        backtest(a.dir, codes)
    else:
        lines = base.load_lines(a.dir, a.lines_file)
        s = slate(a.dir, date, lines, base.current_bankroll(a.dir, a.bankroll), a.kelly, a.min_ev, a.max_stake_pct,
                  log=not a.no_log, season=season, log_model_lines=True)
        print(json.dumps(s, default=str) if a.json else "", end="" if a.json else "")
        if not a.json:
            print_slate(s)


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: kalshi_lines.py (224 lines)
# ======================================================================
_SRC["kalshi_lines"] = r'''"""
kalshi_lines.py  --  pull Kalshi's NHL goal-total markets (public data, read-only, no key, no login, NO trading) and write them
in the lines format the NHL models already read.

WHAT IT PULLS (series seen in the probe)
    KXNHLTOTAL      full-game total goals         -> "AWAY@HOME|total"
    KXNHLTEAMTOTAL  one team's goals              -> "AWAY@HOME|home"  or  "|away"
    KXNHL1PTOTAL / KXNHL2PTOTAL / KXNHL3PTOTAL    -> "AWAY@HOME|p1total" / "|p2total" / "|p3total"
  Each market is a ladder (over 5.5, over 6.5, ...).  Every rung with a real two-sided quote becomes one entry.

SETTLEMENT (from Kalshi's rules): full-game and team totals count regulation + overtime, and a SHOOTOUT WINNER IS CREDITED ONE GOAL.
  Sportsbooks do not.  The models price Kalshi lines with that extra goal and settle logged Kalshi bets the same way.  Period totals
  are regulation only (no overtime, no shootout).

PRICES.  Kalshi quotes yes/no in dollars per $1 contract.  OVER costs the yes ASK, UNDER costs the no ASK (what you would pay now),
  plus Kalshi's taker fee (approx. fee_rate x p x (1-p) per contract, default 0.07 - check your own fee schedule and pass --fee).
  The result is converted to American odds so the existing pricing, EV and Kelly code works unchanged.  Rungs whose bid/ask spread
  is wider than --max-spread (default 0.12) or whose price is near 0/1 are skipped: they are not real markets.

COMMANDS (run in your NHL folder)
    python kalshi_lines.py rules                  # prints Kalshi's settlement rules for one market per series - READ THESE FIRST
    python kalshi_lines.py fetch [--date 2026-10-10] [--all-dates]
    then:  python nhl_goals_model.py slate --date 2026-10-10 ; python nhl_period_model.py slate --date 2026-10-10
  Output file: nhl_lines_kalshi.json (the models read it automatically next to nhl_lines.json; prices older than 8 hours are ignored).
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request

import nhl_goals_model as base

API = "https://api.elections.kalshi.com/trade-api/v2"
OUT_NAME = "nhl_lines_kalshi.json"
SERIES = {"KXNHLTOTAL": "total", "KXNHLTEAMTOTAL": "team", "KXNHL1PTOTAL": 1, "KXNHL2PTOTAL": 2, "KXNHL3PTOTAL": 3}
ALIAS = {"LA": "LAK", "SJ": "SJS", "TB": "TBL", "NJ": "NJD"}            # Kalshi code -> NHL API code
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def http_get(path, **q):
    url = API + path + ("?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None}) if q else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sports-hub-lines/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_series(series, get=http_get):
    out, cursor = [], None
    for _ in range(20):
        d = get("/markets", series_ticker=series, status="open", limit=1000, cursor=cursor)
        out.extend(d.get("markets", []))
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


# ------------------------------------------------------------------ parsing
def _code(x):
    return ALIAS.get(x, x)


def parse_event(event_ticker):
    """'KXNHLTOTAL-26OCT10LAVGK' -> (date '2026-10-10', [(away, home) candidates in NHL codes]).  Which side is home is
    settled later from the real schedule; both team codes are 2-3 letters so every split is tried."""
    try:
        tail = event_ticker.split("-", 1)[1]
        yy, mon, dd, rest = int(tail[:2]), MONTHS[tail[2:5]], int(tail[5:7]), tail[7:]
    except (IndexError, KeyError, ValueError):
        return None, []
    known = set(base.NHL_TEAMS) | set(ALIAS)
    cands = []
    for i in (2, 3):
        a, b = rest[:i], rest[i:]
        if a in known and b in known:
            cands.append((_code(a), _code(b)))
    return dt.date(2000 + yy, mon, dd).isoformat(), cands


def match_game(date, cands, upcoming):
    best = None
    for u in upcoming:
        for a, b in cands:
            if {u["home"], u["away"]} == {a, b}:
                gap = abs((dt.date.fromisoformat(u["date"]) - dt.date.fromisoformat(date)).days)
                if gap <= 1 and (best is None or gap < best[0]):
                    best = (gap, u)
    return best[1] if best else None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def to_american(dec):
    return int(round((dec - 1.0) * 100)) if dec >= 2.0 else int(round(-100.0 / (dec - 1.0)))


def fee(p, rate):
    return rate * p * (1.0 - p)


def rung_from_market(m, fee_rate=0.07, max_spread=0.12):
    """-> dict(line, over, under, ...) or None when there is no real two-sided quote."""
    ya, yb, na, nb = (_f(m.get(k)) for k in ("yes_ask_dollars", "yes_bid_dollars", "no_ask_dollars", "no_bid_dollars"))
    if None in (ya, yb, na, nb):
        return None
    if ya - yb > max_spread or na - nb > max_spread or not (0.03 < ya < 0.97) or not (0.03 < na < 0.97):
        return None
    mm = re.search(r"over\s+([0-9]+(?:\.[0-9]+)?)\s+goals", str(m.get("title", "")), re.I)
    if not mm:
        return None
    co, cu = ya + fee(ya, fee_rate), na + fee(na, fee_rate)
    if co >= 1.0 or cu >= 1.0:
        return None
    return {"line": float(mm.group(1)), "over": to_american(1.0 / co), "under": to_american(1.0 / cu),
            "yes_ask": ya, "yes_bid": yb, "no_ask": na, "no_bid": nb, "ticker": m.get("ticker")}


def build_lines(markets_by_series, upcoming, now=None, fee_rate=0.07, max_spread=0.12, only_date=None):
    now = now or base._utcnow()
    stamp = now.isoformat()
    lines, stats = {}, {"markets": 0, "rungs": 0, "no_game": 0, "thin": 0, "other_date": 0}
    for series, ms in markets_by_series.items():
        kind = SERIES[series]
        for m in ms:
            stats["markets"] += 1
            date, cands = parse_event(str(m.get("event_ticker", "")))
            g = match_game(date, cands, upcoming) if date else None
            if g is None:
                stats["no_game"] += 1
                continue
            if only_date and g["date"] != only_date:
                stats["other_date"] += 1
                continue
            r = rung_from_market(m, fee_rate, max_spread)
            if r is None:
                stats["thin"] += 1
                continue
            if kind == "total":
                mk = "total"
            elif kind == "team":
                code = _code(re.sub(r"\d+$", "", str(m.get("ticker", "")).rsplit("-", 1)[-1]))
                if code not in (g["home"], g["away"]):
                    stats["no_game"] += 1
                    continue
                mk = "home" if code == g["home"] else "away"
            else:
                mk = f"p{kind}total"
            r.update({"date": g["date"], "fetched_at": stamp, "src": "kalshi"})
            lines.setdefault(f"{g['away']}@{g['home']}|{mk}", []).append(r)
            stats["rungs"] += 1
    for k in lines:
        lines[k].sort(key=lambda e: (e["date"], e["line"]))
    return lines, stats


# ------------------------------------------------------------------ commands
def cmd_rules(get=http_get, out=print):
    for series in SERIES:
        try:
            ms = [m for m in fetch_series(series, get) if _f(m.get("yes_ask_dollars")) is not None]
        except Exception as e:
            out(f"{series}: error {e}")
            continue
        if not ms:
            out(f"{series}: no open markets")
            continue
        m = ms[0]
        out(f"\n== {series}   e.g. {m.get('ticker')}  ({m.get('title')})")
        out("   rules_primary  :", m.get("rules_primary"))
        out("   rules_secondary:", m.get("rules_secondary"))


def cmd_fetch(directory, date=None, all_dates=False, fee_rate=0.07, max_spread=0.12, get=http_get, out=print, now=None):
    date = date or (base._utcnow() - dt.timedelta(hours=8)).date().isoformat()
    season = base.season_code_for(date)
    _, upcoming = base.load_games(directory, season)
    if not upcoming:
        out(f"No upcoming games in {season} data: run `python nhl_goals_model.py refresh --season {season}` first.")
        return None
    by = {}
    for series in SERIES:
        try:
            by[series] = fetch_series(series, get)
        except Exception as e:
            out(f"{series}: could not fetch ({e})")
            by[series] = []
    lines, stats = build_lines(by, upcoming, now, fee_rate, max_spread, None if all_dates else date)
    base._atomic_json(os.path.join(directory, OUT_NAME), lines)
    out(f"Kalshi: {stats['markets']} markets read; {stats['rungs']} usable lines saved to {OUT_NAME} "
        f"({stats['thin']} skipped as not a real two-sided quote, {stats['other_date']} for other dates, {stats['no_game']} not matched to a game)")
    by_mk = {}
    for k, v in lines.items():
        by_mk[k.split("|")[1]] = by_mk.get(k.split("|")[1], 0) + len(v)
    out("   by market: " + ", ".join(f"{k} {v}" for k, v in sorted(by_mk.items())))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kalshi NHL goal-total lines (read-only)")
    ap.add_argument("command", choices=["rules", "fetch"])
    ap.add_argument("--dir", default=".")
    ap.add_argument("--date")
    ap.add_argument("--all-dates", action="store_true")
    ap.add_argument("--fee", type=float, default=0.07, help="Kalshi taker fee rate: fee per contract = rate x p x (1-p)")
    ap.add_argument("--max-spread", type=float, default=0.12)
    a = ap.parse_args(argv)
    if a.command == "rules":
        cmd_rules()
    else:
        cmd_fetch(a.dir, a.date, a.all_dates, a.fee, a.max_spread)


if __name__ == "__main__":
    main()
'''


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        module.__file__ = f"<nhl_platform:{name}.py>"
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
import nhl_goals_model as nhl                                                  # noqa: E402
import nhl_period_model as nper                                                # noqa: E402
import kalshi_lines as kal                                                     # noqa: E402
from nhl_io import FileLock, LockBusy, atomic_json_dump, load_json_retry      # noqa: E402

LINES_FILE = "nhl_lines.json"
STARTERS_FILE = "nhl_starters.json"
STATE_FILE = "nhl_refresh_state.json"
REFRESH_LOCK = "nhl_refresh.lock"
LOG_LOCK = "nhl_log.lock"
KALSHI_FILE = "nhl_lines_kalshi.json"
KALSHI_LOCK = "nhl_kalshi.lock"
BOOT_FILE = "nhl_bootstrap_state.json"
INPUT_KEYS_OK = ("total", "home", "away") + tuple(f"p{i}{w}" for i in (1, 2, 3) for w in ("total", "home", "away"))


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


class NHLPlatform:
    def __init__(self, directory=".", now_fn=None, fetch=None, log_predictions=True, min_refresh_interval_s=60,
                 tz_offset_hours=-8, kalshi_get=None):
        self.dir = directory
        self._now_fn = now_fn or (lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
        self._fetch = fetch or nhl.fetch_json
        self._kget = kalshi_get or kal.http_get
        self._plive = {}                      # season -> (key, period live tuple)
        self._kalshi_info = {"last": None}
        self._boot = {"status": "idle"}
        self._log = log_predictions
        self.min_refresh_interval_s = min_refresh_interval_s
        self.tz = tz_offset_hours
        self._lock = threading.RLock()
        self._live = {}                       # season -> (key, live tuple)
        self._inputs = {}                     # filename -> (key, data)
        self._score = (None, None)
        self._refresh_info = {"last": None}
        self._stop = threading.Event()

    # ----------------------------------------------------------------- helpers
    def _now(self):
        return self._now_fn()

    def today(self):
        return (self._now() + datetime.timedelta(hours=self.tz)).date().isoformat()

    def _p(self, name):
        return os.path.join(self.dir, name)

    def _read_input(self, name):
        path = self._p(name)
        key = _stat_key(path)
        hit = self._inputs.get(name)
        if hit and hit[0] == key:
            return hit[1]
        data = load_json_retry(path, {}) if os.path.exists(path) else {}
        data = data if isinstance(data, dict) else {}
        self._inputs[name] = (key, data)
        return data

    def _lines(self, override=None):
        """Typed lines + fetched Kalshi lines, combined into ladders (override replaces both)."""
        if override is not None:
            return override
        return nhl.merge_lines(self._read_input(LINES_FILE), self._read_input(KALSHI_FILE))

    def _live_state(self, season, params):
        start = int(season[:4])
        prev = f"{start - 1}{start}"
        files = [nhl.data_file(self.dir, season), nhl.box_file(self.dir, season),
                 nhl.data_file(self.dir, prev), nhl.box_file(self.dir, prev), self._p(nhl.PARAMS_NAME)]
        key = _stat_key(*files)
        hit = self._live.get(season)
        if hit and hit[0] == key:
            return hit[1]
        live = nhl.build_live_state(self.dir, season, params)
        self._live[season] = (key, live)
        return live

    # ----------------------------------------------------------------- READ path (never touches the network)
    def get_slate(self, date=None, lines=None, starters=None, season=None, log=None):
        try:
            with self._lock:
                date = date or self.today()
                season = season or nhl.season_code_for(date)
                params, validated = nhl.load_params(self.dir)
                live = self._live_state(season, params)
                lines = self._lines(lines)
                starters = starters if starters is not None else self._read_input(STARTERS_FILE)
                do_log = self._log if log is None else log
                kwargs = dict(lines=lines, bankroll=nhl.current_bankroll(self.dir, 1000.0), now=self._now(), log=do_log,
                              season=season, starters=starters, log_model_lines=True, live=live)
                if do_log:
                    try:
                        with FileLock(self._p(LOG_LOCK), timeout=10.0, stale_after=120):
                            s = nhl.slate(self.dir, date, **kwargs)
                    except LockBusy:
                        kwargs["log"] = False
                        s = nhl.slate(self.dir, date, **kwargs)
                        s["warnings"].append("prediction log busy; this call was not logged")
                else:
                    s = nhl.slate(self.dir, date, **kwargs)
            s["status"] = "ok"
            s["generated_at"] = self._now().isoformat(timespec="seconds") + "Z"
            s["data_info"] = {"games_saved": len(live[1]), "upcoming_saved": len(live[2]), "previous_season_loaded": live[3]}
            return clean(s)
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "games": []}

    def _period_live(self, season, params):
        start = int(season[:4])
        prev = f"{start - 1}{start}"
        key = _stat_key(nhl.data_file(self.dir, season), nhl.box_file(self.dir, season), nhl.data_file(self.dir, prev),
                        nhl.box_file(self.dir, prev), self._p(nper.PARAMS_NAME))
        hit = self._plive.get(season)
        if hit and hit[0] == key:
            return hit[1]
        live = nper.build_state(self.dir, season, params)
        self._plive[season] = (key, live)
        return live

    def get_periods(self, date=None, lines=None, season=None, log=None):
        """Goals by period (1st / 2nd / 3rd) for every not-yet-played game on `date`, plus priced period lines."""
        try:
            with self._lock:
                date = date or self.today()
                season = season or nhl.season_code_for(date)
                params, validated = nper.load_params(self.dir)
                live = self._period_live(season, params)
                do_log = self._log if log is None else log
                kwargs = dict(lines=self._lines(lines), bankroll=nhl.current_bankroll(self.dir, 1000.0), now=self._now(),
                              log=do_log, season=season, log_model_lines=True, live=live)
                if do_log:
                    try:
                        with FileLock(self._p(LOG_LOCK), timeout=10.0, stale_after=120):
                            s = nper.slate(self.dir, date, **kwargs)
                    except LockBusy:
                        kwargs["log"] = False
                        s = nper.slate(self.dir, date, **kwargs)
                        s["warnings"].append("prediction log busy; this call was not logged")
                else:
                    s = nper.slate(self.dir, date, **kwargs)
            s["status"] = "ok"
            s["generated_at"] = self._now().isoformat(timespec="seconds") + "Z"
            s["data_info"] = {"games_with_period_data": live[1], "upcoming_saved": len(live[2])}
            return clean(s)
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "games": []}

    def kalshi_status(self):
        try:
            path = self._p(KALSHI_FILE)
            data = load_json_retry(path, {}) if os.path.exists(path) else {}
            rungs = sum(len(v) for v in data.values() if isinstance(v, list))
            by = {}
            for k, v in data.items():
                by[k.split("|")[-1]] = by.get(k.split("|")[-1], 0) + (len(v) if isinstance(v, list) else 1)
            try:
                age = round(time.time() - os.stat(path).st_mtime, 0)
            except OSError:
                age = None
            return clean({"status": "ok", "lines_saved": rungs, "markets": len(data), "by_market": by, "file_age_s": age,
                          "stale_after_s": int(nhl.MAX_AUTO_AGE_HOURS * 3600), "last_fetch": self._kalshi_info["last"],
                          "note": "totals / team totals / period totals only; prices include an estimated taker fee"})
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def get_scorecard(self, start_bankroll=1000.0):
        try:
            with self._lock:
                key = _stat_key(self._p(nhl.LOG_NAME), *[self._p(f) for f in sorted(os.listdir(self.dir))
                                                          if f.startswith("nhl_goals_") and f.endswith(".json")])
                if self._score[0] == key and self._score[1] is not None:
                    return self._score[1]
                rep = clean(nhl.track_report(self.dir, start_bankroll))
                rep["status"] = "ok"
                self._score = (key, rep)
                return rep
        except Exception as e:
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def health(self):
        def age(path):
            try:
                return round(time.time() - os.stat(path).st_mtime, 0)
            except OSError:
                return None
        season = nhl.season_code_for(self.today())
        state = load_json_retry(self._p(STATE_FILE), {}) or {}
        params, validated = nhl.load_params(self.dir)
        saved = load_json_retry(nhl.data_file(self.dir, season), {}) or {}
        box = (load_json_retry(nhl.box_file(self.dir, season), {}) or {}).get("box", {})
        return clean({"status": "ok", "now": self._now().isoformat(timespec="seconds") + "Z", "season": season,
                      "params_validated": validated, "period_params_validated": nper.load_params(self.dir)[1],
                      "bootstrap": self._boot, "kalshi_file_age_s": age(self._p(KALSHI_FILE)),
                      "file_age_s": {"results": age(nhl.data_file(self.dir, season)), "boxscores": age(nhl.box_file(self.dir, season)),
                                     "prediction_log": age(self._p(nhl.LOG_NAME)), "params": age(self._p(nhl.PARAMS_NAME))},
                      "games_saved": len(saved.get("games", {})), "boxscores_saved": len(box),
                      "upcoming_saved": len(saved.get("upcoming", [])),
                      "last_refresh": self._refresh_info["last"] or state.get("last_refresh")})

    # ----------------------------------------------------------------- inputs (your lines / confirmed starters)
    def set_inputs(self, lines=None, starters=None):
        """Replace nhl_lines.json and/or nhl_starters.json (validated, written atomically)."""
        problems = []
        if lines is not None:
            for k, v in lines.items():
                parts = str(k).split("|")
                ok = len(parts) == 2 and "@" in parts[0] and parts[1] in INPUT_KEYS_OK
                one = lambda x: isinstance(x, (int, float)) or (isinstance(x, dict) and "line" in x)
                ok = ok and (one(v) or (isinstance(v, list) and v and all(one(x) for x in v)))
                if not ok:
                    problems.append(f"bad line entry {k!r}")
        if starters is not None:
            for k, v in starters.items():
                if not (isinstance(k, str) and len(k) == 3 and isinstance(v, str)):
                    problems.append(f"bad starter entry {k!r}")
        if problems:
            return {"status": "error", "warnings": problems}
        with self._lock:
            if lines is not None:
                atomic_json_dump(self._p(LINES_FILE), lines)
            if starters is not None:
                atomic_json_dump(self._p(STARTERS_FILE), starters)
        return {"status": "ok", "lines": None if lines is None else len(lines), "starters": None if starters is None else len(starters)}

    # ----------------------------------------------------------------- WRITE path
    def refresh_data(self, season=None, force=False):
        """Pull results, schedule and new boxscores. Never raises. Skips if refreshed < min_refresh_interval_s ago (unless force)."""
        started = time.time()
        lock = FileLock(self._p(REFRESH_LOCK), timeout=0.0, stale_after=3600)
        try:
            lock.__enter__()
        except LockBusy:
            return {"status": "busy"}
        info = {}
        try:
            season = season or nhl.season_code_for(self.today())
            state = load_json_retry(self._p(STATE_FILE), {}) or {}
            last = (state.get("last_refresh") or {}).get("finished_at_ts")
            if not force and last and time.time() - last < self.min_refresh_interval_s:
                return {"status": "skipped", "reason": "refreshed moments ago"}
            r = nhl.refresh_season(self.dir, season, fetch=self._fetch, log=_quiet)
            b = nhl.collect_boxscores(self.dir, season, fetch=self._fetch, log=_quiet, fill_periods=True)
            start = int(season[:4])
            prev = f"{start - 1}{start}"
            bp = None
            if os.path.exists(nhl.data_file(self.dir, prev)):
                bp = nhl.collect_boxscores(self.dir, prev, fetch=self._fetch, log=_quiet, limit=2000)
            ok = r["schedule_errors"] < len(nhl.NHL_TEAMS)
            info = {"status": "ok" if ok else "error", "season": season, "games": r["games"], "upcoming": r["upcoming"],
                    "schedule_errors": r["schedule_errors"], "boxscores_new": b["downloaded"], "boxscore_errors": b["errors"],
                    "boxscores_saved": b["total_saved"], "previous_season_boxscores_new": (bp or {}).get("downloaded", 0),
                    "finished_at": self._now().isoformat(timespec="seconds") + "Z", "finished_at_ts": time.time(),
                    "duration_s": round(time.time() - started, 1)}
            atomic_json_dump(self._p(STATE_FILE), {"last_refresh": info})
        except Exception as e:
            info = {"status": "error", "error": f"{type(e).__name__}: {e}",
                    "finished_at": self._now().isoformat(timespec="seconds") + "Z"}
        finally:
            lock.release()
        self._refresh_info["last"] = info
        return info

    def refresh_kalshi(self, fee_rate=0.07, max_spread=0.12):
        """Fetch Kalshi's NHL goal-total markets for ALL upcoming dates into nhl_lines_kalshi.json.  Never raises."""
        lock = FileLock(self._p(KALSHI_LOCK), timeout=0.0, stale_after=600)
        try:
            lock.__enter__()
        except LockBusy:
            return {"status": "busy"}
        try:
            msgs = []
            lines = kal.cmd_fetch(self.dir, self.today(), all_dates=True, fee_rate=fee_rate, max_spread=max_spread,
                                  get=self._kget, out=lambda *a: msgs.append(" ".join(str(x) for x in a)), now=self._now())
            info = {"status": "ok" if lines is not None else "error", "lines": sum(len(v) for v in (lines or {}).values()),
                    "message": msgs[0] if msgs else None, "finished_at": self._now().isoformat(timespec="seconds") + "Z"}
        except Exception as e:
            info = {"status": "error", "error": f"{type(e).__name__}: {e}", "finished_at": self._now().isoformat(timespec="seconds") + "Z"}
        finally:
            lock.release()
        self._kalshi_info["last"] = info
        return info

    def bootstrap(self, seasons=None, workers=4, log=print, retune=True):
        """One-time (and monthly) setup: download the seasons with period goals, then tune both models.  Resumable."""
        self._boot = {"status": "running", "started_at": self._now().isoformat(timespec="seconds") + "Z", "step": "starting"}
        try:
            cur = nhl.season_code_for(self.today())
            y = int(cur[:4])
            seasons = seasons or [f"{y - 2}{y - 1}", f"{y - 1}{y}", cur]
            for sc in seasons:
                self._boot["step"] = f"{sc}: schedule"
                log(f"{sc}: schedule")
                nhl.refresh_season(self.dir, sc, fetch=self._fetch, log=_quiet)
                self._boot["step"] = f"{sc}: boxscores + period goals"
                log(f"{sc}: boxscores + period goals")
                nhl.collect_boxscores(self.dir, sc, fetch=self._fetch, workers=workers, log=_quiet, fill_periods=True)
            if retune:
                have = [s for s in seasons if os.path.exists(nhl.data_file(self.dir, s))]
                self._boot["step"] = "tuning the game model"
                log("tuning the game model")
                nhl.backtest(self.dir, have, out=_quiet)
                self._boot["step"] = "tuning the period model"
                log("tuning the period model")
                nper.backtest(self.dir, have, out=_quiet)
            self._live.clear(); self._plive.clear()
            self._boot = {"status": "done", "finished_at": self._now().isoformat(timespec="seconds") + "Z", "seasons": seasons}
        except Exception as e:
            self._boot = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        atomic_json_dump(self._p(BOOT_FILE), self._boot)
        return self._boot

    def needs_bootstrap(self):
        return not (os.path.exists(self._p(nhl.PARAMS_NAME)) and os.path.exists(self._p(nper.PARAMS_NAME)))

    def start_background_refresh(self, interval_s=1800, first_delay_s=0, kalshi_interval_s=0, bootstrap=False):
        self._stop.clear()

        def loop():
            if bootstrap and self.needs_bootstrap():
                self.bootstrap()
            if first_delay_s:
                self._stop.wait(first_delay_s)
            while not self._stop.is_set():
                self.refresh_data()
                self._stop.wait(interval_s)

        def kloop():
            while not self._stop.is_set():
                self.refresh_kalshi()
                self._stop.wait(kalshi_interval_s)
        t = threading.Thread(target=loop, name="nhl-refresh", daemon=True)
        t.start()
        if kalshi_interval_s:
            threading.Thread(target=kloop, name="nhl-kalshi", daemon=True).start()
        return t

    def stop(self):
        self._stop.set()


# ===================================================================== HTTP
def serve(platform, host="127.0.0.1", port=8053, cors=False, allow_write=False, token=None):
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
            routes = {"/api/nhl/slate": lambda: platform.get_slate(q.get("date")),
                      "/api/nhl/periods": lambda: platform.get_periods(q.get("date")),
                      "/api/nhl/kalshi": platform.kalshi_status,
                      "/api/nhl/scorecard": platform.get_scorecard,
                      "/health": platform.health, "/api/nhl/health": platform.health}
            fn = routes.get(u.path)
            if fn is None:
                self._send({"status": "error", "warnings": ["not found"]}, 404)
            else:
                self._send(fn())

        def _authorised(self):
            if not token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in ("/api/nhl/inputs", "/api/nhl/refresh"):
                return self._send({"status": "error", "warnings": ["not found"]}, 404)
            if not allow_write:
                return self._send({"status": "error", "warnings": ["writes are disabled (start with --allow-write)"]}, 403)
            if not self._authorised():
                return self._send({"status": "error", "warnings": ["missing or wrong bearer token"]}, 401)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send({"status": "error", "warnings": ["body is not JSON"]}, 400)
            if u.path == "/api/nhl/refresh":
                what = body.get("what", "data")
                if what not in ("data", "kalshi"):
                    return self._send({"status": "error", "warnings": ["what must be 'data' or 'kalshi'"]}, 400)
                return self._send(platform.refresh_kalshi() if what == "kalshi" else platform.refresh_data(force=True))
            res = platform.set_inputs(body.get("lines"), body.get("starters"))
            self._send(res, 200 if res["status"] == "ok" else 400)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/api/nhl/slate, /api/nhl/periods, /api/nhl/scorecard, /api/nhl/kalshi, /health"
          f"{', POST /api/nhl/inputs, POST /api/nhl/refresh' if allow_write else ''}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


# ===================================================================== CLI
def _print_slate(s):
    print(f"\nNHL {s.get('date')}  [status {s.get('status')}]")
    for w in s.get("warnings") or []:
        print(f"  [warn] {w}")
    if s.get("games"):
        nhl.print_slate(s)


def _print_periods(s):
    print(f"\nNHL goals by period {s.get('date')}  [status {s.get('status')}]")
    for w in s.get("warnings") or []:
        print(f"  [warn] {w}")
    if s.get("games"):
        nper.print_slate(s)


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NHL platform: model, data, Kalshi prices, tracking, service -- one file")
    ap.add_argument("--dir", default=os.environ.get("NHL_DATA_DIR", "."), help="folder for the nhl_*.json files (default: $NHL_DATA_DIR or current)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("daily", "slate", "periods"):
        s = sub.add_parser(name)
        s.add_argument("--date", default=None)
        s.add_argument("--no-refresh", action="store_true")
        s.add_argument("--no-log", action="store_true")
    sub.add_parser("refresh")
    sub.add_parser("kalshi")
    sub.add_parser("track")
    bs = sub.add_parser("bootstrap")
    bs.add_argument("--seasons", default=None, help="comma-separated, default: the current and two previous seasons")
    bs.add_argument("--workers", type=int, default=4)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--cors", action="store_true")
    sv.add_argument("--allow-write", action="store_true")
    sv.add_argument("--bootstrap", action="store_true", help="if the tuned parameter files are missing, download + tune in the background")
    sv.add_argument("--refresh-minutes", type=int, default=int(os.environ.get("NHL_REFRESH_MINUTES", "30")))
    sv.add_argument("--kalshi-minutes", type=int, default=int(os.environ.get("NHL_KALSHI_MINUTES", "10")))
    r = sub.add_parser("run", help="run an embedded module's own command line")
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
        if not hasattr(mod, "main"):
            sys.exit(f"{args.module} has no command line")
        rest = [a for a in args.rest if a != "--"]
        if "--dir" not in rest and args.module != "nhl_io":
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
    p = NHLPlatform(args.dir, log_predictions=not getattr(args, "no_log", False))
    if args.cmd == "refresh":
        print(json.dumps(p.refresh_data(force=True), indent=2))
    elif args.cmd == "kalshi":
        print(json.dumps(p.refresh_kalshi(), indent=2))
    elif args.cmd == "track":
        nhl.print_track(nhl.track_report(args.dir))
    elif args.cmd == "bootstrap":
        res = p.bootstrap([x.strip() for x in args.seasons.split(",")] if args.seasons else None, workers=args.workers)
        print(json.dumps(res, indent=2))
    elif args.cmd == "serve":
        port = args.port or int(os.environ.get("PORT", "8053"))
        host = args.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        allow = args.allow_write or _env_flag("NHL_ALLOW_WRITE")
        token = os.environ.get("NHL_API_TOKEN") or None
        if allow and not token and host != "127.0.0.1":
            print("WARNING: writes are enabled on a public address without NHL_API_TOKEN: anyone who can reach it can change your lines.",
                  flush=True)
        if args.refresh_minutes > 0:
            p.start_background_refresh(args.refresh_minutes * 60, kalshi_interval_s=max(0, args.kalshi_minutes) * 60,
                                       bootstrap=args.bootstrap or _env_flag("NHL_BOOTSTRAP"))
        serve(p, host, port, args.cors or _env_flag("NHL_CORS"), allow, token)
    else:
        if args.cmd == "daily" and not args.no_refresh:
            print(json.dumps(p.refresh_data(force=True), indent=2))
            print(json.dumps(p.refresh_kalshi(), indent=2))
        elif args.cmd != "daily" and not args.no_refresh and args.cmd == "slate":
            pass
        if args.cmd in ("daily", "slate"):
            _print_slate(p.get_slate(args.date))
        if args.cmd in ("daily", "periods"):
            _print_periods(p.get_periods(args.date))


if __name__ == "__main__":
    main()
