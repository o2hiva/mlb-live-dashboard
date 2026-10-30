#!/usr/bin/env python
"""
sa_platform.py  --  ONE FILE for the Serie A player plus-minus goals model: data, ratings, predictions, market odds, logging,
running scorecard, web service.  Python 3.9+, standard library only.

THE MODEL (your formula, the ADJUSTED version that tested best):
  Attacking players (midfielders + forwards): index = goals the team scored while the player was on the pitch (per 90) divided by the
      league rate per team per 90 (average player = 1.0), shrunk toward 1.0 by minutes played.
  Defending players (defenders + goalkeeper): score = 1 - (goals conceded on the pitch per 90 / league rate), shrunk toward 0.
  Team ATTACK index = average of the attackers in the starting XI;  team DEFENCE index = 1 - average of the defenders'+GK scores
      (below 1.0 = better defence).
  Expected goals(home) = home attack x away defence x league average home goals;  expected goals(away) likewise with the away average.
  "Adjusted": before a goal counts toward a player's score it is divided by the opponent's strength (their shrunk scoring / conceding
  record) and the venue factor, using only information from before that date.
  Probabilities: independent Poisson on the two expected-goal figures -> 1X2 (home / DRAW / away), team totals (0.5-3.5), total goals
  (1.5-4.5), both teams to score, likeliest scorelines.

WHAT YOU GET per upcoming Serie A match: expected goals, home/draw/away probabilities, over/under for each team and for total goals,
BTTS, the lineup used (confirmed XI if ESPN has published it, otherwise each team's previous XI), football-data.co.uk market odds next to
the model (when listed), and warnings.

STATUS -- READ THIS.  Walk-forward backtest on 351 matches (2025/26 + 2026/27 to date; parameters tuned on 2024/25 only):
    goals log-likelihood per match: league average -3.269, tuned team-level model -3.119, this model (adjusted, actual XI) -3.120 (about
    0.005 lower with previous-XI lineups, which is what is available days ahead)  => clearly better than the league average, a TIE with
    the tuned team-level model (player-level information adds ~nothing on top of team form).
    vs the CLOSING over/under 2.5 and 1X2 prices: the market is better (O/U2.5 Brier +0.003, se 0.003; 1X2 log loss .955 vs .982);
    information beyond the market ~0; flat bets at average closing odds lost about 3-12%.
  => THIS IS A REFERENCE / RESEARCH DISPLAY, NOT A BETTING SYSTEM.  No stakes are produced.  Every pre-match prediction is logged and
  settled against the real result: get_scorecard() is the live out-of-sample check (plus a walk-forward season-to-date record).
LIMITATIONS: independent Poisson tends to understate draws a little (a 'draw_boost' parameter exists, default 1.0, untested); no injuries /
  suspensions / news beyond the lineup; bench players ESPN labels 'Substitute' inherit the role of the player they replaced; promoted clubs
  with no Serie A history in the loaded data get league-average ratings (flagged); cup / European / Serie B matches are not used.

------------------------------------------------------------------ WIRING (library)
    from sa_platform import SerieAPlatform
    bp = SerieAPlatform("/data")
    bp.refresh_data()        # WRITE path: ESPN results + lineups/subs/goals for new matches (first run = several seasons, ~15 min). Never raises.
    bp.refresh_fixtures()    # WRITE path: next 10 days of fixtures + confirmed lineups, logs pre-match predictions.  Never raises.
    bp.refresh_odds()        # WRITE path: football-data.co.uk fixtures odds + this/last season results with closing odds.  Never raises.
    bp.refresh_results()     # WRITE path: settle logged predictions from stored results.  Never raises.
    bp.get_slate()           # upcoming matches with model + market (cached files only, no network)
    bp.predict("Inter Milan", "AC Milan")    # any matchup
    bp.get_scorecard(); bp.health()
  Every read returns a JSON-safe dict with "status" ("ok" | "error") and "warnings" and never raises.

------------------------------------------------------------------ HTTP (stdlib only) -- local or Railway
    python sa_platform.py serve [--dir PATH] [--port N] [--host H] [--cors] [--allow-write] [--bootstrap]
      GET  /                                  built-in page (slate + scorecard, auto-refreshing)
      GET  /api/sa/slate                      GET /api/sa/predict?home=Inter&away=Milan
      GET  /api/sa/scorecard                  GET /api/sa/results (recent settled)      GET /api/sa/teams      GET /health
      POST /api/sa/refresh  {"what": "data" | "fixtures" | "odds" | "results"}          (needs --allow-write)
  Environment variables (used when the flag is not given): PORT (Railway sets it; then binds 0.0.0.0), SA_DATA_DIR (a Railway volume,
    e.g. /data), SA_API_TOKEN (if set, POSTs need  Authorization: Bearer <token>), SA_ALLOW_WRITE=1, SA_CORS=1, SA_BOOTSTRAP=1,
    SA_DATA_MINUTES (default 60), SA_FIXTURES_MINUTES (default 15), SA_ODDS_MINUTES (default 180), SA_FIRST_SEASON (default: 3 seasons back).

------------------------------------------------------------------ RAILWAY, step by step
  1. New repo containing ONLY this file. Add a Volume mounted at /data.  No packages needed.
  2. Start command:   python sa_platform.py serve --bootstrap
     Variables:       SA_DATA_DIR=/data   SA_API_TOKEN=<long random string>   SA_ALLOW_WRITE=1   SA_CORS=1 (if another site calls the API)
  3. First boot with an empty volume: --bootstrap downloads three-plus seasons from ESPN in the background (about 1,000 matches, ~15 min,
     resumable if the service restarts).  /health shows "bootstrap" progress; the slate says 'warming up' until enough matches are loaded.
  4. Health check path: /health.   Open the service URL for the built-in page, or call /api/sa/slate from your dashboard.
  5. It then runs itself: fixtures + lineups every 15 min, results every hour, odds every 3 hours; every pre-match prediction is logged and
     settled automatically (scorecard).  Parameters are the tuned defaults (override with sa_params.json in the data dir).
  TIP: to skip the 15-minute first load, copy your local sa_espn folder into the volume as  /data/espn  (folders days/ and matches/).

------------------------------------------------------------------ COMMAND LINE
    python sa_platform.py bootstrap | refresh | fixtures | odds | results | track
    python sa_platform.py daily               refresh everything, print the slate
    python sa_platform.py slate               print the upcoming matches
    python sa_platform.py backlog [--season Y]  every match already played this season: walk-forward prediction vs result (CSV + summary)
    python sa_platform.py predict --home "Inter Milan" --away "AC Milan"
    python sa_platform.py run sa_model backtest    (the embedded research tools: run sa_model backtest|compare|odds|table --dir DIR)
    python sa_platform.py unpack OUTDIR      write the embedded modules out as .py files
FILES (data dir): espn/days/*.json, espn/matches/*.json (ESPN data), odds/*.csv (football-data.co.uk), sa_fixtures.json, sa_log.jsonl,
  sa_results.json, sa_state.json, optional sa_params.json.
"""

import argparse
import csv
import datetime
import importlib.abc
import importlib.util
import io
import json
import math
import os
import sys
import threading
import time
import urllib.request

# ===================================================================== embedded modules
_ORDER = ['sa_io', 'sa_data', 'sa_model']
_SRC = {}
# ======================================================================
# embedded module: sa_io.py (96 lines)
# ======================================================================
_SRC["sa_io"] = r'''"""
sa_io.py -- small file helpers so the NBA data files are safe to read while a
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
# embedded module: sa_data.py (392 lines)
# ======================================================================
_SRC["sa_data"] = r'''"""
sa_data.py -- collect Serie A lineups / substitutions / goals from ESPN (public JSON, no key) and parse them into
per-player on-pitch intervals and goal times.

    python sa_data.py collect [--seasons 2023 2024 2025 2026] [--dir sa_espn]    (resumable; safe to re-run)
    python sa_data.py check   [--dir sa_espn]                                    (sanity report -- paste this output)

Season N = the season that starts in August of year N (2025 = 2025/26).
"""
import argparse, datetime as dt, gzip, json, os, re, time, unicodedata, urllib.request, urllib.error

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1"
SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/ita.1/summary?event="


# ------------------------------------------------------------------------------------------------ http
def get_json(url, tries=4, pause=0.3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                                       "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=40) as r:
                b = r.read()
            if b[:2] == b"\x1f\x8b":
                b = gzip.decompress(b)
            time.sleep(pause)
            return json.loads(b.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 404):
                break
        except Exception as e:                                     # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url} -> {last}")


# ------------------------------------------------------------------------------------------------ collect
def season_days(year, today=None):
    today = today or dt.date.today()
    d, end = dt.date(year, 8, 1), min(dt.date(year + 1, 6, 1), today)
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def compact_summary(s, ev_meta):
    ros = []
    for r in s.get("rosters") or []:
        pl = []
        for p in r.get("roster") or []:
            a = p.get("athlete") or {}
            pos = p.get("position") or {}
            st = {x.get("name"): x.get("displayValue") for x in (p.get("stats") or []) if x.get("name")}
            pl.append({"id": a.get("id"), "name": a.get("displayName") or a.get("fullName"), "pos": pos.get("name"),
                       "abbr": pos.get("abbreviation"), "starter": bool(p.get("starter")),
                       "subIn": json.dumps(p.get("subbedIn"))[:300] if p.get("subbedIn") else None,
                       "subOut": json.dumps(p.get("subbedOut"))[:300] if p.get("subbedOut") else None,
                       "formationPlace": p.get("formationPlace"), "stats": st})
        ros.append({"homeAway": r.get("homeAway"), "team": (r.get("team") or {}).get("displayName"),
                    "team_id": (r.get("team") or {}).get("id"), "formation": r.get("formation"), "players": pl})
    evs = []
    for k in s.get("keyEvents") or []:
        evs.append({"id": k.get("id"), "type": (k.get("type") or {}).get("type"), "typeText": (k.get("type") or {}).get("text"),
                    "text": k.get("text"), "period": (k.get("period") or {}).get("number"),
                    "disp": (k.get("clock") or {}).get("displayValue"), "value": (k.get("clock") or {}).get("value"),
                    "team": (k.get("team") or {}).get("id"), "scoring": bool(k.get("scoringPlay")),
                    "parts": [((x.get("athlete") or {}).get("displayName") or (x.get("athlete") or {}).get("fullName"))
                              for x in (k.get("participants") or [])][:4]})
    return {**ev_meta, "rosters": ros, "events": evs}


def scoreboard_events(get, d):
    """One day's ESPN scoreboard -> [{id, date, name, completed, state, status, home{id,name,score}, away{...}}]."""
    sb = get(f"{BASE}/scoreboard?dates={d.strftime('%Y%m%d')}")
    evs = []
    for e in sb.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        st = ((e.get("status") or {}).get("type") or {})

        def side(k):
            c = cs.get(k, {})
            return {"id": (c.get("team") or {}).get("id"), "name": (c.get("team") or {}).get("displayName"), "score": c.get("score")}
        evs.append({"id": str(e["id"]), "date": e.get("date"), "name": e.get("name"), "completed": bool(st.get("completed")),
                    "state": st.get("state"), "status": st.get("name"), "home": side("home"), "away": side("away")})
    return evs


def lineups_from_summary(s, min_players=10):
    """ESPN summary -> {"home": [(player id, group)...], "away": [...]} of the STARTING XI when both sides have been published, else None."""
    out = {}
    for r in s.get("rosters") or []:
        lu = []
        for p in r.get("roster") or []:
            if not p.get("starter"):
                continue
            pos = p.get("position") or {}
            g = group_of(pos.get("abbreviation"), pos.get("name"))
            a = p.get("athlete") or {}
            if g and a.get("id"):
                lu.append((str(a["id"]), g))
        if len(lu) >= min_players:
            out[r.get("homeAway")] = lu
    return out if "home" in out and "away" in out else None


def cmd_collect(a):
    os.makedirs(os.path.join(a.dir, "days"), exist_ok=True)
    os.makedirs(os.path.join(a.dir, "matches"), exist_ok=True)
    today = dt.date.today()
    n_new = n_err = 0
    for yr in a.seasons:
        ids = []
        for d in season_days(yr, today):
            fp = os.path.join(a.dir, "days", d.strftime("%Y%m%d") + ".json")
            fresh = os.path.exists(fp) and (today - d).days > 7
            if fresh:
                evs = json.load(open(fp))
            else:
                try:
                    sb = get_json(f"{BASE}/scoreboard?dates={d.strftime('%Y%m%d')}")
                except Exception as e:                              # noqa: BLE001
                    print("  scoreboard fail", d, e); n_err += 1; continue
                evs = []
                for e in sb.get("events", []):
                    comp = (e.get("competitions") or [{}])[0]
                    cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
                    evs.append({"id": e["id"], "date": e.get("date"), "name": e.get("name"),
                                "completed": bool(((e.get("status") or {}).get("type") or {}).get("completed")),
                                "home": {"id": (cs.get("home", {}).get("team") or {}).get("id"),
                                         "name": (cs.get("home", {}).get("team") or {}).get("displayName"), "score": cs.get("home", {}).get("score")},
                                "away": {"id": (cs.get("away", {}).get("team") or {}).get("id"),
                                         "name": (cs.get("away", {}).get("team") or {}).get("displayName"), "score": cs.get("away", {}).get("score")}})
                json.dump(evs, open(fp, "w"))
            ids += [e for e in evs if e["completed"]]
        print(f"season {yr}: {len(ids)} completed matches on scoreboards")
        for i, e in enumerate(ids):
            mp = os.path.join(a.dir, "matches", e["id"] + ".json")
            if os.path.exists(mp):
                continue
            try:
                s = get_json(SUMMARY + e["id"])
                meta = {k: e[k] for k in ("id", "date", "name", "home", "away")}
                meta["season"] = yr
                json.dump(compact_summary(s, meta), open(mp, "w"))
                n_new += 1
            except Exception as ex:                                 # noqa: BLE001
                print("  summary fail", e["id"], e["name"], ex); n_err += 1
            if (i + 1) % 50 == 0:
                print(f"   {i + 1}/{len(ids)}")
    print(f"done: {n_new} new match files, {n_err} errors (re-run to retry errors)")


# ------------------------------------------------------------------------------------------------ parsing
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


def parse_disp(disp):
    m = re.match(r"\s*(\d+)'?(?:\s*\+\s*(\d+)'?)?", disp or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def group_of(abbr, pos):
    """-> 'GK' | 'DF' | 'MF' | 'FW' | None.  Attackers = MF + FW (user's formula: attacking players); defenders = DF + GK."""
    p, ab = (pos or "").lower(), (abbr or "").upper()
    if "goalkeeper" in p or ab == "G":
        return "GK"
    if "forward" in p or "striker" in p or "wing" in p and "back" not in p or ab in ("F", "CF", "LF", "RF", "ST", "SS"):
        return "FW"
    if "back" in p or "defender" in p or "sweeper" in p or ab in ("D", "CD", "CD-L", "CD-R", "LB", "RB", "SW", "LWB", "RWB"):
        return "DF"
    if "midfield" in p or ab in ("M", "CM", "DM", "AM", "LM", "RM", "CM-L", "CM-R", "CAM", "CDM"):
        return "MF"
    return None


def parse_match(m):
    """-> dict(date, home, away, hg, ag, h1_len, end, players[{side,id,name,group,start,end}], goals[{t,side,type}], problems[])
    Times are real elapsed minutes (first-half stoppage pushes the second half later)."""
    prob = []
    evs = m["events"]
    h1_len = 45.0
    for e in evs:
        if e["type"] == "halftime":
            pd = parse_disp(e["disp"])
            if pd:
                h1_len = float(pd[0] + pd[1])

    def tmin(e):
        pd = parse_disp(e["disp"])
        if not pd:
            return None
        base, extra = pd
        if e["period"] == 2:
            return h1_len + max(base - 45, 0) + extra
        return float(base + extra)

    end = 90.0 + (h1_len - 45.0)
    for e in evs:
        t = tmin(e)
        if t is not None and e["period"] == 2:
            end = max(end, t)
    home, away = m["home"]["name"], m["away"]["name"]
    sides = {}
    for r in m["rosters"]:
        sides[r["homeAway"]] = r
    players, byname = [], {}
    for side in ("home", "away"):
        for p in (sides.get(side) or {}).get("players", []):
            g = group_of(p.get("abbr"), p.get("pos"))
            q = {"side": side, "id": p["id"], "name": p["name"], "group": g, "pos": p.get("pos"),
                 "start": 0.0 if p["starter"] else None, "end": end if p["starter"] else None}
            players.append(q)
            byname[(side, norm(p["name"]))] = q
    teamside = {}
    for r in m["rosters"]:
        teamside[str(r["team_id"])] = r["homeAway"]

    import difflib

    def sim(a, b):
        a, b = norm(a), norm(b)
        if not a or not b:
            return 0.0
        ta, tb = sorted(a.split()), sorted(b.split())
        sc = max(difflib.SequenceMatcher(None, a, b).ratio(), difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio())
        if ta[-1:] == tb[-1:] or set(ta) & set(tb):          # shared surname / name token
            sc = max(sc, 0.6 + 0.4 * len(set(ta) & set(tb)) / max(len(set(ta) | set(tb)), 1))
        return sc

    def find(side, nm, pool=None):
        cands = [q for q in players if q["side"] == side and (pool is None or pool(q))]
        k = norm(nm)
        ex = [q for q in cands if norm(q["name"]) == k]
        if len(ex) == 1:
            return ex[0]
        sc = sorted(((sim(nm, q["name"]), i, q) for i, q in enumerate(cands)), key=lambda x: (-x[0], x[1]))
        if not sc or sc[0][0] < 0.62:
            return None
        if len(sc) > 1 and sc[0][0] - sc[1][0] < 0.04:
            return None
        return sc[0][2]

    def on_pitch(q):
        return q["start"] is not None and q["end"] == end

    def on_bench(q):
        return q["start"] is None

    for e in sorted(evs, key=lambda x: (x["period"] or 0, tmin(x) if tmin(x) is not None else 0)):
        t, ty, tx = tmin(e), e["type"] or "", e["text"] or ""
        side = teamside.get(str(e["team"]))
        if ty == "substitution":
            mm = re.match(r"Substitution, (.+?)\. (.+?) replaces (.+?)(?: because of .*)?\.?$", tx.strip())
            if t is None:
                prob.append("sub unparsed: " + tx[:80]); continue
            if mm:
                sd = side or ("home" if norm(mm.group(1)) in norm(home) or norm(home) in norm(mm.group(1)) else "away")
                pin, pout = find(sd, mm.group(2), on_bench), find(sd, mm.group(3), on_pitch)
                if not pin or not pout:
                    prob.append(f"sub names unmatched: {mm.group(2)} / {mm.group(3)}"); continue
            else:                                       # other feed form: the two players are in `parts`; bench man = on, pitch man = off
                parts = [x for x in (e.get("parts") or []) if x]
                if side is None or len(parts) < 2:
                    prob.append("sub unparsed: " + tx[:80]); continue
                qa, qb = find(side, parts[0]), find(side, parts[1])
                if qa and qb and on_bench(qa) and on_pitch(qb):
                    pin, pout = qa, qb
                elif qa and qb and on_bench(qb) and on_pitch(qa):
                    pin, pout = qb, qa
                else:
                    prob.append(f"sub names unmatched: {parts[0]} / {parts[1]}"); continue
            pin["start"], pin["end"] = t, end
            pout["end"] = t
            if pin["group"] is None:                    # ESPN lists bench players as 'Substitute': inherit the replaced player's role
                pin["group"], pin["inherited"] = pout["group"], True
        elif "red" in ty and "card" in ty or ty in ("yellow-red", "second-yellow"):
            mm = re.match(r"(.+?) \((.+?)\)", tx)
            if mm and t is not None and ("red card" in tx.lower()):
                sd = side or ("home" if norm(mm.group(2)) in norm(home) else "away")
                q = find(sd, mm.group(1), lambda z: z["start"] is not None)
                if q and q["end"] is not None:
                    q["end"] = min(q["end"], t); q["red"] = True
    fh, fa = int(m["home"]["score"] or 0), int(m["away"]["score"] or 0)

    def attribute(use_text):
        hg = ag = 0
        goals, pr = [], []
        for e in sorted(evs, key=lambda x: (x["period"] or 0, tmin(x) if tmin(x) is not None else 0)):
            t, ty, tx = tmin(e), e["type"] or "", e["text"] or ""
            if not (e["scoring"] or ty.startswith("goal") or ty in ("own-goal", "penalty---scored")):
                continue
            if t is None:
                pr.append("goal without time"); continue
            sd = teamside.get(str(e["team"])) if not use_text else None   # ESPN's team = the side credited with the goal (also own goals)
            own = "own" in ty or "own goal" in (e.get("typeText") or "").lower()
            if sd is None:
                tx2 = tx.replace("1. FC", "1FC")
                m2 = re.search(r"([^.!]+?) (\d+), (.+?) (\d+)\.", tx2)
                if m2:
                    n1, s1, s2 = m2.group(1), int(m2.group(2)), int(m2.group(4))
                    n1 = n1.split("  ")[-1].replace("1FC", "1. FC")
                    a1_home = sim(n1, home) >= sim(n1, away)
                    sh, sa = (s1, s2) if a1_home else (s2, s1)
                    if sh == hg + 1 and sa == ag:
                        sd = "home"
                    elif sa == ag + 1 and sh == hg:
                        sd = "away"
            if sd is None:
                pr.append("goal side unknown: " + tx[:70]); continue
            if sd == "home":
                hg += 1
            else:
                ag += 1
            goals.append({"t": t, "side": sd, "own": own, "pen": "penalty" in ty or "penalty" in tx.lower()[:40]})
        return goals, hg, ag, pr

    goals, hg, ag, gp = attribute(False)
    if (hg, ag) != (fh, fa):
        g2, h2, a2, p2 = attribute(True)
        if (h2, a2) == (fh, fa):
            goals, hg, ag, gp = g2, h2, a2, p2
    prob += gp
    if (hg, ag) != (fh, fa):
        prob.append(f"goal tally {hg}-{ag} != final {fh}-{fa}")
    return {"id": m["id"], "date": (m["date"] or "")[:10], "season": m.get("season"), "home": home, "away": away,
            "hg": fh, "ag": fa, "end": end, "h1_len": h1_len, "players": players, "goals": goals, "problems": prob,
            "goals_ok": (hg, ag) == (fh, fa)}


def load_matches(d):
    out = []
    md = os.path.join(d, "matches")
    for f in sorted(os.listdir(md)):
        if f.endswith(".json"):
            out.append(parse_match(json.load(open(os.path.join(md, f)))))
    out.sort(key=lambda x: (x["date"], x["id"]))
    return out


def cmd_check(a):
    ms = load_matches(a.dir)
    print(f"matches parsed: {len(ms)}   by season:", {s: sum(1 for m in ms if m['season'] == s) for s in sorted({m['season'] for m in ms})})
    ok = sum(m["goals_ok"] for m in ms)
    print(f"goal tally matches final score: {ok}/{len(ms)}")
    pr = {}
    for m in ms:
        for p in m["problems"]:
            k = p.split(":")[0][:40] if not p.startswith("goal tally") else "goal tally mismatch"
            pr.setdefault(k, []).append((m["id"], p))
    for k, v in sorted(pr.items(), key=lambda x: -len(x[1])):
        print(f"  problem '{k}': {len(v)}   e.g. {v[0][1][:110]}")
    bad = [m for m in ms if not m["goals_ok"]][:4]
    if bad:
        raw = os.path.join(a.dir, "matches")
        print("goal-mismatch examples (raw scoring events):")
        for m in bad:
            r = json.load(open(os.path.join(raw, m["id"] + ".json")))
            print(f"  {m['id']} {m['home']} {m['hg']}-{m['ag']} {m['away']}  parsed={[(g['t'], g['side']) for g in m['goals']]}")
            for e in r["events"]:
                if e["scoring"] or (e["type"] or "").startswith(("goal", "own", "penalty")):
                    print(f"      [{e['type']}] team={e['team']} {e['disp']} {str(e['text'])[:120]}")
    grp = {}
    for m in ms:
        for p in m["players"]:
            grp[(p["group"], p["pos"])] = grp.get((p["group"], p["pos"]), 0) + 1
    print("position -> group mapping (count):")
    for (g, pos), n in sorted(grp.items(), key=lambda x: (str(x[0][0]), -x[1])):
        print(f"   {str(g):4s} {pos}  {n}")
    inh = sum(1 for m in ms for p in m["players"] if p.get("inherited"))
    nog = sum(1 for m in ms for p in m["players"] if p["group"] is None and p["start"] is not None)
    print(f"substitutes that inherited the replaced player's role: {inh};  players who played but have no role: {nog}")
    # minutes sanity: each team should have ~11 x match length on-pitch (less red cards)
    import statistics as st
    tot = []
    played = []
    for m in ms:
        for side in ("home", "away"):
            mins = sum((p["end"] - p["start"]) for p in m["players"] if p["side"] == side and p["start"] is not None and p["end"] is not None)
            tot.append(mins / m["end"])
        played.append(sum(1 for p in m["players"] if p["start"] is not None))
    print(f"team man-minutes / match length: mean {st.mean(tot):.2f} (11 expected), min {min(tot):.2f}, max {max(tot):.2f}; "
          f"share within 10.9-11.1: {sum(1 for x in tot if 10.9 <= x <= 11.1) / len(tot):.1%}")
    print(f"players used per match (both teams): mean {st.mean(played):.1f}   mean match length {st.mean(m['end'] for m in ms):.1f}")
    gg = sum(len(m["goals"]) for m in ms)
    print(f"goals found {gg}  (own goals {sum(g['own'] for m in ms for g in m['goals'])}, penalties {sum(g['pen'] for m in ms for g in m['goals'])});"
          f" mean goals/match {gg / max(len(ms), 1):.2f}")
    if ms:
        m = ms[-1]
        print("sample:", m["date"], m["home"], m["hg"], "-", m["ag"], m["away"])
        for p in m["players"][:6]:
            print("   ", p["side"], p["name"], p["group"], p["start"], p["end"])
        print("   goals:", m["goals"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "check"])
    ap.add_argument("--seasons", type=int, nargs="+", default=[2023, 2024, 2025, 2026])
    ap.add_argument("--dir", default="sa_espn")
    a = ap.parse_args()
    {"collect": cmd_collect, "check": cmd_check}[a.cmd](a)
'''
# ======================================================================
# embedded module: sa_model.py (708 lines)
# ======================================================================
_SRC["sa_model"] = r'''"""
sa_model.py -- Serie A goals model built on the user's plus-minus / index formula.

  ATTACKING players (midfielders + forwards):  index_i = goals scored by the team while i is on the pitch, per 90, divided by
        the league rate per team per 90  (average player = 1.0), shrunk toward 1.0 by minutes.
  DEFENDING players (defenders + goalkeeper):  score_i = 1 - (goals conceded while on pitch per 90 / league rate), shrunk toward
        0 by minutes (average player = 0).
  Team ATTACK index   = sum of attackers' indices / number of attackers in the lineup.
  Team DEFENCE index  = 1 - (sum of defenders'+GK scores / number of those players);  below 1.0 = better defence.
  Expected goals(home) = attack(home) x defence(away) x league average home goals;  expected(away) likewise with league away average.
  Over/under probabilities per team and for total goals = Poisson (independent) on those expectations.

    python sa_model.py odds                       download football-data.co.uk I1 odds files into sa_odds/
    python sa_model.py backtest [--dir sa_espn]   tune on 2024, test on 2025-26 and 2026-27, compare with baselines + market
    python sa_model.py compare                    team formula (team scoring index x opponent goals-allowed index x league avg) vs the player formula
    python sa_model.py table  [--n 10] [--adjust]  expected goals / over-under table for the latest matches (walk-forward)
"""
import argparse, csv, datetime as dt, difflib, gzip, io, math, os, unicodedata, re, urllib.request
from collections import defaultdict
import sa_data

LINES_TEAM = (0.5, 1.5, 2.5)
LINES_TOTAL = (1.5, 2.5, 3.5, 4.5)
LG_DEFAULT = (1.45, 1.15)
X12_STRETCH = 1.35   # home/draw/away ONLY: expected goals are stretched away from the league average, x' = lg * (x / lg) ** 1.35, before the three
#                       probabilities are computed (pooled Serie A / Premier League / La Liga study: k chosen on 2024, leave-one-league-out gain 0.0077 logloss, se 0.0026).
#                       Totals, team totals, BTTS, scorelines and the displayed expected goals are NOT stretched.  1.0 switches it off.
FD_URL = "https://www.football-data.co.uk/mmz4281/{s}/I1.csv"


# ------------------------------------------------------------------------------------------------ maths
def pois_pmf(lam, n=14):
    lam = max(lam, 1e-6)
    p = [math.exp(-lam)]
    for k in range(1, n + 1):
        p.append(p[-1] * lam / k)
    return p


def p_over(lam, line):
    """P(X > line) for Poisson(lam)."""
    k = int(math.floor(line))
    return max(0.0, 1.0 - sum(pois_pmf(lam, k)[:k + 1]))


def outcome_probs(lh, la, n=14):
    ph, pa = pois_pmf(lh, n), pois_pmf(la, n)
    h = d = a = 0.0
    for i in range(n + 1):
        for j in range(n + 1):
            x = ph[i] * pa[j]
            if i > j: h += x
            elif i == j: d += x
            else: a += x
    s = h + d + a
    return h / s, d / s, a / s


def markets(xh, xa, n=14, draw_boost=1.0, team_lines=(0.5, 1.5, 2.5, 3.5), total_lines=(1.5, 2.5, 3.5, 4.5), lg=None):
    """Everything the platform shows for one match from the two expected-goal figures (independent Poisson; optional crude draw boost).
    -> dict(home, draw, away, btts, team_over{home,away}{line}, total_over{line}, scorelines[(h,a,p)] top 6)"""
    ph, pa = pois_pmf(xh, n), pois_pmf(xa, n)
    J = [[ph[i] * pa[j] for j in range(n + 1)] for i in range(n + 1)]
    if draw_boost != 1.0:
        for k in range(n + 1):
            J[k][k] *= draw_boost
    z = sum(sum(r) for r in J)
    J = [[x / z for x in r] for r in J]
    H = sum(J[i][j] for i in range(n + 1) for j in range(n + 1) if i > j)
    D = sum(J[i][i] for i in range(n + 1))
    A = 1.0 - H - D
    if lg is not None and X12_STRETCH != 1.0:                       # calibration step for home/draw/away only (see X12_STRETCH)
        sh, sa_ = pois_pmf(lg[0] * (max(xh, 1e-6) / lg[0]) ** X12_STRETCH, n), pois_pmf(lg[1] * (max(xa, 1e-6) / lg[1]) ** X12_STRETCH, n)
        J2 = [[sh[i] * sa_[j] for j in range(n + 1)] for i in range(n + 1)]
        if draw_boost != 1.0:
            for k in range(n + 1):
                J2[k][k] *= draw_boost
        z2 = sum(sum(r) for r in J2)
        H = sum(J2[i][j] for i in range(n + 1) for j in range(n + 1) if i > j) / z2
        D = sum(J2[i][i] for i in range(n + 1)) / z2
        A = 1.0 - H - D
    mh = [sum(J[i]) for i in range(n + 1)]
    ma = [sum(J[i][j] for i in range(n + 1)) for j in range(n + 1)]
    tot = [0.0] * (2 * n + 1)
    for i in range(n + 1):
        for j in range(n + 1):
            tot[i + j] += J[i][j]
    over = lambda pm, ln: sum(p for k, p in enumerate(pm) if k > ln)
    sl = sorted(((J[i][j], i, j) for i in range(n + 1) for j in range(n + 1)), reverse=True)[:6]
    return {"home": H, "draw": D, "away": A, "btts": sum(J[i][j] for i in range(1, n + 1) for j in range(1, n + 1)),
            "team_over": {"home": {str(l): over(mh, l) for l in team_lines}, "away": {str(l): over(ma, l) for l in team_lines}},
            "total_over": {str(l): over(tot, l) for l in total_lines}, "scorelines": [(i, j, p) for p, i, j in sl]}


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sig(x):
    return 1 / (1 + math.exp(-x))


def d_ord(s):
    return dt.date.fromisoformat(s).toordinal()


# ------------------------------------------------------------------------------------------------ player ledger
def match_rows(m):
    """Per-player on-pitch ledger for one parsed match: [(pid, side, group, minutes, gf, ga, starter)]."""
    rows = []
    for p in m["players"]:
        if p["start"] is None or p["end"] is None or p["end"] <= p["start"]:
            continue
        gf = ga = 0
        for g in m["goals"]:
            if p["start"] <= g["t"] < p["end"]:
                if g["side"] == p["side"]: gf += 1
                else: ga += 1
        rows.append((p["id"], p["side"], p["group"], p["end"] - p["start"], gf, ga, p["start"] == 0.0))
    return rows


class Model:
    def __init__(self, prior90=40.0, half_life=None, lineup="actual", team_prior=10.0, adjust=False, lg_half_life=None, team_min=0):
        self.P, self.hl, self.lineup, self.TP, self.adjust = prior90, half_life, lineup, team_prior, adjust
        self.lghl = lg_half_life
        self.tmin = team_min            # pure team formula: fewer than this many past games -> index 1.0

    def _w(self, now, then):
        return 1.0 if not self.hl else 0.5 ** ((now - then) / self.hl)

    def run(self, matches, first_eval=None):
        """Walk-forward over chronologically sorted parsed matches.  Predictions for a date use only strictly earlier dates.
        -> list of prediction dicts."""
        hist = defaultdict(list)            # pid -> [(day, mins, gf, ga)]
        thist = defaultdict(list)           # team -> [(day, gf, ga, home?)]
        starters_all = {}                   # team -> set of starter ids of its previous match
        starters = {}                       # team -> (group-labelled starter ids) from its previous match
        lg = []                             # (day, goals_home, goals_away, minutes)
        out = []
        i = 0
        while i < len(matches):
            j = i
            while j < len(matches) and matches[j]["date"] == matches[i]["date"]:
                j += 1
            day = d_ord(matches[i]["date"])
            # league rates from past matches
            wl = [((1.0 if not self.lghl else 0.5 ** ((day - d) / self.lghl)), h, a, mn) for d, h, a, mn in lg]
            sw = sum(w for w, *_ in wl)
            if sw > 0:
                lgh = sum(w * h for w, h, a, mn in wl) / sw
                lga = sum(w * a for w, h, a, mn in wl) / sw
                rate = (lgh + lga) / 2 * 90.0 / (sum(w * mn for w, h, a, mn in wl) / sw)       # goals per team per 90 min
            else:
                lgh, lga = LG_DEFAULT
                rate = (lgh + lga) / 2 * 90.0 / 98.0
            if sw < 20:                    # too little history: blend toward defaults
                b = sw / 20.0
                lgh, lga = b * lgh + (1 - b) * LG_DEFAULT[0], b * lga + (1 - b) * LG_DEFAULT[1]
            for m in matches[i:j]:
                rec = {"id": m["id"], "date": m["date"], "season": m["season"], "home": m["home"], "away": m["away"],
                       "hg": m["hg"], "ag": m["ag"], "lgh": lgh, "lga": lga}
                ind = {}
                for side, team in (("home", m["home"]), ("away", m["away"])):
                    if m.get("_future"):                  # an upcoming match: confirmed lineup if given, else the team's previous XI
                        lu = list((m.get("_lineup") or {}).get(side) or starters.get(team, []))
                    elif self.lineup == "actual":
                        lu = [(p["id"], p["group"]) for p in m["players"] if p["side"] == side and p["start"] == 0.0 and p["group"]]
                    else:
                        lu = starters.get(team, [])
                    ind[side] = lu
                for side in ("home", "away"):
                    att, dfn = [], []
                    for pid, grp in ind[side]:
                        h = hist.get(pid, [])
                        w = [(self._w(day, d), mn, gf, ga) for d, mn, gf, ga in h]
                        m90 = sum(x[0] * x[1] for x in w) / 90.0
                        gfw = sum(x[0] * x[2] for x in w)
                        gaw = sum(x[0] * x[3] for x in w)
                        if grp in ("MF", "FW"):
                            att.append((gfw / rate + self.P) / (m90 + self.P))
                        elif grp in ("DF", "GK"):
                            dfn.append((m90 - gaw / rate) / (m90 + self.P))
                    rec[side + "_att"] = sum(att) / len(att) if att else 1.0
                    rec[side + "_def"] = 1.0 - (sum(dfn) / len(dfn) if dfn else 0.0)
                    rec[side + "_n"] = (len(att), len(dfn))
                rot = None if m.get("_future") else 0
                for side, team in (("home", m["home"]), ("away", m["away"])):
                    cur = {p["id"] for p in m["players"] if p["side"] == side and p["start"] == 0.0}
                    prev = starters_all.get(team)
                    rot = None if (prev is None or rot is None) else rot + len(cur - prev)
                rec["rot"] = rot
                if m.get("_future"):
                    rec["future"] = True
                    rec["lineup_source"] = "confirmed" if (m.get("_lineup") or {}).get("home") and (m.get("_lineup") or {}).get("away") else "previous XI"
                    rec["known_teams"] = (m["home"] in starters, m["away"] in starters)
                rec["xh"] = rec["home_att"] * rec["away_def"] * lgh
                rec["xa"] = rec["away_att"] * rec["home_def"] * lga
                # baselines
                rec["bh"], rec["ba"] = lgh, lga
                rec["th"], rec["ta"] = self._team_xg(thist, m["home"], m["away"], day, lgh, lga)
                out.append(rec)
            # update with this date's matches (opponent / venue adjustment factors use only information from before this date)
            fac = {}
            avg = (lgh + lga) / 2
            for m in matches[i:j]:
                if m.get("_future"):
                    continue
                if self.adjust:
                    ah, dh = self._rates(thist, m["home"], day, avg)
                    aa, da = self._rates(thist, m["away"], day, avg)
                    # home side: scoring expectation ~ opp defence x home venue factor ; conceding ~ opp attack x away venue factor
                    fac[m["id"]] = {"home": (da * lgh / avg, aa * lga / avg), "away": (dh * lga / avg, ah * lgh / avg)}
                else:
                    fac[m["id"]] = {"home": (1.0, 1.0), "away": (1.0, 1.0)}
            for m in matches[i:j]:
                if m.get("_future"):
                    continue
                d = day
                for pid, side, grp, mn, gf, ga, st in match_rows(m):
                    ff, fa_ = fac[m["id"]][side]
                    hist[pid].append((d, mn, gf / max(ff, 0.2), ga / max(fa_, 0.2)))
                thist[m["home"]].append((d, m["hg"], m["ag"], True))
                thist[m["away"]].append((d, m["ag"], m["hg"], False))
                lg.append((d, m["hg"], m["ag"], m["end"]))
                for side, team in (("home", m["home"]), ("away", m["away"])):
                    starters[team] = [(p["id"], p["group"]) for p in m["players"] if p["side"] == side and p["start"] == 0.0 and p["group"]]
                    starters_all[team] = {p["id"] for p in m["players"] if p["side"] == side and p["start"] == 0.0}
            i = j
        return out

    def _rates(self, thist, team, day, avg):
        """shrunk team (attack, defence-conceded) ratios relative to the league average, from history before `day`."""
        w = [(self._w(day, d), gf, ga) for d, gf, ga, hm in thist.get(team, [])]
        n = sum(x[0] for x in w)
        if n < self.tmin:
            return 1.0, 1.0
        gf = sum(x[0] * x[1] for x in w)
        ga = sum(x[0] * x[2] for x in w)
        return (gf + self.TP * avg) / ((n + self.TP) * avg), (ga + self.TP * avg) / ((n + self.TP) * avg)

    def _team_xg(self, thist, home, away, day, lgh, lga):
        """Classic team-level attack x defence baseline with the same shrinkage idea (prior TP matches)."""
        avg = (lgh + lga) / 2
        ah, dh = self._rates(thist, home, day, avg)
        aa, da = self._rates(thist, away, day, avg)
        return ah * da * lgh, aa * dh * lga


# ------------------------------------------------------------------------------------------------ evaluation
def pois_ll(lam, k):
    lam = max(lam, 1e-6)
    return k * math.log(lam) - lam - math.lgamma(k + 1)


def mean_se(xs):
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else float("nan")), float("nan")
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(v / n)


def score(preds, hk="xh", ak="xa"):
    """-> dict of per-match metric lists for the given expectation keys."""
    ll, b25, l25 = [], [], []
    for r in preds:
        ll.append(pois_ll(r[hk], r["hg"]) + pois_ll(r[ak], r["ag"]))
        p = p_over(r[hk] + r[ak], 2.5)
        y = 1.0 if r["hg"] + r["ag"] > 2.5 else 0.0
        b25.append((p - y) ** 2)
        l25.append(-(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9))))
    return {"ll": ll, "brier25": b25, "logloss25": l25}


def paired(a, b):
    d = [x - y for x, y in zip(a, b)]
    return mean_se(d)


VARIANTS = [("player formula (raw)", "xh", "xa"), ("player formula + opponent & venue adj.", "yh", "ya"), ("BLEND adjusted player + team-level", "zh", "za")]


def report_block(preds, label, variants=VARIANTS):
    n = len(preds)
    T = score(preds, "th", "ta")
    L = score(preds, "bh", "ba")
    print(f"\n--- {label}: {n} matches ---")
    print(f"{'':40s}{'goals loglik/match':>20s}{'O/U2.5 Brier':>15s}{'O/U2.5 logloss':>16s}")
    rows = [("league average (home/away)", L), ("team attack x defence", T)]
    Ms = []
    for nm, hk, ak in variants:
        if hk in preds[0]:
            S = score(preds, hk, ak); Ms.append((nm, hk, ak, S)); rows.append((nm, S))
    for nm, S in rows:
        print(f"{nm:40s}{mean_se(S['ll'])[0]:20.4f}{mean_se(S['brier25'])[0]:15.4f}{mean_se(S['logloss25'])[0]:16.4f}")
    for nm, hk, ak, S in Ms:
        for cmp_nm, B in (("league", L), ("team-level", T)):
            d, se = paired(S["ll"], B["ll"])
            db, seb = paired(S["brier25"], B["brier25"])
            print(f"  {nm[:38]:38s} vs {cmp_nm:10s} loglik {d:+.4f} (se {se:.4f})  Brier O/U2.5 {db:+.4f} (se {seb:.4f})")
    byk = {x[1]: x for x in Ms}
    if "xh" in byk and "yh" in byk:
        Ms2 = [byk["xh"], byk["yh"]]
        d, se = paired(Ms2[1][3]["ll"], Ms2[0][3]["ll"])
        db, seb = paired(Ms2[1][3]["brier25"], Ms2[0][3]["brier25"])
        print(f"  ADJUSTED vs RAW: loglik {d:+.4f} (se {se:.4f})  Brier O/U2.5 {db:+.4f} (se {seb:.4f})   [loglik + / Brier - = adjusted better]")
    print("  (loglik: higher is better; Brier: lower is better)")
    for nm, hk, ak, S in Ms:
        mh = sum(r[hk] for r in preds) / n; ma = sum(r[ak] for r in preds) / n
        ah = sum(r["hg"] for r in preds) / n; aa = sum(r["ag"] for r in preds) / n
        print(f"  [{nm}] mean expected goals home {mh:.3f} / away {ma:.3f}   actual {ah:.3f} / {aa:.3f}")
        print("     total O2.5 calibration (model prob bin -> n, mean pred, actual):")
        for lo, hi in [(0, .40), (.40, .48), (.48, .55), (.55, .62), (.62, 1.01)]:
            s_ = [(p_over(r[hk] + r[ak], 2.5), 1.0 if r["hg"] + r["ag"] > 2.5 else 0.0) for r in preds]
            s_ = [x for x in s_ if lo <= x[0] < hi]
            if s_:
                print(f"        {lo:.2f}-{hi:.2f}: n={len(s_):4d}  pred {sum(x[0] for x in s_) / len(s_):.3f}  actual {sum(x[1] for x in s_) / len(s_):.3f}")
        for ln in LINES_TEAM:
            for side, k, g in (("home", hk, "hg"), ("away", ak, "ag")):
                Bm = sum((p_over(r[k], ln) - (1.0 if r[g] > ln else 0.0)) ** 2 for r in preds) / n
                Bl = sum((p_over(r["bh" if side == "home" else "ba"], ln) - (1.0 if r[g] > ln else 0.0)) ** 2 for r in preds) / n
                print(f"        {side} team O{ln}: Brier {Bm:.4f} vs league {Bl:.4f} (diff {Bm - Bl:+.4f})")
    return Ms


def _sel(preds, season):
    return [r for r in preds if r["season"] == season]


def tune(matches, tune_season, grid_P, grid_hl, lineup, adjust=False, lg_hl=None):
    rows = []
    for hl in grid_hl:
        for P in grid_P:
            preds = Model(prior90=P, half_life=hl, lineup=lineup, adjust=adjust, lg_half_life=lg_hl).run(matches)
            rows.append((mean_se(score(_sel(preds, tune_season))["ll"])[0], P, hl))
    return sorted(rows, reverse=True)


def tune_league(matches, tune_season, grid):
    """half-life of the league home/away averages, tuned on the league-average baseline alone (independent of any player model)."""
    rows = []
    for g in grid:
        preds = Model(prior90=1e9, lg_half_life=g).run(matches)
        rows.append((mean_se(score(_sel(preds, tune_season), "bh", "ba")["ll"])[0], g))
    return sorted(rows, key=lambda x: -x[0])


def tune_team(matches, tune_season, grid_tp, grid_hl, lg_hl):
    rows = []
    for hl in grid_hl:
        for tp in grid_tp:
            preds = Model(prior90=1e9, team_prior=tp, half_life=hl, lg_half_life=lg_hl).run(matches)
            rows.append((mean_se(score(_sel(preds, tune_season), "th", "ta")["ll"])[0], tp, hl))
    return sorted(rows, reverse=True)


def blend_keys(preds, pk, tk, w, out):
    """out = w * player + (1-w) * team-level expectations, in place."""
    for r in preds:
        r[out[0]] = w * r[pk[0]] + (1 - w) * r[tk[0]]
        r[out[1]] = w * r[pk[1]] + (1 - w) * r[tk[1]]


def tune_blend(preds, tune_season, pk, tk):
    sel = _sel(preds, tune_season)
    rows = []
    for k in range(0, 11):
        w = k / 10
        for r in sel:
            r["_h"], r["_a"] = w * r[pk[0]] + (1 - w) * r[tk[0]], w * r[pk[1]] + (1 - w) * r[tk[1]]
        rows.append((mean_se(score(sel, "_h", "_a")["ll"])[0], w))
    return sorted(rows, reverse=True)


def rotation_block(preds, variants):
    sel = [r for r in preds if r.get("rot") is not None]
    if len(sel) < 60:
        return
    rots = sorted(r["rot"] for r in sel)
    lo, hi = rots[len(rots) // 3], rots[2 * len(rots) // 3]
    groups = (("low rotation", [r for r in sel if r["rot"] <= lo]), ("high rotation", [r for r in sel if r["rot"] >= hi]))
    print(f"\n--- ROTATION SUBSETS (rotation = starters not in either team's previous XI, both teams; low <= {lo}, high >= {hi}) ---")
    for gname, g in groups:
        T = score(g, "th", "ta")
        print(f"  {gname}: {len(g)} matches, mean rotation {sum(r['rot'] for r in g) / len(g):.1f};  team-level loglik {mean_se(T['ll'])[0]:.4f}")
        for nm, hk, ak in variants:
            if hk in g[0]:
                S = score(g, hk, ak)
                d, se = paired(S["ll"], T["ll"])
                db, seb = paired(S["brier25"], T["brier25"])
                print(f"     {nm[:44]:44s} loglik {mean_se(S['ll'])[0]:.4f}  vs team-level {d:+.4f} (se {se:.4f})   Brier O/U2.5 {db:+.4f} (se {seb:.4f})")


# ------------------------------------------------------------------------------------------------ odds
def fd_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


ALIAS = {
    # ESPN spellings and football-data spellings of the same club are both mapped to one canonical form (lower case, letters only)
    "inter milan": "inter", "internazionale": "inter", "fc internazionale": "inter", "fc internazionale milano": "inter",
    "ac milan": "milan", "as roma": "roma", "hellas verona": "verona", "verona fc": "verona", "parma calcio": "parma",
    "parma calcio ": "parma", "us lecce": "lecce", "us sassuolo": "sassuolo", "sassuolo calcio": "sassuolo", "ac pisa": "pisa",
    "pisa sc": "pisa", "us cremonese": "cremonese", "ac monza": "monza", "ss lazio": "lazio", "ssc napoli": "napoli",
    "acf fiorentina": "fiorentina", "bologna fc": "bologna", "torino fc": "torino", "udinese calcio": "udinese",
    "genoa cfc": "genoa", "cagliari calcio": "cagliari", "como": "como", "como calcio": "como", "venezia fc": "venezia",
    "empoli fc": "empoli", "atalanta bc": "atalanta", "juventus fc": "juventus", "us salernitana": "salernitana",
    "frosinone calcio": "frosinone", "spezia calcio": "spezia", "us cremonese ": "cremonese", "sudtirol": "sudtirol",
    "fc sudtirol": "sudtirol", "palermo fc": "palermo", "carrarese": "carrarese", "us avellino": "avellino",
}


def nsim(a, b):
    a, b = fd_name(a), fd_name(b)
    a, b = ALIAS.get(a, a), ALIAS.get(b, b)
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    sc = difflib.SequenceMatcher(None, a, b).ratio()
    if ta & tb:
        sc = max(sc, 0.6 + 0.4 * len(ta & tb) / len(ta | tb))
    if a in b or b in a:
        sc = max(sc, 0.8)
    return sc


def cmd_odds(a):
    os.makedirs(a.odds, exist_ok=True)
    for s in a.fd:
        url = FD_URL.format(s=s)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=40) as r:
                b = r.read()
            if b[:2] == b"\x1f\x8b":
                b = gzip.decompress(b)
            open(os.path.join(a.odds, f"I1_{s}.csv"), "wb").write(b)
            print("ok", url, len(b), "bytes")
        except Exception as e:                                      # noqa: BLE001
            print("FAILED", url, e)


def _f(x):
    try:
        v = float(x)
        return v if v > 1.0 else None
    except (TypeError, ValueError):
        return None


def load_odds(d):
    rows = []
    if not os.path.isdir(d):
        return rows
    for f in sorted(os.listdir(d)):
        if not f.endswith(".csv"):
            continue
        txt = open(os.path.join(d, f), "rb").read().decode("utf-8-sig", "ignore")
        for r in csv.DictReader(io.StringIO(txt)):
            ds = r.get("Date") or ""
            for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                try:
                    day = dt.datetime.strptime(ds, fmt).date()
                    break
                except ValueError:
                    day = None
            if not day or not r.get("HomeTeam"):
                continue
            ou = None
            for o, u in (("PC>2.5", "PC<2.5"), ("AvgC>2.5", "AvgC<2.5"), ("P>2.5", "P<2.5"), ("Avg>2.5", "Avg<2.5"), ("B365>2.5", "B365<2.5")):
                if _f(r.get(o)) and _f(r.get(u)):
                    ou = (_f(r[o]), _f(r[u]), o)
                    break
            ou_bet = None
            for o, u in (("AvgC>2.5", "AvgC<2.5"), ("PC>2.5", "PC<2.5"), ("Avg>2.5", "Avg<2.5")):
                if _f(r.get(o)) and _f(r.get(u)):
                    ou_bet = (_f(r[o]), _f(r[u]))
                    break
            x12 = None
            for h, dd, aa in (("PSCH", "PSCD", "PSCA"), ("AvgCH", "AvgCD", "AvgCA"), ("PSH", "PSD", "PSA"), ("AvgH", "AvgD", "AvgA"), ("B365H", "B365D", "B365A")):
                if _f(r.get(h)) and _f(r.get(dd)) and _f(r.get(aa)):
                    x12 = (_f(r[h]), _f(r[dd]), _f(r[aa]))
                    break
            rows.append({"day": day, "home": r["HomeTeam"], "away": r["AwayTeam"], "ou": ou, "ou_bet": ou_bet, "x12": x12})
    return rows


def join_odds(preds, odds):
    by = defaultdict(list)
    for o in odds:
        by[o["day"].toordinal()].append(o)
    used, out, miss = set(), {}, []
    for r in preds:
        d = d_ord(r["date"])
        best = None
        for dd in (d, d - 1, d + 1):
            for o in by.get(dd, []):
                if id(o) in used:
                    continue
                sc = nsim(r["home"], o["home"]) + nsim(r["away"], o["away"])
                pen = 0 if dd == d else 0.05
                if best is None or sc - pen > best[0]:
                    best = (sc - pen, o)
        if best and best[0] >= 1.0 and nsim(r["home"], best[1]["home"]) >= 0.4 and nsim(r["away"], best[1]["away"]) >= 0.4:
            used.add(id(best[1]))
            out[r["id"]] = best[1]
        else:
            miss.append((r["date"], r["home"], r["away"], best[1]["home"] + " / " + best[1]["away"] if best else None))
    return out, miss


def info_weight(y, pm, pq):
    """y ~ sigmoid(logit(pm) + b*(logit(pq)-logit(pm)))  -> (b, se)"""
    b = 0.0
    for _ in range(30):
        g = h = 0.0
        for yy, m, q in zip(y, pm, pq):
            d = logit(q) - logit(m)
            p = sig(logit(m) + b * d)
            g += (yy - p) * d
            h += p * (1 - p) * d * d
        if h < 1e-9:
            return float("nan"), float("nan")
        step = g / h
        b += step
        if abs(step) < 1e-8:
            break
    return b, 1 / math.sqrt(max(h, 1e-12))


def market_block(preds, joined, hk="xh", ak="xa", name="player formula"):
    sel = [r for r in preds if r["id"] in joined and joined[r["id"]]["ou"]]
    print(f"\n--- vs closing market (football-data.co.uk) -- {name}: {len(sel)} matches with O/U 2.5 odds ---")
    if len(sel) < 50:
        print("   too few joined matches"); return
    y, pm, pq, pb = [], [], [], []
    for r in sel:
        o, u, _ = joined[r["id"]]["ou"]
        io_, iu = 1 / o, 1 / u
        pm.append(io_ / (io_ + iu))
        pq.append(min(max(p_over(r[hk] + r[ak], 2.5), 0.02), 0.98))
        pb.append(min(max(p_over(r["th"] + r["ta"], 2.5), 0.02), 0.98))
        y.append(1.0 if r["hg"] + r["ag"] > 2.5 else 0.0)
    br = lambda p: sum((a - b) ** 2 for a, b in zip(p, y)) / len(y)
    print(f"   O/U 2.5 Brier: market {br(pm):.4f}  player-model {br(pq):.4f}  team-level {br(pb):.4f}  (actual over rate {sum(y) / len(y):.3f}, market mean {sum(pm) / len(pm):.3f}, model mean {sum(pq) / len(pq):.3f})")
    d = [(a - yy) ** 2 - (b - yy) ** 2 for a, b, yy in zip(pq, pm, y)]
    m, se = mean_se(d)
    print(f"   player-model minus market Brier {m:+.4f} (se {se:.4f})  [negative = model better]")
    b, se = info_weight(y, pm, pq)
    print(f"   information weight b of player-model beyond market: {b:+.2f} (se {se:.2f})   [0 = nothing beyond market, 1 = model right]")
    b2, se2 = info_weight(y, pm, pb)
    print(f"   same for team-level model: {b2:+.2f} (se {se2:.2f})")
    print("   flat 1-unit bets at average closing odds (no fee), edge = model prob - 1/odds:")
    for thr in (0.0, 0.03, 0.05, 0.08):
        pl = []
        for r, yy in zip(sel, y):
            ob = joined[r["id"]]["ou_bet"]
            if not ob:
                continue
            po = p_over(r[hk] + r[ak], 2.5)
            eo, eu = po - 1 / ob[0], (1 - po) - 1 / ob[1]
            if eo >= thr and eo >= eu:
                pl.append((ob[0] - 1) if yy == 1 else -1.0)
            elif eu >= thr:
                pl.append((ob[1] - 1) if yy == 0 else -1.0)
        if len(pl) > 5:
            m, se = mean_se(pl)
            print(f"     edge>={thr:.2f}: {len(pl):4d} bets  ROI {m * 100:+6.1f}% (se {se * 100:.1f}%)")
    # 1X2
    s2 = [r for r in sel if joined[r["id"]]["x12"]]
    if len(s2) > 50:
        def rps_like(ph):
            return sum(-math.log(max(p[0 if r['hg'] > r['ag'] else 1 if r['hg'] == r['ag'] else 2], 1e-9)) for r, p in ph) / len(ph)
        pm3, pq3 = [], []
        for r in s2:
            h, dd, a = joined[r["id"]]["x12"]
            z = 1 / h + 1 / dd + 1 / a
            pm3.append((r, (1 / h / z, 1 / dd / z, 1 / a / z)))
            pq3.append((r, outcome_probs(r[hk], r[ak])))
        print(f"   1X2 log loss ({len(s2)} matches): market {rps_like(pm3):.4f}  player-model {rps_like(pq3):.4f}")


# ------------------------------------------------------------------------------------------------ commands
def cmd_backtest(a):
    ms = sa_data.load_matches(a.dir)
    ms = [m for m in ms if m["goals_ok"]]
    seasons = sorted({m["season"] for m in ms})
    print(f"{len(ms)} matches with reconciled goals; seasons {seasons}")
    ts = a.tune_season
    # 1. league home/away averages: recency half-life
    rows = tune_league(ms, ts, [None, 730, 365, 240, 120])
    print(f"\nLeague-average half-life (tuned on season {ts}, league baseline only; goals loglik/match):")
    for ll, g in rows:
        print(f"   half-life={str(g):>5}   {ll:.4f}")
    lg_hl = rows[0][1]
    print(f"   chosen: {lg_hl}" + ("   WARNING: edge of grid" if lg_hl in (None, 120) else ""))
    # 2. team-level baseline gets its own tuning (so the comparison is fair)
    rows = tune_team(ms, ts, [3, 5, 10, 20, 40, 80, 160], [None, 730, 365], lg_hl)
    print("\nTeam-level baseline tuning (prior in matches, half-life):")
    for ll, tp, hl in rows[:5]:
        print(f"   prior={tp:>3}  half-life={str(hl):>5}   {ll:.4f}")
    tp, thl = rows[0][1], rows[0][2]
    print(f"   chosen: prior {tp}, half-life {thl}" + ("   WARNING: edge of grid" if tp in (3, 160) else ""))
    # 3. player models
    grid_P, grid_hl = [2, 3, 5, 10, 20, 40, 80, 160, 320], [None, 730, 365]
    chosen = {}
    for adj, nm in ((False, "RAW formula"), (True, "ADJUSTED (opponent strength + venue)")):
        res = tune(ms, ts, grid_P, grid_hl, "actual", adj, lg_hl)
        print(f"\nTuning {nm} on season {ts} (earlier seasons are burn-in; goals loglik/match):")
        for ll, P, hl in res[:6]:
            print(f"   prior={P:>4}  half-life={str(hl):>5}   {ll:.4f}")
        ll, P, hl = res[0]
        print(f"   chosen: prior {P} (90-min units), half-life {hl}" + ("   WARNING: best prior is at the edge of the grid" if P in (grid_P[0], grid_P[-1]) else ""))
        chosen[adj] = (P, hl)
    test = [x for x in seasons if x > ts]
    odds = load_odds(a.odds)
    for mode in ("actual", "last"):
        raw = Model(prior90=chosen[False][0], half_life=chosen[False][1], lineup=mode, adjust=False, lg_half_life=lg_hl).run(ms)
        adj = Model(prior90=chosen[True][0], half_life=chosen[True][1], lineup=mode, adjust=True, lg_half_life=lg_hl).run(ms)
        tm = Model(prior90=1e9, team_prior=tp, half_life=thl, lg_half_life=lg_hl).run(ms)
        for r, q, t in zip(raw, adj, tm):
            assert r["id"] == q["id"] == t["id"]
            r["yh"], r["ya"] = q["xh"], q["xa"]
            r["th"], r["ta"] = t["th"], t["ta"]
            r["bh"], r["ba"] = t["bh"], t["ba"]
        bl = tune_blend(raw, ts, ("yh", "ya"), ("th", "ta"))
        w = bl[0][1]
        if mode == "actual":
            print("\nBlend weight on the adjusted player model (rest = team-level), tuned on", ts, ":", ", ".join(f"w={x[1]:.1f}: {x[0]:.4f}" for x in sorted(bl, key=lambda z: z[1])))
            print(f"   chosen w = {w:.1f}" + ("   WARNING: edge (0 or 1)" if w in (0.0, 1.0) else ""))
        blend_keys(raw, ("yh", "ya"), ("th", "ta"), w, ("zh", "za"))
        sel = [r for r in raw if r["season"] in test]
        lab = "lineup = the actual starting XI (known ~1h before kickoff)" if mode == "actual" else "lineup = each team's previous starting XI (deployable days ahead)"
        report_block(sel, f"TEST seasons {test}; {lab}")
        if mode == "actual":
            rotation_block(sel, VARIANTS)
            if odds:
                joined, miss = join_odds(sel, odds)
                print(f"\nodds join: {len(joined)}/{len(sel)} matches joined", f"; unmatched examples: {miss[:4]}" if miss else "")
                market_block(sel, joined, "xh", "xa", "RAW formula")
                market_block(sel, joined, "yh", "ya", "ADJUSTED formula")
                market_block(sel, joined, "zh", "za", "BLEND (adjusted player + team-level)")
            else:
                print("\n(no odds files in", a.odds, "-- run:  python sa_model.py odds)")


def cmd_compare(a):
    """Team formula (team scoring index x opponent goals-allowed index x league average) vs the player plus-minus formula, same walk-forward."""
    ms = [m for m in sa_data.load_matches(a.dir) if m["goals_ok"]]
    seasons = sorted({m["season"] for m in ms})
    test = [x for x in seasons if x > a.tune_season]
    print(f"{len(ms)} matches; test seasons {test}.  Settings (tuned earlier on {a.tune_season}): league half-life {a.lg_half_life}, "
          f"team shrunk prior {a.team_prior}/half-life {a.team_half_life}, player raw prior {a.p_raw}, player adjusted prior {a.p_adj}")
    lg = a.lg_half_life
    raw = Model(prior90=a.p_raw, lg_half_life=lg).run(ms)
    adj = Model(prior90=a.p_adj, adjust=True, lg_half_life=lg).run(ms)
    pure = Model(prior90=1e9, team_prior=1e-9, team_min=a.min_games, lg_half_life=lg).run(ms)
    tuned = Model(prior90=1e9, team_prior=a.team_prior, half_life=a.team_half_life, lg_half_life=lg).run(ms)
    cur = Model(prior90=1e9, team_prior=1e-9, team_min=a.min_games, half_life=a.season_half_life, lg_half_life=lg).run(ms)
    for r, q, p, t, c in zip(raw, adj, pure, tuned, cur):
        assert r["id"] == q["id"] == p["id"] == t["id"] == c["id"]
        r["yh"], r["ya"] = q["xh"], q["xa"]
        r["t0h"], r["t0a"] = p["th"], p["ta"]
        r["th"], r["ta"] = t["th"], t["ta"]
        r["t2h"], r["t2a"] = c["th"], c["ta"]
        r["bh"], r["ba"] = t["bh"], t["ba"]
    sel = [r for r in raw if r["season"] in test]
    variants = [("TEAM formula, pure (all history, no shrink)", "t0h", "t0a"),
                (f"TEAM formula, pure, recent weighting ({a.season_half_life:g}d)", "t2h", "t2a"),
                ("PLAYER formula (raw)", "xh", "xa"), ("PLAYER formula + opponent & venue adj.", "yh", "ya")]
    print("\nTEAM formula: attack index = team goals scored per game / league avg; defence index = goals allowed per game / league avg"
          f" (<1.0 = better); xG = attack x opponent defence x league avg home/away goals.  Pure = no shrinkage, index 1.0 until {a.min_games} games are played.")
    print("(the row 'team attack x defence' below is the SHRUNK + TUNED version of the same team formula)")
    report_block(sel, "TEAM formula vs PLAYER formula", variants)
    print("\n--- HEAD TO HEAD (paired by match; goals loglik: + = first model better; O/U2.5 Brier: - = first model better) ---")
    keys = {v[0]: v for v in variants}
    for an, bn in (("PLAYER formula (raw)", "TEAM formula, pure (all history, no shrink)"),
                   ("PLAYER formula + opponent & venue adj.", "TEAM formula, pure (all history, no shrink)"),
                   ("PLAYER formula + opponent & venue adj.", f"TEAM formula, pure, recent weighting ({a.season_half_life:g}d)")):
        A, B = keys[an], keys[bn]
        SA, SB = score(sel, A[1], A[2]), score(sel, B[1], B[2])
        d, se = paired(SA["ll"], SB["ll"]); db, seb = paired(SA["brier25"], SB["brier25"])
        print(f"   {an[:40]:40s} vs {bn[:52]:52s} loglik {d:+.4f} (se {se:.4f})  Brier {db:+.4f} (se {seb:.4f})")
        # who wins more matches on goals likelihood
        wins = sum(1 for x, y in zip(SA["ll"], SB["ll"]) if x > y)
        print(f"      first model has the higher likelihood in {wins}/{len(sel)} matches")
    rotation_block(sel, variants)
    odds = load_odds(a.odds)
    if odds:
        joined, miss = join_odds(sel, odds)
        print(f"\nodds join: {len(joined)}/{len(sel)} matches joined")
        for nm, hk, ak in variants:
            market_block(sel, joined, hk, ak, nm)
        market_block(sel, joined, "th", "ta", "TEAM formula, shrunk + tuned")


def cmd_table(a):
    ms = [m for m in sa_data.load_matches(a.dir) if m["goals_ok"]]
    preds = Model(prior90=a.prior, half_life=a.half_life, lineup="actual", adjust=a.adjust, lg_half_life=a.lg_half_life).run(ms)[-a.n:]
    print(f"{'date':10s} {'home':22s} {'away':22s} {'xG h':>5s} {'xG a':>5s} | {'H O.5':>6s}{'H O1.5':>7s} {'A O.5':>6s}{'A O1.5':>7s} | {'T O1.5':>6s}{'O2.5':>6s}{'O3.5':>6s} | result")
    for r in preds:
        print(f"{r['date']:10s} {r['home'][:22]:22s} {r['away'][:22]:22s} {r['xh']:5.2f} {r['xa']:5.2f} | "
              f"{p_over(r['xh'], .5):6.2f}{p_over(r['xh'], 1.5):7.2f} {p_over(r['xa'], .5):6.2f}{p_over(r['xa'], 1.5):7.2f} | "
              f"{p_over(r['xh'] + r['xa'], 1.5):6.2f}{p_over(r['xh'] + r['xa'], 2.5):6.2f}{p_over(r['xh'] + r['xa'], 3.5):6.2f} | {r['hg']}-{r['ag']}")


# ------------------------------------------------------------------------------------------------ sharpening study
SHARPEN_GRID = (1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 2.0, 2.5)


def sharpen_xg(x, base, k):
    """Stretch an expected-goals figure away from the league average: base * (x / base) ** k  (k = 1: no change)."""
    return base * (max(x, 1e-6) / base) ** k


def _res(r):
    return 0 if r["hg"] > r["ag"] else (1 if r["hg"] == r["ag"] else 2)


def _metrics(rows, hk, ak, k):
    """per-match lists for expected-goals keys hk/ak sharpened by exponent k."""
    out = {"ll": [], "x12": [], "b3": [], "o25": [], "t15": [], "btts": [], "ph": [], "pd": [], "pa": [], "xh": [], "xa": []}
    for r in rows:
        xh, xa = sharpen_xg(r[hk], r["lgh"], k), sharpen_xg(r[ak], r["lga"], k)
        ph, pdw, pa = outcome_probs(xh, xa)
        y = _res(r)
        p = (ph, pdw, pa)
        out["ll"].append(pois_ll(xh, r["hg"]) + pois_ll(xa, r["ag"]))
        out["x12"].append(-math.log(max(p[y], 1e-9)))
        out["b3"].append(sum((p[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)))
        po = p_over(xh + xa, 2.5)
        yo = 1.0 if r["hg"] + r["ag"] > 2.5 else 0.0
        out["o25"].append((po - yo) ** 2)
        out["t15"].append(((p_over(xh, 1.5) - (1.0 if r["hg"] > 1.5 else 0.0)) ** 2 + (p_over(xa, 1.5) - (1.0 if r["ag"] > 1.5 else 0.0)) ** 2) / 2)
        pb = (1 - math.exp(-xh)) * (1 - math.exp(-xa))
        out["btts"].append((pb - (1.0 if r["hg"] > 0 and r["ag"] > 0 else 0.0)) ** 2)
        out["ph"].append(ph); out["pd"].append(pdw); out["pa"].append(pa); out["xh"].append(xh); out["xa"].append(xa)
    return out


def _dump_rows(rows, mkt, path, league):
    """Write per-match expected goals (three variants), results and the de-vigged closing 1X2 so leagues can be pooled."""
    import json as _json
    recs = []
    for r in rows:
        d = {k: r[k] for k in ("id", "season", "hg", "ag", "lgh", "lga", "yh", "ya", "th", "ta", "zh", "za")}
        d["id"] = str(d["id"])
        if r["id"] in mkt:
            d["mkt"] = list(mkt[r["id"]])
        recs.append(d)
    with open(path, "w") as f:
        _json.dump({"league": league, "rows": recs}, f)
    print(f"wrote {len(recs)} matches to {path}")


def sharpen_study(ms, odds, ts, grid=SHARPEN_GRID, out=print, dump=None, league=""):
    """Walk-forward: does stretching the expected goals away from the league average (k > 1) fix the too-timid favourites?
    k is chosen on the tune season only (earlier seasons are burn-in); tested on later seasons.  Previous-XI lineups (deployable)."""
    seasons = sorted({m["season"] for m in ms})
    test = [s for s in seasons if s > ts]
    adj = Model(prior90=10.0, half_life=None, lineup="last", adjust=True, lg_half_life=240.0).run(ms)
    tm = Model(prior90=1e9, team_prior=10.0, half_life=730.0, lg_half_life=240.0).run(ms)
    for r, t in zip(adj, tm):
        assert r["id"] == t["id"]
        r["th"], r["ta"] = t["th"], t["ta"]
        r["yh"], r["ya"] = r["xh"], r["xa"]
        r["zh"], r["za"] = 0.4 * r["yh"] + 0.6 * r["th"], 0.4 * r["ya"] + 0.6 * r["ta"]
    tune_rows = [r for r in adj if r["season"] == ts]
    test_rows = [r for r in adj if r["season"] in test]
    out(f"\n=== SHARPENING STUDY: expected goals stretched away from the league average, x' = league * (x / league) ** k  (chosen on season {ts}: "
        f"{len(tune_rows)} matches; tested on {test}: {len(test_rows)} matches; previous-XI lineups) ===")
    variants = [("ADJUSTED player formula (what the platform shows)", "yh", "ya"), ("TEAM-LEVEL", "th", "ta"), ("BLEND 0.4 player / 0.6 team", "zh", "za")]
    results = {}
    mkt = {}
    for r in test_rows:
        j = odds.get(r["id"]) if odds else None
        if j and j.get("x12"):
            h, d, a = j["x12"]
            z = 1 / h + 1 / d + 1 / a
            mkt[r["id"]] = (1 / h / z, 1 / d / z, 1 / a / z)
    for nm, hk, ak in variants:
        tune = {k: _metrics(tune_rows, hk, ak, k) for k in grid}
        k_goals = max(grid, key=lambda k: mean_se(tune[k]["ll"])[0])
        k_1x2 = min(grid, key=lambda k: mean_se(tune[k]["x12"])[0])
        out(f"\n{nm}")
        out("   tune-season goals loglik / 1X2 logloss by k:  " + "   ".join(f"k={k:.1f}: {mean_se(tune[k]['ll'])[0]:.4f}/{mean_se(tune[k]['x12'])[0]:.4f}" for k in grid))
        out(f"   chosen on the tune season: k = {k_goals:.1f} by goals loglik;  k = {k_1x2:.1f} by 1X2 logloss" +
            ("   (grid edge!)" if k_goals == grid[-1] or k_1x2 == grid[-1] else ""))
        base = _metrics(test_rows, hk, ak, 1.0)
        cand = [("uncorrected (k=1)", 1.0, base)]
        for lab, k in ((f"sharpened, k={k_goals:.1f} (goals)", k_goals), (f"sharpened, k={k_1x2:.1f} (1X2)", k_1x2)):
            if k != 1.0 and all(abs(k - c[1]) > 1e-9 for c in cand):
                cand.append((lab, k, _metrics(test_rows, hk, ak, k)))
        out(f"   {'TEST seasons':<30}{'goals ll':>10}{'1X2 ll':>9}{'1X2 Brier':>10}{'O2.5':>8}{'team1.5':>9}{'BTTS':>8}   home win pred/actual   draw pred/actual   xG home/away (actual {mean_se([r['hg'] for r in test_rows])[0]:.3f}/{mean_se([r['ag'] for r in test_rows])[0]:.3f})")
        for lab, k, M in cand:
            out(f"   {lab:<30}{mean_se(M['ll'])[0]:>10.4f}{mean_se(M['x12'])[0]:>9.4f}{mean_se(M['b3'])[0]:>10.4f}{mean_se(M['o25'])[0]:>8.4f}{mean_se(M['t15'])[0]:>9.4f}{mean_se(M['btts'])[0]:>8.4f}"
                f"   {mean_se(M['ph'])[0] * 100:5.1f}% / {sum(1 for r in test_rows if r['hg'] > r['ag']) / len(test_rows) * 100:5.1f}%"
                f"      {mean_se(M['pd'])[0] * 100:5.1f}% / {sum(1 for r in test_rows if r['hg'] == r['ag']) / len(test_rows) * 100:5.1f}%"
                f"     {mean_se(M['xh'])[0]:.3f}/{mean_se(M['xa'])[0]:.3f}")
        entry = {"k_goals": k_goals, "k_1x2": k_1x2, "paired": {}}
        for lab, k, M in cand[1:]:
            d = {}
            for key, sgn in (("ll", 1.0), ("x12", -1.0), ("b3", -1.0), ("o25", -1.0), ("t15", -1.0), ("btts", -1.0)):
                d[key] = mean_se([sgn * (a - b) for a, b in zip(M[key], base[key])])
            entry["paired"][lab] = (k, d)
            out(f"   paired vs uncorrected, {lab}: goals loglik {d['ll'][0]:+.4f} (se {d['ll'][1]:.4f})   1X2 logloss {-d['x12'][0]:+.4f} (se {d['x12'][1]:.4f})"
                f"   1X2 Brier {-d['b3'][0]:+.4f} (se {d['b3'][1]:.4f})   O2.5 Brier {-d['o25'][0]:+.4f} (se {d['o25'][1]:.4f})   team 1.5 Brier {-d['t15'][0]:+.4f} (se {d['t15'][1]:.4f})   [loglik: + better; others: - better]")
            for s in test:
                idx = [i for i, r in enumerate(test_rows) if r["season"] == s]
                dd = mean_se([M["x12"][i] - base["x12"][i] for i in idx])
                out(f"      season {s}: 1X2 logloss change {dd[0]:+.4f} (se {dd[1]:.4f}), n={len(idx)}")
        # calibration of the favourite side: home-win bins, uncorrected vs best sharpened
        if len(cand) > 1:
            Mb = cand[1][2]
            out("   home-win calibration (bin by predicted probability: n, uncorrected pred / sharpened pred / actual):")
            for lo, hi in ((0, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 1.01)):
                idx = [i for i in range(len(test_rows)) if lo <= base["ph"][i] < hi]
                if idx:
                    out(f"      {lo:.2f}-{min(hi, 1.0):.2f}: n={len(idx):3d}  {mean_se([base['ph'][i] for i in idx])[0]:.3f} / {mean_se([Mb['ph'][i] for i in idx])[0]:.3f} / {sum(1 for i in idx if test_rows[i]['hg'] > test_rows[i]['ag']) / len(idx):.3f}")
        if mkt:
            idx = [i for i, r in enumerate(test_rows) if r["id"] in mkt]
            def mll(i):
                return -math.log(max(mkt[test_rows[i]["id"]][_res(test_rows[i])], 1e-9))
            ml = mean_se([mll(i) for i in idx])[0]
            line = f"   vs closing 1X2 market ({len(idx)} matches): market {ml:.4f};"
            for lab, k, M in cand:
                dm = mean_se([M["x12"][i] - mll(i) for i in idx])
                line += f"  {lab.split(',')[0] if k != 1.0 else 'uncorrected'}: {mean_se([M['x12'][i] for i in idx])[0]:.4f} (minus market {dm[0]:+.4f}, se {dm[1]:.4f});"
                entry.setdefault("vs_market", {})[lab] = dm
            out(line)
        results[nm] = entry
    if dump:
        _dump_rows(tune_rows + test_rows, mkt, dump, league)
    out("\nRule: adopt sharpening only if, for the platform's variant, the goals loglik AND the 1X2 logloss both improve by more than about two se on the TEST seasons "
        "(k chosen on the tune season, so the test seasons did not choose it) and the O/U 2.5 and team 1.5 Briers do not get worse.  Otherwise leave the model as it is.")
    return results


def cmd_sharpen(a):
    ms = [m for m in sa_data.load_matches(a.dir) if m["goals_ok"]]
    odds = load_odds(a.odds)
    joined = {}
    if odds:
        sel = Model(prior90=1e9, team_prior=10.0, half_life=730.0, lg_half_life=240.0).run(ms)
        joined, _ = join_odds([r for r in sel if r["season"] > a.tune_season], odds)
    sharpen_study(ms, joined, a.tune_season, dump=getattr(a, "dump", None), league=getattr(a, "league", "") or "Serie A")


def cmd_probe(a, get=None, http=None, today=None, out=print):
    """Is the feed usable for this league?  Reads a few recent completed matches from ESPN and the current football-data file."""
    get = get or sa_data.get_json
    http = http or _fd_get
    today = today or dt.date.today()
    found, fails = [], 0
    for k in range(1, 22):
        d = today - dt.timedelta(days=k)
        try:
            evs = sa_data.scoreboard_events(get, d)
        except Exception as e:                                      # noqa: BLE001
            out(f"scoreboard {d} FAILED: {e}")
            fails += 1
            if fails >= 2 and not found:
                out("PROBLEM: ESPN unreachable from here (two scoreboard calls failed): check the internet connection / proxy, then re-run")
                return 1
            continue
        found += [(d, e) for e in evs if e["completed"]]
        if len(found) >= 40:
            break
    out(f"ESPN {sa_data.BASE.rsplit('/', 1)[-1]}: {len(found)} completed matches in the last 3 weeks")
    problems = 0
    if not found:
        out("PROBLEM: no completed matches found (international break, wrong slug, or feed unreachable)")
        return 1
    pick = [found[0], found[len(found) // 2], found[-1]]
    for d, e in pick:
        try:
            s = get(sa_data.SUMMARY + str(e["id"]))
            meta = {k: e[k] for k in ("id", "date", "name", "home", "away")}
            meta["season"] = d.year if d.month >= 7 else d.year - 1
            m = sa_data.parse_match(sa_data.compact_summary(s, meta))
        except Exception as ex:                                     # noqa: BLE001
            out(f"{e['name']}: summary FAILED: {ex}")
            problems += 1
            continue
        out(f"\n{d} {e['name']}  final {m['hg']}-{m['ag']}   goals reconcile: {m['goals_ok']}")
        for side in ("home", "away"):
            ps = [p for p in m["players"] if p["side"] == side]
            st = [p for p in ps if p["start"] == 0.0]
            gr = {}
            for p in st:
                gr[p["group"]] = gr.get(p["group"], 0) + 1
            mins = sum(((p["end"] if p["end"] is not None else m["end"]) - p["start"]) for p in ps if p["start"] is not None) / m["end"]
            subs = sum(1 for p in ps if p["start"] not in (None, 0.0))
            out(f"   {side}: {len(st)} starters {dict(sorted(gr.items(), key=lambda x: str(x[0])))}   substitutes used {subs}   man-minutes on pitch {mins:.2f} (11 = no red cards)")
            if len(st) != 11 or None in gr or not (9.0 <= mins <= 11.05):
                problems += 1
                out("   PROBLEM: starters / positions / man-minutes look wrong")
        if m["problems"]:
            out("   parse notes:", "; ".join(m["problems"][:4]))
        if not m["goals_ok"]:
            problems += 1
    # football-data
    y = today.year if today.month >= 7 else today.year - 1
    code = f"{y % 100:02d}{(y + 1) % 100:02d}"
    try:
        raw = http(FD_URL.format(s=code))
        txt = raw.decode("utf-8-sig", "ignore")
        rows = list(csv.DictReader(io.StringIO(txt)))
        cols = set(rows[0]) if rows else set()
        out(f"\nfootball-data {FD_URL.format(s=code)}: {len(rows)} rows, last date {rows[-1].get('Date') if rows else None}")
        for nm, group in (("1X2 closing (Pinnacle)", ("PSCH", "PSCD", "PSCA")), ("1X2 closing (average)", ("AvgCH", "AvgCD", "AvgCA")),
                          ("O/U 2.5 closing (Pinnacle)", ("PC>2.5", "PC<2.5")), ("O/U 2.5 closing (average)", ("AvgC>2.5", "AvgC<2.5"))):
            out(f"   {nm}: {'present' if all(c in cols for c in group) else 'MISSING'} {group}")
        fd_teams = sorted({r['HomeTeam'] for r in rows} | {r['AwayTeam'] for r in rows})
        espn_teams = sorted({e[side]["name"] for _, e in found for side in ("home", "away")})
        bad = []
        for t in espn_teams:
            best = max(fd_teams, key=lambda x: nsim(t, x)) if fd_teams else None
            if best is None or nsim(t, best) < 0.8:
                bad.append((t, best))
        out(f"   team names: {len(espn_teams) - len(bad)}/{len(espn_teams)} ESPN names matched to football-data" + (f"; UNMATCHED {bad}" if bad else ""))
        if bad or not rows:
            problems += 1
    except Exception as ex:                                         # noqa: BLE001
        out(f"\nfootball-data FAILED: {type(ex).__name__}: {ex}")
        problems += 1
    out("\nPROBE: " + ("PASS" if not problems else f"{problems} PROBLEM(S) - paste this output"))
    return problems


def _fd_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=40) as r:
        b = r.read()
    return gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["odds", "backtest", "compare", "table", "probe", "sharpen"])
    ap.add_argument("--dir", default="sa_espn")
    ap.add_argument("--odds", default="sa_odds")
    ap.add_argument("--fd", nargs="+", default=["2324", "2425", "2526", "2627"])
    ap.add_argument("--tune-season", type=int, default=2024)
    ap.add_argument("--dump", default=None)
    ap.add_argument("--league", default="")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--prior", type=float, default=40.0)
    ap.add_argument("--half-life", type=float, default=None)
    ap.add_argument("--adjust", action="store_true")
    ap.add_argument("--team-prior", type=float, default=10.0)
    ap.add_argument("--team-half-life", type=float, default=365.0)
    ap.add_argument("--season-half-life", type=float, default=180.0)
    ap.add_argument("--min-games", type=int, default=8)
    ap.add_argument("--p-raw", type=float, default=5.0)
    ap.add_argument("--p-adj", type=float, default=10.0)
    ap.add_argument("--lg-half-life", type=float, default=240.0)
    a = ap.parse_args()
    {"odds": cmd_odds, "backtest": cmd_backtest, "compare": cmd_compare, "table": cmd_table, "probe": cmd_probe, "sharpen": cmd_sharpen}[a.cmd](a)
'''


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__
        module.__file__ = f"<sa_platform:{name}.py>"
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
import sa_data as bd                                                           # noqa: E402
import sa_model as bm                                                          # noqa: E402
from sa_io import FileLock, LockBusy, atomic_json_dump, load_json_retry       # noqa: E402

FD_FIXTURES = "https://www.football-data.co.uk/fixtures.csv"
FD_SEASON = "https://www.football-data.co.uk/mmz4281/{c}/I1.csv"
DEFAULT_PARAMS = {"prior90": 10.0, "half_life": None, "lg_half_life": 240.0, "adjust": True, "draw_boost": 1.0}
MIN_MATCHES = 120                  # fewer reconciled matches than this -> 'warming up'
LOOKAHEAD_DAYS = 10
LINEUP_WINDOW_H = 3.0              # try for the confirmed lineup this long before kickoff
RETRO_BACK = 1                     # retrospective walk-forward record: current season and this many seasons back
STATE_FILE, FIXTURES_FILE, LOG_FILE, RESULTS_FILE, PARAMS_FILE = "sa_state.json", "sa_fixtures.json", "sa_log.jsonl", "sa_results.json", "sa_params.json"
VALIDATION = ("Serie A: same formula as the Bundesliga / Championship / J1 builds; run `backtest` on your data to see how it does "
              "(sister leagues: no demonstrated edge against the closing market): research display only")


def clean(obj):
    """JSON-safe: NaN/inf -> null, tuples -> lists, tuple keys -> 'a-b' strings."""
    if isinstance(obj, dict):
        return {(f"{k[0]}-{k[1]}" if isinstance(k, tuple) else str(k)): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _parse_iso(s):
    try:
        t = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _stat_key(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


def _http_bytes(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (sports-hub-serie-a)", "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def season_of(d):
    """Serie A season (start year) a date belongs to: Jul-Dec -> that year, Jan-Jun -> the year before."""
    return d.year if d.month >= 7 else d.year - 1


def fd_code(y):
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def devig2(o_a, o_b):
    ia, ib = 1.0 / o_a, 1.0 / o_b
    return ia / (ia + ib), ib / (ia + ib)


def market_from_row(row):
    """football-data row -> {H, D, A, o25, u25, odds{...}} (de-vigged probabilities), or None."""
    if not row:
        return None
    out = {"odds": {}}
    if row.get("x12"):
        h, d, a = row["x12"]
        z = 1 / h + 1 / d + 1 / a
        out.update({"H": 1 / h / z, "D": 1 / d / z, "A": 1 / a / z})
        out["odds"].update({"H": h, "D": d, "A": a})
    ou = row.get("ou_bet") or (row.get("ou")[:2] if row.get("ou") else None)
    if ou:
        out["o25"], out["u25"] = devig2(ou[0], ou[1])
        out["odds"].update({"over25": ou[0], "under25": ou[1]})
    return out if len(out) > 1 else None


def _rec_markets(xh, xa, draw_boost=1.0, lg=None):
    return bm.markets(xh, xa, draw_boost=draw_boost, lg=lg)


# ===================================================================== scoring helpers
def _brier3(p, y):
    return sum((p[k] - (1.0 if k == y else 0.0)) ** 2 for k in ("H", "D", "A"))


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _se(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n)


def score_records(recs, draw_boost=1.0):
    """recs: [{xh, xa, bh, ba, hg, ag, mkt (market_from_row dict or None)}]  -> scorecard dict (model vs league average vs market)."""
    n = len(recs)
    if not n:
        return {"n": 0}
    rows = []
    for r in recs:
        M = _rec_markets(r["xh"], r["xa"], draw_boost, lg=(r["bh"], r["ba"]))
        B = _rec_markets(r["bh"], r["ba"], draw_boost, lg=(r["bh"], r["ba"]))
        y = "H" if r["hg"] > r["ag"] else ("D" if r["hg"] == r["ag"] else "A")
        rows.append((r, M, B, y, {"H": M["home"], "D": M["draw"], "A": M["away"]}, {"H": B["home"], "D": B["draw"], "A": B["away"]}))
    ll = lambda p, y: -math.log(max(p[y], 1e-9))
    out = {"n": n,
           "goals": {"home_expected": _mean([r["xh"] for r, *_ in rows]), "home_actual": _mean([r["hg"] for r, *_ in rows]),
                     "away_expected": _mean([r["xa"] for r, *_ in rows]), "away_actual": _mean([r["ag"] for r, *_ in rows]),
                     "mae_total": _mean([abs(r["xh"] + r["xa"] - r["hg"] - r["ag"]) for r, *_ in rows]),
                     "mae_total_league_avg": _mean([abs(r["bh"] + r["ba"] - r["hg"] - r["ag"]) for r, *_ in rows])}}
    out["x12"] = {"logloss": _mean([ll(p, y) for _, _, _, y, p, _ in rows]), "logloss_league_avg": _mean([ll(b, y) for _, _, _, y, _, b in rows]),
                  "brier": _mean([_brier3(p, y) for _, _, _, y, p, _ in rows]), "brier_league_avg": _mean([_brier3(b, y) for _, _, _, y, _, b in rows]),
                  "accuracy": _mean([1.0 if max(p, key=p.get) == y else 0.0 for _, _, _, y, p, _ in rows]),
                  "home_pred": _mean([p["H"] for *_, p, _ in rows]), "home_rate": _mean([1.0 if y == "H" else 0.0 for _, _, _, y, _, _ in rows]),
                  "draw_pred": _mean([p["D"] for *_, p, _ in rows]), "draw_rate": _mean([1.0 if y == "D" else 0.0 for _, _, _, y, _, _ in rows]),
                  "away_pred": _mean([p["A"] for *_, p, _ in rows]), "away_rate": _mean([1.0 if y == "A" else 0.0 for _, _, _, y, _, _ in rows])}
    d = [ll(p, y) - ll(b, y) for _, _, _, y, p, b in rows]
    out["x12"]["logloss_minus_league_avg"], out["x12"]["se"] = _mean(d), _se(d)
    tot, team = {}, {}
    for ln in ("1.5", "2.5", "3.5", "4.5"):
        ys = [1.0 if r["hg"] + r["ag"] > float(ln) else 0.0 for r, *_ in rows]
        pm = [M["total_over"][ln] for _, M, *_ in rows]
        pb = [B["total_over"][ln] for _, _, B, *_ in rows]
        tot[ln] = {"pred": _mean(pm), "rate": _mean(ys), "brier": _mean([(a - b) ** 2 for a, b in zip(pm, ys)]),
                   "brier_league_avg": _mean([(a - b) ** 2 for a, b in zip(pb, ys)])}
    for ln in ("0.5", "1.5", "2.5", "3.5"):
        ys, pm, pb = [], [], []
        for r, M, B, *_ in rows:
            ys += [1.0 if r["hg"] > float(ln) else 0.0, 1.0 if r["ag"] > float(ln) else 0.0]
            pm += [M["team_over"]["home"][ln], M["team_over"]["away"][ln]]
            pb += [B["team_over"]["home"][ln], B["team_over"]["away"][ln]]
        team[ln] = {"pred": _mean(pm), "rate": _mean(ys), "brier": _mean([(a - b) ** 2 for a, b in zip(pm, ys)]),
                    "brier_league_avg": _mean([(a - b) ** 2 for a, b in zip(pb, ys)])}
    ys = [1.0 if r["hg"] > 0 and r["ag"] > 0 else 0.0 for r, *_ in rows]
    out["total_over"], out["team_over"] = tot, team
    out["btts"] = {"pred": _mean([M["btts"] for _, M, *_ in rows]), "rate": _mean(ys)}

    def bins(vals, edges):
        res = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            s = [(p, y) for p, y in vals if lo <= p < hi]
            if s:
                res.append({"bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(s), "pred": _mean([p for p, _ in s]), "actual": _mean([y for _, y in s])})
        return res
    out["calibration"] = {"home_win": bins([(p["H"], 1.0 if y == "H" else 0.0) for _, _, _, y, p, _ in rows], [0, .3, .4, .5, .6, 1.01]),
                          "draw": bins([(p["D"], 1.0 if y == "D" else 0.0) for _, _, _, y, p, _ in rows], [0, .2, .25, .3, 1.01]),
                          "over_2_5": bins([(M["total_over"]["2.5"], 1.0 if r["hg"] + r["ag"] > 2.5 else 0.0) for r, M, *_ in rows], [0, .45, .55, .65, 1.01])}
    mk = [(r, M, y, p) for r, M, B, y, p, _ in rows if r.get("mkt") and r["mkt"].get("H") is not None]
    if mk:
        dm = [(_brier3(p, y) - _brier3(r["mkt"], y)) for r, M, y, p in mk]
        out["vs_market_1x2"] = {"n": len(mk), "logloss_model": _mean([ll(p, y) for r, M, y, p in mk]), "logloss_market": _mean([ll(r["mkt"], y) for r, M, y, p in mk]),
                                "brier_model": _mean([_brier3(p, y) for r, M, y, p in mk]), "brier_market": _mean([_brier3(r["mkt"], y) for r, M, y, p in mk]),
                                "model_minus_market_brier": _mean(dm), "se": _se(dm), "note": "negative = model better; only trust when |diff| > 2 se"}
    mo = [(r, M, y) for r, M, B, y, p, _ in rows if r.get("mkt") and r["mkt"].get("o25") is not None]
    if mo:
        yy = [1.0 if r["hg"] + r["ag"] > 2.5 else 0.0 for r, M, y in mo]
        dm = [(M["total_over"]["2.5"] - o) ** 2 - (r["mkt"]["o25"] - o) ** 2 for (r, M, y), o in zip(mo, yy)]
        out["vs_market_ou25"] = {"n": len(mo), "brier_model": _mean([(M["total_over"]["2.5"] - o) ** 2 for (r, M, y), o in zip(mo, yy)]),
                                 "brier_market": _mean([(r["mkt"]["o25"] - o) ** 2 for (r, M, y), o in zip(mo, yy)]),
                                 "model_minus_market_brier": _mean(dm), "se": _se(dm), "over_rate": _mean(yy),
                                 "model_pred": _mean([M["total_over"]["2.5"] for r, M, y in mo]), "market_pred": _mean([r["mkt"]["o25"] for r, M, y in mo]),
                                 "note": "negative = model better; only trust when |diff| > 2 se"}
    return out


# ===================================================================== platform
class SerieAPlatform:
    def __init__(self, directory=".", now_fn=None, get=None, http_bytes=None, log_predictions=True, sleep=None):
        self.dir = directory
        self.espn = os.path.join(directory, "espn")
        for sub in ("days", "matches"):
            os.makedirs(os.path.join(self.espn, sub), exist_ok=True)
        os.makedirs(os.path.join(directory, "odds"), exist_ok=True)
        self._now_fn = now_fn
        self._get = get or bd.get_json
        self._http = http_bytes or _http_bytes
        self._log_on = log_predictions
        self._lock = threading.RLock()
        self._parsed = {}
        self._mkey = None
        self._matches = None
        self._run_cache = {}
        self._odds_cache = (None, [])
        self._sigs = None
        self._stop = threading.Event()
        self._busy = {k: False for k in ("data", "fixtures", "odds", "results")}

    # ------------------------------------------------------------------ helpers
    def _now(self):
        n = self._now_fn() if self._now_fn else datetime.datetime.now(datetime.timezone.utc)
        return n if n.tzinfo else n.replace(tzinfo=datetime.timezone.utc)

    def _p(self, name):
        return os.path.join(self.dir, name)

    def params(self):
        p = dict(DEFAULT_PARAMS)
        try:
            u = load_json_retry(self._p(PARAMS_FILE), None)
            if isinstance(u, dict):
                p.update({k: v for k, v in u.items() if k in DEFAULT_PARAMS})
        except Exception:                                  # noqa: BLE001
            pass
        return p

    def _save_state(self, **kw):
        try:
            with self._lock:
                s = load_json_retry(self._p(STATE_FILE), {}) or {}
                s.update(kw)
                atomic_json_dump(self._p(STATE_FILE), s)
        except Exception:                                  # noqa: BLE001
            pass

    def _state(self):
        try:
            return load_json_retry(self._p(STATE_FILE), {}) or {}
        except Exception:                                  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ stored matches -> parsed matches
    def _match_paths(self):
        d = os.path.join(self.espn, "matches")
        return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".json")] if os.path.isdir(d) else []

    def matches(self):
        """Parsed matches with reconciled goals, sorted by date (cached; re-parses only changed files)."""
        paths = self._match_paths()
        try:
            key = (len(paths), max((os.stat(p).st_mtime_ns for p in paths), default=0))
        except OSError:
            key = None
        with self._lock:
            if self._matches is not None and key == self._mkey:
                return self._matches
            out = []
            for p in paths:
                try:
                    mt = os.stat(p).st_mtime_ns
                    c = self._parsed.get(p)
                    if c is None or c[0] != mt:
                        c = (mt, bd.parse_match(json.load(open(p))))
                        self._parsed[p] = c
                    if c[1]["goals_ok"] and c[1]["date"]:
                        out.append(c[1])
                except Exception:                          # noqa: BLE001 -- one bad file must not stop the rest
                    continue
            out.sort(key=lambda m: (m["date"], m["id"]))
            self._matches, self._mkey = out, key
            self._run_cache.clear()
            return out

    def needs_bootstrap(self):
        return len(self.matches()) < MIN_MATCHES

    # ------------------------------------------------------------------ fixtures / odds blobs
    def _fixtures_blob(self):
        try:
            return load_json_retry(self._p(FIXTURES_FILE), None)
        except Exception:                                  # noqa: BLE001
            return None

    def _odds_rows(self):
        d = os.path.join(self.dir, "odds")
        files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".csv")]
        key = _stat_key(*files)
        with self._lock:
            if key == self._odds_cache[0]:
                return self._odds_cache[1]
            rows = bm.load_odds(d)
            self._odds_cache = (key, rows)
            return rows

    def _odds_for(self, ev_like):
        rows = self._odds_rows()
        if not rows:
            return None
        j, _ = bm.join_odds([ev_like], rows)
        return j.get(ev_like["id"])

    # ------------------------------------------------------------------ model runs
    def _pseudo(self, fx):
        k = _parse_iso(fx.get("date"))
        if not k:
            return None
        lu = fx.get("lineups")
        return {"id": str(fx["id"]), "date": k.date().isoformat(), "season": season_of(k), "home": fx["home"]["name"], "away": fx["away"]["name"],
                "hg": 0, "ag": 0, "end": 98.0, "players": [], "goals": [], "goals_ok": True, "_future": True,
                "_lineup": ({s: [(a, b) for a, b in lu[s]] for s in ("home", "away")} if lu and lu.get("home") and lu.get("away") else None)}

    def _model(self):
        p = self.params()
        return bm.Model(prior90=float(p["prior90"]), half_life=p["half_life"], lineup="last", adjust=bool(p["adjust"]),
                        lg_half_life=p["lg_half_life"])

    def _run(self, extra=None):
        """One walk-forward pass over history + upcoming fixtures.  -> {"hist": [...], "fut": {id: rec}} or None."""
        ms = self.matches()
        if len(ms) < MIN_MATCHES:
            return None
        fb = self._fixtures_blob() or {}
        fixtures = [f for f in fb.get("events", []) if f.get("state") in ("pre", "in") and f.get("status") not in ("STATUS_POSTPONED", "STATUS_CANCELED")]
        pseudo = [x for x in (self._pseudo(f) for f in fixtures) if x]
        pseudo += list(extra or [])
        key = (self._mkey, repr(self.params()), repr([(m["id"], m["date"], m["home"], m["away"], repr(m.get("_lineup"))) for m in pseudo]))
        with self._lock:
            if key in self._run_cache:
                return self._run_cache[key]
        have = {m["id"] for m in ms}
        allm = ms + [m for m in pseudo if m["id"] not in have]
        allm.sort(key=lambda m: (m["date"], bool(m.get("_future")), m["id"]))
        recs = self._model().run(allm)
        res = {"hist": [r for r in recs if not r.get("future")], "fut": {r["id"]: r for r in recs if r.get("future")}}
        with self._lock:
            if len(self._run_cache) > 6:
                self._run_cache.clear()
            self._run_cache[key] = res
        return res

    # ------------------------------------------------------------------ teams
    def teams(self):
        ms = self.matches()
        if not ms:
            return []
        last = {}
        for m in ms:
            last[m["home"]] = last[m["away"]] = m["date"]
        cutoff = (datetime.date.fromisoformat(ms[-1]["date"]) - datetime.timedelta(days=400)).isoformat()
        return sorted(t for t, d in last.items() if d >= cutoff)

    def resolve_team(self, name):
        teams = self.teams()
        n = (name or "").strip()
        if not n or not teams:
            return None, "no teams loaded" if not teams else "empty name"
        for t in teams:
            if t.lower() == n.lower():
                return t, None
        q = bd.norm(n)
        sub = [t for t in teams if q and q in bd.norm(t)]
        if len(sub) == 1:
            return sub[0], None
        sc = sorted(((bm.nsim(n, t), t) for t in teams), reverse=True)
        if sc[0][0] >= 0.62 and (len(sc) == 1 or sc[0][0] - sc[1][0] >= 0.05):
            return sc[0][1], None
        return None, f"'{name}' not recognised; teams: " + ", ".join(teams)

    # ------------------------------------------------------------------ views
    def _view(self, rec, fx=None, now=None):
        p = self.params()
        M = _rec_markets(rec["xh"], rec["xa"], float(p["draw_boost"]), lg=(rec["lgh"], rec["lga"]))
        warns = []
        if rec.get("future"):
            if rec.get("lineup_source") != "confirmed":
                warns.append("lineups not published yet: using each team's previous starting XI")
            kt = rec.get("known_teams") or (True, True)
            for ok, side in zip(kt, ("home", "away")):
                if not ok:
                    warns.append(f"no match history loaded for the {side} team: league-average rating used")
        v = {"id": rec["id"], "home": rec["home"], "away": rec["away"], "expected_goals": {"home": rec["xh"], "away": rec["xa"], "total": rec["xh"] + rec["xa"]},
             "indices": {"home_attack": rec["home_att"], "home_defence": rec["home_def"], "away_attack": rec["away_att"], "away_defence": rec["away_def"],
                         "league_home_goals": rec["lgh"], "league_away_goals": rec["lga"], "x12_stretch": bm.X12_STRETCH},
             "lineup": {"source": rec.get("lineup_source"), "attackers_defenders_home": rec.get("home_n"), "attackers_defenders_away": rec.get("away_n")},
             "probabilities": {"home": M["home"], "draw": M["draw"], "away": M["away"], "btts": M["btts"]},
             "team_over": M["team_over"], "total_over": M["total_over"],
             "likeliest_scores": [{"score": f"{i}-{j}", "p": pr} for i, j, pr in M["scorelines"]], "warnings": warns}
        if fx:
            v.update({"kickoff": fx.get("date"), "state": fx.get("state"), "status": fx.get("status")})
            if fx.get("state") == "in":
                v["warnings"].append("match is under way: prediction is pre-match (not updated in play)")
            row = self._odds_for({"id": fx["id"], "date": (_parse_iso(fx.get("date")) or datetime.datetime(2000, 1, 1)).date().isoformat(),
                                  "home": fx["home"]["name"], "away": fx["away"]["name"]})
            mk = market_from_row(row)
            if mk:
                v["market"] = {"source": "football-data.co.uk (as of its last update; can be days old, usually closer to kickoff the better)", **mk}
                if mk.get("H") is not None:
                    v["market"]["model_minus_market"] = {"home": M["home"] - mk["H"], "draw": M["draw"] - mk["D"], "away": M["away"] - mk["A"]}
                if mk.get("o25") is not None:
                    v["market"]["model_minus_market"] = {**v["market"].get("model_minus_market", {}), "over_2_5": M["total_over"]["2.5"] - mk["o25"]}
        return v

    def get_slate(self, days=None):
        try:
            run = self._run()
            fb = self._fixtures_blob()
            now = self._now()
            warns = []
            st = self._state()
            if run is None:
                prog = st.get("bootstrap") or {}
                msg = f"warming up: {len(self.matches())} matches loaded (need {MIN_MATCHES})"
                if prog:
                    msg += f"; collecting {prog.get('day', '?')} ({prog.get('matches', 0)} matches stored)"
                return {"status": "ok", "warnings": [msg], "events": [], "n_events": 0}
            if not fb:
                return {"status": "ok", "warnings": ["no fixtures pulled yet"], "events": [], "n_events": 0}
            horizon = None if not days else now + datetime.timedelta(days=int(days))
            evs = []
            for fx in sorted(fb.get("events", []), key=lambda f: f.get("date") or ""):
                rec = run["fut"].get(str(fx["id"]))
                kt = _parse_iso(fx.get("date"))
                if rec is None or fx.get("state") not in ("pre", "in"):
                    continue
                if horizon and kt and kt > horizon:
                    continue
                evs.append(self._view(rec, fx, now))
            age = None
            ft = _parse_iso(fb.get("fetched_at"))
            if ft:
                age = (now - ft).total_seconds() / 60
                if age > 90:
                    warns.append(f"fixtures last pulled {int(age)} minutes ago")
            last = self.matches()[-1]["date"]
            return clean({"status": "ok", "warnings": warns + ["Reference display: tested no better than the closing market (see scorecard)."],
                          "n_events": len(evs), "events": evs, "fixtures_age_minutes": age, "data_through": last, "params": self.params()})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "events": []}

    def predict(self, home, away, lineups=None):
        try:
            h, eh = self.resolve_team(home)
            a, ea = self.resolve_team(away)
            if not h or not a:
                return {"status": "error", "warnings": [eh or ea]}
            if h == a:
                return {"status": "error", "warnings": ["home and away are the same team"]}
            now = self._now()
            ms = self.matches()
            if len(ms) < MIN_MATCHES:
                return {"status": "error", "warnings": ["warming up: not enough match data yet"]}
            fx = {"id": "adhoc", "date": (now + datetime.timedelta(days=2)).isoformat(), "home": {"name": h}, "away": {"name": a}, "lineups": lineups}
            ps = self._pseudo(fx)
            ps["date"] = max(ps["date"], ms[-1]["date"])
            run = self._run(extra=[ps])
            rec = run["fut"].get("adhoc") if run else None
            if rec is None:
                return {"status": "error", "warnings": ["prediction failed"]}
            v = self._view(rec)
            v["warnings"].append("hypothetical fixture: kickoff date set to the next free day; previous starting XIs used")
            return clean({"status": "ok", "warnings": v.pop("warnings"), **v})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    # ------------------------------------------------------------------ write path: ESPN data
    def refresh_data(self, bootstrap=False, max_seconds=None):
        """Scoreboards day by day + summaries for finished matches not stored yet.  Resumable; advances 'collected_through' only over days
        fetched without errors.  First run = FIRST_SEASON..now (about 15 minutes)."""
        if self._busy["data"]:
            return {"status": "ok", "warnings": ["a data refresh is already running"]}
        lock = FileLock(self._p("sa_data.lock"), timeout=0.0, stale_after=3600.0)
        try:
            lock.acquire()
        except LockBusy:
            return {"status": "ok", "warnings": ["another process is refreshing the data"]}
        self._busy["data"] = True
        warns, new, errs = [], 0, 0
        t0 = time.time()
        try:
            now = self._now()
            today = now.date()
            cur = season_of(today)
            first = int(os.environ.get("SA_FIRST_SEASON", cur - 3))
            st = self._state()
            thru = st.get("collected_through")
            start = datetime.date(first, 8, 1)
            if thru and not bootstrap:
                start = max(start, datetime.date.fromisoformat(thru) - datetime.timedelta(days=7))
            total = (today - start).days + 1
            d, ok_through, stopped = start, None, False
            while d <= today:
                if max_seconds and time.time() - t0 > max_seconds:
                    stopped = True
                    warns.append("time budget reached: will continue on the next run")
                    break
                fp = os.path.join(self.espn, "days", d.strftime("%Y%m%d") + ".json")
                events = None
                if os.path.exists(fp) and (today - d).days > 7:
                    try:
                        events = json.load(open(fp))
                    except Exception:                      # noqa: BLE001
                        events = None
                if events is None:
                    try:
                        events = bd.scoreboard_events(self._get, d)
                        atomic_json_dump(fp, events)
                    except Exception as e:                 # noqa: BLE001
                        errs += 1
                        warns.append(f"scoreboard {d}: {type(e).__name__}: {e}"[:200])
                        ok_through = ok_through if ok_through is not None else None
                        d += datetime.timedelta(days=1)
                        stopped = True if errs >= 5 else stopped
                        if stopped:
                            break
                        continue
                day_ok = True
                for e in events:
                    if not e.get("completed"):
                        continue
                    mp = os.path.join(self.espn, "matches", str(e["id"]) + ".json")
                    if os.path.exists(mp):
                        continue
                    try:
                        s = self._get(bd.SUMMARY + str(e["id"]))
                        k = _parse_iso(e.get("date"))
                        meta = {x: e[x] for x in ("id", "date", "name", "home", "away")}
                        meta["season"] = season_of(k) if k else season_of(d)
                        atomic_json_dump(mp, bd.compact_summary(s, meta))
                        new += 1
                    except Exception as ex:                # noqa: BLE001
                        errs += 1
                        day_ok = False
                        warns.append(f"summary {e['id']}: {type(ex).__name__}: {ex}"[:200])
                if day_ok and not (errs and ok_through is None and False):
                    ok_through = d
                if (d - start).days % 25 == 0:
                    self._save_state(bootstrap={"day": d.isoformat(), "days_done": (d - start).days, "days_total": total, "matches": len(self._match_paths())})
                if not day_ok:
                    stopped = True                         # do not advance the cursor past a failed day
                    break
                d += datetime.timedelta(days=1)
            if ok_through and (not thru or ok_through.isoformat() > thru or bootstrap):
                self._save_state(collected_through=ok_through.isoformat())
            self._save_state(data_refreshed_at=now.isoformat(), bootstrap=None if not stopped else self._state().get("bootstrap"))
            n = len(self.matches())
            return clean({"status": "ok" if (new or not errs) else "error", "warnings": warns[:10], "new_matches": new, "errors": errs,
                          "matches_loaded": n, "collected_through": (ok_through.isoformat() if ok_through else thru)})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["data"] = False
            lock.release()

    def bootstrap(self):
        return self.refresh_data(bootstrap=True)

    # ------------------------------------------------------------------ write path: fixtures + lineups
    def refresh_fixtures(self):
        if self._busy["fixtures"]:
            return {"status": "ok", "warnings": ["a fixtures refresh is already running"]}
        lock = FileLock(self._p("sa_fixtures.lock"), timeout=0.0, stale_after=600.0)
        try:
            lock.acquire()
        except LockBusy:
            return {"status": "ok", "warnings": ["another process is pulling fixtures"]}
        self._busy["fixtures"] = True
        try:
            now = self._now()
            today = now.date()
            old = {str(e["id"]): e for e in (self._fixtures_blob() or {}).get("events", [])}
            events, errors = [], {}
            for k in range(LOOKAHEAD_DAYS):
                d = today + datetime.timedelta(days=k)
                try:
                    for e in bd.scoreboard_events(self._get, d):
                        if e.get("completed") or e.get("state") == "post":
                            continue
                        if any(str(e["id"]) == str(x["id"]) for x in events):
                            continue
                        prev = old.get(str(e["id"]))
                        if prev and prev.get("lineups"):
                            e["lineups"], e["lineups_at"] = prev["lineups"], prev.get("lineups_at")
                        events.append(e)
                except Exception as ex:                    # noqa: BLE001
                    errors[d.isoformat()] = f"{type(ex).__name__}: {ex}"[:150]
            if not events and errors:
                return {"status": "error", "warnings": ["fixtures pull failed: " + "; ".join(errors.values())[:300]]}
            got = 0
            for e in events:
                kt = _parse_iso(e.get("date"))
                if e.get("state") != "pre" or e.get("lineups") or not kt:
                    continue
                if datetime.timedelta(0) < kt - now <= datetime.timedelta(hours=LINEUP_WINDOW_H):
                    try:
                        lu = bd.lineups_from_summary(self._get(bd.SUMMARY + str(e["id"])))
                        if lu:
                            e["lineups"], e["lineups_at"] = {s: [list(x) for x in lu[s]] for s in ("home", "away")}, now.isoformat()
                            got += 1
                    except Exception as ex:                # noqa: BLE001
                        errors["lineup " + str(e["id"])] = f"{type(ex).__name__}: {ex}"[:150]
            events.sort(key=lambda x: x.get("date") or "")
            atomic_json_dump(self._p(FIXTURES_FILE), {"fetched_at": now.isoformat(), "events": events, "errors": errors})
            logged = self._log_slate() if self._log_on else 0
            self._save_state(fixtures_refreshed_at=now.isoformat(), fixtures=len(events))
            return clean({"status": "ok", "warnings": [f"{k}: {v}" for k, v in errors.items()][:5], "fixtures": len(events), "new_lineups": got, "logged": logged})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["fixtures"] = False
            lock.release()

    # ------------------------------------------------------------------ write path: market odds (football-data.co.uk)
    def refresh_odds(self):
        if self._busy["odds"]:
            return {"status": "ok", "warnings": ["an odds refresh is already running"]}
        self._busy["odds"] = True
        res, warns = {}, []
        try:
            now = self._now()
            cur = season_of(now.date())
            targets = [(f"I1_{fd_code(y)}.csv", FD_SEASON.format(c=fd_code(y)), None) for y in range(cur - RETRO_BACK, cur + 1)]
            targets.append(("fixtures_I1.csv", FD_FIXTURES, "I1"))
            for name, url, div in targets:
                try:
                    raw = self._http(url)
                    text = raw.decode("utf-8-sig", "replace")
                    header = text.split("\n", 1)[0]
                    if "HomeTeam" not in header or "Date" not in header:
                        raise ValueError("unexpected header")
                    if div:
                        rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get("Div") or "").strip() == div]
                        buf = io.StringIO()
                        w = csv.DictWriter(buf, fieldnames=list(csv.DictReader(io.StringIO(text)).fieldnames))
                        w.writeheader()
                        w.writerows(rows)
                        data = buf.getvalue().encode("utf-8")
                        res[name] = f"{len(rows)} fixtures"
                    else:
                        data = raw
                        res[name] = f"{text.count(chr(10))} lines"
                    path = os.path.join(self.dir, "odds", name)
                    old = os.path.getsize(path) if os.path.exists(path) else 0
                    if old and len(data) < 0.5 * old and not div:
                        raise ValueError("new file much smaller than the one on disk: kept the old one")
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                except Exception as e:                     # noqa: BLE001
                    res[name] = f"FAILED: {type(e).__name__}: {e}"[:150]
                    warns.append(f"{name}: {res[name]}")
            self._save_state(odds_refreshed_at=now.isoformat(), odds_result=res)
            return clean({"status": "ok" if any(not v.startswith("FAILED") for v in res.values()) else "error", "warnings": warns, "files": res})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["odds"] = False

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
                            self._sigs[r["id"]] = r["sig"]
                        except (ValueError, KeyError):
                            continue
        return self._sigs

    def _log_slate(self):
        """Append the current pre-match prediction for every fixture that has not kicked off (dedupe on change)."""
        run = self._run()
        fb = self._fixtures_blob()
        if run is None or not fb:
            return 0
        now = self._now()
        p = self.params()
        lock = FileLock(self._p("sa_log.lock"), timeout=5.0, stale_after=120.0)
        try:
            lock.acquire()
        except LockBusy:
            return 0
        n = 0
        try:
            sigs = self._load_sigs()
            lines = []
            for fx in fb.get("events", []):
                kt = _parse_iso(fx.get("date"))
                rec = run["fut"].get(str(fx["id"]))
                if rec is None or fx.get("state") != "pre" or not kt or kt <= now:
                    continue
                M = _rec_markets(rec["xh"], rec["xa"], float(p["draw_boost"]), lg=(rec["lgh"], rec["lga"]))
                row = self._odds_for({"id": fx["id"], "date": kt.date().isoformat(), "home": fx["home"]["name"], "away": fx["away"]["name"]})
                mk = market_from_row(row)
                sig = [round(rec["xh"], 3), round(rec["xa"], 3), rec.get("lineup_source"),
                       None if not mk else [round(mk.get("H") or 0, 3), round(mk.get("o25") or 0, 3)]]
                if sigs.get(str(fx["id"])) == sig:
                    continue
                sigs[str(fx["id"])] = sig
                lines.append(json.dumps(clean({"id": str(fx["id"]), "kickoff": fx.get("date"), "logged_at": now.isoformat(), "home": rec["home"], "away": rec["away"],
                                               "season": rec["season"], "xh": rec["xh"], "xa": rec["xa"], "bh": rec["lgh"], "ba": rec["lga"],
                                               "lineup_source": rec.get("lineup_source"), "mkt": mk, "sig": sig,
                                               "probs": {"H": M["home"], "D": M["draw"], "A": M["away"], "o25": M["total_over"]["2.5"]}})))
                n += 1
            if lines:
                with open(self._p(LOG_FILE), "a") as f:
                    f.write("\n".join(lines) + "\n")
            return n
        finally:
            lock.release()

    def refresh_results(self):
        """Settle logged predictions from stored match results (the data refresh stores every finished match)."""
        if self._busy["results"]:
            return {"status": "ok", "warnings": ["a results refresh is already running"]}
        self._busy["results"] = True
        try:
            p = self._p(LOG_FILE)
            if not os.path.exists(p):
                return {"status": "ok", "warnings": ["nothing logged yet"], "settled": 0}
            res = load_json_retry(self._p(RESULTS_FILE), {}) or {}
            done = {m["id"]: [m["hg"], m["ag"]] for m in self.matches()}
            last = {}
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        last[r["id"]] = r
                    except (ValueError, KeyError):
                        continue
            now = self._now()
            n = 0
            for i, r in last.items():
                if i in res:
                    continue
                if i in done:
                    res[i] = done[i]
                    n += 1
                else:
                    kt = _parse_iso(r.get("kickoff"))
                    if kt and (now - kt).days > 12:
                        res[i] = "void"                    # never played / not stored: excluded from scoring
            atomic_json_dump(self._p(RESULTS_FILE), res)
            self._save_state(results_refreshed_at=now.isoformat())
            return {"status": "ok", "warnings": [], "settled": n, "pending": sum(1 for i in last if i not in res)}
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy["results"] = False

    # ------------------------------------------------------------------ scorecard
    def _live_records(self):
        p = self._p(LOG_FILE)
        res = load_json_retry(self._p(RESULTS_FILE), {}) or {}
        done = {m["id"]: [m["hg"], m["ag"]] for m in self.matches()}
        if not os.path.exists(p):
            return [], 0
        last, logged = {}, 0
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                logged += 1
                last[r["id"]] = r
        recs = []
        for i, r in last.items():
            g = res.get(i) if isinstance(res.get(i), list) else done.get(i)
            if not g:
                continue
            recs.append({"id": i, "date": (r.get("kickoff") or "")[:10], "home": r["home"], "away": r["away"], "xh": r["xh"], "xa": r["xa"],
                         "bh": r["bh"], "ba": r["ba"], "hg": g[0], "ag": g[1], "mkt": r.get("mkt"), "probs": r.get("probs"),
                         "lineup_source": r.get("lineup_source")})
        recs.sort(key=lambda x: x["date"])
        rows = self._odds_rows()
        if rows:                                           # closing odds appear in football-data only after the match: attach them now
            need = [r for r in recs if not (r.get("mkt") and (r["mkt"].get("H") is not None or r["mkt"].get("o25") is not None))]
            if need:
                j = bm.join_odds(need, rows)[0]
                for r in need:
                    mk = market_from_row(j.get(r["id"]))
                    if mk:
                        r["mkt"] = mk
        return recs, logged

    def _retro_records(self):
        """Walk-forward record on stored history (deployable lineup: previous XI), current season and RETRO_BACK back, with closing odds."""
        run = self._run()
        if run is None:
            return []
        cur = season_of(self._now().date())
        sel = [r for r in run["hist"] if r["season"] >= cur - RETRO_BACK]
        rows = self._odds_rows()
        joined = bm.join_odds(sel, rows)[0] if rows else {}
        return [{"id": r["id"], "date": r["date"], "home": r["home"], "away": r["away"], "xh": r["xh"], "xa": r["xa"], "bh": r["lgh"], "ba": r["lga"],
                 "hg": r["hg"], "ag": r["ag"], "mkt": market_from_row(joined.get(r["id"]))} for r in sel]

    def get_scorecard(self):
        try:
            p = self.params()
            db = float(p["draw_boost"])
            live, logged = self._live_records()
            retro = self._retro_records()
            recent = []
            for r in live[-25:][::-1]:
                pr = r.get("probs") or {}
                y = "H" if r["hg"] > r["ag"] else ("D" if r["hg"] == r["ag"] else "A")
                recent.append({"date": r["date"], "match": f"{r['home']} {r['hg']}-{r['ag']} {r['away']}", "p_home": pr.get("H"), "p_draw": pr.get("D"),
                               "p_away": pr.get("A"), "p_over_2_5": pr.get("o25"), "result": y, "total": r["hg"] + r["ag"],
                               "lineup": r.get("lineup_source")})
            return clean({"status": "ok",
                          "warnings": ["Backtest (351 matches): the model ties a tuned team-level model and is worse than the closing market. "
                                       "The LIVE record below is the out-of-sample check; the RETROSPECTIVE record is walk-forward on stored history "
                                       "(parameters were tuned on 2024/25, so 2025/26 onward is out of sample for tuning)."],
                          "logged_predictions": logged, "live": score_records(live, db), "live_recent": recent,
                          "retrospective": {"label": "walk-forward season-to-date, previous-XI lineups, closing odds where football-data has them",
                                            **score_records(retro, db)}})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def backlog(self, season=None):
        """Every match already played in `season` (default: the current one): the walk-forward prediction (previous-XI lineups, using only
        matches before it), the actual result, closing odds where football-data has them.  Written to sa_backlog_<season>.csv and summarised."""
        try:
            run = self._run()
            if run is None:
                return {"status": "error", "warnings": ["not enough match data yet (run bootstrap first)"]}
            cur = season if season is not None else season_of(self._now().date())
            sel = [r for r in run["hist"] if r["season"] == cur]
            if not sel:
                return {"status": "error", "warnings": [f"no played matches found for season {cur}"]}
            db = float(self.params()["draw_boost"])
            rows = self._odds_rows()
            joined = bm.join_odds(sel, rows)[0] if rows else {}
            recs, table = [], []
            for r in sel:
                M = bm.markets(r["xh"], r["xa"], draw_boost=db, lg=(r["lgh"], r["lga"]))
                mk = market_from_row(joined.get(r["id"])) or {}
                y = "H" if r["hg"] > r["ag"] else ("D" if r["hg"] == r["ag"] else "A")
                p = {"H": M["home"], "D": M["draw"], "A": M["away"]}
                recs.append({"id": r["id"], "date": r["date"], "home": r["home"], "away": r["away"], "xh": r["xh"], "xa": r["xa"], "bh": r["lgh"],
                             "ba": r["lga"], "hg": r["hg"], "ag": r["ag"], "mkt": mk or None})
                table.append({"date": r["date"], "season": r["season"], "home": r["home"], "away": r["away"], "lineups": "previous XI",
                              "xg_home": round(r["xh"], 3), "xg_away": round(r["xa"], 3), "p_home": round(M["home"], 4), "p_draw": round(M["draw"], 4),
                              "p_away": round(M["away"], 4), "p_btts": round(M["btts"], 4), "p_over_1_5": round(M["total_over"]["1.5"], 4),
                              "p_over_2_5": round(M["total_over"]["2.5"], 4), "p_over_3_5": round(M["total_over"]["3.5"], 4),
                              "p_home_over_0_5": round(M["team_over"]["home"]["0.5"], 4), "p_home_over_1_5": round(M["team_over"]["home"]["1.5"], 4),
                              "p_away_over_0_5": round(M["team_over"]["away"]["0.5"], 4), "p_away_over_1_5": round(M["team_over"]["away"]["1.5"], 4),
                              "home_goals": r["hg"], "away_goals": r["ag"], "result": y, "total_goals": r["hg"] + r["ag"],
                              "btts": int(r["hg"] > 0 and r["ag"] > 0), "over_2_5": int(r["hg"] + r["ag"] > 2.5),
                              "p_actual_result": round(p[y], 4), "logloss": round(-math.log(max(p[y], 1e-9)), 4),
                              "mkt_home": mk.get("H"), "mkt_draw": mk.get("D"), "mkt_away": mk.get("A"), "mkt_over_2_5": mk.get("o25")})
            path = self._p(f"sa_backlog_{cur}.csv")
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, list(table[0]))
                w.writeheader()
                w.writerows(table)
            os.replace(tmp, path)
            teams = {}
            for t in table:
                for side, opp in (("home", "away"), ("away", "home")):
                    d = teams.setdefault(t[side], {"team": t[side], "played": 0, "pts": 0, "exp_pts": 0.0, "gf": 0, "xgf": 0.0, "ga": 0, "xga": 0.0})
                    d["played"] += 1
                    d["gf"] += t[side + "_goals"]
                    d["ga"] += t[opp + "_goals"]
                    d["xgf"] += t["xg_" + side]
                    d["xga"] += t["xg_" + opp]
                    pw, pd_, pl = (t["p_home"], t["p_draw"], t["p_away"]) if side == "home" else (t["p_away"], t["p_draw"], t["p_home"])
                    d["exp_pts"] += 3 * pw + pd_
                    won = (t["result"] == "H") if side == "home" else (t["result"] == "A")
                    d["pts"] += 3 if won else (1 if t["result"] == "D" else 0)
            tt = sorted(({**d, "xgf": round(d["xgf"], 1), "xga": round(d["xga"], 1), "exp_pts": round(d["exp_pts"], 1)} for d in teams.values()),
                        key=lambda d: -d["pts"])
            return clean({"status": "ok", "warnings": [], "season": cur, "matches": len(table), "csv": path,
                          "scorecard": score_records(recs, db), "teams": tt})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def get_results(self, n=40):
        try:
            live, _ = self._live_records()
            return clean({"status": "ok", "warnings": [], "results": live[-n:][::-1]})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def health(self):
        try:
            st = self._state()
            fb = self._fixtures_blob() or {}
            now = self._now()
            ms = self.matches()

            def age(ts):
                t = _parse_iso(ts)
                return None if t is None else (now - t).total_seconds() / 60
            logged = 0
            if os.path.exists(self._p(LOG_FILE)):
                with open(self._p(LOG_FILE)) as f:
                    logged = sum(1 for _ in f)
            return clean({"status": "ok", "warnings": [] if len(ms) >= MIN_MATCHES else ["warming up: run bootstrap / wait for the first data load"],
                          "needs_bootstrap": len(ms) < MIN_MATCHES, "bootstrap": st.get("bootstrap"), "matches_loaded": len(ms),
                          "data_through": ms[-1]["date"] if ms else None, "collected_through": st.get("collected_through"),
                          "data_refreshed_minutes_ago": age(st.get("data_refreshed_at")), "fixtures": len(fb.get("events", [])),
                          "fixtures_pulled_minutes_ago": age(fb.get("fetched_at")), "odds_refreshed_minutes_ago": age(st.get("odds_refreshed_at")),
                          "odds_files": st.get("odds_result"), "results_refreshed_minutes_ago": age(st.get("results_refreshed_at")),
                          "logged_predictions": logged, "settled": len(load_json_retry(self._p(RESULTS_FILE), {}) or {}),
                          "params": self.params(), "validation": VALIDATION})
        except Exception as e:                             # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    # ------------------------------------------------------------------ background loops
    def start_background_refresh(self, data_s=3600, fixtures_s=900, odds_s=10800, results_s=3600, bootstrap=False):
        self._stop.clear()

        def dloop():
            if bootstrap and self.needs_bootstrap():
                self.bootstrap()
            while not self._stop.is_set():
                self.refresh_data()
                self._stop.wait(data_s)

        def floop():
            self._stop.wait(8)
            while not self._stop.is_set():
                self.refresh_fixtures()
                self._stop.wait(fixtures_s)

        def oloop():
            self._stop.wait(15)
            while not self._stop.is_set():
                self.refresh_odds()
                self._stop.wait(odds_s)

        def rloop():
            self._stop.wait(180)
            while not self._stop.is_set():
                self.refresh_results()
                self._stop.wait(results_s)
        for fn, name in ((dloop, "sa-data"), (floop, "sa-fixtures"), (oloop, "sa-odds"), (rloop, "sa-results")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def stop(self):
        self._stop.set()


# ===================================================================== built-in page
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Serie A Model</title>
<style>
:root{--bg:#0f1115;--fg:#e8eaed;--mut:#9aa0a6;--card:#171a21;--line:#262b36;--ok:#3ddc97;--bad:#ff6b6b;--warn:#f4b942;--acc:#5aa9ff}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#14171c;--mut:#5f6773;--card:#fff;--line:#dfe3ea;--ok:#0a8f5b;--bad:#c62828;--warn:#9a6a00;--acc:#1565c0}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px}
.banner{margin:10px 16px;padding:8px 12px;border:1px solid var(--warn);border-radius:8px;color:var(--warn);font-size:12px}
main{padding:0 16px 40px;max-width:1100px;margin:auto}.tabs{margin-top:8px}.tabs button{margin-right:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:12px 14px}
.hd{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}.nm{font-weight:600;font-size:15px}.meta{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;margin-top:8px}th,td{text-align:right;padding:3px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500;font-size:12px}
.pos{color:var(--ok)}.neg{color:var(--bad)}.w{color:var(--warn);font-size:12px}h3{margin:10px 0 0;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.bar{display:flex;height:22px;border-radius:5px;overflow:hidden;margin-top:6px;font-size:12px}.bar div{display:flex;align-items:center;justify-content:center;color:#fff;white-space:nowrap}
.h{background:#2e7d5b}.d{background:#6b7280}.a{background:#b5603a}
select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;cursor:pointer}button.on{border-color:var(--acc);color:var(--acc)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:700px){.two{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Serie A model · player plus-minus formula</h1><div class="sub" id="sub">loading...</div>
<div class="tabs"><button id="t1" class="on" onclick="tab(1)">Upcoming matches</button><button id="t2" onclick="tab(2)">Scorecard</button> <button onclick="load()">Refresh</button></div></header>
<div class="banner" id="ban"></div><main id="main"></main>
<script>
let T=1;const pc=x=>x==null?'–':(100*x).toFixed(1)+'%',n2=x=>x==null?'–':x.toFixed(2),n3=x=>x==null?'–':x.toFixed(3);
const sg=x=>x==null?'–':'<span class="'+(x>=0?'pos':'neg')+'">'+(x>=0?'+':'')+(100*x).toFixed(1)+'</span>';
function tab(i){T=i;document.getElementById('t1').className=i==1?'on':'';document.getElementById('t2').className=i==2?'on':'';load()}
function bar(p){return '<div class="bar"><div class="h" style="width:'+100*p.home+'%">'+pc(p.home)+'</div><div class="d" style="width:'+100*p.draw+'%">'+pc(p.draw)+'</div><div class="a" style="width:'+100*p.away+'%">'+pc(p.away)+'</div></div>'}
function card(e){
 const p=e.probabilities,mk=e.market,d=(mk&&mk.model_minus_market)||{};
 let h='<div class="card"><div class="hd"><div class="nm">'+e.home+' vs '+e.away+'</div><div class="meta">'+(e.kickoff?new Date(e.kickoff).toLocaleString():'')+' · lineup: '+(e.lineup.source||'?')+'</div></div>';
 (e.warnings||[]).forEach(w=>h+='<div class="w">⚠ '+w+'</div>');
 h+='<div class="meta">Expected goals '+n2(e.expected_goals.home)+' – '+n2(e.expected_goals.away)+' (total '+n2(e.expected_goals.total)+') · attack/defence index home '+n2(e.indices.home_attack)+' / '+n2(e.indices.home_defence)+', away '+n2(e.indices.away_attack)+' / '+n2(e.indices.away_defence)+'</div>';
 h+='<h3>Result probabilities (home · draw · away)</h3>'+bar(p);
 if(mk&&mk.H!=null)h+='<table><tr><th></th><th>Home</th><th>Draw</th><th>Away</th></tr><tr><td>Model</td><td>'+pc(p.home)+'</td><td>'+pc(p.draw)+'</td><td>'+pc(p.away)+'</td></tr><tr><td>Market (de-vigged)</td><td>'+pc(mk.H)+'</td><td>'+pc(mk.D)+'</td><td>'+pc(mk.A)+'</td></tr><tr><td>Model − market (pts)</td><td>'+sg(d.home)+'</td><td>'+sg(d.draw)+'</td><td>'+sg(d.away)+'</td></tr></table>';
 h+='<div class="two"><div><h3>Team totals · over</h3><table><tr><th>Line</th><th>'+e.home+'</th><th>'+e.away+'</th></tr>';
 Object.keys(e.team_over.home).forEach(l=>h+='<tr><td>'+l+'</td><td>'+pc(e.team_over.home[l])+'</td><td>'+pc(e.team_over.away[l])+'</td></tr>');
 h+='</table></div><div><h3>Total goals · over</h3><table><tr><th>Line</th><th>Over</th><th>Under</th><th>Market over</th></tr>';
 Object.keys(e.total_over).forEach(l=>h+='<tr><td>'+l+'</td><td>'+pc(e.total_over[l])+'</td><td>'+pc(1-e.total_over[l])+'</td><td>'+(l=='2.5'&&mk&&mk.o25!=null?pc(mk.o25)+' ('+sg(d.over_2_5)+')':'–')+'</td></tr>');
 h+='</table></div></div><div class="meta">Both teams to score '+pc(p.btts)+' · likeliest scores: '+e.likeliest_scores.map(s=>s.score+' ('+pc(s.p)+')').join(', ')+(mk&&mk.odds&&mk.odds.H?' · market odds '+mk.odds.H+' / '+mk.odds.D+' / '+mk.odds.A:'')+'</div></div>';
 return h}
function tbl(title,rows,head){return '<h3>'+title+'</h3><table><tr>'+head.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+x+'</td>').join('')+'</tr>').join('')+'</table>'}
function block(S,title){
 if(!S||!S.n)return '<div class="card"><div class="nm">'+title+'</div><div class="meta">No settled predictions yet.</div></div>';
 const x=S.x12,g=S.goals;let h='<div class="card"><div class="nm">'+title+' · '+S.n+' matches</div>';
 h+='<div class="meta">Goals: expected home '+n2(g.home_expected)+' vs actual '+n2(g.home_actual)+' · away '+n2(g.away_expected)+' vs '+n2(g.away_actual)+' · total error (MAE) '+n2(g.mae_total)+' vs league-average '+n2(g.mae_total_league_avg)+'</div>';
 h+=tbl('Result probabilities',[['Log loss',n3(x.logloss),n3(x.logloss_league_avg)],['Brier (3-way)',n3(x.brier),n3(x.brier_league_avg)],['Favourite correct',pc(x.accuracy),'–'],['Home: mean prediction / actual',pc(x.home_pred),pc(x.home_rate)],['Draw: mean prediction / actual',pc(x.draw_pred),pc(x.draw_rate)],['Away: mean prediction / actual',pc(x.away_pred),pc(x.away_rate)]],['','Model / predicted','League avg / actual']);
 h+=tbl('Total goals over/under',Object.keys(S.total_over).map(l=>{const o=S.total_over[l];return ['Over '+l,pc(o.pred),pc(o.rate),n3(o.brier),n3(o.brier_league_avg)]}),['','Mean predicted','Actual rate','Brier','League-avg Brier']);
 h+=tbl('Team totals over/under (both teams pooled)',Object.keys(S.team_over).map(l=>{const o=S.team_over[l];return ['Over '+l,pc(o.pred),pc(o.rate),n3(o.brier),n3(o.brier_league_avg)]}),['','Mean predicted','Actual rate','Brier','League-avg Brier']);
 h+='<div class="meta">Both teams to score: predicted '+pc(S.btts.pred)+' vs actual '+pc(S.btts.rate)+'</div>';
 const m1=S.vs_market_1x2,m2=S.vs_market_ou25;
 if(m1)h+=tbl('Versus the market · 1X2 ('+m1.n+' matches)',[['Log loss',n3(m1.logloss_model),n3(m1.logloss_market)],['Brier',n3(m1.brier_model),n3(m1.brier_market)]],['','Model','Market'])+'<div class="meta">Brier model − market '+n3(m1.model_minus_market_brier)+' (se '+n3(m1.se)+') · negative = model better</div>';
 if(m2)h+=tbl('Versus the market · over 2.5 ('+m2.n+' matches)',[['Brier',n3(m2.brier_model),n3(m2.brier_market)],['Mean predicted over',pc(m2.model_pred),pc(m2.market_pred)]],['','Model','Market'])+'<div class="meta">Brier model − market '+n3(m2.model_minus_market_brier)+' (se '+n3(m2.se)+') · actual over rate '+pc(m2.over_rate)+'</div>';
 h+=tbl('Calibration · home win',S.calibration.home_win.map(b=>[b.bin,b.n,pc(b.pred),pc(b.actual)]),['Predicted bin','n','Mean predicted','Actual'])+tbl('Calibration · draw',S.calibration.draw.map(b=>[b.bin,b.n,pc(b.pred),pc(b.actual)]),['Predicted bin','n','Mean predicted','Actual'])+tbl('Calibration · over 2.5',S.calibration.over_2_5.map(b=>[b.bin,b.n,pc(b.pred),pc(b.actual)]),['Predicted bin','n','Mean predicted','Actual']);
 return h+'</div>'}
async function load(){
 if(T==1){
  const j=await(await fetch('/api/sa/slate')).json();
  document.getElementById('sub').textContent=j.status==='ok'?(j.n_events||0)+' upcoming matches · data through '+(j.data_through||'–')+' · fixtures pulled '+Math.round(j.fixtures_age_minutes||0)+' min ago':'';
  document.getElementById('ban').textContent=(j.warnings||[]).join('  ·  ');
  document.getElementById('main').innerHTML=(j.events||[]).map(card).join('')||'<p class="meta">No upcoming matches to show.</p>';
 }else{
  const j=await(await fetch('/api/sa/scorecard')).json();
  document.getElementById('sub').textContent='running scorecard · '+(j.logged_predictions||0)+' predictions logged';
  document.getElementById('ban').textContent=(j.warnings||[]).join('  ·  ');
  let h=block(j.live,'LIVE record (predictions logged before kickoff)');
  if(j.live_recent&&j.live_recent.length)h+='<div class="card">'+tbl('Recent settled predictions',j.live_recent.map(r=>[r.date,r.match,pc(r.p_home),pc(r.p_draw),pc(r.p_away),pc(r.p_over_2_5),r.result+' / '+r.total+' goals']),['Date','Match','Home','Draw','Away','Over 2.5','Result / goals'])+'</div>';
  h+=block(j.retrospective,'RETROSPECTIVE walk-forward record');
  document.getElementById('main').innerHTML=h;
 }
}
load();setInterval(load,60000);
</script></body></html>"""


# ===================================================================== HTTP
def serve(platform, host="127.0.0.1", port=8056, cors=False, allow_write=False, token=None):
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
            routes = {"/api/sa/slate": lambda: platform.get_slate(q.get("days")),
                      "/api/sa/predict": lambda: platform.predict(q.get("home", ""), q.get("away", "")),
                      "/api/sa/scorecard": platform.get_scorecard, "/api/sa/results": platform.get_results,
                      "/api/sa/teams": lambda: {"status": "ok", "warnings": [], "teams": platform.teams()},
                      "/health": platform.health, "/api/sa/health": platform.health}
            fn = routes.get(u.path)
            if fn is None:
                self._send({"status": "error", "warnings": ["not found"]}, 404)
            else:
                self._send(fn())

        def _authorised(self):
            return (not token) or self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/sa/refresh":
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
            what = body.get("what", "fixtures")
            fn = {"data": lambda: platform.refresh_data(max_seconds=600), "fixtures": platform.refresh_fixtures, "odds": platform.refresh_odds,
                  "results": platform.refresh_results}.get(what)
            if fn is None:
                return self._send({"status": "error", "warnings": ["what must be 'data', 'fixtures', 'odds' or 'results'"]}, 400)
            self._send(fn())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/, /api/sa/slate, /api/sa/predict, /api/sa/scorecard, /api/sa/results, /api/sa/teams, /health"
          f"{', POST /api/sa/refresh' if allow_write else ''}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


# ===================================================================== command line
def _pct(x):
    return "  –  " if x is None else f"{100 * x:5.1f}%"


def print_slate(s):
    for w in s.get("warnings", []):
        print("NOTE:", w)
    for e in s.get("events", []):
        p, x = e["probabilities"], e["expected_goals"]
        print(f"\n{(e.get('kickoff') or '')[:16]}  {e['home']} vs {e['away']}   (lineups: {e['lineup']['source']})")
        print(f"   xG {x['home']:.2f}-{x['away']:.2f}   H {_pct(p['home'])}  D {_pct(p['draw'])}  A {_pct(p['away'])}   BTTS {_pct(p['btts'])}")
        print("   team over 0.5/1.5/2.5: home " + " ".join(_pct(e['team_over']['home'][k]) for k in ("0.5", "1.5", "2.5")) +
              " | away " + " ".join(_pct(e['team_over']['away'][k]) for k in ("0.5", "1.5", "2.5")))
        print("   total over 1.5/2.5/3.5/4.5: " + " ".join(_pct(e['total_over'][k]) for k in ("1.5", "2.5", "3.5", "4.5")))
        mk = e.get("market")
        if mk and mk.get("H") is not None:
            print(f"   market H {_pct(mk['H'])}  D {_pct(mk['D'])}  A {_pct(mk['A'])}" + (f"   over 2.5 {_pct(mk['o25'])}" if mk.get("o25") is not None else ""))
        for w in e.get("warnings", []):
            print("   !", w)


def print_backlog(b):
    if b.get("status") != "ok":
        print(b.get("warnings"))
        return
    sc = b["scorecard"]
    x, g = sc["x12"], sc["goals"]
    print(f"\nSeason {b['season']}: {b['matches']} matches already played, each predicted WALK-FORWARD (only earlier matches, previous-XI lineups).")
    print(f"Every match is in: {b['csv']}")
    print(f"\n1X2   log loss {x['logloss']:.4f} (league-average baseline {x['logloss_league_avg']:.4f}; lower is better, difference {x['logloss_minus_league_avg']:+.4f} +/- {x['se']:.4f})   accuracy {x['accuracy']:.1%}")
    print(f"      home {x['home_pred']:.1%} predicted / {x['home_rate']:.1%} actual   draw {x['draw_pred']:.1%} / {x['draw_rate']:.1%}   away {x['away_pred']:.1%} / {x['away_rate']:.1%}")
    print(f"Goals expected home {g['home_expected']:.2f} / away {g['away_expected']:.2f}   actual {g['home_actual']:.2f} / {g['away_actual']:.2f}   MAE of total {g['mae_total']:.2f} (league avg {g['mae_total_league_avg']:.2f})")
    for ln in ("1.5", "2.5", "3.5"):
        t = sc["total_over"][ln]
        print(f"Total over {ln}: predicted {t['pred']:.1%}, actual {t['rate']:.1%}, Brier {t['brier']:.4f} (league avg {t['brier_league_avg']:.4f})")
    print(f"BTTS: predicted {sc['btts']['pred']:.1%}, actual {sc['btts']['rate']:.1%}")
    for key, name in (("vs_market_1x2", "1X2"), ("vs_market_ou25", "O/U 2.5")):
        m = sc.get(key)
        if m:
            print(f"vs closing market {name} ({m['n']} matches): Brier model {m['brier_model']:.4f} vs market {m['brier_market']:.4f}  (model minus market {m['model_minus_market_brier']:+.4f} +/- {m['se']:.4f}; negative = model better)")
    print(f"\n{'team':<26}{'P':>3}{'pts':>5}{'exp pts':>9}{'GF':>5}{'xGF':>7}{'GA':>5}{'xGA':>7}")
    for t in b["teams"]:
        print(f"{t['team']:<26}{t['played']:>3}{t['pts']:>5}{t['exp_pts']:>9.1f}{t['gf']:>5}{t['xgf']:>7.1f}{t['ga']:>5}{t['xga']:>7.1f}")
    print("\n(pts vs exp pts: a team far above its expected points has been lucky or is better than its history says; the model only sees past matches.)")


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serie A player plus-minus model: data, predictions, odds, scorecard, service -- one file")
    ap.add_argument("--dir", default=os.environ.get("SA_DATA_DIR", "."), help="data folder (default: $SA_DATA_DIR or current)")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("daily", "slate", "refresh", "fixtures", "odds", "results", "track", "bootstrap"):
        sub.add_parser(name)
    bk = sub.add_parser("backlog", help="every match already played this season: walk-forward prediction vs result -> CSV + summary")
    bk.add_argument("--season", type=int, default=None, help="season start year (default: current)")
    pr = sub.add_parser("predict")
    pr.add_argument("--home", required=True)
    pr.add_argument("--away", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--cors", action="store_true")
    sv.add_argument("--allow-write", action="store_true")
    sv.add_argument("--bootstrap", action="store_true", help="if too little match data is on disk, download it in the background")
    sv.add_argument("--data-minutes", type=int, default=int(os.environ.get("SA_DATA_MINUTES", "60")))
    sv.add_argument("--fixtures-minutes", type=int, default=int(os.environ.get("SA_FIXTURES_MINUTES", "15")))
    sv.add_argument("--odds-minutes", type=int, default=int(os.environ.get("SA_ODDS_MINUTES", "180")))
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
        main_fn = getattr(mod, "main", None)
        rest = [a for a in args.rest if a != "--"]
        if args.module == "sa_model" and rest and rest[0] in ("backtest", "compare", "table", "sharpen") and "--dir" not in rest:
            rest += ["--dir", os.path.join(args.dir, "espn"), "--odds", os.path.join(args.dir, "odds")]
        if args.module == "sa_data" and rest and rest[0] in ("collect", "check") and "--dir" not in rest:
            rest += ["--dir", os.path.join(args.dir, "espn")]
        if main_fn is None:                                # sa_model / sa_data use a __main__ block
            sys.argv = [args.module] + rest
            return exec(compile(_SRC[args.module], f"<{args.module}>", "exec"), {"__name__": "__main__"})
        sys.argv = [args.module] + rest
        return main_fn()
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
    p = SerieAPlatform(args.dir)
    if args.cmd == "bootstrap":
        print(json.dumps(p.bootstrap(), indent=2))
    elif args.cmd == "refresh":
        print(json.dumps(p.refresh_data(), indent=2))
    elif args.cmd == "fixtures":
        print(json.dumps(p.refresh_fixtures(), indent=2))
    elif args.cmd == "odds":
        print(json.dumps(p.refresh_odds(), indent=2))
    elif args.cmd == "results":
        print(json.dumps(p.refresh_results(), indent=2))
    elif args.cmd == "backlog":
        print_backlog(p.backlog(args.season))
    elif args.cmd == "track":
        print(json.dumps(p.get_scorecard(), indent=2))
    elif args.cmd == "predict":
        print(json.dumps(p.predict(args.home, args.away), indent=2))
    elif args.cmd == "serve":
        port = args.port or int(os.environ.get("PORT", "8056"))
        host = args.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        allow = args.allow_write or _env_flag("SA_ALLOW_WRITE")
        token = os.environ.get("SA_API_TOKEN") or None
        if allow and not token and host != "127.0.0.1":
            print("WARNING: writes are enabled on a public address without SA_API_TOKEN: anyone who can reach it can trigger refreshes.", flush=True)
        p.start_background_refresh(max(1, args.data_minutes) * 60, max(1, args.fixtures_minutes) * 60, max(1, args.odds_minutes) * 60,
                                   bootstrap=args.bootstrap or _env_flag("SA_BOOTSTRAP"))
        serve(p, host, port, args.cors or _env_flag("SA_CORS"), allow, token)
    else:                                                  # daily / slate
        if args.cmd == "daily":
            for fn in (p.refresh_data, p.refresh_odds, p.refresh_fixtures, p.refresh_results):
                print(json.dumps(fn(), indent=2))
        print_slate(p.get_slate())


if __name__ == "__main__":
    main()
