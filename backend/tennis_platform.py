#!/usr/bin/env python
"""
tennis_platform.py  --  ONE FILE for the ATP tennis simulator: data, ratings, predictions, live Kalshi prices, logging, tracking, web service.

Embedded, unchanged and individually tested:
  tennis_engine       exact point -> game -> tiebreak -> set -> match Markov engine (winner, set score, total games, margin)
  tennis_model        serve/return ratings (decayed, shrunk, opponent- and surface-adjusted), Elo, moneyline blend, backtest
  kalshi_tennis_live  Kalshi's OPEN ATP markets (moneyline, total games, total sets): public, read-only, no key, NO trading
  tennis_io           atomic JSON writes, tolerant reads, cross-process file lock
plus the platform layer below: one class, TennisPlatform, with the read / write paths a website needs, and a built-in web page.

WHAT YOU GET per match Kalshi currently lists (open markets ARE the slate):
  moneyline win probability for both players (simulator + Elo blend) next to Kalshi's bid / ask / mid,
  total-games Over probability for every Kalshi rung (and the model's fair line and expected games) next to Kalshi's prices,
  total-sets Over probability for every Kalshi rung and the full set-score distribution,
  the fee-inclusive "edge" arithmetic Kalshi's prices imply (informational), and warnings (thin history, prices stale, maybe in play).

STATUS -- READ THIS.  Validated against RESULTS (5,000+ ATP matches: beats Elo on log loss, beats climatology on games and sets) and then
against KALSHI PRICES (2,773 matches, Aug 2025 - Oct 2026, pre-match prices, fees and 1c slippage included):
    moneyline  model WORSE than the market (Brier +0.011, se 0.002); flat bets lose 5-8%.
    total games  Brier tie (+0.005, se 0.003); information weight 0.32 (se 0.14); bet ROI within noise (-3% .. +3%).
    total sets   too few markets (100) to say; set spreads: model worse (not shown).
  => THIS IS A REFERENCE / RESEARCH DISPLAY, NOT A BETTING SYSTEM.  No stake sizing is produced.  Every prediction made before
  a match is logged with Kalshi's price, and get_scorecard() settles them against Kalshi's results, so the live record builds itself.
NOT COVERED: WTA, Challenger, qualifying draws (the ATP data has none -> players without history are reported 'unrated'), doubles,
  injuries / withdrawals / form news, in-play pricing (prices for matches already under way are flagged 'maybe_in_play').

------------------------------------------------------------------ WIRING (library)
    from tennis_platform import TennisPlatform
    tp = TennisPlatform("/data")
    tp.refresh_data()          # WRITE path: ATP results (stats.tennismylife.org).  Never raises.
    tp.refresh_kalshi()        # WRITE path: Kalshi open markets -> tennis_kalshi_open.json, logs pre-match predictions.  Never raises.
    tp.refresh_results()       # WRITE path: settle logged markets from Kalshi.  Never raises.
    tp.get_slate(date=None)    # every open Kalshi ATP match with model + market (cheap, cached files only, no network)
    tp.predict("Carlos Alcaraz", "Jannik Sinner", "Hard", 3, lines=[21.5, 22.5])     # any matchup, no Kalshi needed
    tp.get_scorecard(); tp.health()
  Every read returns a JSON-safe dict with "status" ("ok" | "error") and "warnings" and never raises.

------------------------------------------------------------------ HTTP (stdlib only) -- local or Railway
    python tennis_platform.py serve [--dir PATH] [--port N] [--host H] [--data-minutes 180] [--kalshi-minutes 5] [--cors] [--allow-write] [--bootstrap]
      GET  /                                   the built-in page (auto-refreshing)
      GET  /api/tennis/slate?date=2026-10-13   GET /api/tennis/predict?p1=..&p2=..&surface=Hard&best_of=3&lines=21.5,22.5
      GET  /api/tennis/scorecard               GET /api/tennis/kalshi      GET /health  (also /api/tennis/health)
      POST /api/tennis/refresh  {"what": "data" | "kalshi" | "results"}        (needs --allow-write)
  Environment variables (used when the flag is not given):  PORT (Railway sets it; the server then binds 0.0.0.0),
    TENNIS_DATA_DIR (a Railway volume, e.g. /data), TENNIS_API_TOKEN (if set, POSTs need  Authorization: Bearer <token>),
    TENNIS_DATA_MINUTES, TENNIS_KALSHI_MINUTES, TENNIS_ALLOW_WRITE=1, TENNIS_CORS=1, TENNIS_BOOTSTRAP=1.

------------------------------------------------------------------ RAILWAY, step by step
  1. New repo (or folder) containing ONLY this file (plus nothing else; it needs no packages). Add a Volume mounted at /data.
  2. Start command:   python tennis_platform.py serve --bootstrap
     Variables:       TENNIS_DATA_DIR=/data   TENNIS_API_TOKEN=<long random string>   TENNIS_ALLOW_WRITE=1   TENNIS_CORS=1 (if another site calls the API)
  3. First boot with an empty volume: --bootstrap downloads 2016-now of ATP results (about 11 small files, a minute), builds ratings,
     and starts pulling Kalshi.  Until the data lands /health shows "needs_bootstrap": true and the slate says 'no match data'.
  4. Health check path: /health.   Your dashboard calls /api/tennis/slate (JSON) or you can open the service URL for the built-in page.
  5. It then runs itself: Kalshi prices every 5 minutes, ATP results every 3 hours, settlement of logged predictions hourly.
     Nothing to tune: the model parameters are the fitted ones (override with a tennis_params.json in the data dir after a re-fit).

------------------------------------------------------------------ COMMAND LINE (run from the tennis folder)
    python tennis_platform.py bootstrap                download all ATP results 2016-now
    python tennis_platform.py daily                    refresh data + Kalshi + results, print the slate, log predictions
    python tennis_platform.py slate | kalshi | refresh | results | track
    python tennis_platform.py predict --p1 "Carlos Alcaraz" --p2 "Jannik Sinner" --surface Hard --best-of 3 [--lines 21.5,22.5]
    python tennis_platform.py run tennis_model backtest --tour atp --burn-in-until 20190101 --test-from 20240101     (needs numpy)
    python tennis_platform.py unpack OUTDIR            write the embedded modules out as .py files

FILES (in the data dir): atp_matches_{year}.csv (results), tennis_kalshi_open.json (latest Kalshi pull), tennis_log.jsonl (every logged
  pre-match prediction + price), tennis_results.json (settled outcomes), tennis_refresh_state.json, optional tennis_params.json (re-fit
  parameters) and tennis_overrides.json ({"shanghai": "Hard"} surface overrides by tournament name).
Needs: python 3.9+ and nothing else (numpy only for `run tennis_model backtest`).
"""

import argparse
import datetime
import importlib.abc
import importlib.util
import json
import math
import os
import re
import sys
import threading
import unicodedata
import urllib.request
import zlib
from collections import Counter, defaultdict

# ===================================================================== embedded modules
_ORDER = ['tennis_io', 'tennis_engine', 'tennis_model', 'kalshi_tennis_live']
_SRC = {}
# ======================================================================
# embedded module: tennis_io.py (96 lines)
# ======================================================================
_SRC["tennis_io"] = r'''"""
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
# embedded module: tennis_engine.py (180 lines)
# ======================================================================
_SRC["tennis_engine"] = r'''"""Exact point->game->tiebreak->set->match engine (no Monte Carlo).

pA = P(A wins a point on A's serve), pB = P(B wins a point on B's serve).
Outputs the joint distribution of (winner, sets score, total games, game margin).
Assumes iid points given (pA, pB); callers add parameter-uncertainty mixing.
"""
from functools import lru_cache


def game_win(p):
    q = 1.0 - p
    return p ** 4 * (1 + 4 * q + 10 * q * q) + 20 * p ** 3 * q ** 3 * p * p / (1 - 2 * p * q)


@lru_cache(maxsize=None)
def _tb(pa_serve, pb_serve, first_is_a, target):
    """P(A wins tiebreak to `target`, win by 2). first_is_a: A serves point 1."""
    qa = pa_serve          # A wins a point when A serves
    qb = 1.0 - pb_serve    # A wins a point when B serves
    MAXP = 90
    cur = {(0, 0): 1.0}
    win = 0.0
    lose = 0.0
    for i in range(MAXP):
        server_is_first = ((i + 1) // 2) % 2 == 0
        a_serves = server_is_first if first_is_a else (not server_is_first)
        pa = qa if a_serves else qb
        nxt = {}
        for (a, b), pr in cur.items():
            for da, pp in ((1, pa), (0, 1 - pa)):
                na, nb = a + da, b + (1 - da)
                w = pr * pp
                if na >= target and na - nb >= 2:
                    win += w
                elif nb >= target and nb - na >= 2:
                    lose += w
                else:
                    # collapse deep deuce states to their tie-break-equivalent offset
                    if na >= target - 1 and nb >= target - 1:
                        m = min(na, nb) - (target - 1)
                        na, nb = na - m, nb - m
                    nxt[(na, nb)] = nxt.get((na, nb), 0.0) + w
        cur = nxt
        if sum(cur.values()) < 1e-12:
            break
    tot = sum(cur.values())
    return win / (win + lose) if (win + lose) > 0 else 0.5


def tb_win_a(pa_serve, pb_serve, first_is_a, target=7):
    # exact up to truncation tail (<1e-12); renormalise by probability resolved
    return _tb(round(pa_serve, 4), round(pb_serve, 4), first_is_a, target)


@lru_cache(maxsize=200000)
def set_dist(pa, pb, a_first, tb_target=7, tb_last=False):
    """Distribution over set outcomes.
    Returns tuple of ((a_won, gA, gB, next_a_first), prob). Tiebreak counts as 1 game
    for server rotation but 7-6 counts 13 games total (7+6)."""
    hA, hB = game_win(pa), game_win(pb)
    out = {}
    # explicit forward DP by game count
    layer = {(0, 0): 1.0}
    for n in range(0, 13):
        new = {}
        for (a, b), pr in layer.items():
            a_serves = (a_first if n % 2 == 0 else (not a_first))
            pA_game = hA if a_serves else 1.0 - hB
            for a_win, pg in ((True, pA_game), (False, 1.0 - pA_game)):
                na, nb = (a + 1, b) if a_win else (a, b + 1)
                w = pr * pg
                done = None
                if na >= 6 and na - nb >= 2:
                    done = True
                elif nb >= 6 and nb - na >= 2:
                    done = False
                elif na == 7 and nb == 5:
                    done = True
                elif nb == 7 and na == 5:
                    done = False
                if done is not None:
                    games = na + nb
                    nf = a_first if games % 2 == 0 else (not a_first)
                    k = (done, na, nb, nf)
                    out[k] = out.get(k, 0.0) + w
                else:
                    new[(na, nb)] = new.get((na, nb), 0.0) + w
        layer = new
        # tiebreak at 6-6 reached after 12 games
        if n == 11 and (6, 6) in layer:
            pr = layer.pop((6, 6))
            tb_first_a = a_first  # 12 games played -> same first server as game 1
            pt = tb_win_a(pa, pb, tb_first_a, tb_target)
            nf = (not a_first)    # receiver of first TB point serves next set; 13 games odd
            out[(True, 7, 6, nf)] = out.get((True, 7, 6, nf), 0.0) + pr * pt
            out[(False, 6, 7, nf)] = out.get((False, 6, 7, nf), 0.0) + pr * (1 - pt)
    return tuple(out.items())


def match_dist(pa, pb, a_first=True, best_of=3, tb_target=7, final_tb_target=None, mom=0.0):
    """Returns dict with p_a_win; sets {(sa,sb):p}; games pmf; margin pmf; sg {(sa,sb,games):p};
    p_set1_winner_wins.  mom = set-to-set momentum: when A leads in sets by L, A's serve prob gets
    +mom*L and B's gets -mom*L (and vice versa), so sets are positively dependent."""
    if final_tb_target is None:
        final_tb_target = tb_target
    need = best_of // 2 + 1
    pa = round(pa, 4)
    pb = round(pb, 4)
    state = {(0, 0, 0, 0, a_first, None): 1.0}   # (sa, sb, games, margin, a_first_next, set1_winner_is_a)
    final = {}
    for _ in range(best_of):
        nxt = {}
        for (sa, sb, g, m, af, fs), pr in state.items():
            if sa == need or sb == need:
                final[(sa, sb, g, m, fs)] = final.get((sa, sb, g, m, fs), 0.0) + pr
                continue
            is_final = (sa == need - 1 and sb == need - 1)
            tbt = final_tb_target if is_final else tb_target
            lead = sa - sb
            pa_e = min(.95, max(.2, pa + mom * lead)) if mom else pa
            pb_e = min(.95, max(.2, pb - mom * lead)) if mom else pb
            for (aw, gA, gB, nf), pset in set_dist(round(pa_e, 4), round(pb_e, 4), af, tbt):
                nfs = fs if fs is not None else aw
                k = (sa + (1 if aw else 0), sb + (0 if aw else 1), g + gA + gB, m + gA - gB, nf, nfs)
                nxt[k] = nxt.get(k, 0.0) + pr * pset
        state = nxt
    for (sa, sb, g, m, af, fs), pr in state.items():
        final[(sa, sb, g, m, fs)] = final.get((sa, sb, g, m, fs), 0.0) + pr
    tot = sum(final.values())
    res = {"p_a_win": 0.0, "sets": {}, "games": {}, "margin": {}, "sg": {}, "p_set1_winner_wins": 0.0}
    for (sa, sb, g, m, fs), pr in final.items():
        pr /= tot
        a_won = sa == need
        res["sg"][(sa, sb, g, fs)] = res["sg"].get((sa, sb, g, fs), 0.0) + pr
        if a_won:
            res["p_a_win"] += pr
        if fs is not None and fs == a_won:
            res["p_set1_winner_wins"] += pr
        res["sets"][(sa, sb)] = res["sets"].get((sa, sb), 0.0) + pr
        res["games"][g] = res["games"].get(g, 0.0) + pr
        res["margin"][m] = res["margin"].get(m, 0.0) + pr
    return res


def mix_match(pa, pb, a_first_prob=0.5, best_of=3, tb_target=7, final_tb_target=None, shifts=((0.0, 0.0, 1.0),), mom=0.0):
    """Parameter-uncertainty mixture. shifts = ((da, db, weight), ...): serve-point probs become
    (pa+da, pb+db). Averages over who serves first (a_first_prob for A)."""
    agg = {"p_a_win": 0.0, "sets": {}, "games": {}, "margin": {}, "sg": {}, "p_set1_winner_wins": 0.0}
    for da, db, w in shifts:
        for af, wf in ((True, a_first_prob), (False, 1 - a_first_prob)):
            if wf <= 0:
                continue
            r = match_dist(min(.95, max(.2, pa + da)), min(.95, max(.2, pb + db)), af, best_of, tb_target,
                           final_tb_target, mom)
            ww = w * wf
            agg["p_a_win"] += ww * r["p_a_win"]
            agg["p_set1_winner_wins"] += ww * r["p_set1_winner_wins"]
            for key in ("sets", "games", "margin", "sg"):
                for k, v in r[key].items():
                    agg[key][k] = agg[key].get(k, 0.0) + ww * v
    return agg


def p_over(games_pmf, line):
    return sum(p for g, p in games_pmf.items() if g > line)


def pmf_mean(pmf):
    return sum(k * v for k, v in pmf.items())


def pmf_median_line(pmf):
    """Half-integer line closest to the median (P(over) nearest 50%)."""
    best = None
    for g in sorted(pmf):
        line = g + 0.5
        d = abs(p_over(pmf, line) - 0.5)
        if best is None or d < best[0]:
            best = (d, line)
    return best[1]
'''
# ======================================================================
# embedded module: tennis_model.py (725 lines)
# ======================================================================
_SRC["tennis_model"] = r'''"""
tennis_model.py -- tennis match simulator: moneyline, total-games O/U, sets O/U.

Data: Jeff Sackmann's TML-Database (ATP; Sackmann's repos were removed from GitHub).
  python tennis_model.py fetch    --tour atp --years 2016-2026
  python tennis_model.py backtest --tour atp --burn-in-until 20190101 --test-from 20230101
  python tennis_model.py predict  --tour atp --p1 "Carlos Alcaraz" --p2 "Jannik Sinner" --surface Hard --best-of 3
  python tennis_model.py slate    --tour atp --csv slate.csv        (columns: p1,p2,surface,best_of[,line_games])

Model: each player has serve-point and return-point deviations (shrunk, time-decayed, opponent-adjusted,
surface-aware).  P(A wins a point on serve vs B) = mu_surface + srvdev_A - retdev_B.  Those two
point probabilities feed an exact Markov engine (tennis_engine.py) that yields the full joint
distribution of winner, set score, total games and game margin.  A small parameter-uncertainty mixture
(fit on a pre-test window) widens the totals distribution; an Elo-blended logistic recalibrates the moneyline.
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tennis_engine as eng

DATA_DIR = "tennis_data"
PARAMS_FILE = "tennis_params.json"
SURF = {"Hard": "Hard", "Clay": "Clay", "Grass": "Grass", "Carpet": "Hard"}

# ----------------------------------------------------------------------------- data
SOURCES = {
    # Jeff Sackmann removed tennis_atp / tennis_wta from GitHub (2026); TML-Database is a maintained derivative
    # with the same columns (ATP only).
    "tml": ("https://raw.githubusercontent.com/Tennismylife/TML-Database/master/{y}.csv", "atp_matches_{y}.csv"),
    "tmlsite": ("https://stats.tennismylife.org/data/{y}.csv", "atp_matches_{y}.csv"),  # kept current by TML (GitHub copy froze Jan 2026)
    "sackmann": ("https://raw.githubusercontent.com/JeffSackmann/tennis_{tour}/master/{tour}_matches_{y}.csv",
                 "{tour}_matches_{y}.csv"),
}
REQUIRED = ["tourney_date", "surface", "best_of", "score", "winner_id", "loser_id", "winner_name", "loser_name",
            "w_svpt", "w_1stWon", "w_2ndWon", "l_svpt", "l_1stWon", "l_2ndWon", "match_num"]


def fetch(tour, years, chall=False, out=DATA_DIR, source="tml"):
    import requests
    os.makedirs(out, exist_ok=True)
    url_t, name_t = SOURCES[source]
    ok = 0
    for y in years:
        url = url_t.format(y=y, tour=tour)
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            print(f"  {y}: HTTP {r.status_code} ({url})")
            continue
        text = r.content.decode("utf-8", errors="replace")
        header = text.split("\n", 1)[0].strip().lstrip("\ufeff").split(",")
        miss = [c for c in REQUIRED if c not in header]
        open(os.path.join(out, name_t.format(y=y, tour=tour)), "wb").write(r.content)
        ok += 1
        print(f"  {y}: {len(r.content)//1024} KB, {text.count(chr(10))} lines"
              + (f"   [WARNING missing columns: {miss}]" if miss else ""))
    if not ok:
        print("\nNothing downloaded. Check the URL in a browser, then paste me what you see.")


SET_RE = re.compile(r"^(\d+)-(\d+)(?:\(\d+\))?$")


def parse_score(score, best_of):
    """-> dict(complete, sets=[(w,l)], total_games, winner_sets, loser_sets) or None if unusable."""
    if not score:
        return None
    toks = score.replace(" ", " ").split()
    sets = []
    for t in toks:
        if t.upper() in ("RET", "W/O", "DEF", "DEF.", "ABD", "ABN", "UNK", "WO"):
            return {"complete": False}
        if t.startswith("["):
            return {"complete": False, "supertb": True}
        m = SET_RE.match(t)
        if not m:
            return {"complete": False}
        sets.append((int(m.group(1)), int(m.group(2))))
    need = best_of // 2 + 1
    ws = sum(1 for a, b in sets if a > b)
    ls = sum(1 for a, b in sets if b > a)
    if ws != need or ls >= need or ws + ls != len(sets):
        return {"complete": False}
    return {"complete": True, "sets": sets, "total_games": sum(a + b for a, b in sets),
            "winner_sets": ws, "loser_sets": ls}


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def load_rows(tour, data_dir=DATA_DIR, pattern=None):
    files = sorted(glob.glob(os.path.join(data_dir, pattern or f"{tour}_matches_*.csv")))
    files += sorted(glob.glob(os.path.join(data_dir, "atp_matches_qual_chall_*.csv"))) if tour == "atp" and pattern is None else []
    rows, seen = [], set()
    for fp in files:
        with open(fp, encoding="utf-8", errors="replace", newline="") as fh:
            for r in csv.DictReader(fh):
                key = (r.get("tourney_id"), r.get("match_num"), r.get("winner_id"), r.get("loser_id"))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    date = int(r["tourney_date"])
                except (KeyError, ValueError):
                    continue
                bo = int(_f(r.get("best_of")) or 3)
                surf = SURF.get(r.get("surface") or "")
                ws = ls = None
                w_sv, l_sv = _f(r.get("w_svpt")), _f(r.get("l_svpt"))
                if w_sv and l_sv and w_sv > 0 and l_sv > 0:
                    try:
                        w1, w2 = float(r["w_1stWon"]), float(r["w_2ndWon"])
                        l1, l2 = float(r["l_1stWon"]), float(r["l_2ndWon"])
                        ws, ls = (w_sv, w1 + w2), (l_sv, l1 + l2)
                    except (KeyError, ValueError):
                        ws = ls = None
                rows.append({
                    "date": date, "mnum": int(_f(r.get("match_num")) or 0), "surface": surf, "best_of": bo,
                    "level": r.get("tourney_level"), "tourney": r.get("tourney_name"),
                    "w_id": r["winner_id"], "l_id": r["loser_id"],
                    "w_name": r.get("winner_name"), "l_name": r.get("loser_name"),
                    "w_rank": _f(r.get("winner_rank")), "l_rank": _f(r.get("loser_rank")), "minutes": _f(r.get("minutes")),
                    "w_srv": ws, "l_srv": ls, "score": parse_score(r.get("score"), bo),
                    "raw_score": r.get("score")})
    rows.sort(key=lambda x: (x["date"], x["mnum"]))
    return rows


def days(d):
    y, m, dd = d // 10000, (d // 100) % 100, d % 100
    return y * 365.25 + (m - 1) * 30.44 + dd


# ----------------------------------------------------------------------------- ratings
class Ratings:
    """Serve/return deviations: evidence-weighted, exponentially time-decayed, opponent adjusted."""

    def __init__(self, k_all=1500.0, k_surf=900.0, tau_days=730.0, prior_mu=0.64):
        self.k_all, self.k_surf, self.tau = k_all, k_surf, tau_days
        self.acc = defaultdict(lambda: [0.0, 0.0, 0.0])   # (pid,kind,surf) -> [w, wt, last_day]
        self.mu_sum = defaultdict(lambda: [0.0, 0.0])     # surface -> [pts won, pts]
        self.prior_mu = prior_mu
        self.n_matches = defaultdict(int)

    def mu(self, surf):
        a = self.mu_sum[surf]
        return (a[0] + 400 * self.prior_mu) / (a[1] + 400)

    def _read(self, key, day):
        a = self.acc[key]
        if a[2] and day > a[2]:
            f = math.exp(-(day - a[2]) / self.tau)
            a[0] *= f; a[1] *= f
        a[2] = max(a[2], day)
        return a

    def dev(self, pid, kind, surf, day):
        al = self._read((pid, kind, "all"), day)
        d_all = al[1] / (al[0] + self.k_all)
        if not surf:
            return d_all
        sf = self._read((pid, kind, surf), day)
        return (sf[1] + self.k_surf * d_all) / (sf[0] + self.k_surf)

    def point_probs(self, a, b, surf, date):
        day = days(date)
        mu = self.mu(surf) if surf else self.prior_mu
        sa, ra = self.dev(a, "srv", surf, day), self.dev(a, "ret", surf, day)
        sb, rb = self.dev(b, "srv", surf, day), self.dev(b, "ret", surf, day)
        pa = min(.9, max(.35, mu + sa - rb))
        pb = min(.9, max(.35, mu + sb - ra))
        return pa, pb

    def update(self, m):
        ws, ls = m["w_srv"], m["l_srv"]
        if not ws or not ls:
            return
        if m["score"] and not m["score"].get("complete") and m["score"] is not None:
            return                                   # retirements: partial stats, skip
        surf, day = m["surface"], days(m["date"])
        mu = self.mu(surf) if surf else self.prior_mu
        w, l = m["w_id"], m["l_id"]
        # pre-match devs of both players (used as the opponent adjustment)
        dev = {(p, k): self.dev(p, k, surf, day) for p in (w, l) for k in ("srv", "ret")}
        for (srv_id, ret_id, (n, won)) in ((w, l, ws), (l, w, ls)):
            r_srv = won / n
            ev_srv = r_srv - mu + dev[(ret_id, "ret")]            # server evidence
            ev_ret = (1 - r_srv) - (1 - mu) + dev[(srv_id, "srv")]  # returner evidence
            for pid, kind, ev in ((srv_id, "srv", ev_srv), (ret_id, "ret", ev_ret)):
                for s in ("all", surf):
                    if s is None:
                        continue
                    a = self._read((pid, kind, s), day)
                    a[0] += n; a[1] += n * ev
            ms = self.mu_sum[surf] if surf else self.mu_sum["none"]
            ms[0] += won; ms[1] += n
        self.n_matches[w] += 1; self.n_matches[l] += 1


class Elo:
    def __init__(self):
        self.r = defaultdict(lambda: 1500.0)
        self.rs = defaultdict(lambda: 1500.0)
        self.n = defaultdict(int)

    def rating(self, p, surf):
        return 0.5 * self.r[p] + 0.5 * self.rs[(p, surf)] if surf else self.r[p]

    def p(self, a, b, surf):
        return 1 / (1 + 10 ** (-(self.rating(a, surf) - self.rating(b, surf)) / 400))

    def update(self, w, l, surf):
        pw = self.p(w, l, surf)
        for pid, sgn in ((w, 1), (l, -1)):
            k = 250 / (self.n[pid] + 5) ** 0.4
            self.r[pid] += sgn * k * (1 - pw)
            if surf:
                self.rs[(pid, surf)] += sgn * k * (1 - pw)
        self.n[w] += 1; self.n[l] += 1


# ----------------------------------------------------------------------------- distributions
GH_NODES = (-math.sqrt(3.0), 0.0, math.sqrt(3.0))     # 3-point Gauss-Hermite: mean 0, variance 1
GH_W = (1 / 6, 2 / 3, 1 / 6)


def shifts_for(par):
    """par: dict(off, sc, sd).  off = constant serve-prob offset applied to both players;
    sc = sd of a match-level COMMON serve shock (both players, same sign: conditions);
    sd = sd of a match-level STRENGTH-GAP shock (A up, B down: form/matchup).  Returns ((da, db, w), ...)."""
    off, sc, sd = par.get("off", 0.0), par.get("sc", 0.0), par.get("sd", 0.0)
    out = []
    for ic, nc in enumerate(GH_NODES if sc > 0 else (0.0,)):
        for idd, nd in enumerate(GH_NODES if sd > 0 else (0.0,)):
            w = (GH_W[ic] if sc > 0 else 1.0) * (GH_W[idd] if sd > 0 else 1.0)
            c, d = nc * sc, nd * sd
            out.append((off + c + d, off + c - d, w))
    return tuple(out)


def match_pmfs(pa, pb, best_of, par, tb=7, final_tb=None):
    mom = par.get("mom5", par.get("mom", 0.0)) if best_of == 5 else par.get("mom", 0.0)
    return eng.mix_match(pa, pb, 0.5, best_of, tb, final_tb, shifts_for(par), mom)


def logit(p):
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def fit_logistic(X, y, l2=1e-3, iters=50):
    import numpy as np
    X = np.array(X, float); y = np.array(y, float)
    X = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        z = X @ w
        p = 1 / (1 + np.exp(-z))
        g = X.T @ (p - y) + l2 * w
        H = (X.T * (p * (1 - p))) @ X + l2 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return [float(v) for v in w]


# ----------------------------------------------------------------------------- walk-forward pass
def walk_forward(rows, burn_in_until, rating_kw=None):
    """Chronological pass; predictions recorded BEFORE each match updates anything."""
    R, E = Ratings(**(rating_kw or {})), Elo()
    recs = []
    for m in rows:
        a, b = m["w_id"], m["l_id"]      # a = winner (model is label-symmetric)
        if m["date"] >= burn_in_until:
            pa, pb = R.point_probs(a, b, m["surface"], m["date"])
            wr, lr = m["w_rank"], m["l_rank"]
            recs.append({"m": m, "pa": pa, "pb": pb, "elo": E.p(a, b, m["surface"]),
                         "rank": None if (wr is None or lr is None) else math.log(lr / wr),
                         "n_w": R.n_matches[a], "n_l": R.n_matches[b]})
        R.update(m)
        E.update(a, b, m["surface"])
    return recs, R, E


def eligible(r, min_prior=10):
    return r["n_w"] >= min_prior and r["n_l"] >= min_prior


def _compact(r):
    s = r["m"]["score"]
    return (r["pa"], r["pb"], r["m"]["best_of"], s["winner_sets"], s["loser_sets"], s["total_games"],
            s["sets"][0][0] > s["sets"][0][1])


def _moment_rows(samp, par):
    """Per-match vectors of (model value, actual value) for each moment, grouped by best_of."""
    out = {3: [], 5: []}
    for pa, pb, bo, ws, ls, tg, fs_w in samp:
        res = match_pmfs(pa, pb, bo, par)
        pw = res["p_a_win"]                      # A = actual winner
        mg = eng.pmf_mean(res["games"])
        if bo == 3:
            p_long = res["sets"].get((2, 1), 0.0) + res["sets"].get((1, 2), 0.0)
            a_long = 1.0 if ls == 1 else 0.0
        else:
            p_long = sum(v for (x, y), v in res["sets"].items() if x + y >= 4)
            a_long = 1.0 if ws + ls >= 4 else 0.0
        out[bo].append(((mg, float(tg)), (p_long, a_long), (res["p_set1_winner_wins"], 1.0 if fs_w else 0.0),
                        ((1 - pw) * (2 * pw - 1), 0.0)))
    return out


MOMENT_NAMES = ("mean total games", "P(long match: 3 sets bo3 / 4+ bo5)", "P(set-1 winner wins)", "win-prob sharpness")


def _eval_candidate(args):
    """Chi-square of model-vs-actual aggregate moments (fit window only). Lower is better."""
    samp, par, bos = args
    samp = [s for s in samp if s[2] in bos]
    rows = _moment_rows(samp, par)
    chi, detail = 0.0, []
    for bo, lst in rows.items():
        if len(lst) < 80:
            continue
        for k in range(4):
            d = [x[k][0] - x[k][1] for x in lst]
            n = len(d)
            mean = sum(d) / n
            if k == 3:                                   # sharpness: mean of the zero-mean moment itself
                vals = [x[k][0] for x in lst]
                mean = sum(vals) / n
                var = sum((v - mean) ** 2 for v in vals) / (n - 1)
            else:
                # SE of the actual-minus-model mean: use the variance of the ACTUAL per-match values
                act = [x[k][1] for x in lst]
                am = sum(act) / n
                var = sum((v - am) ** 2 for v in act) / (n - 1)
            se = math.sqrt(var / n) or 1e-9
            z = mean / se
            chi += z * z
            detail.append((bo, MOMENT_NAMES[k], mean, se))
    return chi, detail


def _eval_many(samp, pars, workers, bos=(3, 5)):
    jobs = [(samp, p, bos) for p in pars]
    if workers > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=workers) as ex:
                return list(ex.map(_eval_candidate, jobs))
        except Exception as e:                       # fall back to serial rather than fail the fit
            print(f"  [note] parallel fit unavailable ({type(e).__name__}); running serially")
    return [_eval_candidate(j) for j in jobs]


def fit_params(fit_recs, max_sample=1500, passes=2, workers=None):
    """Method-of-moments fit on the fit window only: choose (mom, sd, off) so that model-predicted
    mean total games, P(long match), P(set-1 winner wins) and win-prob sharpness match what actually
    happened (chi-square in standard errors).  mom = set-to-set momentum, sd = match-level strength-gap
    shock, off = serve-probability offset.  Then the moneyline logistic."""
    if workers is None:
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    comp3 = [r for r in fit_recs if r["m"]["score"] and r["m"]["score"].get("complete") and r["m"]["best_of"] == 3]
    comp5 = [r for r in fit_recs if r["m"]["score"] and r["m"]["score"].get("complete") and r["m"]["best_of"] == 5]
    samp = [_compact(r) for r in comp3[::max(1, len(comp3) // max_sample)]] + \
           [_compact(r) for r in comp5[::max(1, len(comp5) // max(1, max_sample // 3))]]
    # stages: (parameter names, grid per parameter, which best_of groups' moments are used).
    # mom and sd are searched JOINTLY (both create set-to-set dependence; a one-at-a-time search lets
    # whichever goes first absorb the other).  Best-of-5 gets its own momentum.
    import itertools
    stages = ((("mom", "sd"), ((0.0, 0.01, 0.015, 0.02, 0.03), (0.0, 0.015, 0.03, 0.045)), (3,)),
              (("off",), ((0.02, 0.01, 0.005, 0.0, -0.01, -0.02),), (3,)),
              (("mom5",), ((0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04),), (5,)))
    par = {"off": 0.0, "sc": 0.0, "sd": 0.0, "mom": 0.0, "mom5": 0.0}
    tables = {}
    for _ in range(passes):
        for names, grids_, bos in stages:
            combos = list(itertools.product(*grids_))
            pars = []
            for combo in combos:
                trial = dict(par)
                trial.update(dict(zip(names, combo)))
                pars.append(trial)
            res = _eval_many(samp, pars, workers, bos)
            scored = [(combo, r[0]) for combo, r in zip(combos, res)]
            best = min(scored, key=lambda x: x[1])
            par.update(dict(zip(names, best[0])))
            for i, nm in enumerate(names):
                edge = best[0][i] in (min(grids_[i]), max(grids_[i])) and best[0][i] != 0.0
                if len(names) == 1:
                    tables[nm] = (sorted((c[0], s) for c, s in scored), best[0][i], edge)
                else:
                    tables[nm] = (sorted(scored), best[0][i], edge, names)
    best_detail = _eval_candidate((samp, par, (3, 5)))[1]
    X, y = [], []
    mstep = max(1, len(fit_recs) // 2500)
    for i, r in enumerate(fit_recs[::mstep]):
        res = match_pmfs(r["pa"], r["pb"], r["m"]["best_of"], par)
        s, e_ = logit(res["p_a_win"]), logit(r["elo"])
        if i % 2 == 0:
            X.append([s, e_]); y.append(1)
        else:
            X.append([-s, -e_]); y.append(0)
    coef = fit_logistic(X, y)
    return {"par": par, "tables": tables, "detail": best_detail, "ml_coef": coef,
            "n_fit": len(fit_recs), "n_sample": len(samp)}


def apply_ml(p_sim, p_elo, coef):
    return sigmoid(coef[0] + coef[1] * logit(p_sim) + coef[2] * logit(p_elo))


# ----------------------------------------------------------------------------- evaluation
def _mean_se(xs):
    n = len(xs)
    if n < 2:
        return float("nan"), float("nan")
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, sd / math.sqrt(n)


def _calib_table(pairs, bins=8, title=""):
    pairs = sorted(pairs)
    n = len(pairs)
    print(f"  calibration {title} (equal-count bins):")
    for i in range(bins):
        ch = pairs[i * n // bins:(i + 1) * n // bins]
        if not ch:
            continue
        print(f"    n={len(ch):5d}  predicted {sum(p for p, _ in ch)/len(ch):.3f}   actual {sum(y for _, y in ch)/len(ch):.3f}")


def backtest(rows, burn_in_until, test_from, save=PARAMS_FILE, rating_kw=None, min_prior=10, max_fit_sample=1500):
    recs, R, E = walk_forward(rows, burn_in_until, rating_kw)
    fit = [r for r in recs if r["m"]["date"] < test_from and eligible(r, min_prior)]
    test = [r for r in recs if r["m"]["date"] >= test_from and eligible(r, min_prior)]
    print(f"\n{len(recs)} predictions after burn-in; fit window {len(fit)} matches; test window {len(test)} matches")
    if len(fit) < 500 or len(test) < 300:
        print("[WARNING] too few matches in fit/test windows for a reliable result; widen the data range.")
        if not fit or not test:
            return None
    params = fit_params(fit, max_sample=max_fit_sample)
    d = params["par"]
    print(f"\n=== Fit window parameters (nothing here sees the test window; {params['n_sample']} matches sampled) ===")
    labels = {"mom5": "best-of-5 set-to-set momentum", "mom": "set-to-set momentum (serve-prob shift per set of lead)", "off": "serve-probability offset (both players)", "sd": "strength-gap shock sd (match-level form/matchup)",
              "sc": "common serve shock sd (match-level conditions)"}
    for name in ("mom", "off", "mom5"):
        if name not in params["tables"]:
            continue
        tb = params["tables"][name]
        rows_, best, edge = tb[0], tb[1], tb[2]
        if name == "mom":
            print(f"  joint grid: set-to-set momentum x strength-gap shock sd -> moment chi-square (lower is better):")
            moms = sorted({c[0] for c, _ in rows_}); sds = sorted({c[1] for c, _ in rows_})
            lk = {c: s for c, s in rows_}
            print("      mom \\ sd " + "".join(f"{s:>9.3f}" for s in sds))
            for mo in moms:
                print(f"      {mo:>7.3f}  " + "".join(f"{lk[(mo, s)]:9.1f}" for s in sds))
            print(f"    chosen mom={params['par']['mom']}, sd={params['par']['sd']}")
            sdt = params["tables"]["sd"]
            if tb[2] or sdt[2]:
                print("    [WARNING] a joint-grid optimum is on the edge of its grid -- not an interior optimum; do not trust until widened.")
            continue
        print(f"  {labels[name]} -> moment chi-square (lower is better):")
        print("    " + "   ".join(f"{v:+.3f}:{ll:.1f}{'*' if v == best else ''}" for v, ll in rows_) + "    (* best)")
        if edge:
            print(f"    [WARNING] best {name} is at the edge of its grid -- not an interior optimum; do not trust until widened.")
    print(f"  chosen: {d}")
    print("  fit-window residual (model - actual, in standard errors) at the chosen parameters:")
    for bo, nm, mean, se in params["detail"]:
        print(f"    best-of-{bo} {nm:38s} {mean:+.4f} ({mean/se:+.1f} se)")
    c = params["ml_coef"]
    print(f"  moneyline logistic: intercept {c[0]:+.3f}  w_sim {c[1]:.3f}  w_elo {c[2]:.3f}")

    # rank baseline (fit window)
    Xr = []; yr = []
    for i, r in enumerate(x for x in fit if x["rank"] is not None):
        Xr.append([r["rank"] if i % 2 == 0 else -r["rank"]]); yr.append(1 if i % 2 == 0 else 0)
    rk = fit_logistic(Xr, yr)

    # ---------------- moneyline
    cache = {}
    for r in test:
        cache[id(r)] = match_pmfs(r["pa"], r["pb"], r["m"]["best_of"], d)
    ll = defaultdict(list); acc = defaultdict(list)
    for r in test:
        pm = cache[id(r)]["p_a_win"]
        cand = {"50%": 0.5, "sim_raw": pm, "elo": r["elo"], "sim+elo (fit)": apply_ml(pm, r["elo"], c)}
        if r["rank"] is not None:
            cand["rank"] = sigmoid(rk[0] + rk[1] * r["rank"])
        for k, p in cand.items():
            p = min(1 - 1e-6, max(1e-6, p))
            ll[k].append(-math.log(p)); acc[k].append(1.0 if p > 0.5 else (0.5 if p == 0.5 else 0.0))
    print("\n=== MONEYLINE (test window, winner-perspective log loss; lower is better) ===")
    base = ll["50%"]
    for k in cand:
        if k not in ll:
            continue
        n = len(ll[k]); dm = [a - b for a, b in zip(ll[k], base[-n:])] if n == len(base) else None
        m, _ = _mean_se(ll[k])
        line = f"  {k:14s} logloss {m:.4f}  accuracy {sum(acc[k])/n*100:5.1f}%"
        if k in ("sim_raw", "elo", "sim+elo (fit)") and dm:
            mm, se = _mean_se(dm)
            line += f"   vs 50%: {mm:+.4f} (se {se:.4f})"
        print(line)
    pe = [a - b for a, b in zip(ll["sim_raw"], ll["elo"])]
    mm, se = _mean_se(pe)
    print(f"  sim_raw minus elo: {mm:+.4f} (se {se:.4f})   [negative = simulator better]")
    pe = [a - b for a, b in zip(ll["sim+elo (fit)"], ll["elo"])]
    mm, se = _mean_se(pe)
    print(f"  sim+elo minus elo: {mm:+.4f} (se {se:.4f})")
    cal = []
    for r in test:
        p = apply_ml(cache[id(r)]["p_a_win"], r["elo"], c)
        cal.append((p, 1)); cal.append((1 - p, 0))
    _calib_table(cal, 10, "sim+elo moneyline (each match both sides)")

    # ---------------- climatology baselines from the fit window
    fit_c = [r for r in fit if r["m"]["score"] and r["m"]["score"].get("complete")]
    test_c = [r for r in test if r["m"]["score"] and r["m"]["score"].get("complete")]
    games_by_bo = defaultdict(list)
    for r in fit_c:
        games_by_bo[r["m"]["best_of"]].append(r["m"]["score"]["total_games"])

    def clim_over(bo, line):
        xs = games_by_bo.get(bo) or [0]
        return sum(1 for g in xs if g > line) / len(xs)

    # ---------------- total games
    print(f"\n=== TOTAL GAMES (test window, {len(test_c)} completed matches; no market lines in this data) ===")
    for bo in (3, 5):
        sub = [r for r in test_c if r["m"]["best_of"] == bo]
        if len(sub) < 50:
            continue
        pm_mean = sum(eng.pmf_mean(cache[id(r)]["games"]) for r in sub) / len(sub)
        act = sum(r["m"]["score"]["total_games"] for r in sub) / len(sub)
        print(f"  best-of-{bo}: n={len(sub)}  mean games predicted {pm_mean:.2f}  actual {act:.2f}")
    br_m, br_c, pairs, lsd = [], [], [], []
    for r in test_c:
        res = cache[id(r)]; bo = r["m"]["best_of"]
        line = eng.pmf_median_line(res["games"])
        po = eng.p_over(res["games"], line); y = 1 if r["m"]["score"]["total_games"] > line else 0
        pc = clim_over(bo, line)
        br_m.append((po - y) ** 2); br_c.append((pc - y) ** 2); pairs.append((po, y))
        xs = games_by_bo[bo]
        lc = (sum(1 for g in xs if g == r["m"]["score"]["total_games"]) + 0.5) / (len(xs) + 0.5 * 60)
        lsd.append(-math.log(max(1e-9, res["games"].get(r["m"]["score"]["total_games"], 0.0))) + math.log(lc))
    mb, _ = _mean_se(br_m); cb, _ = _mean_se(br_c)
    dd, se = _mean_se([a - b for a, b in zip(br_m, br_c)])
    print(f"  Over/Under at each match's own fair (median) line: Brier model {mb:.4f} vs climatology {cb:.4f}  "
          f"diff {dd:+.4f} (se {se:.4f}) [negative = model better]")
    m_, se_ = _mean_se(lsd)
    print(f"  pmf log score vs climatological pmf: {-m_:+.4f} nats/match better (se {se_:.4f}) [positive = model better]")
    _calib_table(pairs, 8, "P(over fair line)")
    for bo, lines in ((3, (19.5, 21.5, 22.5, 23.5, 25.5)), (5, (33.5, 36.5, 38.5, 40.5))):
        sub = [r for r in test_c if r["m"]["best_of"] == bo]
        if len(sub) < 100:
            continue
        print(f"  fixed lines, best-of-{bo} (n={len(sub)}): model P(over) vs actual")
        for L in lines:
            pmv = sum(eng.p_over(cache[id(r)]["games"], L) for r in sub) / len(sub)
            ac = sum(1 for r in sub if r["m"]["score"]["total_games"] > L) / len(sub)
            print(f"    line {L:5.1f}: model {pmv:.3f}  actual {ac:.3f}  (climatology {clim_over(bo, L):.3f})")

    # ---------------- sets
    print("\n=== SETS ===")
    s1 = [(cache[id(r)]["p_set1_winner_wins"], 1 if r["m"]["score"]["sets"][0][0] > r["m"]["score"]["sets"][0][1] else 0)
          for r in test_c if r["m"]["best_of"] == 3]
    if s1:
        print(f"  P(set-1 winner wins match), best-of-3: model {sum(p for p,_ in s1)/len(s1):.3f}  actual {sum(y for _,y in s1)/len(s1):.3f}")
    for bo, label, ev in ((3, "P(match goes 3 sets)", lambda s: s[0] + s[1] == 3),
                          (5, "P(4+ sets)", lambda s: s[0] + s[1] >= 4),
                          (5, "P(5 sets)", lambda s: s[0] + s[1] == 5)):
        sub = [r for r in test_c if r["m"]["best_of"] == bo]
        if len(sub) < 100:
            continue
        clim_f = [r for r in fit_c if r["m"]["best_of"] == bo]
        sets_of = lambda r: (r["m"]["score"]["winner_sets"], r["m"]["score"]["loser_sets"])
        pc = sum(1 for r in clim_f if ev(sets_of(r))) / len(clim_f)
        bm, bc, prs = [], [], []
        for r in sub:
            p = sum(v for s, v in cache[id(r)]["sets"].items() if ev(s))
            y = 1 if ev(sets_of(r)) else 0
            bm.append((p - y) ** 2); bc.append((pc - y) ** 2); prs.append((p, y))
        dd, se = _mean_se([a - b for a, b in zip(bm, bc)])
        print(f"  {label} best-of-{bo} n={len(sub)}: model mean {sum(p for p,_ in prs)/len(prs):.3f}  actual {sum(y for _,y in prs)/len(prs):.3f}  "
              f"Brier {sum(bm)/len(bm):.4f} vs climatology {sum(bc)/len(bc):.4f} diff {dd:+.4f} (se {se:.4f})")
        _calib_table(prs, 5, label)

    if save:
        out = {"par": d, "ml_coef": c, "burn_in_until": burn_in_until, "test_from": test_from,
               "n_fit": len(fit), "n_test": len(test), "rating_kw": rating_kw or {}}
        json.dump(out, open(save, "w"), indent=1)
        print(f"\nsaved {save}")
    print("\nNote: totals/sets are scored against outcomes + climatology, not market lines (this dataset has none).")
    print("Beating climatology is necessary, not sufficient: only a market comparison shows a bettable edge.")
    return params


# ----------------------------------------------------------------------------- prediction
def build_state(rows, rating_kw=None):
    R, E = Ratings(**(rating_kw or {})), Elo()
    for m in rows:
        R.update(m); E.update(m["w_id"], m["l_id"], m["surface"])
    names = {}
    for m in rows:
        names[m["w_id"]] = m["w_name"]; names[m["l_id"]] = m["l_name"]
    return R, E, names


def find_player(names, q):
    qn = re.sub(r"[^a-z ]", "", q.lower()).split()
    hits = [(pid, n) for pid, n in names.items() if n and all(t in re.sub(r"[^a-z ]", "", n.lower()) for t in qn)]
    return hits


def predict_match(R, E, a, b, surf, best_of, date, params, final_tb=None, lines=None):
    pa, pb = R.point_probs(a, b, surf, date)
    res = match_pmfs(pa, pb, best_of, params.get("par", {}), 7, final_tb)
    c = params.get("ml_coef")
    p_ml = apply_ml(res["p_a_win"], E.p(a, b, surf), c) if c else res["p_a_win"]
    return pa, pb, res, p_ml


def print_prediction(name_a, name_b, pa, pb, res, p_ml, best_of, lines=None, elo_p=None):
    print(f"\n{name_a} vs {name_b}   (best-of-{best_of})")
    print(f"  point-win on serve: {name_a} {pa:.3f} | {name_b} {pb:.3f}")
    print(f"  Moneyline: {name_a} {p_ml*100:.1f}%  {name_b} {(1-p_ml)*100:.1f}%   (simulator alone {res['p_a_win']*100:.1f}%"
          + (f", Elo {elo_p*100:.1f}%" if elo_p else "") + ")")
    print("  Sets score:  " + "  ".join(f"{a}-{b}:{v*100:.1f}%" for (a, b), v in sorted(res["sets"].items(), reverse=True)))
    if best_of == 3:
        print(f"  Sets O/U 2.5: Over (3 sets) {(res['sets'].get((2,1),0)+res['sets'].get((1,2),0))*100:.1f}%")
    else:
        for k in (3.5, 4.5):
            o = sum(v for (a, b), v in res["sets"].items() if a + b > k)
            print(f"  Sets O/U {k}: Over {o*100:.1f}%")
    mean = eng.pmf_mean(res["games"])
    med = eng.pmf_median_line(res["games"])
    print(f"  Total games: mean {mean:.1f}, fair line {med}")
    rng = [med - 3, med - 2, med - 1, med, med + 1, med + 2, med + 3]
    if lines:
        rng = sorted(set(rng + list(lines)))
    print("    line :  " + "  ".join(f"{L:5.1f}" for L in rng))
    print("    Over%:  " + "  ".join(f"{eng.p_over(res['games'], L)*100:5.1f}" for L in rng))
    print(f"  Game handicap ({name_a} covers):  " + "   ".join(
        f"{-s:+.1f}: {sum(v for m, v in res['margin'].items() if m > -s)*100:.1f}%" for s in (1.5, 2.5, 3.5, 4.5, 5.5)))


def load_params(path=PARAMS_FILE):
    if os.path.exists(path):
        return json.load(open(path))
    print(f"[note] {path} not found -- run the backtest first to fit parameters; using uncalibrated defaults (no dispersion).")
    return {"par": {}, "ml_coef": None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("--tour", default="atp", choices=["atp", "wta"])
    f.add_argument("--years", default="2016-2026"); f.add_argument("--chall", action="store_true"); f.add_argument("--source", default="tml", choices=list(SOURCES)); f.add_argument("--dir", default=DATA_DIR)
    b = sub.add_parser("backtest"); b.add_argument("--tour", default="atp"); b.add_argument("--dir", default=DATA_DIR)
    b.add_argument("--burn-in-until", type=int, default=20190101); b.add_argument("--test-from", type=int, default=20240101)
    b.add_argument("--min-prior", type=int, default=10); b.add_argument("--fit-sample", type=int, default=1500)
    p = sub.add_parser("predict"); p.add_argument("--tour", default="atp"); p.add_argument("--dir", default=DATA_DIR)
    p.add_argument("--p1", required=True); p.add_argument("--p2", required=True)
    p.add_argument("--surface", default="Hard", choices=["Hard", "Clay", "Grass"]); p.add_argument("--best-of", type=int, default=3, choices=[3, 5])
    p.add_argument("--lines", type=str, default=None, help="comma list of total-games lines to show, e.g. 21.5,22.5")
    p.add_argument("--final-tb", type=int, default=None, help="final-set tiebreak target (10 for AO/Wimbledon/RG 10-pt)")
    s = sub.add_parser("slate"); s.add_argument("--tour", default="atp"); s.add_argument("--dir", default=DATA_DIR); s.add_argument("--csv", required=True)
    a = ap.parse_args()

    if a.cmd == "fetch":
        y0, y1 = [int(x) for x in a.years.split("-")] if "-" in a.years else (int(a.years), int(a.years))
        fetch(a.tour, range(y0, y1 + 1), a.chall, a.dir, a.source); return
    rows = load_rows(a.tour, a.dir)
    if not rows:
        print(f"no data in {a.dir}; run: python tennis_model.py fetch --tour {a.tour} --years 2016-2026"); return
    print(f"loaded {len(rows)} matches, {rows[0]['date']}..{rows[-1]['date']}; "
          f"{sum(1 for r in rows if r['w_srv'])} with serve stats, "
          f"{sum(1 for r in rows if r['score'] and r['score'].get('complete'))} with a complete parsed score")
    if a.cmd == "backtest":
        backtest(rows, a.burn_in_until, a.test_from, min_prior=a.min_prior, max_fit_sample=a.fit_sample); return
    params = load_params()
    R, E, names = build_state(rows, params.get("rating_kw"))
    today = rows[-1]["date"]
    todo = []
    if a.cmd == "predict":
        todo = [(a.p1, a.p2, a.surface, a.best_of, a.lines)]
    else:
        with open(a.csv, newline="") as fh:
            for r in csv.DictReader(fh):
                todo.append((r["p1"], r["p2"], r.get("surface") or "Hard", int(r.get("best_of") or 3), r.get("line_games")))
    for q1, q2, surf, bo, lines in todo:
        h1, h2 = find_player(names, q1), find_player(names, q2)
        if len(h1) != 1 or len(h2) != 1:
            print(f"\n[skip] could not uniquely match: '{q1}' -> {[n for _, n in h1][:5]}  '{q2}' -> {[n for _, n in h2][:5]}")
            continue
        (ia, na), (ib, nb) = h1[0], h2[0]
        ls = [float(x) for x in lines.split(",")] if lines else None
        pa, pb, res, p_ml = predict_match(R, E, ia, ib, surf, bo, today, params, getattr(a, "final_tb", None))
        print_prediction(na, nb, pa, pb, res, p_ml, bo, ls, E.p(ia, ib, surf))
        print(f"  (ratings built from {R.n_matches[ia]} / {R.n_matches[ib]} matches with usable history)")


if __name__ == "__main__":
    main()
'''
# ======================================================================
# embedded module: kalshi_tennis_live.py (153 lines)
# ======================================================================
_SRC["kalshi_tennis_live"] = r'''"""
kalshi_tennis_live.py -- Kalshi's OPEN ATP tennis markets (public market data, read-only, no key, no login, NO trading), parsed
into per-match events the tennis model can price.

SERIES USED (all verified against settled markets):
    KXATPMATCH       moneyline: one market per player, YES = that player wins            -> kind "ml"
    KXATPGTOTAL      total games: YES = total games in the match > floor_strike (x.5)    -> kind "games"
    KXATPGAMETOTAL   same market, the older series name                                  -> kind "games"
    KXATPTOTALSETS   total sets:  YES = total sets in the match > floor_strike (x.5)     -> kind "sets"
  (Set spreads exist (KXATPSSPREAD) but the model was WORSE than the market there, so they are not shown.)

PRICES.  Kalshi quotes dollars per $1 contract.  yes_bid / yes_ask are what you could sell / buy YES at right now; no_ask is what
  you would pay for NO.  A taker fee of about 0.07 x p x (1-p) per contract applies (check your own fee schedule).
EVENT KEY.  Every series names the same match with the same suffix:  KXATPMATCH-26OCT12TSIFRI, KXATPGTOTAL-26OCT12TSIFRI.
"""

import json
import re
import urllib.parse
import urllib.request

API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"KXATPMATCH": "ml", "KXATPGTOTAL": "games", "KXATPGAMETOTAL": "games", "KXATPTOTALSETS": "sets"}
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
EVENT_RE = re.compile(r"^([A-Z0-9]+)-(\d\d)([A-Z]{3})(\d\d)([A-Z0-9]*)$")


def http_get(path, **q):
    url = API + path + ("?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None}) if q else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sports-hub-tennis/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def price(m, name):
    """dollars (0-1) from `<name>_dollars` (string) or legacy integer cents `<name>`; None when absent / zero-width."""
    v = _num(m.get(name + "_dollars"))
    if v is None:
        c = _num(m.get(name))
        v = c / 100.0 if c is not None else None
    return v


def compact(m, series):
    return {"ticker": m.get("ticker"), "event_ticker": m.get("event_ticker"), "series": series, "kind": SERIES[series],
            "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title"), "no_sub_title": m.get("no_sub_title"),
            "floor_strike": _num(m.get("floor_strike")),
            "yes_bid": price(m, "yes_bid"), "yes_ask": price(m, "yes_ask"), "no_bid": price(m, "no_bid"), "no_ask": price(m, "no_ask"),
            "last": price(m, "last_price"), "volume": _num(m.get("volume_fp")) or _num(m.get("volume")),
            "open_interest": _num(m.get("open_interest_fp")) or _num(m.get("open_interest")),
            "close_time": m.get("close_time"), "expected_expiration": m.get("expected_expiration_time"),
            "occurrence": m.get("occurrence_datetime"), "status": m.get("status"),
            "rules": (m.get("rules_primary") or "")[:500]}


def fetch_open(get=http_get, max_pages=12):
    """-> (markets, errors).  Never raises: a failed series is reported in errors and the others still return."""
    out, errors = [], {}
    for s in SERIES:
        cursor = None
        try:
            for _ in range(max_pages):
                d = get("/markets", series_ticker=s, status="open", limit=1000, cursor=cursor)
                out.extend(compact(m, s) for m in d.get("markets", []) if m.get("ticker"))
                cursor = d.get("cursor")
                if not cursor:
                    break
        except Exception as e:                       # noqa: BLE001 -- network problems must not stop the others
            errors[s] = f"{type(e).__name__}: {e}"[:200]
    return out, errors


def fetch_market(ticker, get=http_get):
    d = get("/markets/" + urllib.parse.quote(ticker))
    return d.get("market") or d


# ----------------------------------------------------------------------------------------------- parsing
def event_key(event_ticker):
    """'KXATPMATCH-26OCT12TSIFRI' -> '26OCT12TSIFRI' (same for every series of one match)."""
    return (event_ticker or "").split("-", 1)[-1]


def event_date(event_ticker):
    """-> (year, month, day) from the ticker, or None."""
    m = EVENT_RE.match("X-" + event_key(event_ticker))
    if not m or m.group(3) not in MONTHS:
        return None
    return 2000 + int(m.group(2)), MONTHS[m.group(3)], int(m.group(4))


def parse_players(rules):
    """-> (name_a, name_b, tournament) from rules text ("... the Wolf vs Mochizuki professional tennis match in the 2026 ATP
    Cincinnati Qualification Round 1 after a ball has been played ..."), or None.  Names may be surnames only."""
    if not rules:
        return None
    i = rules.find(" professional tennis match")
    if i < 0 or " vs " not in rules[:i]:
        return None
    left, right = rules[:i].rsplit(" vs ", 1)
    left = re.split(r"\b(?:in|wins) the ", left)[-1]
    rest = rules[i + len(" professional tennis match"):]
    mt = re.match(r" in the (.+?)(?: after a ball has been played)?(?:,| by | then|\.)", rest)
    return left.strip(), right.strip(), (mt.group(1).strip() if mt else "")


def names_from_title(title):
    """'Coleman Wong vs Seongchan Hong: Total Games' -> ('Coleman Wong', 'Seongchan Hong')."""
    m = re.match(r"^(.+?) vs\.? (.+?)(?::|$)", title or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else None


def group_events(markets):
    """-> {event_key: {"key", "ml": [...], "games": [...], "sets": [...], "names": (a, b), "tournament": str, "date": (y, m, d)}}"""
    ev = {}
    for m in markets:
        k = event_key(m["event_ticker"])
        e = ev.setdefault(k, {"key": k, "ml": [], "games": [], "sets": [], "names": None, "tournament": "",
                              "date": event_date(m["event_ticker"])})
        e[m["kind"]].append(m)
    for e in ev.values():
        for kind in ("ml", "games", "sets"):
            for m in e[kind]:
                pl = parse_players(m.get("rules"))
                if pl and not e["tournament"]:
                    e["tournament"] = pl[2]
        # full names: the two moneyline markets' yes_sub_title, else the title, else the rules text
        ml_names = [m["yes_sub_title"] for m in e["ml"] if m.get("yes_sub_title")]
        if len(ml_names) == 2:
            e["names"] = (ml_names[0], ml_names[1])
        else:
            for kind in ("games", "sets", "ml"):
                for m in e[kind]:
                    nt = names_from_title(m.get("title"))
                    if nt and not e["names"]:
                        e["names"] = nt
            if not e["names"]:
                for kind in ("games", "sets", "ml"):
                    for m in e[kind]:
                        pl = parse_players(m.get("rules"))
                        if pl and not e["names"]:
                            e["names"] = (pl[0], pl[1])
        e["games"].sort(key=lambda m: m["floor_strike"] if m["floor_strike"] is not None else 0)
        e["sets"].sort(key=lambda m: m["floor_strike"] if m["floor_strike"] is not None else 0)
    return ev
'''


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        module.__file__ = f"<tennis_platform:{name}.py>"
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
import tennis_engine as eng                                                    # noqa: E402
import tennis_model as tm                                                      # noqa: E402
import kalshi_tennis_live as kal                                               # noqa: E402
from tennis_io import FileLock, LockBusy, atomic_json_dump, load_json_retry   # noqa: E402

TML_URL = "https://stats.tennismylife.org/data/{y}.csv"
FIRST_YEAR = 2016
# Fitted on 2019-2023 matches (moment matching, see the tennis findings doc); a tennis_params.json in the data dir overrides.
DEFAULT_PARAMS = {"par": {"off": 0.01, "sc": 0.0, "sd": 0.045, "mom": 0.0, "mom5": 0.005}, "ml_coef": [0.0, 0.760, 0.358]}
MIN_MATCHES = 10                 # a player needs this many rated matches or the model is withheld ("unrated")
ACTIVE_DAYS = 600                # name matching prefers players seen this recently
KALSHI_FILE = "tennis_kalshi_open.json"
LOG_FILE = "tennis_log.jsonl"
RESULTS_FILE = "tennis_results.json"
STATE_FILE = "tennis_refresh_state.json"
OVERRIDES_FILE = "tennis_overrides.json"
DATA_LOCK = "tennis_data.lock"
KALSHI_LOCK = "tennis_kalshi.lock"
LOG_LOCK = "tennis_log.lock"
STALE_PRICE_MIN = 30
FEE_RATE = 0.07
SLAMS = ("australian open", "french open", "roland garros", "wimbledon", "us open")
ALIASES = {"frenchopen": "rolandgarros", "montreal": "canada", "toronto": "canada", "usopen": "usopen"}
CLAY = ("monte carlo", "madrid", "rome", "barcelona", "munich", "geneva", "hamburg", "roland garros", "french open", "estoril",
        "bucharest", "houston", "marrakech", "buenos aires", "rio", "santiago", "gstaad", "bastad", "umag", "kitzbuhel",
        "cordoba", "lyon", "parma", "bastad", "belgrade", "sao paulo", "bogota")
GRASS = ("wimbledon", "halle", "queen", "stuttgart", "hertogenbosch", "mallorca", "eastbourne", "newport", "london", "birmingham")
REQUIRED_COLS = tm.REQUIRED


def clean(obj):
    """JSON-safe: NaN/inf -> null, tuples -> lists, tuple keys -> 'a-b' strings."""
    if isinstance(obj, dict):
        return {(f"{k[0]}-{k[1]}" if isinstance(k, tuple) else str(k)): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _norm_name(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    toks = [t for t in re.sub(r"[^a-z ]", " ", s).split() if t not in ("jr", "sr", "ii", "iii")]
    return "".join(toks)


def _tourney_key(text):
    t = (text or "").lower()
    t = re.sub(r"\b20\d\d\b", " ", t)
    t = re.sub(r"\b(round of \d+|qualification( round \d+| final)?|quarterfinals?|semifinals?|finals?|round \d+|first round|second round|third round)\b", " ", t)
    t = re.sub(r"\b(atp|men'?s?|singles|masters|open series|tour)\b", " ", t)
    k = re.sub(r"[^a-z]", "", t)
    return ALIASES.get(k, k)


def _fee(p):
    return FEE_RATE * p * (1 - p)


def _stat_key(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _http_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "sports-hub-tennis/1.0", "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def market_view(m, model_p):
    """One Kalshi market -> prices + the model comparison.  `model_p` = model probability that YES settles."""
    if m is None:
        return None
    bid, ask, last = m.get("yes_bid"), m.get("yes_ask"), m.get("last")
    no_ask = m.get("no_ask") if m.get("no_ask") is not None else (None if bid is None else round(1 - bid, 4))
    mid = None
    if bid is not None and ask is not None and ask > 0 and bid <= ask and ask - bid <= 0.15:
        mid = (bid + ask) / 2
    elif last is not None:
        mid = last
    out = {"ticker": m.get("ticker"), "yes_bid": bid, "yes_ask": ask, "no_ask": no_ask, "last": last, "mid": mid,
           "volume": m.get("volume"), "open_interest": m.get("open_interest")}
    if model_p is not None and mid is not None:
        out["diff"] = model_p - mid
    if model_p is not None and ask is not None and 0 < ask < 1:
        out["edge_yes_after_fee"] = model_p - (ask + _fee(ask))
    if model_p is not None and no_ask is not None and 0 < no_ask < 1:
        out["edge_no_after_fee"] = (1 - model_p) - (no_ask + _fee(no_ask))
    return out


class TennisPlatform:
    def __init__(self, directory=".", now_fn=None, http_bytes=None, kget=None, log_predictions=True):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._now_fn = now_fn
        self._http = http_bytes or _http_text
        self._kget = kget or kal.http_get
        self._log_on = log_predictions
        self._lock = threading.RLock()
        self._st = None
        self._st_key = None
        self._cache = {}
        self._name_cache = {}
        self._sigs = None
        self._stop = threading.Event()
        self._busy = {"data": False, "kalshi": False, "results": False}

    # ------------------------------------------------------------------ helpers
    def _now(self):
        n = self._now_fn() if self._now_fn else datetime.datetime.now(datetime.timezone.utc)
        return n if n.tzinfo else n.replace(tzinfo=datetime.timezone.utc)

    def today_int(self):
        n = self._now()
        return n.year * 10000 + n.month * 100 + n.day

    def _p(self, name):
        return os.path.join(self.dir, name)

    def _csvs(self):
        return sorted(f for f in os.listdir(self.dir) if re.match(r"^atp_matches_\d{4}\.csv$", f))

    def params(self):
        try:
            p = load_json_retry(self._p("tennis_params.json"), None)
            if p and isinstance(p.get("par"), dict):
                return p
        except Exception:                                 # noqa: BLE001
            pass
        return DEFAULT_PARAMS

    def _save_state(self, **kw):
        try:
            s = load_json_retry(self._p(STATE_FILE), {}) or {}
            s.update(kw)
            atomic_json_dump(self._p(STATE_FILE), s)
        except Exception:                                 # noqa: BLE001
            pass

    # ------------------------------------------------------------------ model state (ratings) -- rebuilt only when the CSVs change
    def _state(self):
        files = [self._p(f) for f in self._csvs()]
        key = _stat_key(*files)
        with self._lock:
            if self._st is not None and key == self._st_key:
                return self._st
            if not files:
                return None
            rows = tm.load_rows("atp", self.dir)
            if len(rows) < 500:
                return None
            R, E, names = tm.build_state(rows)
            last_seen, by_norm = {}, defaultdict(list)
            for m in rows:
                for pid in (m["w_id"], m["l_id"]):
                    last_seen[pid] = max(last_seen.get(pid, 0), m["date"])
            for pid, nm in names.items():
                if nm:
                    by_norm[_norm_name(nm)].append(pid)
            recent = rows[-1]["date"] // 10000 - 3
            sc = defaultdict(Counter)
            for m in rows:
                if m["date"] // 10000 >= recent and m["surface"] and m["tourney"]:
                    sc[_tourney_key(m["tourney"])][m["surface"]] += 1
            self._st = {"R": R, "E": E, "names": names, "last_seen": last_seen, "by_norm": dict(by_norm),
                        "surf": {k: c.most_common(1)[0][0] for k, c in sc.items() if len(k) >= 4},
                        "last_date": rows[-1]["date"], "n_rows": len(rows), "version": zlib.crc32(repr(key).encode())}
            self._st_key = key
            self._cache.clear()
            self._name_cache.clear()
            return self._st

    def resolve(self, st, name):
        """Kalshi name (full or surname) -> (player id, None) or (None, reason)."""
        n = _norm_name(name)
        if not n:
            return None, "empty name"
        ck = (st["version"], n)
        if ck in self._name_cache:
            return self._name_cache[ck]
        cands = list(st["by_norm"].get(n, []))
        if not cands:
            cands = [pid for nn, pids in st["by_norm"].items() if nn.endswith(n) and len(n) >= 3 for pid in pids]
        today = tm.days(self.today_int())
        act = [p for p in cands if today - tm.days(st["last_seen"].get(p, 19000101)) <= ACTIVE_DAYS]
        pool = act or []
        if len(pool) == 1:
            res = (pool[0], None)
        elif not pool:
            res = (None, f"'{name}' not found among players active in the last {ACTIVE_DAYS} days (no ATP-level history)")
        else:
            res = (None, f"'{name}' is ambiguous: " + " / ".join(sorted(st["names"].get(p, "?") for p in pool)[:4]))
        self._name_cache[ck] = res
        return res

    def infer_tournament(self, st, text):
        """-> (best_of, surface, warnings)"""
        warn = []
        low = (text or "").lower()
        qual = "qualif" in low
        bo = 5 if (any(s in low for s in SLAMS) and not qual) else 3
        key = _tourney_key(text)
        try:
            ov = load_json_retry(self._p(OVERRIDES_FILE), {}) or {}
        except Exception:                                 # noqa: BLE001
            ov = {}
        surf = None
        for k, v in ov.items():
            if _tourney_key(k) == key and v in ("Hard", "Clay", "Grass"):
                surf = v
        if surf is None and key:
            if key in st["surf"]:
                surf = st["surf"][key]
            else:
                for k, v in st["surf"].items():
                    if len(k) >= 4 and (k in key or key in k):
                        surf = v
                        break
        if surf is None:
            if any(c in low for c in GRASS):
                surf = "Grass"
            elif any(c in low for c in CLAY):
                surf = "Clay"
            else:
                surf = "Hard"
                warn.append(f"surface for '{text}' not found in the data: assumed Hard (set it in {OVERRIDES_FILE} if wrong)")
        if qual:
            warn.append("qualifying match: best-of-3, little ATP-level history for these players")
        return bo, surf, warn

    # ------------------------------------------------------------------ model
    def _model(self, st, a, b, surf, bo):
        pr = self.params()
        today = self.today_int()
        ck = (st["version"], a, b, surf, bo, today, json.dumps(pr, sort_keys=True))
        if ck in self._cache:
            return self._cache[ck]
        R, E = st["R"], st["E"]
        pa, pb = R.point_probs(a, b, surf, today)
        res = tm.match_pmfs(pa, pb, bo, pr["par"], 7, None)
        p_elo = E.p(a, b, surf)
        coef = pr.get("ml_coef")
        p_ml = tm.apply_ml(res["p_a_win"], p_elo, coef) if coef else res["p_a_win"]
        out = {"pa": pa, "pb": pb, "p_sim": res["p_a_win"], "p_elo": p_elo, "p_ml": p_ml, "games": res["games"], "sets": res["sets"],
               "mean_games": eng.pmf_mean(res["games"]), "fair_line": eng.pmf_median_line(res["games"])}
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[ck] = out
        return out

    @staticmethod
    def _sets_over(mod, line):
        return sum(v for (x, y), v in mod["sets"].items() if x + y > line)

    def _model_block(self, st, mod, a_name, b_name, bo, lines_g=None, lines_s=None):
        sets = {f"{x}-{y}": v for (x, y), v in sorted(mod["sets"].items(), reverse=True)}
        g_lines = sorted(set(lines_g or []) | {mod["fair_line"] + d for d in (-2, -1, 0, 1, 2)})
        s_lines = lines_s if lines_s else ([2.5] if bo == 3 else [3.5, 4.5])
        return {"win_probability": {"a": mod["p_ml"], "b": 1 - mod["p_ml"], "simulator_only_a": mod["p_sim"], "elo_a": mod["p_elo"]},
                "point_win_on_serve": {"a": mod["pa"], "b": mod["pb"]},
                "games": {"expected": mod["mean_games"], "fair_line": mod["fair_line"],
                          "over": {str(L): eng.p_over(mod["games"], L) for L in g_lines}},
                "sets": {"score_probabilities": sets, "over": {str(L): self._sets_over(mod, L) for L in s_lines}}}

    def predict(self, p1, p2, surface="Hard", best_of=3, lines=None):
        """Any matchup, no Kalshi needed.  lines = total-games lines to price."""
        w = []
        try:
            st = self._state()
            if st is None:
                return {"status": "error", "warnings": ["no match data yet (run bootstrap)"]}
            if surface not in ("Hard", "Clay", "Grass"):
                return {"status": "error", "warnings": ["surface must be Hard, Clay or Grass"]}
            if int(best_of) not in (3, 5):
                return {"status": "error", "warnings": ["best_of must be 3 or 5"]}
            a, ra = self.resolve(st, p1)
            b, rb = self.resolve(st, p2)
            if a is None or b is None or a == b:
                return {"status": "error", "warnings": [r for r in (ra, rb) if r] or ["same player twice"]}
            for pid in (a, b):
                if st["R"].n_matches[pid] < MIN_MATCHES:
                    w.append(f"{st['names'][pid]} has only {st['R'].n_matches[pid]} rated matches: unreliable")
            mod = self._model(st, a, b, surface, int(best_of))
            blk = self._model_block(st, mod, st["names"][a], st["names"][b], int(best_of), [float(x) for x in (lines or [])])
            return clean({"status": "ok", "warnings": w + self._global_warnings(st), "players": {"a": st["names"][a], "b": st["names"][b]},
                          "surface": surface, "best_of": int(best_of), **blk})
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def _global_warnings(self, st):
        w = []
        gap = tm.days(self.today_int()) - tm.days(st["last_date"])
        if gap > 10:
            w.append(f"ATP results end {st['last_date']} ({gap:.0f} days ago): ratings are stale until the next data refresh")
        return w

    # ------------------------------------------------------------------ Kalshi + slate (read path: files only)
    def _kalshi_blob(self):
        return load_json_retry(self._p(KALSHI_FILE), None)

    def kalshi_status(self):
        b = self._kalshi_blob()
        if not b:
            return {"status": "error", "warnings": ["no Kalshi pull yet (refresh_kalshi)"]}
        age = (self._now() - (_parse_iso(b.get("fetched_at")) or self._now())).total_seconds() / 60
        return clean({"status": "ok", "warnings": [], "fetched_at": b.get("fetched_at"), "age_minutes": age,
                      "markets": len(b.get("markets", [])), "errors": b.get("errors", {}), "by_series": b.get("by_series", {})})

    def _event(self, st, e, now, age_min):
        ev = {"event": e["key"], "tournament": e["tournament"], "date": e["date"], "status": "ok", "warnings": []}
        names = e["names"]
        if not names:
            ev.update(status="skipped", reason="player names could not be read from Kalshi's market text")
            return ev
        bo, surf, w = self.infer_tournament(st, e["tournament"])
        ev.update(best_of=bo, surface=surf)
        ev["warnings"] += w
        a, ra = self.resolve(st, names[0])
        b, rb = self.resolve(st, names[1])
        ev["players"] = {"a": {"name": names[0], "id": a, "rated_matches": st["R"].n_matches[a] if a else None, "note": ra},
                         "b": {"name": names[1], "id": b, "rated_matches": st["R"].n_matches[b] if b else None, "note": rb}}
        # scheduled start: Kalshi's occurrence_datetime (else expected_expiration_time); the EARLIEST across the match's markets, so a
        # late-listed start can never make an in-play price look pre-match
        occs = [_parse_iso(m.get("occurrence")) for k in ("ml", "games", "sets") for m in e[k]]
        occs = [o for o in occs if o] or [_parse_iso(m.get("expected_expiration")) for k in ("ml", "games", "sets") for m in e[k]]
        occ = min([o for o in occs if o], default=None)
        ev["scheduled_start"] = occ.isoformat() if occ else None
        if occ and now > occ + datetime.timedelta(minutes=15):
            ev["warnings"].append("scheduled start has passed: prices may be in-play, not pre-match")
            ev["maybe_in_play"] = True
        if age_min is not None and age_min > STALE_PRICE_MIN:
            ev["warnings"].append(f"Kalshi prices are {age_min:.0f} minutes old")
        ev["_close"] = min([m["close_time"] for k in ("ml", "games", "sets") for m in e[k] if m.get("close_time")] or [""])
        unrated = [r for r in (ra, rb) if r]
        thin = [nm for pid, nm in ((a, names[0]), (b, names[1])) if pid and st["R"].n_matches[pid] < MIN_MATCHES]
        if unrated or thin or a == b:
            ev["status"] = "unrated"
            ev["reason"] = "; ".join(unrated + [f"{n}: fewer than {MIN_MATCHES} rated ATP-level matches" for n in thin])
            ev["markets"] = self._raw_markets(e)
            return ev
        mod = self._model(st, a, b, surf, bo)
        glines = [m["floor_strike"] for m in e["games"] if m.get("floor_strike") is not None]
        slines = [m["floor_strike"] for m in e["sets"] if m.get("floor_strike") is not None]
        blk = self._model_block(st, mod, names[0], names[1], bo, glines, slines)
        ev["model"] = {k: blk[k] for k in ("point_win_on_serve",)}
        # moneyline
        side = {}
        for m in e["ml"]:
            nm = _norm_name(m.get("yes_sub_title"))
            side["a" if nm == _norm_name(names[0]) else "b" if nm == _norm_name(names[1]) else "?"] = m
        mla, mlb = side.get("a"), side.get("b")
        va, vb = market_view(mla, mod["p_ml"]), market_view(mlb, 1 - mod["p_ml"])
        norm_a = None
        if va and vb and va.get("mid") is not None and vb.get("mid") is not None and va["mid"] + vb["mid"] > 0:
            norm_a = va["mid"] / (va["mid"] + vb["mid"])
        ev["moneyline"] = {"model": {"a": mod["p_ml"], "b": 1 - mod["p_ml"], "simulator_only_a": mod["p_sim"], "elo_a": mod["p_elo"]},
                           "kalshi": {"a": va, "b": vb}, "market_normalised_a": norm_a,
                           "diff_a": (mod["p_ml"] - norm_a) if norm_a is not None else None}
        # total games
        rungs = []
        for m in e["games"]:
            L = m["floor_strike"]
            if L is None:
                continue
            p = eng.p_over(mod["games"], L)
            rungs.append({"line": L, "model_over": p, "kalshi": market_view(m, p)})
        extra = [{"line": L, "model_over": p} for L, p in blk["games"]["over"].items() if float(L) not in {r["line"] for r in rungs}]
        ev["games"] = {"expected": mod["mean_games"], "fair_line": mod["fair_line"], "kalshi_rungs": rungs,
                       "model_only_lines": [{"line": float(x["line"]), "model_over": x["model_over"]} for x in extra]}
        # total sets
        srungs = []
        for m in e["sets"]:
            L = m["floor_strike"]
            if L is None:
                continue
            p = self._sets_over(mod, L)
            srungs.append({"line": L, "model_over": p, "kalshi": market_view(m, p)})
        ev["sets"] = {"score_probabilities": blk["sets"]["score_probabilities"], "kalshi_rungs": srungs,
                      "model_only_lines": [] if srungs else [{"line": float(k), "model_over": v} for k, v in blk["sets"]["over"].items()]}
        return ev

    @staticmethod
    def _raw_markets(e):
        return {"moneyline": [market_view(m, None) | {"player": m.get("yes_sub_title")} for m in e["ml"]],
                "games": [market_view(m, None) | {"line": m.get("floor_strike")} for m in e["games"]],
                "sets": [market_view(m, None) | {"line": m.get("floor_strike")} for m in e["sets"]]}

    def get_slate(self, date=None):
        warnings = []
        try:
            blob = self._kalshi_blob()
            st = self._state()
            if st is None:
                return {"status": "error", "warnings": ["no ATP match data yet: run bootstrap (python tennis_platform.py bootstrap)"],
                        "needs_bootstrap": True, "events": []}
            if not blob:
                return {"status": "error", "warnings": ["no Kalshi pull yet (refresh_kalshi)"], "events": []}
            now = self._now()
            fetched = _parse_iso(blob.get("fetched_at"))
            age = (now - fetched).total_seconds() / 60 if fetched else None
            if age is not None and age > STALE_PRICE_MIN:
                warnings.append(f"Kalshi pull is {age:.0f} minutes old")
            for s, err in (blob.get("errors") or {}).items():
                warnings.append(f"Kalshi series {s} failed on the last pull: {err}")
            warnings += self._global_warnings(st)
            want = None
            if date:
                try:
                    d = datetime.date.fromisoformat(date)
                    want = (d.year, d.month, d.day)
                except ValueError:
                    return {"status": "error", "warnings": ["date must be YYYY-MM-DD"], "events": []}
            evs = []
            for e in kal.group_events(blob.get("markets", [])).values():
                if want and e["date"] != want:
                    continue
                try:
                    evs.append(self._event(st, e, now, age))
                except Exception as ex:                    # noqa: BLE001
                    evs.append({"event": e["key"], "status": "error", "reason": f"{type(ex).__name__}: {ex}"})
            evs.sort(key=lambda x: (x.get("scheduled_start") or x.get("_close") or "~", x.get("event", "")))
            for x in evs:
                x.pop("_close", None)
            n_ok = sum(1 for x in evs if x.get("status") == "ok")
            return clean({"status": "ok", "warnings": warnings + [
                "RESEARCH DISPLAY: against Kalshi's own prices this model has shown no profitable edge (see the STATUS note); no stakes are produced."],
                "generated_at": now.isoformat(), "kalshi_fetched_at": blob.get("fetched_at"), "kalshi_age_minutes": age,
                "data_through": st["last_date"], "n_events": len(evs), "n_priced": n_ok, "events": evs})
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "events": []}

    # ------------------------------------------------------------------ WRITE paths (never raise)
    def refresh_data(self, force=False, all_years=False):
        """Download ATP results.  First time (or all_years): FIRST_YEAR..now.  Otherwise the current year (and last year in Jan-Feb)."""
        if self._busy["data"]:
            return {"status": "ok", "warnings": ["a data refresh is already running"]}
        lock = FileLock(self._p(DATA_LOCK), timeout=0.0, stale_after=1800.0)
        try:
            lock.acquire()
        except LockBusy:
            return {"status": "ok", "warnings": ["another process is refreshing the data"]}
        self._busy["data"] = True
        res, warns = {}, []
        try:
            now = self._now()
            have = {int(f[12:16]) for f in self._csvs()}
            years = list(range(FIRST_YEAR, now.year + 1)) if (all_years or len(have) < 3) else \
                [y for y in (now.year - 1, now.year) if y == now.year or now.month <= 2]
            for y in years:
                try:
                    raw = self._http(TML_URL.format(y=y))
                    text = raw.decode("utf-8", errors="replace")
                    header = text.split("\n", 1)[0].strip().lstrip("﻿").split(",")
                    miss = [c for c in REQUIRED_COLS if c not in header]
                    if miss:
                        raise ValueError(f"missing columns {miss}")
                    n_lines = text.count("\n")
                    path = self._p(f"atp_matches_{y}.csv")
                    old = os.path.getsize(path) if os.path.exists(path) else 0
                    if old and len(raw) < 0.8 * old:
                        raise ValueError(f"new file is much smaller than the one on disk ({len(raw)} vs {old} bytes): kept the old one")
                    if old and raw == open(path, "rb").read():
                        res[y] = "unchanged"
                        continue
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(raw)
                    os.replace(tmp, path)
                    res[y] = f"{len(raw) // 1024} KB, {n_lines} lines"
                except Exception as e:                    # noqa: BLE001
                    res[y] = f"FAILED: {type(e).__name__}: {e}"[:200]
                    warns.append(f"{y}: {res[y]}")
            self._save_state(data_refreshed_at=now.isoformat(), data_result=res)
            st = self._state()
            return clean({"status": "ok" if any(not v.startswith("FAILED") for v in res.values()) or not res else "error",
                          "warnings": warns, "years": res, "data_through": st["last_date"] if st else None})
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["data"] = False
            lock.release()

    def bootstrap(self):
        return self.refresh_data(force=True, all_years=True)

    def needs_bootstrap(self):
        return len(self._csvs()) < 3 or self._state() is None

    def refresh_kalshi(self):
        if self._busy["kalshi"]:
            return {"status": "ok", "warnings": ["a Kalshi refresh is already running"]}
        lock = FileLock(self._p(KALSHI_LOCK), timeout=0.0, stale_after=300.0)
        try:
            lock.acquire()
        except LockBusy:
            return {"status": "ok", "warnings": ["another process is pulling Kalshi"]}
        self._busy["kalshi"] = True
        try:
            markets, errors = kal.fetch_open(self._kget)
            by = Counter(m["series"] for m in markets)
            if not markets and errors:
                return {"status": "error", "warnings": ["Kalshi pull failed: " + "; ".join(f"{k}: {v}" for k, v in errors.items())], "errors": errors}
            blob = {"fetched_at": self._now().isoformat(), "markets": markets, "errors": errors, "by_series": dict(by)}
            atomic_json_dump(self._p(KALSHI_FILE), blob)
            logged = self._log_slate() if self._log_on else 0
            self._save_state(kalshi_refreshed_at=blob["fetched_at"], kalshi_markets=len(markets))
            return clean({"status": "ok", "warnings": [f"{k} failed: {v}" for k, v in errors.items()], "markets": len(markets),
                          "by_series": dict(by), "logged": logged})
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["kalshi"] = False
            lock.release()

    # ------------------------------------------------------------------ prediction log (pre-match only) + settlement
    def _load_sigs(self):
        if self._sigs is None:
            self._sigs = {}
            p = self._p(LOG_FILE)
            if os.path.exists(p):
                with open(p) as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                            self._sigs[r["ticker"]] = (round(r["p_model"], 3), round(r["p_mkt"], 2))
                        except (ValueError, KeyError, TypeError):
                            continue
        return self._sigs

    def _log_slate(self):
        s = self.get_slate()
        if s.get("status") != "ok":
            return 0
        now = self._now()
        rows = []
        for ev in s["events"]:
            if ev.get("status") != "ok" or ev.get("maybe_in_play"):
                continue
            occ = _parse_iso(ev.get("scheduled_start"))
            if occ is None or now > occ - datetime.timedelta(minutes=5):
                continue                                   # only strictly pre-match prices are scored
            ml = ev["moneyline"]
            for who, p_mod in (("a", ml["model"]["a"]), ("b", ml["model"]["b"])):
                k = (ml["kalshi"] or {}).get(who)
                if k and k.get("ticker") and ml.get("market_normalised_a") is not None:
                    p_mkt = ml["market_normalised_a"] if who == "a" else 1 - ml["market_normalised_a"]
                    rows.append(("ml", k["ticker"], ev, None, p_mod, p_mkt, k))
            for kind, rungs in (("games", ev["games"]["kalshi_rungs"]), ("sets", ev["sets"]["kalshi_rungs"])):
                for r in rungs:
                    k = r.get("kalshi")
                    if k and k.get("ticker") and k.get("mid") is not None:
                        rows.append((kind, k["ticker"], ev, r["line"], r["model_over"], k["mid"], k))
        if not rows:
            return 0
        sigs = self._load_sigs()
        n = 0
        lock = FileLock(self._p(LOG_LOCK), timeout=5.0, stale_after=60.0)
        lock.acquire()
        try:
            with open(self._p(LOG_FILE), "a") as f:
                for kind, tk, ev, line, p_mod, p_mkt, k in rows:
                    sig = (round(p_mod, 3), round(p_mkt, 2))
                    if sigs.get(tk) == sig:
                        continue
                    sigs[tk] = sig
                    f.write(json.dumps(clean({"ts": now.isoformat(), "ticker": tk, "event": ev["event"], "kind": kind, "line": line,
                                              "p_model": p_mod, "p_mkt": p_mkt, "yes_bid": k.get("yes_bid"), "yes_ask": k.get("yes_ask"),
                                              "scheduled_start": ev.get("scheduled_start"), "surface": ev.get("surface"),
                                              "best_of": ev.get("best_of"), "a": ev["players"]["a"]["name"],
                                              "b": ev["players"]["b"]["name"]})) + "\n")
                    n += 1
        finally:
            lock.release()
        return n

    def refresh_results(self, max_calls=250):
        """Settle logged markets: ask Kalshi for each unsettled logged ticker whose match should be over."""
        if self._busy["results"]:
            return {"status": "ok", "warnings": ["a results refresh is already running"]}
        self._busy["results"] = True
        try:
            res = load_json_retry(self._p(RESULTS_FILE), {}) or {}
            p = self._p(LOG_FILE)
            if not os.path.exists(p):
                return {"status": "ok", "warnings": ["nothing logged yet"], "settled": 0}
            last = {}
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        last[r["ticker"]] = r
                    except (ValueError, KeyError):
                        continue
            now = self._now()
            todo = []
            for tk, r in last.items():
                if tk in res:
                    continue
                st = _parse_iso(r.get("scheduled_start"))
                if st and now > st + datetime.timedelta(hours=2):
                    todo.append((st, tk))
            todo.sort()
            n = err = 0
            for st, tk in todo[:max_calls]:
                try:
                    m = kal.fetch_market(tk, self._kget)
                    r_ = (m.get("result") or "").lower()
                    if m.get("status") in ("finalized", "settled") and r_ in ("yes", "no"):
                        res[tk] = r_
                        n += 1
                    elif (now - st).days > 14:
                        res[tk] = "void"                   # never settled yes/no (postponed / cancelled): excluded from scoring
                except Exception:                          # noqa: BLE001
                    err += 1
            atomic_json_dump(self._p(RESULTS_FILE), res)
            self._save_state(results_refreshed_at=now.isoformat())
            return {"status": "ok", "warnings": ([f"{err} Kalshi lookups failed"] if err else []), "settled": n, "pending": len(todo) - n}
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["results"] = False

    def get_scorecard(self):
        """Live record: the LAST pre-match prediction logged for each settled market vs Kalshi's own price."""
        try:
            p = self._p(LOG_FILE)
            res = load_json_retry(self._p(RESULTS_FILE), {}) or {}
            if not os.path.exists(p):
                return {"status": "ok", "warnings": ["nothing logged yet: the log fills while the service runs"], "kinds": {}}
            last, ml_tickers = {}, defaultdict(set)
            logged = 0
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    logged += 1
                    last[r["ticker"]] = r
                    if r["kind"] == "ml":
                        ml_tickers[r["event"]].add(r["ticker"])
            recs = []
            for tk, r in last.items():
                y = res.get(tk)
                if y not in ("yes", "no"):
                    continue
                if r["kind"] == "ml" and tk != min(ml_tickers[r["event"]]):
                    continue                               # one side per match (the two sides are mirror images)
                recs.append({"kind": r["kind"], "ev": r["event"], "mod": r["p_model"], "mkt": r["p_mkt"], "y": 1 if y == "yes" else 0})
            out = {}
            for kind in ("ml", "games", "sets"):
                rs = [r for r in recs if r["kind"] == kind]
                if not rs:
                    out[kind] = {"n": 0}
                    continue
                n = len(rs)
                d = [((r["mod"] - r["y"]) ** 2) - ((r["mkt"] - r["y"]) ** 2) for r in rs]
                by = defaultdict(float)
                for r, di in zip(rs, d):
                    by[r["ev"]] += di
                m = sum(by.values()) / n
                k = len(by)
                se = math.sqrt(sum((v - m * sum(1 for r in rs if r["ev"] == e)) ** 2 for e, v in by.items())) / n if k > 1 else None
                out[kind] = {"n": n, "matches": k, "mean_model": sum(r["mod"] for r in rs) / n, "mean_market": sum(r["mkt"] for r in rs) / n,
                             "actual_rate": sum(r["y"] for r in rs) / n,
                             "brier_model": sum((r["mod"] - r["y"]) ** 2 for r in rs) / n,
                             "brier_market": sum((r["mkt"] - r["y"]) ** 2 for r in rs) / n,
                             "model_minus_market": m, "se": se, "note": "negative = model better; only trust when |diff| > 2 se"}
            return clean({"status": "ok", "warnings": ["Backtest on 2,773 matches found the model no better than Kalshi (moneyline worse). "
                                                       "This live record is the out-of-sample check."],
                          "logged_predictions": logged, "settled_markets": len(recs), "kinds": out})
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def health(self):
        try:
            st = self._state()
            s = load_json_retry(self._p(STATE_FILE), {}) or {}
            kb = self._kalshi_blob()
            now = self._now()

            def age(ts):
                t = _parse_iso(ts)
                return None if t is None else (now - t).total_seconds() / 60
            logged = 0
            if os.path.exists(self._p(LOG_FILE)):
                with open(self._p(LOG_FILE)) as f:
                    logged = sum(1 for _ in f)
            return clean({"status": "ok", "warnings": [] if st else ["no ATP data: run bootstrap"], "needs_bootstrap": st is None,
                          "data_through": st["last_date"] if st else None, "matches_loaded": st["n_rows"] if st else 0,
                          "players_rated": len(st["names"]) if st else 0, "csv_files": len(self._csvs()),
                          "data_refreshed_minutes_ago": age(s.get("data_refreshed_at")),
                          "kalshi_pulled_minutes_ago": age((kb or {}).get("fetched_at")), "kalshi_markets": len((kb or {}).get("markets", [])),
                          "kalshi_errors": (kb or {}).get("errors", {}), "results_refreshed_minutes_ago": age(s.get("results_refreshed_at")),
                          "logged_predictions": logged, "settled_markets": len(load_json_retry(self._p(RESULTS_FILE), {}) or {}),
                          "params": "tennis_params.json" if os.path.exists(self._p("tennis_params.json")) else "built-in fitted defaults",
                          "validation": "no demonstrated edge vs Kalshi prices: research display only"})
        except Exception as e:                            # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    # ------------------------------------------------------------------ background loops
    def start_background_refresh(self, data_interval_s=10800, kalshi_interval_s=300, results_interval_s=3600, bootstrap=False):
        self._stop.clear()

        def dloop():
            if bootstrap and self.needs_bootstrap():
                self.bootstrap()
            while not self._stop.is_set():
                self.refresh_data()
                self._stop.wait(data_interval_s)

        def kloop():
            self._stop.wait(5)                          # let the first data pull start
            while not self._stop.is_set():
                self.refresh_kalshi()
                self._stop.wait(kalshi_interval_s)

        def rloop():
            self._stop.wait(120)
            while not self._stop.is_set():
                self.refresh_results()
                self._stop.wait(results_interval_s)
        for fn, name in ((dloop, "tennis-data"), (kloop, "tennis-kalshi"), (rloop, "tennis-results")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def stop(self):
        self._stop.set()


# ===================================================================== built-in page
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ATP Tennis Model vs Kalshi</title>
<style>
:root{--bg:#0f1115;--fg:#e8eaed;--mut:#9aa0a6;--card:#171a21;--line:#262b36;--ok:#3ddc97;--bad:#ff6b6b;--warn:#f4b942}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#14171c;--mut:#5f6773;--card:#fff;--line:#dfe3ea;--ok:#0a8f5b;--bad:#c62828;--warn:#9a6a00}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--mut);font-size:12px}.banner{margin:10px 16px;padding:8px 12px;border:1px solid var(--warn);border-radius:8px;color:var(--warn);font-size:12px}
main{padding:0 16px 40px;max-width:1100px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:12px 14px}
.hd{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}.nm{font-weight:600;font-size:15px}.meta{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;margin-top:8px}th,td{text-align:right;padding:3px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500;font-size:12px}
.pos{color:var(--ok)}.neg{color:var(--bad)}.w{color:var(--warn);font-size:12px}h3{margin:10px 0 0;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
</style></head><body>
<header><h1>ATP tennis model vs Kalshi</h1><div class="sub" id="sub">loading...</div>
<div style="margin-top:8px">Day: <select id="day"><option value="">all open matches</option></select> <button onclick="load()">Refresh</button>
<label class="sub"><input type="checkbox" id="unr"> show unrated</label></div></header>
<div class="banner" id="ban"></div><main id="main"></main>
<script>
const pc=x=>x==null?'–':(100*x).toFixed(1)+'%',c=x=>x==null?'–':Math.round(100*x)+'¢';
const sg=x=>x==null?'–':'<span class="'+(x>=0?'pos':'neg')+'">'+(x>=0?'+':'')+(100*x).toFixed(1)+'</span>';
function mk(k){return k?c(k.yes_bid)+' / '+c(k.yes_ask):'–'}
function card(e){
 if(e.status==='skipped'||e.status==='error')return '';
 const p=e.players||{},a=(p.a||{}).name||'?',b=(p.b||{}).name||'?';
 let h='<div class="card"><div class="hd"><div class="nm">'+a+' vs '+b+'</div><div class="meta">'+(e.tournament||'')+' · '+(e.surface||'')+' · best of '+(e.best_of||'?')+(e.scheduled_start?' · '+new Date(e.scheduled_start).toLocaleString():'')+'</div></div>';
 (e.warnings||[]).forEach(w=>h+='<div class="w">⚠ '+w+'</div>');
 if(e.status==='unrated'){return h+'<div class="w">Not priced: '+(e.reason||'')+'</div></div>'}
 const m=e.moneyline;
 h+='<h3>Moneyline</h3><table><tr><th></th><th>Model</th><th>Kalshi bid / ask</th><th>Kalshi mid</th><th>Model − mid (pts)</th></tr>';
 [['a',a],['b',b]].forEach(([s,n])=>{const k=m.kalshi[s];h+='<tr><td>'+n+'</td><td>'+pc(m.model[s])+'</td><td>'+mk(k)+'</td><td>'+c(k&&k.mid)+'</td><td>'+sg(k&&k.diff)+'</td></tr>'});
 h+='</table><div class="meta">simulator alone '+pc(m.model.simulator_only_a)+' · Elo '+pc(m.model.elo_a)+' (for '+a+')</div>';
 const g=e.games;h+='<h3>Total games · expected '+g.expected.toFixed(1)+' · model fair line '+g.fair_line+'</h3><table><tr><th>Over</th><th>Model</th><th>Kalshi bid / ask</th><th>Kalshi mid</th><th>Model − mid (pts)</th></tr>';
 g.kalshi_rungs.forEach(r=>h+='<tr><td>'+r.line+'</td><td>'+pc(r.model_over)+'</td><td>'+mk(r.kalshi)+'</td><td>'+c(r.kalshi&&r.kalshi.mid)+'</td><td>'+sg(r.kalshi&&r.kalshi.diff)+'</td></tr>');
 if(!g.kalshi_rungs.length)g.model_only_lines.forEach(r=>h+='<tr><td>'+r.line+'</td><td>'+pc(r.model_over)+'</td><td>–</td><td>–</td><td>–</td></tr>');
 h+='</table>';
 const s=e.sets,sp=Object.entries(s.score_probabilities).map(([k,v])=>k+': '+pc(v)).join(' · ');
 h+='<h3>Total sets</h3><table><tr><th>Over</th><th>Model</th><th>Kalshi bid / ask</th><th>Kalshi mid</th><th>Model − mid (pts)</th></tr>';
 const rows=s.kalshi_rungs.length?s.kalshi_rungs:s.model_only_lines.map(r=>({line:r.line,model_over:r.model_over}));
 rows.forEach(r=>h+='<tr><td>'+r.line+'</td><td>'+pc(r.model_over)+'</td><td>'+mk(r.kalshi)+'</td><td>'+c(r.kalshi&&r.kalshi.mid)+'</td><td>'+sg(r.kalshi&&r.kalshi.diff)+'</td></tr>');
 return h+'</table><div class="meta">set scores (winner-loser): '+sp+'</div></div>';
}
async function load(){
 const d=document.getElementById('day').value;
 const r=await fetch('/api/tennis/slate'+(d?'?date='+d:''));const j=await r.json();
 const unr=document.getElementById('unr').checked;
 document.getElementById('sub').textContent=(j.status==='ok'?j.n_priced+' priced / '+j.n_events+' Kalshi matches · Kalshi pulled '+Math.round(j.kalshi_age_minutes||0)+' min ago · ATP results through '+j.data_through:'');
 document.getElementById('ban').textContent=(j.warnings||[]).join('  ·  ');
 const sel=document.getElementById('day');
 if(sel.options.length<2&&j.events){[...new Set(j.events.map(e=>e.date&&e.date.join('-').replace(/-(\d)(?=-|$)/g,'-0$1')))].filter(Boolean).sort().forEach(x=>sel.add(new Option(x,x)))}
 document.getElementById('main').innerHTML=(j.events||[]).filter(e=>unr||e.status==='ok').map(card).join('')||'<p class="meta">No matches to show.</p>';
}
load();setInterval(load,60000);
</script></body></html>"""


# ===================================================================== HTTP
def serve(platform, host="127.0.0.1", port=8054, cors=False, allow_write=False, token=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code=200, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else (obj if isinstance(obj, str) else json.dumps(obj)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
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
            if u.path in ("/", "/index.html"):
                return self._send(PAGE, 200, "text/html; charset=utf-8")

            def pred():
                try:
                    lines = [float(x) for x in q.get("lines", "").split(",") if x.strip()]
                    bo = int(q.get("best_of", 3))
                except ValueError:
                    return {"status": "error", "warnings": ["lines must be numbers, best_of 3 or 5"]}
                return platform.predict(q.get("p1", ""), q.get("p2", ""), q.get("surface", "Hard"), bo, lines)
            routes = {"/api/tennis/slate": lambda: platform.get_slate(q.get("date")),
                      "/api/tennis/predict": pred,
                      "/api/tennis/kalshi": platform.kalshi_status,
                      "/api/tennis/scorecard": platform.get_scorecard,
                      "/health": platform.health, "/api/tennis/health": platform.health}
            fn = routes.get(u.path)
            if fn is None:
                self._send({"status": "error", "warnings": ["not found"]}, 404)
            else:
                self._send(fn())

        def _authorised(self):
            return (not token) or self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/tennis/refresh":
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
            what = body.get("what", "kalshi")
            fn = {"data": lambda: platform.refresh_data(force=True), "kalshi": platform.refresh_kalshi,
                  "results": platform.refresh_results}.get(what)
            if fn is None:
                return self._send({"status": "error", "warnings": ["what must be 'data', 'kalshi' or 'results'"]}, 400)
            self._send(fn())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/, /api/tennis/slate, /api/tennis/predict, /api/tennis/scorecard, /api/tennis/kalshi, /health"
          f"{', POST /api/tennis/refresh' if allow_write else ''}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


# ===================================================================== CLI
def _pct(x):
    return "  –  " if x is None else f"{100 * x:5.1f}"


def _c(x):
    return " – " if x is None else f"{round(100 * x):3d}"


def print_slate(s, show_unrated=False):
    print(f"\nATP vs Kalshi  [status {s.get('status')}]  {s.get('n_priced', 0)} priced / {s.get('n_events', 0)} matches, results through {s.get('data_through')}")
    for w in s.get("warnings") or []:
        print(f"  [warn] {w}")
    for e in s.get("events") or []:
        if e.get("status") == "unrated" and not show_unrated:
            continue
        if e.get("status") != "ok":
            if show_unrated:
                print(f"\n{e.get('event')}: {e.get('status')} {e.get('reason', '')}")
            continue
        p = e["players"]
        print(f"\n{p['a']['name']} vs {p['b']['name']}  ({e['tournament']}, {e['surface']}, bo{e['best_of']})")
        for w in e["warnings"]:
            print(f"   [warn] {w}")
        m = e["moneyline"]
        for side in ("a", "b"):
            k = m["kalshi"][side] or {}
            print(f"   ML  {p[side]['name'][:22]:22} model {_pct(m['model'][side])}%   kalshi {_c(k.get('yes_bid'))}/{_c(k.get('yes_ask'))}c   diff {_pct(k.get('diff'))}")
        g = e["games"]
        print(f"   GAMES expected {g['expected']:.1f}, fair line {g['fair_line']}")
        for r in g["kalshi_rungs"]:
            k = r.get("kalshi") or {}
            print(f"      over {r['line']:5.1f}  model {_pct(r['model_over'])}%   kalshi {_c(k.get('yes_bid'))}/{_c(k.get('yes_ask'))}c   diff {_pct(k.get('diff'))}")
        for r in e["sets"]["kalshi_rungs"]:
            k = r.get("kalshi") or {}
            print(f"   SETS over {r['line']:4.1f}  model {_pct(r['model_over'])}%   kalshi {_c(k.get('yes_bid'))}/{_c(k.get('yes_ask'))}c   diff {_pct(k.get('diff'))}")


def print_prediction(r):
    if r.get("status") != "ok":
        print("  ", "; ".join(r.get("warnings") or []))
        return
    w = r["win_probability"]
    print(f"\n{r['players']['a']} vs {r['players']['b']}  ({r['surface']}, best of {r['best_of']})")
    for x in r["warnings"]:
        print(f"  [warn] {x}")
    print(f"  Win: {r['players']['a']} {100 * w['a']:.1f}%  {r['players']['b']} {100 * w['b']:.1f}%   (simulator {100 * w['simulator_only_a']:.1f}%, Elo {100 * w['elo_a']:.1f}%)")
    print(f"  Total games: expected {r['games']['expected']:.1f}, fair line {r['games']['fair_line']}")
    print("    over:  " + "  ".join(f"{float(k):.1f}:{100 * v:.1f}%" for k, v in r["games"]["over"].items()))
    print("  Sets: " + "  ".join(f"{k}:{100 * v:.1f}%" for k, v in r["sets"]["score_probabilities"].items()))
    print("    over:  " + "  ".join(f"{float(k):.1f}:{100 * v:.1f}%" for k, v in r["sets"]["over"].items()))


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    ap = argparse.ArgumentParser(description="ATP tennis platform: model, data, Kalshi prices, tracking, service -- one file")
    ap.add_argument("--dir", default=os.environ.get("TENNIS_DATA_DIR", "."), help="data folder (default: $TENNIS_DATA_DIR or current)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("daily", "slate"):
        s = sub.add_parser(name)
        s.add_argument("--date", default=None)
        s.add_argument("--no-refresh", action="store_true")
        s.add_argument("--unrated", action="store_true")
    for name in ("refresh", "kalshi", "results", "track", "bootstrap"):
        sub.add_parser(name)
    pr = sub.add_parser("predict")
    pr.add_argument("--p1", required=True)
    pr.add_argument("--p2", required=True)
    pr.add_argument("--surface", default="Hard")
    pr.add_argument("--best-of", type=int, default=3)
    pr.add_argument("--lines", default="")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--cors", action="store_true")
    sv.add_argument("--allow-write", action="store_true")
    sv.add_argument("--bootstrap", action="store_true", help="if no ATP data is on disk, download it in the background")
    sv.add_argument("--data-minutes", type=int, default=int(os.environ.get("TENNIS_DATA_MINUTES", "180")))
    sv.add_argument("--kalshi-minutes", type=int, default=int(os.environ.get("TENNIS_KALSHI_MINUTES", "5")))
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
        if args.module == "tennis_model" and "--dir" not in rest and rest and rest[0] in ("fetch", "backtest"):
            rest += ["--dir", args.dir]
        sys.argv = [args.module] + rest
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
    os.makedirs(args.dir, exist_ok=True)
    p = TennisPlatform(args.dir)
    if args.cmd == "bootstrap":
        print(json.dumps(p.bootstrap(), indent=2))
    elif args.cmd == "refresh":
        print(json.dumps(p.refresh_data(force=True), indent=2))
    elif args.cmd == "kalshi":
        print(json.dumps(p.refresh_kalshi(), indent=2))
    elif args.cmd == "results":
        print(json.dumps(p.refresh_results(), indent=2))
    elif args.cmd == "track":
        print(json.dumps(p.get_scorecard(), indent=2))
    elif args.cmd == "predict":
        print_prediction(p.predict(args.p1, args.p2, args.surface, args.best_of, [float(x) for x in args.lines.split(",") if x.strip()]))
    elif args.cmd == "serve":
        port = args.port or int(os.environ.get("PORT", "8054"))
        host = args.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        allow = args.allow_write or _env_flag("TENNIS_ALLOW_WRITE")
        token = os.environ.get("TENNIS_API_TOKEN") or None
        if allow and not token and host != "127.0.0.1":
            print("WARNING: writes are enabled on a public address without TENNIS_API_TOKEN: anyone who can reach it can trigger refreshes.", flush=True)
        p.start_background_refresh(max(1, args.data_minutes) * 60, max(1, args.kalshi_minutes) * 60,
                                   bootstrap=args.bootstrap or _env_flag("TENNIS_BOOTSTRAP"))
        serve(p, host, port, args.cors or _env_flag("TENNIS_CORS"), allow, token)
    else:                                                 # daily / slate
        if args.cmd == "daily" and not args.no_refresh:
            print(json.dumps(p.refresh_data(force=True), indent=2))
            print(json.dumps(p.refresh_kalshi(), indent=2))
            print(json.dumps(p.refresh_results(), indent=2))
        print_slate(p.get_slate(args.date), args.unrated)


if __name__ == "__main__":
    main()
