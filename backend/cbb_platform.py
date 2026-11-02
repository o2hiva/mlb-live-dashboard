#!/usr/bin/env python3
"""
NCAA men's basketball model: data collection, points model, probabilities, walk-forward backtest, live slate + scorecard -- one file, stdlib only.

THE MODEL (the formula you specified)
  home score = (home team's HOME points-scored index) x (away team's AWAY points-allowed index) x league average
  away score = (away team's AWAY points-scored index) x (home team's HOME points-allowed index) x league average
  Each index is the team's per-game average at that venue divided by the league average at that venue, shrunk toward a prior
  because a team has only ~15 home and ~15 away games a season.  Neutral-site games use the team's overall indices and the
  neutral-site league average (no home-court term).
  Probabilities are Normal around the two predicted scores:  moneyline from the margin, team over/unders from each score,
  total over/under from the total; the spreads (standard deviations) are fitted on a tuning season and tested out of sample.

THREE VARIANTS ARE BACKTESTED (the rule for which one ships is fixed in advance, see RULE below)
  raw    your formula literally: venue indices shrunk toward 1.0 (league average)
  blend  venue indices shrunk toward the team's OVERALL index, which is shrunk toward last season's index (roster carry-over)
  adj    opponent-adjusted offence/defence ratings (schedule strength), same venue averages
  (plus a 'league' baseline that predicts the league home/away averages for every game)

COMMANDS   (always pass --dir)
  python cbb_platform.py probe                 --dir cbb_data      check the ESPN feed (reachability, fields, Division I filter)
  python cbb_platform.py collect               --dir cbb_data      download every Division I game since the first season (resumable)
  python cbb_platform.py backtest              --dir cbb_data      walk-forward test, tunes on one season, tests on later seasons, saves parameters
  python cbb_platform.py slate                 --dir cbb_data      upcoming games: scores, moneyline, team and total over/under ladders (logged)
  python cbb_platform.py predict --home X --away Y [--neutral] [--total-line 145.5] [--home-line 74.5] [--away-line 71.5]
  python cbb_platform.py results               --dir cbb_data      settle logged predictions from collected finals
  python cbb_platform.py track                 --dir cbb_data      live scorecard of logged predictions
  python cbb_platform.py daily                 --dir cbb_data      collect recent days + slate + results (+ Kalshi prices)
  python cbb_platform.py kalshi                --dir cbb_data      pull Kalshi game, spread, total and team-total prices; show matches
  python cbb_platform.py serve [--port N] [--host H] [--cors] [--allow-write] [--bootstrap] --dir cbb_data     web page + JSON API (below)

WEB SERVICE (stdlib only; local or Railway)
  GET  /                      page: upcoming games (model vs Kalshi), scorecard, predict a game
  GET  /api/cbb/slate[?days=N]   /api/cbb/predict?home=Duke&away=Kansas[&neutral=1&conference=1&spread=-4.5&total_line=145.5&home_line=74.5&away_line=71.5]
  GET  /api/cbb/scorecard   /api/cbb/results   /api/cbb/teams   /health
  POST /api/cbb/refresh {"what": "data"|"slate"|"kalshi"|"results"|"bootstrap"}      (needs --allow-write; bearer token if CBB_API_TOKEN is set)
  Environment: PORT (Railway; binds 0.0.0.0), CBB_DATA_DIR (volume, e.g. /data), CBB_API_TOKEN, CBB_ALLOW_WRITE=1, CBB_CORS=1, CBB_BOOTSTRAP=1,
               CBB_DATA_MINUTES (60), CBB_SLATE_MINUTES (15), CBB_RESULTS_MINUTES (60).
  RAILWAY: 1) new repo with ONLY this file (plus a one-line Procfile if you like).  2) add a Volume mounted at /data.
           3) Start command:  python cbb_platform.py serve --bootstrap      Variables: CBB_DATA_DIR=/data  CBB_API_TOKEN=<long random>  CBB_ALLOW_WRITE=1
           4) Health check path: /health.   First boot downloads six seasons (about 10-15 min, resumable) and runs the backtest; the page says
              'warming up' until then.  TO SKIP IT: copy your local cbb_data folder into the volume at /data.
           5) It then runs itself: fixtures + Kalshi every 15 min, recent scoreboards every hour, results every hour; every pre-game prediction is logged
              with the Kalshi prices and settled automatically (scorecard).

RULE (set before any real-data result was seen)
  The shipped variant is the best of raw/blend/adj on the TUNING season (lowest squared error of the two predicted scores).  It replaces
  'raw' (your formula as written) only if, on the TEST seasons, its paired squared-error improvement over raw is larger than 2 standard
  errors.  Otherwise raw ships.  Standard deviations for the probabilities come from the tuning season only.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
UA = "Mozilla/5.0 (sports-hub-cbb)"
MIN_GAMES = 6            # a team needs this many games in a season to count as Division I
LOOKAHEAD_DAYS = 3
PARAMS_FILE, LOG_FILE, RESULTS_FILE = "cbb_params.json", "cbb_log.jsonl", "cbb_results.json"
VARIANTS = ("league", "raw", "blend", "adj")
LEAGUE_W = 400           # weight (games) of the previous-seasons league averages inside a new season
DEFAULT_PARAMS = {"variant": "raw", "k_raw": 8.0, "k_all": 8.0, "k_ven": 16.0, "carry": 0.5, "k_adj": 6.0,
                  "ml_stretch": 1.0, "shift": {}, "sd_margin": 11.0, "sd_total": 15.5, "sd_team": 10.5, "tuned_on": None}
PARAM_KEYS = {"league": [], "raw": ["k_raw"], "blend": ["k_all", "k_ven", "carry"], "adj": ["k_adj", "carry"]}
GRIDS = {"raw": [{"k_raw": k} for k in (0, 2, 4, 8, 16, 32)],
         "blend": [{"k_all": a, "k_ven": v, "carry": c} for a in (2, 4, 8, 16) for v in (4, 8, 16, 32, 64) for c in (0.0, 0.25, 0.5, 0.75)],
         "adj": [{"k_adj": k, "carry": c} for k in (2, 4, 8) for c in (0.25, 0.5)]}


# ===================================================================== plumbing
def get_json(url, tries=4, pause=0.4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            time.sleep(pause * (i + 1))
    raise RuntimeError(f"GET failed: {url}: {type(last).__name__}: {last}")


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def season_of(d):
    """Season label = the year it ends in (Nov 2026 .. Apr 2027 is season 2027)."""
    return d.year + 1 if d.month >= 7 else d.year


def season_days(season):
    d, end = dt.date(season - 1, 11, 1), dt.date(season, 4, 20)
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def parse_day(s):
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def ymd(d):
    return d.strftime("%Y%m%d")


# ===================================================================== ESPN parsing
def parse_event(ev, day):
    comp = (ev.get("competitions") or [None])[0]
    if not comp:
        return None
    cs = comp.get("competitors") or []
    h = [c for c in cs if c.get("homeAway") == "home"]
    a = [c for c in cs if c.get("homeAway") == "away"]
    if len(h) != 1 or len(a) != 1:
        return None
    st = comp.get("status") or ev.get("status") or {}
    typ = st.get("type") or {}

    def side(c):
        t = c.get("team") or {}
        try:
            sc = int(float(c.get("score")))
        except (TypeError, ValueError):
            sc = None
        return {"id": str(t.get("id")), "name": t.get("displayName") or t.get("name") or t.get("location"), "abbr": t.get("abbreviation"), "score": sc,
                "loc": t.get("location"), "short": t.get("shortDisplayName")}
    try:
        period = int(st.get("period") or 0)
    except (TypeError, ValueError):
        period = 0
    state = typ.get("state")
    return {"id": str(ev.get("id")), "day": day.isoformat(), "date": ev.get("date"), "state": state,
            "final": bool(typ.get("completed")) and state == "post", "home": side(h[0]), "away": side(a[0]),
            "neutral": bool(comp.get("neutralSite")), "conf": bool(comp.get("conferenceCompetition")),
            "post": ((ev.get("season") or {}).get("type") == 3), "ot": max(0, period - 2)}


def fetch_day(day, get=None):
    get = get or get_json
    data = get(f"{BASE}/scoreboard?dates={ymd(day)}&groups=50&limit=400")
    out = []
    for ev in data.get("events") or []:
        g = parse_event(ev, day)
        if g and g["home"]["id"] and g["away"]["id"]:
            out.append(g)
    return out


def cmd_collect(a, get=None, today=None, out=print):
    today = today or dt.date.today()
    dd = os.path.join(a.dir, "days")
    os.makedirs(dd, exist_ok=True)
    cur = season_of(today)
    first = a.first_season or (cur - 5)
    fetched = skipped = errs = 0
    for s in range(first, cur + 1):
        for day in season_days(s):
            if day > today:
                break
            p = os.path.join(dd, ymd(day) + ".json")
            if os.path.exists(p) and day < today - dt.timedelta(days=3):
                skipped += 1
                continue
            try:
                games = fetch_day(day, get)
                atomic_write(p, {"day": day.isoformat(), "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "games": games})
                fetched += 1
            except Exception as e:                       # noqa: BLE001
                errs += 1
                out(f"  {day}: {type(e).__name__}: {str(e)[:100]}")
            if getattr(a, "pause", 0):
                time.sleep(a.pause)
        out(f"season {s}: done (fetched {fetched}, already stored {skipped}, errors {errs})")
    allg = load_games(a.dir)
    out(f"{len(allg)} finished Division I-feed games on disk, seasons {sorted({g['season'] for g in allg})}")
    return {"fetched": fetched, "skipped": skipped, "errors": errs, "games": len(allg)}


def load_all_records(d):
    dd = os.path.join(d, "days")
    recs = {}
    if os.path.isdir(dd):
        for f in sorted(os.listdir(dd)):
            if f.endswith(".json"):
                doc = load_json(os.path.join(dd, f), {}) or {}
                for g in doc.get("games", []):
                    recs[g["id"]] = g
    return list(recs.values())


def load_games(d):
    """Finished games with scores, as plain tuples-in-dicts, oldest first, restricted to teams that look like Division I
    (>= MIN_GAMES games in that season: one-off non-Division I opponents appear in the feed too)."""
    raw = [g for g in load_all_records(d) if g.get("final") and g["home"]["score"] is not None and g["away"]["score"] is not None]
    return filter_di(normalize(raw))


def normalize(raw):
    out = []
    for g in raw:
        day = dt.date.fromisoformat(g["day"])
        out.append({"id": g["id"], "day": day, "season": season_of(day), "h": g["home"]["id"], "a": g["away"]["id"], "hn": g["home"]["name"], "an": g["away"]["name"],
                    "hs": g["home"]["score"], "as": g["away"]["score"], "neutral": g["neutral"], "post": g.get("post", False), "conf": g.get("conf", False), "ot": g.get("ot", 0)})
    out.sort(key=lambda g: (g["day"], g["id"]))
    return out


def filter_di(games):
    cnt = {}
    for g in games:
        for t in (g["h"], g["a"]):
            cnt[(g["season"], t)] = cnt.get((g["season"], t), 0) + 1
    return [g for g in games if cnt[(g["season"], g["h"])] >= MIN_GAMES and cnt[(g["season"], g["a"])] >= MIN_GAMES]


# ===================================================================== the model
class League:
    """Running league averages: home score, away score, neutral-site score (per team), overall per-team score."""
    def __init__(self, prior=None):
        zero = {"sh": 0.0, "nh": 0, "sa": 0.0, "na": 0, "sn": 0.0, "nn": 0, "sall": 0.0, "nall": 0, "rh": 0.0, "ra": 0.0, "nr": 0}
        self.p = {**zero, **(prior or {})}
        self.c = dict(zero)

    def add(self, g):
        c = self.c
        if g["neutral"]:
            c["sn"] += g["hs"] + g["as"]
            c["nn"] += 2
        else:
            c["sh"] += g["hs"]
            c["nh"] += 1
            c["sa"] += g["as"]
            c["na"] += 1
        c["sall"] += g["hs"] + g["as"]
        c["nall"] += 2

    def add_ratio(self, rh, ra):
        """One non-neutral game's home and away scores divided by what the teams' strength alone predicts (no home-court term)."""
        self.c["rh"] += rh
        self.c["ra"] += ra
        self.c["nr"] += 1

    def eta(self):
        """Home-court factor NET of team quality: strong teams host more games (and weak opponents), so the raw home-minus-away
        scoring gap overstates home court; this compares home and away scores after dividing out each team's strength."""
        rh, ra, n = self.p["rh"] + self.c["rh"], self.p["ra"] + self.c["ra"], self.p["nr"] + self.c["nr"]
        return (rh - ra) / (rh + ra) if n >= 50 and (rh + ra) else 0.03

    def lbar(self):
        """Average per-team score in non-neutral games (home and away pooled)."""
        lh0 = self._avg("sh", "nh", 72.0, LEAGUE_W)
        la0 = self._avg("sa", "na", 69.0, LEAGUE_W)
        return (lh0 + la0) / 2.0

    def totals(self):
        return {k: self.p[k] + self.c[k] for k in self.c}

    def _avg(self, s, n, fallback, w):
        ps, pn = self.p[s], self.p[n]
        prior = ps / pn if pn else fallback
        cs, cn = self.c[s], self.c[n]
        return (cs + w * prior) / (cn + w) if (cn + w) else fallback

    def avgs(self, raw=False):
        """-> (overall per-team average, home average, away average, neutral-site average).  Unless raw, the home and away averages
        use the home-court factor net of team quality (see eta); raw is the plain home/away averages (used only by the 'league' baseline)."""
        mu = self._avg("sall", "nall", 71.0, LEAGUE_W * 2)
        lh0 = self._avg("sh", "nh", mu + 1.5, LEAGUE_W)
        la0 = self._avg("sa", "na", mu - 1.5, LEAGUE_W)
        if raw:
            lh, la = lh0, la0
        else:
            e = self.eta()
            lb = (lh0 + la0) / 2.0
            lh, la = lb * (1 + e), lb * (1 - e)
        ln = self._avg("sn", "nn", (lh0 + la0) / 2.0, LEAGUE_W)
        return mu, lh, la, ln


class Model:
    """Walk-forward model for one season.  prev = {team: (off, def)} indices from the previous season (or None); league_prior = totals from previous seasons."""
    def __init__(self, variant, params, prev=None, league_prior=None):
        self.v, self.p = variant, params
        self.prev = prev or {}
        self.lg = League(league_prior)
        self.st = {}                       # team -> [n_all, pf_all, pa_all, n_h, pf_h, pa_h, n_a, pf_a, pa_a]
        self.games = []                    # this season's games so far (adj needs them)
        self.adj = {}                      # team -> (off, def), warm-started between days
        self._adj_dirty = False

    # ---- data in
    def add_games(self, games):
        if games:
            mu = self.lg.avgs()[0]
            lb = self.lg.lbar()
            for g in games:                                  # ratios use ONLY information from before this day
                if not g["neutral"]:
                    oh, dh = self._overall(g["h"], mu, True)
                    oa, da = self._overall(g["a"], mu, True)
                    self.lg.add_ratio(g["hs"] / (lb * oh * da), g["as"] / (lb * oa * dh))
        for g in games:
            self.lg.add(g)
            for t, pf, pa, v in ((g["h"], g["hs"], g["as"], "h"), (g["a"], g["as"], g["hs"], "a")):
                s = self.st.setdefault(t, [0, 0.0, 0.0, 0, 0.0, 0.0, 0, 0.0, 0.0])
                s[0] += 1
                s[1] += pf
                s[2] += pa
                if not g["neutral"]:
                    i = 3 if v == "h" else 6
                    s[i] += 1
                    s[i + 1] += pf
                    s[i + 2] += pa
            self.games.append(g)
        if games:
            self._adj_dirty = True

    # ---- indices for raw / blend
    def _overall(self, t, mu, shrink_to_prev):
        s = self.st.get(t)
        n, pf, pa = (s[0], s[1], s[2]) if s else (0, 0.0, 0.0)
        if shrink_to_prev:
            c, k = self.p["carry"], self.p["k_all"]
            po, pd = self.prev.get(t, (1.0, 1.0))
            to, td = c * po + (1 - c) * 1.0, c * pd + (1 - c) * 1.0
        else:
            k, to, td = self.p["k_raw"], 1.0, 1.0
        o = (pf + k * to * mu) / (n + k) / mu if (n + k) else 1.0
        d = (pa + k * td * mu) / (n + k) / mu if (n + k) else 1.0
        return o, d

    def _venue(self, t, mu, lh, la):
        """-> (HO, HD, AO, AD, O, D): home/away points-scored and points-allowed indices (relative to the matching league average) and overall."""
        blend = self.v == "blend"
        s = self.st.get(t) or [0, 0.0, 0.0, 0, 0.0, 0.0, 0, 0.0, 0.0]
        o, d = self._overall(t, mu, blend)
        kv = self.p["k_ven"] if blend else self.p["k_raw"]
        # shrink targets: blend -> overall index x venue league average; raw -> the league average itself (index 1.0)
        to_h, td_h = (o * lh, d * la) if blend else (lh, la)
        to_a, td_a = (o * la, d * lh) if blend else (la, lh)
        den_h, den_a = s[3] + kv, s[6] + kv
        ho = ((s[4] + kv * to_h) / den_h) / lh if den_h else 1.0       # home points scored, vs league home score
        hd = ((s[5] + kv * td_h) / den_h) / la if den_h else 1.0       # home points allowed, vs league away score
        ao = ((s[7] + kv * to_a) / den_a) / la if den_a else 1.0       # away points scored, vs league away score
        ad = ((s[8] + kv * td_a) / den_a) / lh if den_a else 1.0       # away points allowed, vs league home score
        if not blend:
            o, d = (ho + ao) / 2.0, (hd + ad) / 2.0                   # raw: overall = average of the venue indices (neutral games)
        return ho, hd, ao, ad, o, d

    # ---- adjusted ratings
    def _fit_adj(self, iters=4):
        mu, lh, la, ln = self.lg.avgs()
        k, c = self.p["k_adj"], self.p["carry"]
        teams = {}
        for g in self.games:
            teams[g["h"]] = teams.get(g["h"], 0) + 1
            teams[g["a"]] = teams.get(g["a"], 0) + 1
        prior = {t: (c * self.prev.get(t, (1.0, 1.0))[0] + (1 - c), c * self.prev.get(t, (1.0, 1.0))[1] + (1 - c)) for t in teams}
        off = {t: self.adj.get(t, prior[t])[0] for t in teams}
        dfn = {t: self.adj.get(t, prior[t])[1] for t in teams}
        for _ in range(iters):
            no, nd = dict.fromkeys(teams, 0.0), dict.fromkeys(teams, 0.0)
            for g in self.games:
                lvh, lva = (ln, ln) if g["neutral"] else (lh, la)
                h, a = g["h"], g["a"]
                no[h] += g["hs"] / (lvh * dfn[a])
                no[a] += g["as"] / (lva * dfn[h])
                nd[a] += g["hs"] / (lvh * off[h])
                nd[h] += g["as"] / (lva * off[a])
            off = {t: (no[t] + k * prior[t][0]) / (teams[t] + k) for t in teams}
            dfn = {t: (nd[t] + k * prior[t][1]) / (teams[t] + k) for t in teams}
            mo, md = sum(off.values()) / len(off), sum(dfn.values()) / len(dfn)
            off = {t: v / mo for t, v in off.items()}
            dfn = {t: v / md for t, v in dfn.items()}
        self.adj = {t: (off[t], dfn[t]) for t in teams}
        self._adj_dirty = False

    # ---- predict
    def predict(self, h, a, neutral=False):
        if self.v == "league":
            _, lh, la, ln = self.lg.avgs(raw=True)
            return (ln, ln) if neutral else (lh, la)
        mu, lh, la, ln = self.lg.avgs()
        if self.v == "adj":
            if self._adj_dirty or not self.adj:
                if self.games:
                    self._fit_adj()
            c = self.p["carry"]
            def r(t):
                if t in self.adj:
                    return self.adj[t]
                po, pd = self.prev.get(t, (1.0, 1.0))
                return c * po + (1 - c), c * pd + (1 - c)
            (oh, dh), (oa, da) = r(h), r(a)
            return (ln * oh * da, ln * oa * dh) if neutral else (lh * oh * da, la * oa * dh)
        ho, hd, _, _, oh, dh = self._venue(h, mu, lh, la)          # the home team's HOME indices (+ overall)
        _, _, ao, ad, oa, da = self._venue(a, mu, lh, la)          # the away team's AWAY indices (+ overall)
        if neutral:
            return ln * oh * da, ln * oa * dh
        return lh * ho * ad, la * ao * hd                           # THE FORMULA: home scored idx x away allowed idx x avg ; and its mirror

    def season_end(self):
        """Overall (off, def) indices at season end, handed to next season as the roster carry-over prior, plus league totals."""
        if self.v == "adj":
            if self.games:
                self._fit_adj(iters=8)
            prev = dict(self.adj)
        else:
            mu = self.lg.avgs()[0]
            prev = {t: self._overall(t, mu, True) for t in self.st}
        return prev, self.lg.totals()


def by_day(games):
    days, cur, buf = [], None, []
    for g in games:
        if g["day"] != cur:
            if buf:
                days.append((cur, buf))
            cur, buf = g["day"], []
        buf.append(g)
    if buf:
        days.append((cur, buf))
    return days


def walk(games, variant, params, record_seasons):
    """Walk-forward predictions.  A game is predicted using ONLY games from earlier calendar days (the previous season's end-state seeds the new season)."""
    rows, prev, lgp = [], None, None
    for s in sorted({g["season"] for g in games}):
        m = Model(variant, params, prev, lgp)
        for day, dg in by_day([g for g in games if g["season"] == s]):
            if s in record_seasons:
                for g in dg:
                    ph, pa = m.predict(g["h"], g["a"], g["neutral"])
                    st = m.st
                    gp = min((st.get(g["h"]) or [0])[0], (st.get(g["a"]) or [0])[0])
                    rows.append({"id": g["id"], "day": day, "season": s, "ph": ph, "pa": pa, "hs": g["hs"], "as": g["as"], "neutral": g["neutral"],
                                 "post": g["post"], "conf": g["conf"], "min_gp": gp})
            m.add_games(dg)
        prev, lgp = m.season_end()
    return rows


# ===================================================================== probabilities
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_over(mu, sd, line):
    return 1.0 - ncdf((line - mu) / sd)


def game_probs(ph, pa, p):
    sdm, sdt, sdh = p["sd_margin"], p["sd_total"], p["sd_team"]
    return {"home_win": ncdf((ph - pa) / sdm), "total": ph + pa, "margin": ph - pa,
            "total_over": lambda line: p_over(ph + pa, sdt, line),
            "home_over": lambda line: p_over(ph, sdh, line),
            "away_over": lambda line: p_over(pa, sdh, line)}


# ===================================================================== backtest
def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def se(xs):
    xs = list(xs)
    n = len(xs)
    if n < 2:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n)


def sse(r):
    return (r["ph"] - r["hs"]) ** 2 + (r["pa"] - r["as"]) ** 2


def fit_sds(rows):
    def sd(xs):
        m = mean(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1))
    return {"sd_margin": sd([(r["hs"] - r["as"]) - (r["ph"] - r["pa"]) for r in rows]),
            "sd_total": sd([(r["hs"] + r["as"]) - (r["ph"] + r["pa"]) for r in rows]),
            "sd_team": sd([r["hs"] - r["ph"] for r in rows] + [r["as"] - r["pa"] for r in rows])}


def tune_variant(games, variant, tune_season):
    if variant == "league":
        return {}, mean(sse(r) for r in walk(games, "league", DEFAULT_PARAMS, {tune_season}))
    best = None
    for g in GRIDS[variant]:
        p = {**DEFAULT_PARAMS, **g}
        loss = mean(sse(r) for r in walk(games, variant, p, {tune_season}))
        if best is None or loss < best[1]:
            best = (g, loss)
    return best


SHIFT_MIN_N = 200


def situation(neutral, conf, min_gp):
    """Cell for the home-court shift: home-court games only, split by conference vs non-conference and early (a team's first 8 games) vs later."""
    if neutral:
        return None
    return ("conf" if conf else "nonconf") + "_" + ("early" if min_gp < 8 else "late")


def shift_scores(ph, pa, key, p):
    """Move the projected margin by the cell's fitted shift d (points, + = home better), keeping the projected total: home +d/2, away -d/2."""
    d = (p.get("shift") or {}).get(key, 0.0) if key else 0.0
    return ph + d / 2.0, pa - d / 2.0


def shifted_rows(rows, p):
    out = []
    for r in rows:
        ph, pa = shift_scores(r["ph"], r["pa"], situation(r["neutral"], r["conf"], r["min_gp"]), p)
        out.append({**r, "ph": ph, "pa": pa})
    return out


def fit_shift(rows):
    """Mean (actual margin - projected margin) per cell on the given (tuning-season) rows; cells under SHIFT_MIN_N games get no shift."""
    cells = {}
    for r in rows:
        k = situation(r["neutral"], r["conf"], r["min_gp"])
        if k:
            cells.setdefault(k, []).append((r["hs"] - r["as"]) - (r["ph"] - r["pa"]))
    return {k: mean(v) for k, v in cells.items() if len(v) >= SHIFT_MIN_N}, {k: len(v) for k, v in cells.items()}


def win_p(ph, pa, p, stretch=None):
    """P(home win): Normal on the projected margin; ml_stretch > 1 sharpens it (fitted on the tuning season, shipped only if it passes the pre-set test)."""
    k = p.get("ml_stretch", 1.0) if stretch is None else stretch
    return min(max(ncdf(k * (ph - pa) / p["sd_margin"]), 1e-6), 1 - 1e-6)


def fit_stretch(rows, p):
    """Slope on the margin that minimises home-win log loss on the given (tuning-season) rows; grid 0.80..1.80."""
    best = None
    for i in range(80, 181):
        k = i / 100.0
        ll = mean(-math.log(win_p(r["ph"], r["pa"], p, k) if r["hs"] > r["as"] else 1 - win_p(r["ph"], r["pa"], p, k)) for r in rows)
        if best is None or ll < best[1]:
            best = (k, ll)
    return best


def score_rows(rows, p):
    out = {"n": len(rows)}
    out["mae_home"], out["mae_away"] = mean(abs(r["ph"] - r["hs"]) for r in rows), mean(abs(r["pa"] - r["as"]) for r in rows)
    out["mae_margin"] = mean(abs((r["ph"] - r["pa"]) - (r["hs"] - r["as"])) for r in rows)
    out["mae_total"] = mean(abs((r["ph"] + r["pa"]) - (r["hs"] + r["as"])) for r in rows)
    out["rmse_team"] = math.sqrt(mean(sse(r) / 2.0 for r in rows))
    ll, br, acc = [], [], []
    for r in rows:
        pw = win_p(r["ph"], r["pa"], p)
        y = 1.0 if r["hs"] > r["as"] else 0.0
        ll.append(-math.log(pw if y else 1 - pw))
        br.append((pw - y) ** 2)
        acc.append(1.0 if (pw > 0.5) == bool(y) else 0.0)
    out["win_logloss"], out["win_brier"], out["win_acc"] = mean(ll), mean(br), mean(acc)

    def z(vals, sd):
        zs = [v / sd for v in vals]
        return mean(zs), math.sqrt(mean(x * x for x in zs) - mean(zs) ** 2)
    out["z_margin"] = z([(r["hs"] - r["as"]) - (r["ph"] - r["pa"]) for r in rows], p["sd_margin"])
    out["z_total"] = z([(r["hs"] + r["as"]) - (r["ph"] + r["pa"]) for r in rows], p["sd_total"])
    out["z_team"] = z([r["hs"] - r["ph"] for r in rows] + [r["as"] - r["pa"] for r in rows], p["sd_team"])
    return out


def ladder(rows, p):
    """Calibration across lines: for lines offset from each game's own prediction, predicted vs actual over rates (should agree if the spread is right)."""
    res = {"total": [], "team": [], "margin": []}
    for off in (-12, -8, -4, 0, 4, 8, 12):
        pm, ac = [], []
        for r in rows:
            mu = r["ph"] + r["pa"]
            pm.append(p_over(mu, p["sd_total"], mu + off))
            ac.append(1.0 if r["hs"] + r["as"] > mu + off else 0.0)
        res["total"].append((off, mean(pm), mean(ac)))
    for off in (-8, -4, 0, 4, 8):
        pm, ac = [], []
        for r in rows:
            for mu, y in ((r["ph"], r["hs"]), (r["pa"], r["as"])):
                pm.append(p_over(mu, p["sd_team"], mu + off))
                ac.append(1.0 if y > mu + off else 0.0)
        res["team"].append((off, mean(pm), mean(ac)))
    for off in (-10, -5, 0, 5, 10):
        pm, ac = [], []
        for r in rows:
            mu = r["ph"] - r["pa"]
            pm.append(p_over(mu, p["sd_margin"], mu + off))
            ac.append(1.0 if r["hs"] - r["as"] > mu + off else 0.0)
        res["margin"].append((off, mean(pm), mean(ac)))
    return res


def win_bins(rows, p):
    edges = [0, .2, .35, .5, .65, .8, 1.01]
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        s = [(win_p(r["ph"], r["pa"], p), 1.0 if r["hs"] > r["as"] else 0.0) for r in rows]
        s = [(a, b) for a, b in s if lo <= a < hi]
        if s:
            out.append((f"{lo:.2f}-{min(hi, 1):.2f}", len(s), mean(a for a, _ in s), mean(b for _, b in s)))
    return out


def pick_seasons(games, today, tune=None, test=None):
    cur = season_of(today)
    done = sorted(s for s in {g["season"] for g in games} if s < cur)
    if len(done) < 3:
        return None, None, done
    test = test or done[-2:]
    tune = tune or (min(test) - 1)
    return tune, test, done


def cmd_backtest(a, today=None, out=print):
    games = load_games(a.dir)
    today = today or dt.date.today()
    tune, test, done = pick_seasons(games, today, a.tune_season, a.test_seasons)
    if tune is None:
        out(f"need at least 3 completed seasons on disk (have {done}); run collect first")
        return None
    out(f"games on disk: {len(games)}   completed seasons: {done}   TUNE season {tune}   TEST seasons {test}")
    chosen, tuned_loss = {}, {}
    for v in VARIANTS:
        g, loss = tune_variant(games, v, tune)
        chosen[v], tuned_loss[v] = {**DEFAULT_PARAMS, **g}, loss
        out(f"  tuned {v:<6} on {tune}: {g if g else '(no parameters)'}   squared error per team-score {loss / 2:.2f}")
    results = {}
    for v in VARIANTS:
        p = {**chosen[v], **fit_sds(walk(games, v, chosen[v], {tune}))}
        rows = walk(games, v, p, set(test))
        results[v] = (p, rows)
    out("\n=== TEST seasons " + ", ".join(map(str, test)) + " (out of sample; standard deviations fitted on the tuning season only) ===")
    out(f"{'variant':<8}{'games':>7}{'MAE home':>10}{'MAE away':>10}{'MAE margin':>12}{'MAE total':>11}{'RMSE/team':>11}{'win LL':>9}{'Brier':>8}{'acc':>7}")
    for v in VARIANTS:
        s = score_rows(results[v][1], results[v][0])
        out(f"{v:<8}{s['n']:>7}{s['mae_home']:>10.2f}{s['mae_away']:>10.2f}{s['mae_margin']:>12.2f}{s['mae_total']:>11.2f}{s['rmse_team']:>11.2f}{s['win_logloss']:>9.4f}{s['win_brier']:>8.4f}{s['win_acc']:>7.3f}")
    base = {r["id"]: sse(r) for r in results["league"][1]}
    rawe = {r["id"]: sse(r) for r in results["raw"][1]}
    out("\npaired squared-error difference per game (negative = first is better):")
    verdict = {}
    for v in ("raw", "blend", "adj"):
        d = [sse(r) - base[r["id"]] for r in results[v][1]]
        out(f"  {v:<6} vs league average: {mean(d):+8.2f} +/- {se(d):.2f}")
    for v in ("blend", "adj"):
        d = [sse(r) - rawe[r["id"]] for r in results[v][1]]
        verdict[v] = (mean(d), se(d))
        out(f"  {v:<6} vs raw (your formula): {mean(d):+8.2f} +/- {se(d):.2f}   {'-> better than raw by more than 2 se' if mean(d) < -2 * se(d) else '-> not clearly better than raw'}")
    best_tuned = min(("raw", "blend", "adj"), key=lambda v: tuned_loss[v])
    ship = best_tuned if (best_tuned == "raw" or verdict[best_tuned][0] < -2 * verdict[best_tuned][1]) else "raw"
    out(f"\nRULE: best on the tuning season = {best_tuned}; ships {ship}" + ("" if ship == best_tuned else f" (the {best_tuned} gain over raw is not > 2 se)"))
    p = dict(results[ship][0])
    # ---- situational home-court SHIFT test (rule fixed in advance): per-cell mean margin residual fitted on the tuning season only; adopted only if
    #      test win log loss improves by > 2 se (paired) over no shift.  Then the STRETCH test is run on top of whatever shift was adopted.
    tune_rows = walk(games, ship, p, {tune})
    shift, cell_n = fit_shift(tune_rows)
    test_rows0 = results[ship][1]
    test_shift = shifted_rows(test_rows0, {**p, "shift": shift})

    def lls(rows, k=1.0):
        return [-math.log(win_p(r["ph"], r["pa"], p, k) if r["hs"] > r["as"] else 1 - win_p(r["ph"], r["pa"], p, k)) for r in rows]
    dsh = [b - a for a, b in zip(lls(test_rows0), lls(test_shift))]
    adopt_sh = mean(dsh) < -2 * se(dsh)
    p["shift"] = shift if adopt_sh else {}
    out("\nHOME-COURT SHIFT TEST: margin shift per situation, fitted on " + str(tune) + " (points added to the home margin; n = tuning games):")
    for k in sorted(cell_n):
        out(f"   {k:<14} n={cell_n[k]:<6} shift {shift.get(k, 0.0):+.2f}")
    out(f"   test win log loss change per game {mean(dsh):+.4f} +/- {se(dsh):.4f} (negative = better) -> "
        + ("ADOPTED (better by more than 2 se)" if adopt_sh else "NOT adopted (not better by more than 2 se); no shift"))
    for lab, rr in (("no shift", test_rows0), ("shifted", test_shift)):
        ss = score_rows(rr, {**p, "shift": {}})
        out(f"   {lab:<9}: test win LL {ss['win_logloss']:.4f}  Brier {ss['win_brier']:.4f}  accuracy {ss['win_acc']:.3f}  MAE margin {ss['mae_margin']:.2f}  z margin {ss['z_margin'][0]:+.3f}")
    test_rows = test_shift if adopt_sh else test_rows0
    tune_use = shifted_rows(tune_rows, p) if adopt_sh else tune_rows
    # ---- moneyline STRETCH test: slope fitted on the tuning season only; ships only if test log loss improves by > 2 se (paired), else 1.0
    k_fit, ll_tune = fit_stretch(tune_use, p)
    dll = [b - a for a, b in zip(lls(test_rows, 1.0), lls(test_rows, k_fit))]
    adopt = mean(dll) < -2 * se(dll)
    p["ml_stretch"] = k_fit if adopt else 1.0
    out(f"\nMONEYLINE STRETCH TEST (after the shift decision): slope fitted on {tune} = {k_fit:.2f}; test log loss change per game {mean(dll):+.4f} +/- {se(dll):.4f} (negative = better) -> "
        + (f"ADOPTED (better by more than 2 se), ml_stretch {k_fit:.2f}" if adopt else "NOT adopted (not better by more than 2 se), ml_stretch stays 1.00"))
    for k in (1.0, k_fit):
        ss = score_rows(test_rows, {**p, "ml_stretch": k})
        out(f"   slope {k:.2f}: test win LL {ss['win_logloss']:.4f}  Brier {ss['win_brier']:.4f}  accuracy {ss['win_acc']:.3f}")
    out(f"\nSHIPPED: {ship}   parameters {({k: p[k] for k in PARAM_KEYS[ship]})}")
    out(f"spreads fitted on {tune}: margin {p['sd_margin']:.2f}   total {p['sd_total']:.2f}   per-team {p['sd_team']:.2f}")
    rows = test_rows
    s = score_rows(rows, p)
    out(f"z-scores on test (mean should be ~0, sd ~1): margin {s['z_margin'][0]:+.3f}/{s['z_margin'][1]:.3f}   total {s['z_total'][0]:+.3f}/{s['z_total'][1]:.3f}   team score {s['z_team'][0]:+.3f}/{s['z_team'][1]:.3f}")
    lad = ladder(rows, p)
    out("\ncalibration across lines (model over-probability vs actual rate, test seasons):")
    for key, label in (("total", "total points over (line = predicted total + offset)"), ("team", "team points over (line = predicted score + offset)"), ("margin", "spread: home covers (home handicap = -(predicted margin + offset))")):
        out("  " + label)
        for off, pm, ac in lad[key]:
            out(f"    offset {off:+4d}: model {pm:.3f}   actual {ac:.3f}   diff {pm - ac:+.3f}")
    out("\nmoneyline calibration (P(home win) bins):")
    for b, n, pm, ac in win_bins(rows, p):
        out(f"    {b}  n={n:<6} model {pm:.3f}  actual {ac:.3f}")
    out("\nby situation (shipped variant):")
    sit = []
    for label, f in (("first 8 games of a team", lambda r: r["min_gp"] < 8), ("8-15 games", lambda r: 8 <= r["min_gp"] < 16), ("16+ games", lambda r: r["min_gp"] >= 16),
                     ("neutral site", lambda r: r["neutral"]), ("home games", lambda r: not r["neutral"]), ("conference games", lambda r: r["conf"]), ("postseason", lambda r: r["post"])):
        sub = [r for r in rows if f(r)]
        if len(sub) >= 30:
            ss = score_rows(sub, p)
            sit.append({"label": label, **ss})
            out(f"    {label:<24} n={ss['n']:<6} MAE margin {ss['mae_margin']:.2f}  MAE total {ss['mae_total']:.2f}  win LL {ss['win_logloss']:.4f}  z margin {ss['z_margin'][0]:+.3f}  z total {ss['z_total'][0]:+.3f}")
    if getattr(a, "save", True):
        keep = {k: p[k] for k in DEFAULT_PARAMS if k in p}
        keep["variant"], keep["tuned_on"] = ship, {"tune": tune, "test": test}
        atomic_write(os.path.join(a.dir, PARAMS_FILE), keep)
        atomic_write(os.path.join(a.dir, SUMMARY_FILE), {
            "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(), "ship": ship, "tune": tune, "test": test, "params": {k: p[k] for k in PARAM_KEYS[ship]},
            "variants": {v: score_rows(results[v][1], results[v][0]) for v in VARIANTS},
            "shipped": s, "ladder": lad, "bins": win_bins(rows, p), "situations": sit,
            "spreads": {k: p[k] for k in ("sd_margin", "sd_total", "sd_team")}, "ml_stretch": p.get("ml_stretch", 1.0), "shift": p.get("shift") or {}})
        out(f"\nsaved {PARAMS_FILE}")
    return {"ship": ship, "params": p, "results": results, "tuned_loss": tuned_loss, "verdict": verdict}


# ===================================================================== Kalshi market prices (moneyline)
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXNCAAMBGAME"          # two yes/no markets per game: "<team> wins" (no draw in basketball)
SUMMARY_FILE = "cbb_backtest.json"
KALSHI_FILE = "cbb_kalshi.json"
KALSHI_SNAP = "cbb_kalshi_snapshots.jsonl"
KALSHI_MAX_AGE_H = 24                   # stored prices older than this are ignored
KALSHI_WIDE_SPREAD = 0.15               # bid/ask gap above this is flagged SOFT
KALSHI_MAX_SPREAD = 0.30                # gap above this is a placeholder book (e.g. 0.06 / 0.80 on a fresh listing): not a price
_MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def kalshi_quote(m):
    """-> (price, soft) for one yes/no market: bid/ask midpoint when the book is real, else the last trade, else (None, False)."""
    def f(k):
        try:
            return float(m.get(k))
        except (TypeError, ValueError):
            return 0.0
    b, a, l = f("yes_bid_dollars"), f("yes_ask_dollars"), f("last_price_dollars")
    if 0 < b <= a < 1 and a - b <= KALSHI_MAX_SPREAD:
        return (b + a) / 2.0, (a - b) > KALSHI_WIDE_SPREAD
    if 0 < l < 1:
        return l, True
    return None, False


def kalshi_event_date(event_ticker):
    """KXNCAAMBGAME-26NOV05TOWSUTAH -> date(2026, 11, 5), or None."""
    try:
        tag = event_ticker.split("-", 1)[1]
        return dt.date(2000 + int(tag[:2]), _MON[tag[2:5]], int(tag[5:7]))
    except (IndexError, KeyError, ValueError):
        return None


def kalshi_parse(markets):
    """Group the raw market list into games: [{event, date, sides: [{abbr, name, price, soft}]}] (only games with two priced sides)."""
    ev = {}
    for m in markets:
        ev.setdefault(m.get("event_ticker"), []).append(m)
    out = []
    for et, ms in ev.items():
        d = kalshi_event_date(et or "")
        if d is None or len(ms) != 2:
            continue
        sides = []
        for m in ms:
            price, soft = kalshi_quote(m)
            sides.append({"abbr": (m.get("ticker") or "").rsplit("-", 1)[-1], "name": m.get("yes_sub_title") or "", "price": price, "soft": soft})
        if all(sd["price"] is not None for sd in sides):
            out.append({"event": et, "date": d.isoformat(), "sides": sides})
    return out


def _norm(name):
    import re
    import unicodedata
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n.replace("&", " and "))
    n = re.sub(r"\bst\b$", "state", n.strip())
    n = re.sub(r"^st\b", "saint", n)
    n = re.sub(r"\bst\b", "state", n)
    return " ".join(n.split())


# Kalshi name (normalised) -> the name ESPN uses, for schools the two sources spell differently.  Add to this when `kalshi` lists an UNMATCHED game.
KALSHI_ALIASES = {"usc upstate": "sc upstate", "new orleans": "lsu new orleans"}


def _team_score(sd, team):
    """How well a Kalshi side {abbr, name} fits an ESPN team: 3 = same abbreviation or exact short/location name, 2 = ESPN full name starts with it, 0 = no."""
    kn = _norm(sd["name"])
    kn = KALSHI_ALIASES.get(kn, kn)
    if not kn:
        return 0
    if sd["abbr"] and team.get("abbr") and sd["abbr"].upper() == str(team["abbr"]).upper():
        return 3
    if kn in {_norm(team.get("short")), _norm(team.get("loc")), _norm(team.get("name"))}:
        return 3
    return 2 if _norm(team.get("name")).startswith(kn + " ") else 0


def kalshi_match(event, fixtures):
    """-> (fixture, p_home_normalised, overround, soft) for the one fixture whose two teams fit the Kalshi game, else None.
    Kalshi's order is not trusted: each priced side is assigned to home/away by NAME."""
    d = dt.date.fromisoformat(event["date"])
    best = []
    for g in fixtures:
        try:
            gd = dt.date.fromisoformat(g["day"])
        except (KeyError, ValueError):
            continue
        if abs((gd - d).days) > 1:
            continue
        for s0, s1 in ((0, 1), (1, 0)):
            sh, sa = event["sides"][s0], event["sides"][s1]
            a, b = _team_score(sh, g["home"]), _team_score(sa, g["away"])
            if a and b:
                best.append((a + b, g, sh, sa))
    if not best:
        return None
    best.sort(key=lambda x: -x[0])
    if len(best) > 1 and best[0][0] == best[1][0] and best[0][1]["id"] != best[1][1]["id"]:
        return None                                                 # ambiguous: refuse rather than guess
    _, g, sh, sa = best[0]
    tot = sh["price"] + sa["price"]
    return g, sh["price"] / tot, tot, bool(sh["soft"] or sa["soft"]), sh["abbr"], sa["abbr"]


KALSHI_LADDERS = {"spread": "KXNCAAMBSPREAD", "total": "KXNCAAMBTOTAL", "team": "KXNCAAMBTEAMTOTAL"}


def kalshi_parse_ladders(kind, markets):
    """Ladder series (one yes/no market per line).  -> {event tag: [{abbr, k, p}]} for lines with a usable price only.
    spread: 'Team wins by over k' (abbr = the team);  total: 'Over k' (abbr empty);  team: 'Team over k' (abbr = the team)."""
    import re
    out = {}
    for m in markets:
        et = (m.get("event_ticker") or "")
        if "-" not in et:
            continue
        price, soft = kalshi_quote(m)
        try:
            k = float(m.get("floor_strike"))
        except (TypeError, ValueError):
            continue
        if price is None:
            continue
        suffix = (m.get("ticker") or "").rsplit("-", 1)[-1]
        mm = re.match(r"^(.*?)(\d+)$", suffix)
        abbr = (mm.group(1) if mm else suffix) if kind != "total" else ""
        out.setdefault(et.split("-", 1)[1], []).append({"abbr": abbr, "k": k, "p": round(price, 4), "soft": soft})
    return out


def kalshi_ladders_for(ladders, tag, home_abbr, away_abbr):
    """Re-express one game's ladders from the home team's point of view.
    -> {'spread': [(home handicap h, P(home covers))], 'total': [(k, P(over))], 'home': [(k, P(home over))], 'away': [(k, P(away over))]}.
    Tag is the event suffix (date + both abbreviations); the same tag is used by every series of a game."""
    res = {"spread": [], "total": [], "home": [], "away": []}
    for e in (ladders.get("spread") or {}).get(tag, []):
        if e["abbr"].upper() == str(home_abbr).upper():
            res["spread"].append((-e["k"], e["p"]))                       # home wins by over k  == home covers handicap -k
        elif e["abbr"].upper() == str(away_abbr).upper():
            res["spread"].append((e["k"], round(1.0 - e["p"], 4)))        # away wins by over k  => home covers +k with probability 1 - p
    for e in (ladders.get("total") or {}).get(tag, []):
        res["total"].append((e["k"], e["p"]))
    for e in (ladders.get("team") or {}).get(tag, []):
        if e["abbr"].upper() == str(home_abbr).upper():
            res["home"].append((e["k"], e["p"]))
        elif e["abbr"].upper() == str(away_abbr).upper():
            res["away"].append((e["k"], e["p"]))
    for v in res.values():
        v.sort()
    return {k: v for k, v in res.items() if v}


def implied_line(points, span=6.0):
    """Line where the market's probability crosses 50%, interpolated between the two real prices that straddle it (None if they are far apart or none straddle).
    points = [(line, probability)] with probability falling as the line rises (totals, team totals) or rising with the handicap (spread, passed via flip)."""
    pts = sorted(points)
    for (x0, p0), (x1, p1) in zip(pts, pts[1:]):
        if (p0 - 0.5) * (p1 - 0.5) <= 0 and p0 != p1 and abs(x1 - x0) <= span:
            return x0 + (0.5 - p0) * (x1 - x0) / (p1 - p0)
    return None


def kalshi_fetch(get=None, series=None):
    get = get or get_json
    series = series or KALSHI_SERIES
    out, cursor = [], ""
    for _ in range(20):
        data = get(f"{KALSHI_BASE}/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        out.extend(data.get("markets") or [])
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    return out


def refresh_kalshi(d, get=None, now=None):
    """Pull the open game markets, store them (cbb_kalshi.json) and append a price-move snapshot line per game."""
    now = now or dt.datetime.now(dt.timezone.utc)
    events = kalshi_parse(kalshi_fetch(get))
    ladders = {}
    for kind, series in KALSHI_LADDERS.items():
        try:
            ladders[kind] = kalshi_parse_ladders(kind, kalshi_fetch(get, series))
        except Exception:                                       # noqa: BLE001  a missing ladder series never blocks the moneyline
            ladders[kind] = {}
    atomic_write(os.path.join(d, KALSHI_FILE), {"fetched_at": now.isoformat(), "events": events, "ladders": ladders})
    with open(os.path.join(d, KALSHI_SNAP), "a") as f:
        for e in events:
            f.write(json.dumps({"t": now.isoformat(), "event": e["event"], "date": e["date"], "p": {sd["abbr"]: round(sd["price"], 4) for sd in e["sides"]}}) + "\n")
    return events


def kalshi_markets(d, fixtures, now=None):
    """-> ({fixture id: {home, away, overround, soft}}, unmatched Kalshi games) from the stored Kalshi file (ignored if older than KALSHI_MAX_AGE_H)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    st = load_json(os.path.join(d, KALSHI_FILE), None)
    if not st:
        return {}, []
    try:
        age = (now - dt.datetime.fromisoformat(st["fetched_at"])).total_seconds() / 3600.0
    except (KeyError, ValueError, TypeError):
        return {}, []
    if age > KALSHI_MAX_AGE_H:
        return {}, []
    found, miss = {}, []
    for e in st.get("events") or []:
        r = kalshi_match(e, fixtures)
        if r is None:
            if any(abs((dt.date.fromisoformat(g["day"]) - dt.date.fromisoformat(e["date"])).days) <= 1 for g in fixtures if g.get("day")):
                miss.append(e)
            continue
        g, ph, tot, soft, ha, aa = r
        tag = e["event"].split("-", 1)[1]
        lad = kalshi_ladders_for(st.get("ladders") or {}, tag, ha, aa)
        found[g["id"]] = {"home": ph, "away": 1 - ph, "overround": tot, "soft": soft, "lad": lad}
    return found, miss


# ===================================================================== live: slate, predict, log, track
def load_params(d):
    p = {**DEFAULT_PARAMS}
    u = load_json(os.path.join(d, PARAMS_FILE), None)
    if isinstance(u, dict):
        p.update({k: v for k, v in u.items() if k in DEFAULT_PARAMS})
    return p


def current_model(d, params, as_of=None, games=None):
    """Model built from every finished game before `as_of` (default: now), with last season's end-state as the carry-over prior."""
    games = games if games is not None else load_games(d)
    as_of = as_of or dt.date.today()
    cur = season_of(as_of)
    prev, lgp = None, None
    for s in sorted({g["season"] for g in games if g["season"] < cur}):
        m = Model(params["variant"], params, prev, lgp)
        for _, dg in by_day([g for g in games if g["season"] == s]):
            m.add_games(dg)
        prev, lgp = m.season_end()
    m = Model(params["variant"], params, prev, lgp)
    for _, dg in by_day([g for g in games if g["season"] == cur and g["day"] < as_of]):
        m.add_games(dg)
    known = {g["h"] for g in games if g["season"] >= cur - 1} | {g["a"] for g in games if g["season"] >= cur - 1}
    return m, known


def lines_for(ph, pa, p):
    tot, mh, ma = ph + pa, ph, pa
    rnd = lambda x: math.floor(x) + 0.5
    return {"total": [(l, p_over(tot, p["sd_total"], l)) for l in sorted({rnd(tot) + o for o in (-6, -3, 0, 3, 6)})],
            "home": [(l, p_over(mh, p["sd_team"], l)) for l in sorted({rnd(mh) + o for o in (-4, -2, 0, 2, 4)})],
            "away": [(l, p_over(ma, p["sd_team"], l)) for l in sorted({rnd(ma) + o for o in (-4, -2, 0, 2, 4)})],
            # spread ladder: key = home handicap (negative = home favoured); P(home covers) = P(margin + handicap > 0)
            "spread": [(h, p_over(mh - ma, p["sd_margin"], -h)) for h in sorted({-rnd(mh - ma) + o for o in (-6, -3, 0, 3, 6)})]}


def ladder_compare(ph, pa, lad, p):
    """Market ladders vs model at the same lines: {kind: [(line, market prob, model prob)]} plus the 50% crossing lines."""
    mu_m, mu_t = ph - pa, ph + pa
    model = {"spread": lambda h: p_over(mu_m, p["sd_margin"], -h), "total": lambda k: p_over(mu_t, p["sd_total"], k),
             "home": lambda k: p_over(ph, p["sd_team"], k), "away": lambda k: p_over(pa, p["sd_team"], k)}
    rows = {kind: [(x, pm, model[kind](x)) for x, pm in pts] for kind, pts in (lad or {}).items() if kind in model}
    lines = {}
    for kind, r in rows.items():
        x = implied_line([(a, b) for a, b, _ in r])
        if x is not None:
            lines[kind] = x
    return rows, lines


def make_view(g, m, known, p, mk=None):
    hk, ak = g["home"]["id"] in known, g["away"]["id"] in known
    warns = []
    if not hk or not ak:
        warns.append("a team has no Division I history on disk (non-Division I or brand-new): no prediction")
        return {"id": g["id"], "home": g["home"]["name"], "away": g["away"]["name"], "kickoff": g.get("date"), "neutral": g["neutral"], "warnings": warns}
    ph, pa = m.predict(g["home"]["id"], g["away"]["id"], g["neutral"])
    gp = {t: (m.st.get(t) or [0])[0] for t in (g["home"]["id"], g["away"]["id"])}
    ph, pa = shift_scores(ph, pa, situation(g["neutral"], g.get("conf", False), min(gp.values())), p)
    if min(gp.values()) < 4:
        warns.append(f"early season: a team has only {min(gp.values())} games this season; the model leans on last season's carry-over")
    mkt = (mk or {}).get(g["id"])
    rows, mlines = ladder_compare(ph, pa, (mkt or {}).get("lad"), p)
    return {"id": g["id"], "home": g["home"]["name"], "away": g["away"]["name"], "kickoff": g.get("date"), "neutral": g["neutral"],
            "ph": ph, "pa": pa, "total": ph + pa, "margin": ph - pa, "home_win": win_p(ph, pa, p),
            "lines": lines_for(ph, pa, p), "market": mkt, "market_rows": rows, "market_lines": mlines, "warnings": warns}


def fetch_upcoming(today, get=None, days=LOOKAHEAD_DAYS):
    """Scheduled games from today through today + days (days=0 -> today only)."""
    out = []
    for i in range(days + 1):
        d = today + dt.timedelta(days=i)
        for g in fetch_day(d, get):
            if g["state"] == "pre" and not g["final"]:
                out.append(g)
    return out


def print_market_lines(v):
    rows, ml = v.get("market_rows") or {}, v.get("market_lines") or {}
    if not rows:
        return
    names = {"spread": f"spread ({v['home']} handicap)", "total": "total points over", "home": f"{v['home']} over", "away": f"{v['away']} over"}
    for kind in ("spread", "total", "home", "away"):
        if kind in rows:
            fmt = (lambda x: f"{x:+g}") if kind == "spread" else (lambda x: f"{x:g}")
            print(f"   market vs model, {names[kind]} (market/model): " + "  ".join(f"{fmt(x)}: {pm * 100:.0f}%/{pq * 100:.0f}%" for x, pm, pq in rows[kind]))
    own = {"spread": -v["margin"], "total": v["total"], "home": v["ph"], "away": v["pa"]}
    bits = []
    for kind, x in ml.items():
        if kind == "spread":
            bits.append(f"market spread {v['home']} {x:+.1f} (model {own['spread']:+.1f})")
        else:
            bits.append(f"market {names[kind].replace(' points over', '').replace(' over', '')} line {x:.1f} (model {own[kind]:.1f})")
    if bits:
        print("   market lines (where Kalshi crosses 50%): " + "   ".join(bits))


def print_view(v):
    ven = "neutral site" if v["neutral"] else "home court"
    print(f"\n{(v.get('kickoff') or '')[:16]}  {v['home']} vs {v['away']}  ({ven})")
    for w in v["warnings"]:
        print("   !", w)
    if "ph" not in v:
        return
    print(f"   projected score {v['home']} {v['ph']:.1f} - {v['pa']:.1f} {v['away']}   total {v['total']:.1f}   margin {v['margin']:+.1f}")
    print(f"   moneyline: {v['home']} {v['home_win'] * 100:.1f}%   {v['away']} {(1 - v['home_win']) * 100:.1f}%")
    mk = v.get("market")
    if mk:
        print(f"   market (Kalshi mid, normalised): {v['home']} {mk['home'] * 100:.1f}%   {v['away']} {mk['away'] * 100:.1f}%   model - market {((v['home_win'] - mk['home']) * 100):+.1f} pts on {v['home']}"
              + ("   [thin book]" if mk.get("soft") else ""))
        print_market_lines(v)
    for key, label in (("total", "total over"), ("home", f"{v['home']} over"), ("away", f"{v['away']} over")):
        print(f"   {label}: " + "  ".join(f"{l:g}:{p * 100:.0f}%" for l, p in v["lines"][key]))
    print(f"   {v['home']} covers (home spread: probability): " + "  ".join(f"{h:+g}:{p * 100:.0f}%" for h, p in v["lines"]["spread"]))


def log_views(d, views, now):
    path = os.path.join(d, LOG_FILE)
    seen = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    seen[r["id"]] = (round(r["ph"], 2), round(r["pa"], 2), round((r.get("mkt") or {}).get("home", -1), 3))
                except (ValueError, KeyError):
                    pass
    lines = []
    for v in views:
        mkh = round((v.get("market") or {}).get("home", -1), 3)
        if "ph" in v and seen.get(v["id"]) != (round(v["ph"], 2), round(v["pa"], 2), mkh):
            lines.append(json.dumps({"id": v["id"], "kickoff": v["kickoff"], "logged_at": now.isoformat(), "home": v["home"], "away": v["away"], "neutral": v["neutral"],
                                     "ph": v["ph"], "pa": v["pa"], "mkt": ({"home": v["market"]["home"], "soft": v["market"]["soft"], "lad": v["market"].get("lad") or {}} if v.get("market") else None)}))
    if lines:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(lines)


def pull_kalshi(a, kget=None, out=print):
    """Refresh stored Kalshi prices.  Never fatal: on failure the slate falls back to whatever is stored (ignored once older than 24h)."""
    try:
        ev = refresh_kalshi(a.dir, kget)
        out(f"Kalshi: {len(ev)} games with two priced sides")
        return True
    except Exception as e:                                  # noqa: BLE001
        out(f"Kalshi: could not refresh ({type(e).__name__}: {str(e)[:100]}); using stored prices if recent")
        return False


def cmd_slate(a, get=None, today=None, out=print, kget=None):
    today = today or dt.date.today()
    p = load_params(a.dir)
    games = load_games(a.dir)
    if not games:
        out("no games on disk: run collect first")
        return []
    m, known = current_model(a.dir, p, today, games)
    up = fetch_upcoming(today, get, getattr(a, "days", None) if getattr(a, "days", None) is not None else LOOKAHEAD_DAYS)
    if kget is not None or getattr(a, "kalshi", False):
        pull_kalshi(a, kget, out)
    mk, miss = kalshi_markets(a.dir, up)
    views = [make_view(g, m, known, p, mk) for g in sorted(up, key=lambda g: g.get("date") or "")]
    if not getattr(a, "quiet", False):
        out(f"variant {p['variant']}   spreads: margin {p['sd_margin']:.1f}  total {p['sd_total']:.1f}  team {p['sd_team']:.1f}   {len(views)} upcoming games")
        for v in views:
            print_view(v)
    if not getattr(a, "quiet", False) and (mk or miss):
        out(f"\nKalshi market on {len(mk)} of {len(views)} games; {len(miss)} Kalshi game(s) in this window not matched to a fixture (run `kalshi` for the list)")
    n = log_views(a.dir, views, dt.datetime.now(dt.timezone.utc))
    if n:
        out(f"\nlogged {n} prediction(s)")
    return views


def cmd_kalshi(a, get=None, kget=None, today=None, out=print):
    """Pull Kalshi game prices, then show how they line up with the ESPN fixtures (and which Kalshi games could not be matched)."""
    today = today or dt.date.today()
    if not pull_kalshi(a, kget, out):
        return None
    up = fetch_upcoming(today, get, LOOKAHEAD_DAYS)
    if not up:
        out("no upcoming fixtures loaded from ESPN, so nothing to match against yet")
        return None
    mk, miss = kalshi_markets(a.dir, up)
    names = {g["id"]: g for g in up}
    out(f"\n{len(mk)} of {len(up)} upcoming games have a Kalshi price; {len(miss)} Kalshi game(s) near these dates were not matched")
    for gid, q in sorted(mk.items(), key=lambda kv: names[kv[0]].get("date") or ""):
        g = names[gid]
        out(f"   {(g.get('date') or '')[:10]}  {g['home']['name']} {q['home'] * 100:.1f}%  vs  {g['away']['name']} {q['away'] * 100:.1f}%   (book total {q['overround']:.3f})" + ("  [thin book]" if q["soft"] else ""))
    for e in miss:
        out(f"   UNMATCHED {e['date']}  " + " vs ".join(f"{sd['name']} ({sd['abbr']})" for sd in e["sides"]) + "   -> add an alias if this is a Division I game")
    return mk, miss


def resolve_team(games, q):
    q = q.lower().strip()
    best = None
    names = {}
    for g in games:
        names[g["h"]] = g["hn"]
        names[g["a"]] = g["an"]
    for t, n in names.items():
        nl = (n or "").lower()
        sc = 3 if nl == q else 2 if (q in nl or nl in q) else 0
        if sc and (best is None or sc > best[0] or (sc == best[0] and len(nl) < len(names[best[1]]))):
            best = (sc, t)
    return (best[1], names[best[1]]) if best else (None, None)


def cmd_predict(a, today=None, out=print):
    today = today or dt.date.today()
    p = load_params(a.dir)
    games = load_games(a.dir)
    m, known = current_model(a.dir, p, today, games)
    h, hn = resolve_team(games, a.home)
    aw, an = resolve_team(games, a.away)
    if not h or not aw:
        out(f"could not find team: {a.home if not h else a.away}")
        return None
    ph, pa = m.predict(h, aw, a.neutral)
    ph, pa = shift_scores(ph, pa, situation(a.neutral, getattr(a, "conference", False), min((m.st.get(t) or [0])[0] for t in (h, aw))), p)
    res = {"home": hn, "away": an, "neutral": a.neutral, "ph": ph, "pa": pa, "total": ph + pa, "margin": ph - pa, "home_win": win_p(ph, pa, p)}
    if a.total_line is not None:
        res["total_over"] = p_over(ph + pa, p["sd_total"], a.total_line)
    if getattr(a, "spread", None) is not None:
        res["home_cover"] = p_over(ph - pa, p["sd_margin"], -a.spread)
    if a.home_line is not None:
        res["home_over"] = p_over(ph, p["sd_team"], a.home_line)
    if a.away_line is not None:
        res["away_over"] = p_over(pa, p["sd_team"], a.away_line)
    v = make_view({"id": "x", "date": None, "neutral": a.neutral, "conf": getattr(a, "conference", False), "home": {"id": h, "name": hn}, "away": {"id": aw, "name": an}}, m, known, p)
    print_view(v)
    for k in ("total_over", "home_over", "away_over"):
        if k in res:
            out(f"   {k}: {res[k] * 100:.1f}%")
    if "home_cover" in res:
        out(f"   {hn} {a.spread:+g}: covers {res['home_cover'] * 100:.1f}%   {an} {-a.spread:+g}: covers {(1 - res['home_cover']) * 100:.1f}%")
    return res


def cmd_results(a, out=print):
    path = os.path.join(a.dir, LOG_FILE)
    if not os.path.exists(path):
        out("nothing logged yet")
        return 0
    done = {g["id"]: g for g in load_games(a.dir)}
    last = {}
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
                last[r["id"]] = r
            except (ValueError, KeyError):
                pass
    res = load_json(os.path.join(a.dir, RESULTS_FILE), {}) or {}
    n = 0
    for i, r in last.items():
        if i in res or i not in done:
            continue
        g = done[i]
        res[i] = {**r, "hs": g["hs"], "as": g["as"]}
        n += 1
    atomic_write(os.path.join(a.dir, RESULTS_FILE), res)
    out(f"settled {n} new, {len(res)} total, {len(last) - len(res)} still pending")
    return n


def track_stats(d):
    """Structured scorecard over settled logged predictions (None if there are none)."""
    res = load_json(os.path.join(d, RESULTS_FILE), {}) or {}
    rows = [{"ph": r["ph"], "pa": r["pa"], "hs": r["hs"], "as": r["as"], "id": i} for i, r in res.items()]
    if not rows:
        return None
    p = load_params(d)
    out = {"score": score_rows(rows, p), "market": None, "ladders": {}}
    mrows = [r for r in res.values() if (r.get("mkt") or {}).get("home") is not None]
    if mrows:
        dd, lm, lk = [], [], []
        for r in mrows:
            y = r["hs"] > r["as"]
            pm_ = win_p(r["ph"], r["pa"], p)
            pk_ = min(max(r["mkt"]["home"], 1e-6), 1 - 1e-6)
            a_, b_ = -math.log(pm_ if y else 1 - pm_), -math.log(pk_ if y else 1 - pk_)
            lm.append(a_)
            lk.append(b_)
            dd.append(a_ - b_)
        out["market"] = {"n": len(mrows), "model_ll": mean(lm), "market_ll": mean(lk), "diff": mean(dd), "se": se(dd)}
    for kind, label in (("spread", "spread cover"), ("total", "total points over"), ("home", "home team total over"), ("away", "away team total over")):
        per_game = []
        for r in res.values():
            lad = ((r.get("mkt") or {}).get("lad") or {}).get(kind) or []
            ds = []
            for x, pm_ in lad:
                if kind == "spread":
                    mu, sd, y = r["ph"] - r["pa"], p["sd_margin"], (r["hs"] - r["as"]) > -x
                    pq_ = p_over(mu, sd, -x)
                elif kind == "total":
                    mu, sd, y = r["ph"] + r["pa"], p["sd_total"], (r["hs"] + r["as"]) > x
                    pq_ = p_over(mu, sd, x)
                else:
                    mu, y = (r["ph"], r["hs"] > x) if kind == "home" else (r["pa"], r["as"] > x)
                    pq_ = p_over(mu, p["sd_team"], x)
                pq_, pm_ = min(max(pq_, 1e-6), 1 - 1e-6), min(max(pm_, 1e-6), 1 - 1e-6)
                ds.append((-math.log(pq_ if y else 1 - pq_), -math.log(pm_ if y else 1 - pm_)))
            if ds:
                per_game.append((mean(a for a, _ in ds), mean(b for _, b in ds)))
        if per_game:
            dg = [a - b for a, b in per_game]
            out["ladders"][kind] = {"label": label, "n": len(per_game), "model_ll": mean(a for a, _ in per_game), "market_ll": mean(b for _, b in per_game), "diff": mean(dg), "se": se(dg)}
    return out


def cmd_track(a, out=print):
    t = track_stats(a.dir)
    if t is None:
        out("no settled predictions yet")
        return None
    s = t["score"]
    out(f"{s['n']} settled predictions   MAE margin {s['mae_margin']:.2f}  MAE total {s['mae_total']:.2f}  win LL {s['win_logloss']:.4f} (coin flip 0.6931)  Brier {s['win_brier']:.4f} (0.2500)  accuracy {s['win_acc']:.3f}")
    m = t["market"]
    if m:
        out(f"vs Kalshi on {m['n']} settled games with a market: model win LL {m['model_ll']:.4f}   market win LL {m['market_ll']:.4f}   model - market {m['diff']:+.4f} +/- {m['se']:.4f}  (negative = model better)")
    for kind in ("spread", "total", "home", "away"):
        L = t["ladders"].get(kind)
        if L:
            out(f"vs Kalshi {L['label']}: {L['n']} settled games with priced lines   model LL {L['model_ll']:.4f}   market LL {L['market_ll']:.4f}   model - market {L['diff']:+.4f} +/- {L['se']:.4f}")
    out(f"z-scores: margin {s['z_margin'][0]:+.3f}/{s['z_margin'][1]:.3f}   total {s['z_total'][0]:+.3f}/{s['z_total'][1]:.3f}   (mean ~0, sd ~1 when calibrated)")
    return s


# ===================================================================== probe
def cmd_probe(a, get=None, today=None, out=print):
    get = get or get_json
    today = today or dt.date.today()
    ok = True
    try:
        day = today
        for _ in range(60):                                # walk back a week at a time until a day with games (off-season has none)
            games = fetch_day(day, get)
            if len([g for g in games if g["final"]]) >= 3:
                break
            day -= dt.timedelta(days=7)
        out(f"scoreboard {day}: {len(games)} games parsed")
    except Exception as e:                                 # noqa: BLE001
        out(f"PROBE: FAIL cannot read the ESPN scoreboard ({type(e).__name__}: {str(e)[:120]})")
        return 1
    fin = [g for g in games if g["final"]]
    out(f"  finished: {len(fin)}   neutral-site: {sum(g['neutral'] for g in games)}   postseason: {sum(g['post'] for g in games)}   conference: {sum(g['conf'] for g in games)}   OT: {sum(1 for g in games if g['ot'])}")
    if fin:
        g = fin[0]
        out(f"  sample: {g['away']['name']} {g['away']['score']} at {g['home']['name']} {g['home']['score']}  (neutral={g['neutral']})")
        bad = [g for g in fin if g["home"]["score"] is None or g["away"]["score"] is None]
        if bad:
            out(f"  PROBLEM: {len(bad)} finished games without scores")
            ok = False
        low = [g for g in fin if g["home"]["score"] + g["away"]["score"] < 80 or g["home"]["score"] + g["away"]["score"] > 260]
        if low:
            out(f"  note: {len(low)} finished games with an unusual total (forfeits/partial scores?)")
    else:
        out("  no finished games on that day to check scores")
    out("PROBE: PASS" if ok and games else "PROBE: PROBLEM")
    return 0 if ok and games else 1


# ===================================================================== platform (web service)
import threading


def us_today(now=None):
    """Calendar date in the US (ET, minus a late-night buffer) so evening games and after-midnight finishes stay under their own date on a UTC server."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - dt.timedelta(hours=6)).date()


def jsafe(o):
    """JSON-safe copy: tuples -> lists, dates -> iso strings, NaN/inf -> None."""
    if isinstance(o, dict):
        return {str(k): jsafe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsafe(v) for v in o]
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return o


class Platform:
    """Everything the web page and API need.  Reads never raise and never touch the network (cached files / in-memory slate);
    refresh_* methods do the network work and are what the background threads call."""
    def __init__(self, d, get=None, kget=None, today_fn=None):
        self.d, self.get, self.kget, self._today_fn = d, get, kget, today_fn
        os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.boot = {"state": "idle", "note": "", "started": None, "finished": None}
        self._games, self._model_key, self._model = None, None, None
        self._data_version = 0
        self._slate, self._slate_at, self._slate_err = None, None, None
        self.last = {"data": None, "slate": None, "results": None}

    # ---------------------------------------------------------------- state
    def today(self):
        return self._today_fn() if self._today_fn else us_today()

    def needs_bootstrap(self):
        games = load_games(self.d)
        tune, _, _ = pick_seasons(games, self.today())
        return tune is None or not os.path.exists(os.path.join(self.d, PARAMS_FILE))

    def _load(self):
        """-> (model, known, params, games) cached until new data arrives or the date changes."""
        with self.lock:
            key = (self._data_version, self.today())
            if self._model is None or self._model_key != key:
                p = load_params(self.d)
                games = load_games(self.d)
                if not games:
                    raise RuntimeError("no games on disk yet (bootstrap still running)")
                m, known = current_model(self.d, p, self.today(), games)
                self._games, self._model, self._model_key = games, (m, known, p), key
            m, known, p = self._model
            return m, known, p, self._games

    # ---------------------------------------------------------------- refreshers (network)
    def bootstrap(self):
        with self.lock:
            if self.boot["state"] == "running":
                return {"status": "ok", "warnings": ["bootstrap already running"]}
            self.boot = {"state": "running", "note": "starting", "started": dt.datetime.now(dt.timezone.utc).isoformat(), "finished": None}
        note = lambda *x: self.boot.__setitem__("note", " ".join(map(str, x))[:200])
        try:
            cmd_collect(argparse.Namespace(dir=self.d, first_season=None, pause=0.25), get=self.get, today=self.today(), out=note)
            self._data_version += 1
            if self.needs_bootstrap():
                note("collected; running the backtest to tune and save parameters")
                cmd_backtest(argparse.Namespace(dir=self.d, tune_season=None, test_seasons=None, save=True), today=self.today(), out=note)
                self._data_version += 1
            self.boot.update(state="done", note="ready", finished=dt.datetime.now(dt.timezone.utc).isoformat())
            self.refresh_slate()
        except Exception as e:                                   # noqa: BLE001
            self.boot.update(state="error", note=f"{type(e).__name__}: {str(e)[:150]}", finished=dt.datetime.now(dt.timezone.utc).isoformat())
        return {"status": "ok" if self.boot["state"] == "done" else "error", "warnings": [self.boot["note"]]}

    def refresh_data(self, max_seconds=None):
        """Re-pull recent scoreboards (today and the last 3 days are always re-fetched, so finished scores arrive)."""
        try:
            cur = season_of(self.today())
            r = cmd_collect(argparse.Namespace(dir=self.d, first_season=cur - 1, pause=0.1), get=self.get, today=self.today(), out=lambda *x: None)
            self._data_version += 1
            self.last["data"] = dt.datetime.now(dt.timezone.utc).isoformat()
            return {"status": "ok", "warnings": [], **r}
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"]}

    def refresh_slate(self, kalshi=True):
        """Fetch upcoming fixtures + Kalshi prices, price every game, log new predictions, cache the result."""
        try:
            m, known, p, games = self._load()
            today = self.today()
            up = fetch_upcoming(today, self.get, LOOKAHEAD_DAYS)
            msgs = []
            if kalshi:
                pull_kalshi(argparse.Namespace(dir=self.d), self.kget, msgs.append)
            mk, miss = kalshi_markets(self.d, up)
            views = [make_view(g, m, known, p, mk) for g in sorted(up, key=lambda g: g.get("date") or "")]
            n = log_views(self.d, views, dt.datetime.now(dt.timezone.utc))
            with self.lock:
                self._slate, self._slate_at, self._slate_err = views, dt.datetime.now(dt.timezone.utc), None
            self.last["slate"] = self._slate_at.isoformat()
            return {"status": "ok", "warnings": msgs, "games": len(views), "with_market": len(mk), "unmatched_kalshi": len(miss), "logged": n}
        except Exception as e:                                   # noqa: BLE001
            self._slate_err = f"{type(e).__name__}: {str(e)[:150]}"
            return {"status": "error", "warnings": [self._slate_err]}

    def refresh_results(self):
        try:
            n = cmd_results(argparse.Namespace(dir=self.d), out=lambda *x: None)
            self.last["results"] = dt.datetime.now(dt.timezone.utc).isoformat()
            return {"status": "ok", "warnings": [], "settled": n}
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"]}

    def start_background_refresh(self, data_s=3600, slate_s=900, results_s=3600, bootstrap=False):
        self._stop.clear()

        def dloop():
            if bootstrap and self.needs_bootstrap():
                self.bootstrap()
            while not self._stop.is_set():
                self.refresh_data()
                self._stop.wait(data_s)

        def sloop():
            self._stop.wait(10)
            while not self._stop.is_set():
                if self.boot["state"] != "running":
                    self.refresh_slate()
                self._stop.wait(slate_s)

        def rloop():
            self._stop.wait(120)
            while not self._stop.is_set():
                self.refresh_results()
                self._stop.wait(results_s)
        for fn, name in ((dloop, "cbb-data"), (sloop, "cbb-slate"), (rloop, "cbb-results")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------------------- reads (never raise, no network)
    def _warm(self):
        w = []
        if self.boot["state"] == "running":
            w.append("warming up: " + self.boot.get("note", ""))
        elif self.boot["state"] == "error":
            w.append("bootstrap failed: " + self.boot.get("note", ""))
        return w

    def get_slate(self, days=None):
        try:
            warnings = self._warm()
            views = self._slate
            if views is None:
                if self._slate_err:
                    warnings.append("slate error: " + self._slate_err)
                return {"status": "ok", "warnings": warnings + ["no slate computed yet"], "events": [], "n_events": 0}
            if days is not None:
                try:
                    limit = (self.today() + dt.timedelta(days=int(days))).isoformat()
                    views = [v for v in views if (v.get("kickoff") or "")[:10] <= limit or not v.get("kickoff")]
                except ValueError:
                    pass
            age = (dt.datetime.now(dt.timezone.utc) - self._slate_at).total_seconds() / 60.0
            ks = load_json(os.path.join(self.d, KALSHI_FILE), None) or {}
            try:
                kage = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ks["fetched_at"])).total_seconds() / 60.0
            except (KeyError, ValueError, TypeError):
                kage = None
            p = load_params(self.d)
            if kage is None or kage > KALSHI_MAX_AGE_H * 60:
                warnings.append("no recent Kalshi prices (stored file missing or older than 24h)")
            nm = sum(1 for v in views if v.get("market"))
            return jsafe({"status": "ok", "warnings": warnings, "n_events": len(views), "n_with_market": nm, "fixtures_age_minutes": age, "kalshi_age_minutes": kage,
                          "data_through": self._data_through(), "variant": p["variant"], "ml_stretch": p.get("ml_stretch", 1.0),
                          "spreads": {k: p[k] for k in ("sd_margin", "sd_total", "sd_team")}, "events": views})
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"], "events": []}

    def _data_through(self):
        g = self._games
        return g[-1]["day"].isoformat() if g else None

    def predict(self, home, away, neutral=False, conference=False, spread=None, total_line=None, home_line=None, away_line=None):
        try:
            m, known, p, games = self._load()
            h, hn = resolve_team(games, home or "")
            aw, an = resolve_team(games, away or "")
            if not h or not aw:
                return {"status": "error", "warnings": [f"could not find team: {home if not h else away}"]}
            v = make_view({"id": "x", "date": None, "neutral": bool(neutral), "conf": bool(conference), "home": {"id": h, "name": hn}, "away": {"id": aw, "name": an}}, m, known, p)
            res = {"status": "ok", "warnings": v.get("warnings", []), "view": v}
            if "ph" in v:
                if spread is not None:
                    res["home_cover"] = p_over(v["ph"] - v["pa"], p["sd_margin"], -float(spread))
                if total_line is not None:
                    res["total_over"] = p_over(v["ph"] + v["pa"], p["sd_total"], float(total_line))
                if home_line is not None:
                    res["home_over"] = p_over(v["ph"], p["sd_team"], float(home_line))
                if away_line is not None:
                    res["away_over"] = p_over(v["pa"], p["sd_team"], float(away_line))
            return jsafe(res)
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"]}

    def teams(self):
        try:
            games = self._games or load_games(self.d)
            cur = max((g["season"] for g in games), default=None)
            names = {}
            for g in games:
                if g["season"] >= (cur or 0) - 1:
                    names[g["h"]], names[g["a"]] = g["hn"], g["an"]
            return sorted(set(names.values()))
        except Exception:                                        # noqa: BLE001
            return []

    def get_scorecard(self):
        try:
            return jsafe({"status": "ok", "warnings": self._warm(), "logged_predictions": self._count_log(), "live": track_stats(self.d),
                          "retrospective": load_json(os.path.join(self.d, SUMMARY_FILE), None)})
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"]}

    def _count_log(self):
        path = os.path.join(self.d, LOG_FILE)
        try:
            with open(path) as f:
                return len({json.loads(l)["id"] for l in f if l.strip()})
        except (OSError, ValueError, KeyError):
            return 0

    def get_results(self, limit=60):
        try:
            res = load_json(os.path.join(self.d, RESULTS_FILE), {}) or {}
            p = load_params(self.d)
            rows = []
            for i, r in res.items():
                mk = (r.get("mkt") or {}).get("home")
                rows.append({"date": (r.get("kickoff") or "")[:10], "match": f"{r['home']} vs {r['away']}", "p_home": win_p(r["ph"], r["pa"], p), "market_home": mk,
                             "proj": f"{r['ph']:.1f}-{r['pa']:.1f}", "score": f"{r['hs']}-{r['as']}", "home_won": r["hs"] > r["as"]})
            rows.sort(key=lambda x: x["date"], reverse=True)
            return jsafe({"status": "ok", "warnings": [], "n": len(rows), "results": rows[:limit]})
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"]}

    def health(self):
        try:
            nd = len([f for f in os.listdir(os.path.join(self.d, "days"))]) if os.path.isdir(os.path.join(self.d, "days")) else 0
            ks = load_json(os.path.join(self.d, KALSHI_FILE), None) or {}
            return jsafe({"status": "ok", "warnings": self._warm(), "bootstrap": self.boot, "day_files": nd, "data_through": self._data_through(), "params_saved": os.path.exists(os.path.join(self.d, PARAMS_FILE)),
                          "slate_games": len(self._slate or []), "slate_at": self._slate_at, "slate_error": self._slate_err, "kalshi_fetched_at": ks.get("fetched_at"),
                          "kalshi_games": len(ks.get("events") or []), "last": self.last, "logged_predictions": self._count_log()})
        except Exception as e:                                   # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {str(e)[:150]}"]}


# ===================================================================== built-in page
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Men's College Basketball Model</title>
<style>
:root{--bg:#0f1115;--fg:#e8eaed;--mut:#9aa0a6;--card:#171a21;--line:#262b36;--ok:#3ddc97;--bad:#ff6b6b;--warn:#f4b942;--acc:#5aa9ff}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#14171c;--mut:#5f6773;--card:#fff;--line:#dfe3ea;--ok:#0a8f5b;--bad:#c62828;--warn:#9a6a00;--acc:#1565c0}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px}
.banner{margin:10px 16px;padding:8px 12px;border:1px solid var(--warn);border-radius:8px;color:var(--warn);font-size:12px}.banner:empty{display:none}
main{padding:0 16px 40px;max-width:1100px;margin:auto}.tabs{margin-top:8px}.tabs button{margin-right:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:12px 14px}
.hd{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}.nm{font-weight:600;font-size:15px}.meta{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;margin-top:8px}th,td{text-align:right;padding:3px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500;font-size:12px}
.pos{color:var(--ok)}.neg{color:var(--bad)}.w{color:var(--warn);font-size:12px}h3{margin:10px 0 0;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.bar{display:flex;height:22px;border-radius:5px;overflow:hidden;margin-top:6px;font-size:12px}.bar div{display:flex;align-items:center;justify-content:center;color:#fff;white-space:nowrap;min-width:2px}
.h{background:#2e7d5b}.a{background:#b5603a}
select,button,input{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}button{cursor:pointer}button.on{border-color:var(--acc);color:var(--acc)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:700px){.two{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Men's college basketball model · venue-split points indices</h1><div class="sub" id="sub">loading...</div>
<div class="tabs"><button id="t1" class="on" onclick="tab(1)">Upcoming games</button><button id="t2" onclick="tab(2)">Scorecard</button><button id="t3" onclick="tab(3)">Predict a game</button> <button onclick="load()">Refresh</button></div></header>
<div class="banner" id="ban"></div><main id="main"></main>
<script>
let T=1;const pc=x=>x==null?'–':(100*x).toFixed(1)+'%',n1=x=>x==null?'–':x.toFixed(1),n3=x=>x==null?'–':x.toFixed(3);
const sg=x=>x==null?'–':'<span class="'+(x>=0?'pos':'neg')+'">'+(x>=0?'+':'')+(100*x).toFixed(1)+'</span>';
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function tab(i){T=i;for(let k=1;k<=3;k++)document.getElementById('t'+k).className=k==i?'on':'';load()}
function bar(p,hn,an){return '<div class="bar"><div class="h" style="width:'+100*p+'%">'+esc(hn)+' '+pc(p)+'</div><div class="a" style="width:'+100*(1-p)+'%">'+esc(an)+' '+pc(1-p)+'</div></div>'}
function tbl(title,rows,head){return '<h3>'+title+'</h3><table><tr>'+head.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+x+'</td>').join('')+'</tr>').join('')+'</table>'}
function lad(v){
 let h='';const r=v.market_rows||{},L=v.lines,hn=esc(v.home),an=esc(v.away);
 const f=(k,fmt)=>(r[k]||[]).map(x=>[fmt(x[0]),pc(x[1]),pc(x[2]),sg(x[1]!=null?x[2]-x[1]:null)]);
 const sp=f('spread',x=>(x>0?'+':'')+x),to=f('total',x=>x),ho=f('home',x=>x),aw=f('away',x=>x);
 if(sp.length)h+=tbl('Spread · '+hn+' handicap · Kalshi vs model',sp,['Handicap','Kalshi','Model','Model − mkt']);
 if(to.length)h+=tbl('Total points over · Kalshi vs model',to,['Line','Kalshi','Model','Model − mkt']);
 if(ho.length)h+=tbl(hn+' team total over',ho,['Line','Kalshi','Model','Model − mkt']);
 if(aw.length)h+=tbl(an+' team total over',aw,['Line','Kalshi','Model','Model − mkt']);
 h+='<div class="two"><div>'+tbl('Spread · model',L.spread.map(x=>[(x[0]>0?'+':'')+x[0],pc(x[1])]),[hn+' covers','Prob'])+'</div><div>'+tbl('Total points · model',L.total.map(x=>[x[0],pc(x[1]),pc(1-x[1])]),['Line','Over','Under'])+'</div></div>';
 h+='<div class="two"><div>'+tbl(hn+' over',L.home.map(x=>[x[0],pc(x[1])]),['Line','Prob'])+'</div><div>'+tbl(an+' over',L.away.map(x=>[x[0],pc(x[1])]),['Line','Prob'])+'</div></div>';
 const ml=v.market_lines||{};const bits=[];
 if(ml.spread!=null)bits.push('market spread '+hn+' '+(ml.spread>0?'+':'')+ml.spread.toFixed(1)+' (model '+(-v.margin).toFixed(1)+')');
 if(ml.total!=null)bits.push('market total '+ml.total.toFixed(1)+' (model '+v.total.toFixed(1)+')');
 if(bits.length)h+='<div class="meta">Where Kalshi crosses 50%: '+bits.join(' · ')+'</div>';
 return h}
function card(v){
 let h='<div class="card"><div class="hd"><div class="nm">'+esc(v.home)+' vs '+esc(v.away)+'</div><div class="meta">'+(v.kickoff?new Date(v.kickoff).toLocaleString():'')+' · '+(v.neutral?'neutral site':'home court')+'</div></div>';
 (v.warnings||[]).forEach(w=>h+='<div class="w">⚠ '+esc(w)+'</div>');
 if(v.ph==null)return h+'</div>';
 h+='<div class="meta">Projected score '+esc(v.home)+' '+n1(v.ph)+' – '+n1(v.pa)+' '+esc(v.away)+' · total '+n1(v.total)+' · margin '+(v.margin>=0?'+':'')+n1(v.margin)+'</div>';
 h+='<h3>Moneyline</h3>'+bar(v.home_win,v.home,v.away);
 const mk=v.market;
 if(mk){const d=v.home_win-mk.home;h+='<table><tr><th></th><th>'+esc(v.home)+'</th><th>'+esc(v.away)+'</th></tr><tr><td>Model</td><td>'+pc(v.home_win)+'</td><td>'+pc(1-v.home_win)+'</td></tr><tr><td>Market (Kalshi mid)'+(mk.soft?' · thin book':'')+'</td><td>'+pc(mk.home)+'</td><td>'+pc(mk.away)+'</td></tr><tr><td>Model − market (pts)</td><td>'+sg(d)+'</td><td>'+sg(-d)+'</td></tr></table>'}
 return h+lad(v)+'</div>'}
function block(S,title){
 if(!S)return '<div class="card"><div class="nm">'+title+'</div><div class="meta">No settled predictions yet.</div></div>';
 const s=S.score;let h='<div class="card"><div class="nm">'+title+' · '+s.n+' games</div>';
 h+=tbl('Moneyline',[['Win log loss',n3(s.win_logloss),'0.693 (coin flip)'],['Brier',n3(s.win_brier),'0.250'],['Winner picked',pc(s.win_acc),'–']],['','Model','Baseline']);
 h+='<div class="meta">MAE margin '+n1(s.mae_margin)+' · MAE total '+n1(s.mae_total)+' · z margin '+(s.z_margin[0]>=0?'+':'')+s.z_margin[0].toFixed(2)+' · z total '+(s.z_total[0]>=0?'+':'')+s.z_total[0].toFixed(2)+'</div>';
 const rows=[];if(S.market)rows.push(['Moneyline',S.market.n,n3(S.market.model_ll),n3(S.market.market_ll),(S.market.diff>=0?'+':'')+S.market.diff.toFixed(4)+' ± '+S.market.se.toFixed(4)]);
 Object.keys(S.ladders||{}).forEach(k=>{const L=S.ladders[k];rows.push([L.label,L.n,n3(L.model_ll),n3(L.market_ll),(L.diff>=0?'+':'')+L.diff.toFixed(4)+' ± '+L.se.toFixed(4)])});
 if(rows.length)h+=tbl('Versus Kalshi (log loss; negative difference = model better)',rows,['Market','Games','Model','Kalshi','Model − Kalshi']);
 return h+'</div>'}
function retro(R){
 if(!R)return '<div class="card"><div class="nm">Backtest</div><div class="meta">No backtest saved yet. Run: python cbb_platform.py backtest</div></div>';
 let h='<div class="card"><div class="nm">Walk-forward backtest · tuned on '+R.tune+', tested out of sample on '+R.test.join(', ')+'</div><div class="meta">Shipped variant: '+R.ship+' · spreads margin '+n1(R.spreads.sd_margin)+' total '+n1(R.spreads.sd_total)+' team '+n1(R.spreads.sd_team)+'</div>';
 h+=tbl('Variants',Object.keys(R.variants).map(k=>{const x=R.variants[k];return [k,x.n,n1(x.mae_margin),n1(x.mae_total),n1(x.rmse_team),n3(x.win_logloss),n3(x.win_brier),pc(x.win_acc)]}),['Variant','Games','MAE margin','MAE total','RMSE/team','Win LL','Brier','Winner picked']);
 h+=tbl('Moneyline calibration',R.bins.map(b=>[b[0],b[1],pc(b[2]),pc(b[3])]),['P(home win) bin','Games','Predicted','Actual']);
 [['total','Total points over'],['team','Team points over'],['margin','Spread: home covers']].forEach(([k,t])=>{h+=tbl(t+' · model vs actual by line offset',R.ladder[k].map(x=>[(x[0]>0?'+':'')+x[0],pc(x[1]),pc(x[2]),sg(x[2]-x[1]).replace('class="pos"','class="pos"')]),['Offset','Model','Actual','Actual − model'])});
 h+=tbl('By situation',R.situations.map(x=>[x.label,x.n,n1(x.mae_margin),n1(x.mae_total),n3(x.win_logloss),(x.z_margin[0]>=0?'+':'')+x.z_margin[0].toFixed(2)]),['Situation','Games','MAE margin','MAE total','Win LL','z margin']);
 return h+'</div>'}
async function predict(){
 const q=new URLSearchParams({home:document.getElementById('ph').value,away:document.getElementById('pa').value});
 ['pn','pcf'].forEach(i=>{if(document.getElementById(i).checked)q.set(i=='pn'?'neutral':'conference','1')});
 ['psp:spread','ptl:total_line','phl:home_line','pal:away_line'].forEach(s=>{const[a,b]=s.split(':');const v=document.getElementById(a).value;if(v!=='')q.set(b,v)});
 const j=await(await fetch('/api/cbb/predict?'+q)).json();let h='';
 if(j.status!=='ok')h='<div class="w">'+esc((j.warnings||[]).join(' · '))+'</div>';
 else{h=card(j.view);const x=[];if(j.home_cover!=null)x.push('Home covers the handicap: '+pc(j.home_cover));if(j.total_over!=null)x.push('Total over: '+pc(j.total_over));if(j.home_over!=null)x.push('Home team over: '+pc(j.home_over));if(j.away_over!=null)x.push('Away team over: '+pc(j.away_over));if(x.length)h+='<div class="card">'+x.join(' · ')+'</div>'}
 document.getElementById('pres').innerHTML=h}
async function load(){
 if(T==1){
  const j=await(await fetch('/api/cbb/slate')).json();
  document.getElementById('sub').textContent=j.status==='ok'?(j.n_events||0)+' upcoming games ('+(j.n_with_market||0)+' with a Kalshi price) · data through '+(j.data_through||'–')+' · fixtures pulled '+Math.round(j.fixtures_age_minutes||0)+' min ago · model '+(j.variant||''):'';
  document.getElementById('ban').textContent=(j.warnings||[]).join('  ·  ');
  document.getElementById('main').innerHTML=(j.events||[]).map(card).join('')||'<p class="meta">No upcoming games to show yet.</p>';
 }else if(T==2){
  const j=await(await fetch('/api/cbb/scorecard')).json();
  document.getElementById('sub').textContent='running scorecard · '+(j.logged_predictions||0)+' predictions logged';
  document.getElementById('ban').textContent=(j.warnings||[]).join('  ·  ');
  document.getElementById('main').innerHTML=block(j.live,'LIVE record (predictions logged before tip-off)')+retro(j.retrospective);
 }else{
  const t=await(await fetch('/api/cbb/teams')).json();
  document.getElementById('sub').textContent='price any matchup';document.getElementById('ban').textContent='';
  document.getElementById('main').innerHTML='<div class="card"><datalist id="tl">'+(t.teams||[]).map(x=>'<option value="'+esc(x)+'">').join('')+'</datalist>Home <input id="ph" list="tl"> Away <input id="pa" list="tl"> <label><input type="checkbox" id="pn"> neutral</label> <label><input type="checkbox" id="pcf"> conference</label><br><br>Optional lines: home handicap <input id="psp" size="5" placeholder="-4.5"> total <input id="ptl" size="5"> home team <input id="phl" size="5"> away team <input id="pal" size="5"> <button onclick="predict()">Price it</button></div><div id="pres"></div>';
 }
}
load();setInterval(()=>{if(T<3)load()},60000);
</script></body></html>"""


# ===================================================================== HTTP
def serve(platform, host="127.0.0.1", port=8060, cors=False, allow_write=False, token=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    def flag(q, k):
        return str(q.get(k, "")).lower() in ("1", "true", "yes", "on")

    def num(q, k):
        try:
            return float(q[k]) if q.get(k, "") != "" else None
        except ValueError:
            return None

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code=200, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else (obj if isinstance(obj, str) else json.dumps(jsafe(obj))).encode()
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
            routes = {"/api/cbb/slate": lambda: platform.get_slate(q.get("days")),
                      "/api/cbb/predict": lambda: platform.predict(q.get("home", ""), q.get("away", ""), flag(q, "neutral"), flag(q, "conference"), num(q, "spread"),
                                                                    num(q, "total_line"), num(q, "home_line"), num(q, "away_line")),
                      "/api/cbb/scorecard": platform.get_scorecard, "/api/cbb/results": platform.get_results,
                      "/api/cbb/teams": lambda: {"status": "ok", "warnings": [], "teams": platform.teams()},
                      "/health": platform.health, "/api/cbb/health": platform.health}
            fn = routes.get(u.path)
            if fn is None:
                self._send({"status": "error", "warnings": ["not found"]}, 404)
            else:
                self._send(fn())

        def _authorised(self):
            return (not token) or self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/cbb/refresh":
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
            what = body.get("what", "slate")
            fn = {"data": platform.refresh_data, "slate": platform.refresh_slate, "kalshi": platform.refresh_slate, "results": platform.refresh_results,
                  "bootstrap": lambda: (threading.Thread(target=platform.bootstrap, daemon=True).start() or {"status": "ok", "warnings": ["bootstrap started"]})}.get(what)
            if fn is None:
                return self._send({"status": "error", "warnings": ["what must be 'data', 'slate', 'kalshi', 'results' or 'bootstrap'"]}, 400)
            self._send(fn())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/, /api/cbb/slate, /api/cbb/predict, /api/cbb/scorecard, /api/cbb/results, /api/cbb/teams, /health"
          f"{', POST /api/cbb/refresh' if allow_write else ''}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


# ===================================================================== CLI
def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NCAA men's basketball points model: collect, backtest, slate (one file)")
    ap.add_argument("--dir", default=os.environ.get("CBB_DATA_DIR", "cbb_data"))
    sub = ap.add_subparsers(dest="cmd")
    for name in ("probe", "results", "track", "daily", "kalshi"):
        sub.add_parser(name)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--host", default=None)
    sv.add_argument("--cors", action="store_true", default=_env_flag("CBB_CORS"))
    sv.add_argument("--allow-write", action="store_true", default=_env_flag("CBB_ALLOW_WRITE"))
    sv.add_argument("--bootstrap", action="store_true", default=_env_flag("CBB_BOOTSTRAP"))
    sv.add_argument("--data-minutes", type=int, default=int(os.environ.get("CBB_DATA_MINUTES", "60")))
    sv.add_argument("--slate-minutes", type=int, default=int(os.environ.get("CBB_SLATE_MINUTES", "15")))
    sv.add_argument("--results-minutes", type=int, default=int(os.environ.get("CBB_RESULTS_MINUTES", "60")))
    sl = sub.add_parser("slate")
    sl.add_argument("--no-kalshi", dest="kalshi", action="store_false", help="do not refresh Kalshi prices")
    sl.add_argument("--days", type=int, default=None, help="look-ahead in days after today (default 3; 0 = today only)")
    c = sub.add_parser("collect")
    c.add_argument("--first-season", type=int, default=None, help="season label (ending year) to start from (default: current - 5)")
    c.add_argument("--pause", type=float, default=0.25)
    b = sub.add_parser("backtest")
    b.add_argument("--tune-season", type=int, default=None)
    b.add_argument("--test-seasons", type=lambda s: [int(x) for x in s.split(",")], default=None)
    b.add_argument("--no-save", dest="save", action="store_false")
    pr = sub.add_parser("predict")
    pr.add_argument("--home", required=True)
    pr.add_argument("--away", required=True)
    pr.add_argument("--neutral", action="store_true")
    pr.add_argument("--conference", action="store_true", help="conference game (the home-court shift differs for conference play)")
    pr.add_argument("--total-line", type=float, default=None)
    pr.add_argument("--spread", type=float, default=None, help="home team handicap, e.g. -4.5 = home favoured by 4.5")
    pr.add_argument("--home-line", type=float, default=None)
    pr.add_argument("--away-line", type=float, default=None)
    for sp_ in sub.choices.values():
        sp_.add_argument("--dir", default=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.cmd is None:
        ap.print_help()
        return 0
    os.makedirs(a.dir, exist_ok=True)
    if a.cmd == "probe":
        return cmd_probe(a)
    if a.cmd == "collect":
        cmd_collect(a)
    elif a.cmd == "backtest":
        cmd_backtest(a)
    elif a.cmd == "serve":
        pl = Platform(a.dir)
        pl.start_background_refresh(a.data_minutes * 60, a.slate_minutes * 60, a.results_minutes * 60, bootstrap=a.bootstrap)
        port = a.port or int(os.environ.get("PORT", "8060"))
        host = a.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        serve(pl, host, port, cors=a.cors, allow_write=a.allow_write, token=os.environ.get("CBB_API_TOKEN") or None)
    elif a.cmd == "kalshi":
        cmd_kalshi(a)
    elif a.cmd == "slate":
        cmd_slate(a)
    elif a.cmd == "predict":
        cmd_predict(a)
    elif a.cmd == "results":
        cmd_results(a)
    elif a.cmd == "track":
        cmd_track(a)
    elif a.cmd == "daily":
        ns = argparse.Namespace(dir=a.dir, first_season=None, pause=0.25)
        cmd_collect(ns)
        cmd_results(a)
        cmd_slate(argparse.Namespace(dir=a.dir, days=None, quiet=False, kalshi=True))
        cmd_track(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
