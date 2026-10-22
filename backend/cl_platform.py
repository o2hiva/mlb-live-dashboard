"""SERVICE (this file, cl_platform.py): the two Champions League models plus everything around them in one file.
  python cl_platform.py serve --bootstrap      web page + JSON API; first start downloads 4 seasons of the Champions League and 14 domestic leagues,
                                               tunes both models, builds the slate and the backlog; then keeps collecting: Champions League hourly,
                                               domestic leagues every 6h, Kalshi prices every 5 min, model every 30 min, results hourly.
  python cl_platform.py backlog                every Champions League match already played this season, predicted walk-forward -> CSV + summary
  python cl_platform.py bootstrap | refresh | tune | slate | prices | results
  (backtest, project, track, kalshi, probe, collect, check) still work as in cl_models.py.
  Railway: needs numpy (requirements.txt: numpy); Volume at /data; vars CL_DATA_DIR=/data, CL_API_TOKEN, CL_ALLOW_WRITE=1, CL_CORS=1, CL_BOOTSTRAP=1;
  start command: python cl_platform.py serve --bootstrap ; health path /health.  Endpoints: /, /api/cl/slate, /api/cl/scorecard, /api/cl/backlog, /health.


cl_engine -- two Champions League goal models with the same math as the Bundesliga / Championship / J1 platforms:

  MODEL 1 ("CL only")      player plus-minus index + team attack x defence baseline, built ONLY from Champions League matches.
  MODEL 2 ("all leagues")  the same indexes, built from every match of the Champions League AND the domestic leagues of its clubs
                           (ESPN athlete / club ids are shared across competitions), with each match measured against its own
                           competition's average, then a walk-forward league-strength correction fitted on earlier Champions
                           League results (the only place where leagues meet).

Math (unchanged): independent Poisson goals; expected goals home = lg_home x attack index (mean of the MF+FW starters) x defence
index (1 - mean of the DF+GK starters); each player's index is his team's goals for (against) per 90 while he was on the pitch, in
units of league-average goals, shrunk toward the average with a prior of P x 90 minutes; team-level baseline = shrunk attack x
defence ratios (prior TP matches); optional opponent and venue adjustment of every ledger entry; blend of adjusted-player and
team-level expectations.

    python cl_models.py probe                       # FIRST: which competitions ESPN covers well
    python cl_models.py collect                     # download (resumable, slow the first time)
    python cl_models.py check
    python cl_models.py backtest                    # walk-forward comparison of the two models
    python cl_models.py project                     # the next Champions League slate under both models
"""
# ================================================================================================ data collection and parsing

import argparse, datetime as dt, difflib, gzip, json, os, re, sys, time, unicodedata, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

CL_SLUG = "uefa.champions"
# domestic leagues of the clubs that usually reach the Champions League (ESPN slugs; run `probe` to see which ones ESPN covers well)
DOMESTIC = ["eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "ned.1", "por.1", "bel.1", "sco.1", "tur.1", "aut.1", "gre.1", "den.1", "nor.1"]
API = "https://site.api.espn.com/apis/site/v2/sports/soccer/"
WEB = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/"


def base(slug):
    return API + slug


def summary_url(slug):
    return WEB + slug + "/summary?event="


def season_of(d):
    """Season label of a date: the year in which the season started (August start)."""
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.year if d.month >= 7 else d.year - 1


def season_range(label):
    return dt.date(label, 7, 1), dt.date(label + 1, 6, 30)


# ------------------------------------------------------------------------------------------------ http
def get_json(url, tries=4, pause=0.15):
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


def compact_summary(s, ev_meta):
    ros = []
    for r in s.get("rosters") or []:
        pl = []
        for p in r.get("roster") or []:
            a = p.get("athlete") or {}
            pos = p.get("position") or {}
            pl.append({"id": a.get("id"), "name": a.get("displayName") or a.get("fullName"), "pos": pos.get("name"),
                       "abbr": pos.get("abbreviation"), "starter": bool(p.get("starter")), "formationPlace": p.get("formationPlace")})
        ros.append({"homeAway": r.get("homeAway"), "team": (r.get("team") or {}).get("displayName"),
                    "team_id": (r.get("team") or {}).get("id"), "formation": r.get("formation"), "players": pl})
    evs = []
    for k in s.get("keyEvents") or []:
        evs.append({"id": k.get("id"), "type": (k.get("type") or {}).get("type"), "typeText": (k.get("type") or {}).get("text"),
                    "text": k.get("text"), "period": (k.get("period") or {}).get("number"),
                    "disp": (k.get("clock") or {}).get("displayValue"), "value": (k.get("clock") or {}).get("value"),
                    "team": (k.get("team") or {}).get("id"), "scoring": bool(k.get("scoringPlay")), "shootout": bool(k.get("shootout")),
                    "parts": [(x.get("athlete") or {}).get("id") for x in (k.get("participants") or [])]})
    extra = {}
    for k in ("pickcenter", "odds"):
        v = s.get(k)
        if v and len(json.dumps(v)) < 8000:
            extra[k] = v                                   # stored raw: ESPN betting-line data, parsed later if it proves useful
    return {**ev_meta, "rosters": ros, "events": evs, **extra}


def _events_of(sb):
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


_RANGE = {"ok": True}


def scoreboard_range(get, slug, d0, d1, allow_fallback=True):
    """Events for slug between two dates inclusive.  One request per day is the form known to work (`dates=YYYYMMDD`); a single range request
    (`dates=YYYYMMDD-YYYYMMDD`) is tried for longer spans and the collector validates it against day-by-day before trusting it."""
    if d0 == d1:
        return _events_of(get(f"{base(slug)}/scoreboard?dates={d0:%Y%m%d}"))
    if allow_fallback and not _RANGE["ok"]:
        return _daily(get, slug, d0, d1)
    try:
        return _events_of(get(f"{base(slug)}/scoreboard?dates={d0:%Y%m%d}-{d1:%Y%m%d}"))
    except RuntimeError as e:
        if allow_fallback and "HTTP 400" in str(e):
            _RANGE["ok"] = False
            return _daily(get, slug, d0, d1)
        raise


def _daily(get, slug, d0, d1):
    evs, seen = [], set()
    d = d0
    while d <= d1:
        for e in scoreboard_range(get, slug, d, d):
            if e["id"] not in seen:
                seen.add(e["id"]); evs.append(e)
        d += dt.timedelta(days=1)
    return evs


def range_is_complete(get, slug, d0, days=7):
    """Validation: the one-request range over `days` days returns exactly the events found by asking day by day."""
    d1 = d0 + dt.timedelta(days=days - 1)
    r = {e["id"] for e in scoreboard_range(get, slug, d0, d1, allow_fallback=False)}
    s = set()
    for i in range(days):
        d = d0 + dt.timedelta(days=i)
        s |= {e["id"] for e in scoreboard_range(get, slug, d, d)}
    return r == s, len(r), len(s)


VALIDATION_DAYS = [(10, 6), (10, 20), (11, 3), (11, 24), (12, 8), (9, 22), (2, 16), (3, 9), (4, 6), (1, 19)]


def validate_range(get, slug, seasons, today, days=7):
    """Find a week that really contains matches and check the one-request range against day-by-day.  -> (ok, n_range, n_daily, start) or None."""
    for y in seasons:
        for mo, dy in VALIDATION_DAYS:
            c = dt.date(y if mo >= 7 else y + 1, mo, dy)
            if c > today - dt.timedelta(days=days + 1):
                continue
            try:
                ok, nr, ns = range_is_complete(get, slug, c, days)
            except Exception:                                          # noqa: BLE001
                return (False, -1, -1, c)
            if max(nr, ns) >= 3:
                return (ok, nr, ns, c)
    return None


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


# ------------------------------------------------------------------------------------------------ collect
def collect_league(slug, root, seasons, workers=6, range_days=7, today=None, log=print, get=None):
    get = get or get_json
    d = os.path.join(root, slug)
    os.makedirs(os.path.join(d, "days"), exist_ok=True)
    os.makedirs(os.path.join(d, "matches"), exist_ok=True)
    today = today or dt.date.today()
    n_new = n_err = 0
    # is the range request trustworthy for this competition?  (checked on a busy week; falls back to day-by-day)
    rd = range_days
    if rd > 1:
        res = validate_range(get, slug, seasons, today, rd)
        if res is None:
            log(f"  {slug}: could not find a busy week to validate the range request -> using day-by-day scoreboards"); rd = 1
        elif not res[0]:
            log(f"  {slug}: range request " + ("not supported by ESPN" if res[1] < 0 else f"returned {res[1]} events vs {res[2]} day-by-day") + " -> using day-by-day scoreboards"); rd = 1
    for yr in seasons:
        a0, a1 = season_range(yr)
        a1 = min(a1, today)
        specs = []
        cur = a0
        while cur <= a1:
            end = min(cur + dt.timedelta(days=rd - 1), a1)
            specs.append((cur, end, os.path.join(d, "days", f"{cur:%Y%m%d}_{end:%Y%m%d}.json")))
            cur = end + dt.timedelta(days=1)

        def board(sp, slug=slug):
            c0, c1, fp = sp
            if os.path.exists(fp) and (today - c1).days > 7:
                return json.load(open(fp))
            try:
                evs = scoreboard_range(get, slug, c0, c1)
                json.dump(evs, open(fp, "w"))
                return evs
            except Exception as e:                                  # noqa: BLE001
                log(f"  {slug} scoreboard fail {c0}..{c1}: {e}")
                return None
        done = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for evs in ex.map(board, specs):
                if evs is None:
                    n_err += 1
                else:
                    done += [e for e in evs if e["completed"]]
        seen, ids = set(), []
        for e in done:
            if e["id"] not in seen:
                seen.add(e["id"]); ids.append(e)
        todo = [e for e in ids if not os.path.exists(os.path.join(d, "matches", e["id"] + ".json"))]
        log(f"{slug} season {yr}: {len(ids)} completed matches, {len(todo)} to download")

        def one(e, yr=yr):
            try:
                s = get(summary_url(slug) + e["id"])
                meta = {k: e[k] for k in ("id", "date", "name", "home", "away")}
                meta["season"] = yr
                meta["comp"] = slug
                json.dump(compact_summary(s, meta), open(os.path.join(d, "matches", e["id"] + ".json"), "w"))
                return 1
            except Exception as ex:                                 # noqa: BLE001
                log(f"  summary fail {e['id']} {e['name']}: {ex}")
                return 0
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                for i, r in enumerate(ex.map(one, todo)):
                    n_new += r; n_err += 1 - r
                    if (i + 1) % 100 == 0:
                        log(f"   {slug} {yr}: {i + 1}/{len(todo)}")
    log(f"{slug}: done, {n_new} new match files, {n_err} errors (re-run to retry errors)")
    return n_new, n_err



# ------------------------------------------------------------------------------------------------ parsing (as in the Bundesliga / J1 platforms; plus extra time and shootouts)
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
    evs = [e for e in m["events"] if not e.get("shootout")]            # penalty-shootout kicks are not goals
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
            # J1 feed: text names ONE player ("X (Team) Substitution at 45'"); the two players are in `participants`, order not relied on:
            # the one currently on the bench is coming on, the one on the pitch is going off.
            ids = [str(x) for x in (e.get("parts") or []) if x is not None]
            if len(ids) >= 2 and t is not None:
                pp = [next((q for q in players if q["id"] is not None and str(q["id"]) == i), None) for i in ids[:2]]
                pin = next((q for q in pp if q and on_bench(q)), None)
                pout = next((q for q in pp if q and q is not pin and on_pitch(q)), None)
                if pin and pout:
                    pin["start"], pin["end"] = t, end
                    pout["end"] = t
                    if pin["group"] is None:
                        pin["group"], pin["inherited"] = pout["group"], True
                    continue
                prob.append("sub participants not (bench, pitch): " + tx[:60])
            mm = re.match(r"Substitution, (.+?)\. (.+?) replaces (.+?)(?: because of .*)?\.?$", tx.strip())
            if not mm or t is None:
                prob.append("sub unparsed: " + tx[:80]); continue
            sd = side or ("home" if norm(mm.group(1)) in norm(home) or norm(home) in norm(mm.group(1)) else "away")
            pin, pout = find(sd, mm.group(2), on_bench), find(sd, mm.group(3), on_pitch)
            if not pin or not pout:
                prob.append(f"sub names unmatched: {mm.group(2)} / {mm.group(3)}"); continue
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
    for q in players:                                   # anything after the regulation clock (extra time) is outside the model's 90-minute window
        if q["start"] is not None and q["start"] >= end:
            q["start"] = q["end"] = None
        elif q["end"] is not None and q["end"] > end:
            q["end"] = end
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
            goals.append({"t": t, "side": sd, "own": own, "pen": "penalty" in ty or "penalty" in tx.lower()[:40], "per": e["period"] or 1})
        return goals, hg, ag, pr

    goals, hg, ag, gp = attribute(False)
    if (hg, ag) != (fh, fa):
        g2, h2, a2, p2 = attribute(True)
        if (h2, a2) == (fh, fa):
            goals, hg, ag, gp = g2, h2, a2, p2
    prob += gp
    if (hg, ag) != (fh, fa):
        prob.append(f"goal tally {hg}-{ag} != final {fh}-{fa}")
    et = any(g.get("per", 1) > 2 for g in goals)
    rh = sum(1 for g in goals if g.get("per", 1) <= 2 and g["side"] == "home")
    ra = sum(1 for g in goals if g.get("per", 1) <= 2 and g["side"] == "away")
    if (hg, ag) != (fh, fa):
        rh, ra = fh, fa                                  # unreconciled match: kept only so `check` can report it (goals_ok is False)
    return {"home_id": str(m["home"].get("id")), "away_id": str(m["away"].get("id")), "comp": m.get("comp"), "et": et, "id": m["id"], "date": (m["date"] or "")[:10], "season": m.get("season"), "home": home, "away": away,
            "hg": rh, "ag": ra, "end": end, "h1_len": h1_len, "players": players, "goals": [g for g in goals if g.get("per", 1) <= 2], "problems": prob,
            "goals_ok": (hg, ag) == (fh, fa)}




CL2DOM = {}


def unify_player_ids(ms):
    """ESPN can give the same player different athlete ids in the Champions League and in his domestic league.  Clubs ARE shared, so a
    Champions League player is mapped to his domestic id through (club id, normalised name) (or, failing that, club + surname + first initial
    when that is unique).  Mutates Champions League player ids; fills CL2DOM (CL id -> domestic id).  Returns counts."""
    dom, by_last = {}, {}
    for m in ms:
        if m["comp"] == CL_SLUG:
            continue
        for p in m["players"]:
            if p["start"] is None or not p["name"]:
                continue
            tid = m["home_id"] if p["side"] == "home" else m["away_id"]
            k = norm(p["name"])
            dom.setdefault((tid, k), p["id"])
            toks = k.split()
            if toks:
                by_last.setdefault((tid, toks[-1], toks[0][:1]), set()).add(p["id"])
    st = {"cl_players": 0, "same": 0, "remapped": 0, "unmatched": 0}
    seen = {}
    for m in ms:
        if m["comp"] != CL_SLUG:
            continue
        for p in m["players"]:
            if not p["name"]:
                continue
            tid = m["home_id"] if p["side"] == "home" else m["away_id"]
            key = (tid, p["id"])
            if key not in seen:
                k = norm(p["name"])
                new = dom.get((tid, k))
                if new is None:
                    toks = k.split()
                    c = by_last.get((tid, toks[-1], toks[0][:1])) if toks else None
                    new = next(iter(c)) if c and len(c) == 1 else None
                seen[key] = new
                st["cl_players"] += 1
                st["unmatched" if new is None else ("same" if new == p["id"] else "remapped")] += 1
            new = seen[key]
            if new is not None and new != p["id"]:
                CL2DOM[p["id"]] = new
                p["id"] = new
    return st


def load_matches(root, slugs=None, drop_qualifiers=True, unify=True):
    """All stored matches parsed, chronological.  Champions League matches played in July/August are qualifying rounds and are left out
    unless drop_qualifiers=False.  Champions League player ids are mapped to domestic ids (unify=False leaves them as ESPN gave them)."""
    out = []
    if not os.path.isdir(root):
        return out
    for slug in (slugs or sorted(x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x, "matches")))):
        md = os.path.join(root, slug, "matches")
        if not os.path.isdir(md):
            continue
        for f in sorted(os.listdir(md)):
            if f.endswith(".json"):
                raw = json.load(open(os.path.join(md, f)))
                raw["comp"] = slug
                if drop_qualifiers and slug == CL_SLUG and (raw.get("date") or "")[5:7] in ("07", "08"):
                    continue
                out.append(parse_match(raw))
    out.sort(key=lambda x: (x["date"], x["id"]))
    if unify:
        unify_player_ids(out)
    return out


def cmd_check(a):
    import statistics as st
    ms = load_matches(a.dir, a.leagues or None)
    allm = load_matches(a.dir, [CL_SLUG], drop_qualifiers=False) if os.path.isdir(os.path.join(a.dir, CL_SLUG, "matches")) else []
    nq = len(allm) - sum(1 for m in ms if m["comp"] == CL_SLUG)
    comps = sorted({m["comp"] for m in ms})
    print(f"{len(ms)} matches parsed across {len(comps)} competitions\n")
    print(f"{'competition':16s}{'matches':>8s}{'goals ok':>10s}{'minutes ~11':>13s}  by season")
    for c in comps:
        x = [m for m in ms if m["comp"] == c]
        tot = []
        for m in x:
            for side in ("home", "away"):
                mins = sum((p["end"] - p["start"]) for p in m["players"] if p["side"] == side and p["start"] is not None and p["end"] is not None)
                tot.append(mins / m["end"])
        okm = sum(1 for t in tot if 10.0 <= t <= 11.1) / max(len(tot), 1)
        seas = {s: sum(1 for m in x if m["season"] == s) for s in sorted({m["season"] for m in x})}
        print(f"{c:16s}{len(x):8d}{sum(m['goals_ok'] for m in x) / len(x):10.1%}{okm:13.1%}  {seas}")
    cl = [m for m in ms if m["comp"] == CL_SLUG]
    if cl:
        et = [m for m in cl if m.get("et")]
        mon = {}
        for m in allm:
            mon[m["date"][5:7]] = mon.get(m["date"][5:7], 0) + 1
        print(f"\nChampions League matches by month (all stored): {dict(sorted(mon.items()))}; July/August matches (qualifying rounds) left out of the models: {nq}")
        print(f"Champions League: {len(cl)} matches; with extra-time goals: {len(et)} (regulation score used); goals reconcile in {sum(m['goals_ok'] for m in cl)}")
        ids = {}
        for m in ms:
            if m["comp"] != CL_SLUG:
                ids[m["home_id"]] = m["comp"]; ids[m["away_id"]] = m["comp"]
        teams = {}
        for m in cl:
            teams[m["home_id"]] = m["home"]; teams[m["away_id"]] = m["away"]
        un = sorted(n for t, n in teams.items() if t not in ids)
        print(f"Champions League clubs: {len(teams)}; with domestic data collected: {len(teams) - len(un)}; without: {len(un)}")
        if un:
            print("   no domestic league collected for: " + ", ".join(un[:40]) + (" ..." if len(un) > 40 else ""))
        n_odds, sample = 0, None
        for m in cl:
            rp = os.path.join(a.dir, CL_SLUG, "matches", m["id"] + ".json")
            raw = json.load(open(rp)) if os.path.exists(rp) else {}
            if raw.get("pickcenter") or raw.get("odds"):
                n_odds += 1
                if sample is None and raw.get("pickcenter"):
                    sample = (m["date"], m["home"], m["away"], json.dumps(raw["pickcenter"], ensure_ascii=False)[:900])
        print(f"Champions League matches with ESPN betting-line data stored (pickcenter/odds): {n_odds}/{len(cl)}")
        if sample:
            print(f"   sample {sample[0]} {sample[1]} v {sample[2]}: {sample[3]}")
        raw_ms = load_matches(a.dir, a.leagues or None, unify=False)
        pid_dom = {p["id"] for m in raw_ms if m["comp"] != CL_SLUG for p in m["players"] if p["start"] is not None}
        pid_cl = {p["id"] for m in raw_ms if m["comp"] == CL_SLUG for p in m["players"] if p["start"] is not None}
        print(f"Champions League players (ESPN ids as stored): {len(pid_cl)}; same id also in a domestic match: {len(pid_cl & pid_dom)} ({len(pid_cl & pid_dom) / max(len(pid_cl), 1):.0%})")
        st_u = unify_player_ids(raw_ms)
        print(f"   matched to a domestic player through (club, name): {st_u['same'] + st_u['remapped']} of {st_u['cl_players']} (club, player) pairs -- same id {st_u['same']}, different id (re-mapped) {st_u['remapped']}, no domestic match {st_u['unmatched']}")
        nm = {}
        for m in raw_ms:
            if m["comp"] != CL_SLUG:
                for p in m["players"]:
                    if p["start"] is not None and p["name"]:
                        nm.setdefault(norm(p["name"]), {}).setdefault(p["id"], set()).add(m["comp"])
        multi = [v for v in nm.values() if len({c for cs in v.values() for c in cs}) >= 2]
        print(f"   players whose name appears in 2+ domestic leagues: {len(multi)}; of those with one single id (transfers keep their id): {sum(1 for v in multi if len(v) == 1)}")
    pr = {}
    for m in ms:
        for p in m["problems"]:
            k = "goal tally mismatch" if p.startswith("goal tally") else p.split(":")[0][:40]
            pr.setdefault(k, []).append((m["comp"], m["id"], p))
    for k, v in sorted(pr.items(), key=lambda x: -len(x[1]))[:8]:
        print(f"  problem '{k}': {len(v)}   e.g. [{v[0][0]}] {v[0][2][:100]}")
    grp = {}
    for m in ms:
        for p in m["players"]:
            if p["start"] is not None and p["group"] is None:
                grp[p["pos"]] = grp.get(p["pos"], 0) + 1
    if grp:
        print("players who played but have no role group (position -> count):", dict(sorted(grp.items(), key=lambda x: -x[1])[:8]))
    print("mean players used per match", f"{st.mean(sum(1 for p in m['players'] if p['start'] is not None) for m in ms):.1f}")


# ------------------------------------------------------------------------------------------------ probe
def _probe_matches(get, slug, today, n, out):
    """The most recent completed matches of one competition: parsed, with the checks that decide whether the player formula can use it."""
    found = []
    for back in range(0, 120, 14):
        d1 = today - dt.timedelta(days=back)
        d0 = d1 - dt.timedelta(days=13)
        try:
            evs = scoreboard_range(get, slug, d0, d1)
        except Exception as e:                                       # noqa: BLE001
            return {"error": str(e).split(" -> ")[-1][:80]}
        found += [e for e in evs if e["completed"]]
        if len(found) >= n:
            break
    found.sort(key=lambda e: e["date"] or "", reverse=True)
    res = {"found": len(found), "n": 0, "xi": 0, "subs": 0, "goals": 0, "mins": 0, "teams": {}, "players": set(), "probs": [], "months": {}}
    for e in found[:n]:
        try:
            s = get(summary_url(slug) + e["id"])
            m = parse_match(compact_summary(s, {"id": e["id"], "date": e["date"], "name": e["name"], "home": e["home"], "away": e["away"], "season": 0, "comp": slug}))
        except Exception as ex:                                      # noqa: BLE001
            res["probs"].append(f"{e['name']}: {type(ex).__name__}: {ex}"); continue
        res["n"] += 1
        xi = {sd: sum(1 for p in m["players"] if p["side"] == sd and p["start"] == 0.0) for sd in ("home", "away")}
        nsub = sum(1 for p in m["players"] if p["start"] not in (None, 0.0))
        mins = [sum(p["end"] - p["start"] for p in m["players"] if p["side"] == sd and p["start"] is not None and p["end"] is not None) / m["end"] for sd in ("home", "away")]
        res["xi"] += xi["home"] == 11 and xi["away"] == 11
        res["subs"] += nsub > 0
        res["goals"] += bool(m["goals_ok"])
        res["mins"] += all(10.0 <= x <= 11.1 for x in mins)
        res["teams"][m["home_id"]] = m["home"]; res["teams"][m["away_id"]] = m["away"]
        res["players"] |= {p["id"] for p in m["players"] if p["start"] is not None}
        res["probs"] += [f"{m['home']}-{m['away']}: {p}" for p in m["problems"][:2]]
        res["months"][(m["date"] or "")[:7]] = res["months"].get((m["date"] or "")[:7], 0) + 1
        res.setdefault("odds_keys", set()).update(k for k in ("pickcenter", "odds", "winprobability") if s.get(k))
        if slug == CL_SLUG and "odds_sample" not in res and (s.get("pickcenter") or s.get("odds")):
            res["odds_sample"] = {"pickcenter": json.dumps(s.get("pickcenter"), ensure_ascii=False)[:1200], "odds": json.dumps(s.get("odds"), ensure_ascii=False)[:600]}
        res["et"] = res.get("et", 0) + bool(m.get("et"))
    return res


def probe(a, get=None, today=None, out=print):
    get = get or get_json
    today = today or dt.date.today()
    slugs = [CL_SLUG] + [s for s in (a.leagues or DOMESTIC)]
    n = a.n
    out(f"checking {len(slugs)} competitions, {n} recent completed matches each (a few requests per competition)\n")
    out(f"{'competition':16s}{'matches':>8s}{'11v11':>8s}{'subs':>7s}{'goals':>7s}{'min~11':>8s}  verdict")
    R = {}
    for slug in slugs:
        r = _probe_matches(get, slug, today, n, out)
        R[slug] = r
        if "error" in r:
            out(f"{slug:16s}{'-':>8s}{'-':>8s}{'-':>7s}{'-':>7s}{'-':>8s}  NOT AVAILABLE ({r['error']})")
            continue
        ok = r["n"] > 0 and all(r[x] >= 0.75 * r["n"] for x in ("xi", "subs", "goals", "mins"))
        out(f"{slug:16s}{r['n']:8d}{r['xi']:>5d}/{r['n']:<2d}{r['subs']:>4d}/{r['n']:<2d}{r['goals']:>4d}/{r['n']:<2d}{r['mins']:>5d}/{r['n']:<2d}  "
            + ("PASS" if ok else ("PROBLEM" if r["n"] else "no completed matches in the last 120 days")))
        if r["probs"] and not ok:
            out("      " + " | ".join(p[:90] for p in r["probs"][:3]))
    out("")
    cl = R.get(CL_SLUG, {})
    if cl.get("n"):
        dom_teams = {}
        dom_players = set()
        for s_, r in R.items():
            if s_ == CL_SLUG or "error" in r:
                continue
            for t in r["teams"]:
                dom_teams[t] = s_
            dom_players |= r["players"]
        cl_t = cl["teams"]
        mapped = {t: dom_teams[t] for t in cl_t if t in dom_teams}
        out(f"Champions League sample: {cl['n']} matches, months {dict(sorted(cl['months'].items()))}; extra-time matches in sample: {cl.get('et', 0)}")
        out(f"   club ids shared between the Champions League and domestic scoreboards: {len(mapped)}/{len(cl_t)} clubs of the sample found in the domestic samples (small samples: not every club played recently)")
        out(f"   player ids seen in both the Champions League sample and the domestic samples: {len(cl['players'] & dom_players)}")
        out(f"   odds / win-probability fields present in the Champions League summary: {sorted(cl.get('odds_keys', [])) or 'none'}")
        if cl.get("odds_sample"):
            out("   ODDS SAMPLE pickcenter: " + cl["odds_sample"]["pickcenter"])
            out("   ODDS SAMPLE odds: " + cl["odds_sample"]["odds"])
    for slug in slugs[:3]:
        try:
            res = validate_range(get, slug, [season_of(today) - 1, season_of(today)], today)
            if res is None:
                out(f"scoreboard range check {slug}: no busy week found to test")
            else:
                ok, nr, ns, d0 = res
                out(f"scoreboard range check {slug} ({d0}..+6 days): one request {nr if nr >= 0 else 'FAILED'} events vs day-by-day {ns if ns >= 0 else 'n/a'}  -> {'OK' if ok else 'range request not usable (collector will use day-by-day)'}")
        except Exception as e:                                       # noqa: BLE001
            out(f"scoreboard range check {slug}: failed {e}")
    good = [s for s in slugs if "error" not in R[s] and R[s]["n"] and all(R[s][x] >= 0.75 * R[s]["n"] for x in ("xi", "subs", "goals", "mins"))]
    bad = [s for s in slugs if s not in good]
    out("\nPASS: " + ", ".join(good) if good else "\nPASS: none")
    if bad:
        out("not usable as they are (leave them out with --leagues, or paste this output back): " + ", ".join(bad))
    out("CHAMPIONS LEAGUE FEED: " + ("PASS" if CL_SLUG in good else "PROBLEM - paste this output back before collecting"))
    return CL_SLUG in good


def data_main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["probe", "collect", "check"])
    ap.add_argument("--dir", default="cl_data")
    ap.add_argument("--leagues", nargs="*", default=None, help="domestic ESPN slugs (default: the built-in list)")
    ap.add_argument("--seasons", nargs="*", type=int, default=None, help="season labels (start year), default: the last 4 seasons incl. the current")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--n", type=int, default=3, help="probe: recent matches per competition")
    a = ap.parse_args(argv)
    if a.cmd == "probe":
        return 0 if probe(a) else 1
    if a.cmd == "check":
        return cmd_check(a) or 0
    cur = season_of(dt.date.today())
    seasons = a.seasons or list(range(cur - 3, cur + 1))
    slugs = [CL_SLUG] + [s for s in (a.leagues if a.leagues is not None else DOMESTIC)]
    for slug in slugs:
        collect_league(slug, a.dir, seasons, workers=a.workers)
    return 0



# ================================================================================================ models

import math
from collections import defaultdict

LG_DEFAULT = (1.50, 1.20)
LINES_TOTAL = (1.5, 2.5, 3.5)


# ------------------------------------------------------------------------------------------------ maths
def pois_pmf(lam, n=14):
    lam = max(lam, 1e-6)
    p = [math.exp(-lam)]
    for k in range(1, n + 1):
        p.append(p[-1] * lam / k)
    return p


def p_over(lam, line):
    k = int(math.floor(line))
    return max(0.0, 1.0 - sum(pois_pmf(lam, k)[:k + 1]))


def p_btts(lh, la):
    """Both teams score under independent Poisson goals."""
    return (1.0 - math.exp(-max(lh, 1e-6))) * (1.0 - math.exp(-max(la, 1e-6)))


def likeliest_scores(lh, la, k=3, n=8):
    ph, pa = pois_pmf(lh, n), pois_pmf(la, n)
    cells = sorted(((ph[i] * pa[j], i, j) for i in range(n + 1) for j in range(n + 1)), reverse=True)[:k]
    return [{"score": f"{i}-{j}", "p": p} for p, i, j in cells]


TEAM_LINES = (0.5, 1.5, 2.5)


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


def d_ord(s):
    return dt.date.fromisoformat(s[:10]).toordinal()


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


# ------------------------------------------------------------------------------------------------ engine
class LeagueState:
    """Weighted league averages of one competition (home goals, away goals, minutes), as the single-league models compute them,
    maintained incrementally (exponential decay = the same weights)."""
    def __init__(self, hl):
        self.hl, self.sw, self.sh, self.sa, self.smn, self.last = hl, 0.0, 0.0, 0.0, 0.0, None

    def _f(self, day):
        return 0.5 ** ((day - self.last) / self.hl) if (self.hl and self.last is not None and day > self.last) else 1.0

    def add(self, day, h, a, mn):
        f = self._f(day)
        self.sw, self.sh, self.sa, self.smn = self.sw * f, self.sh * f, self.sa * f, self.smn * f
        self.sw += 1; self.sh += h; self.sa += a; self.smn += mn
        self.last = day if self.last is None else max(self.last, day)

    def stats(self, day):
        f = self._f(day)
        sw, sh, sa, smn = self.sw * f, self.sh * f, self.sa * f, self.smn * f
        if sw > 0:
            lgh, lga = sh / sw, sa / sw
            rate = (lgh + lga) / 2 * 90.0 / (smn / sw)
        else:
            lgh, lga = LG_DEFAULT
            rate = (lgh + lga) / 2 * 90.0 / 98.0
        if sw < 20:
            b = sw / 20.0
            lgh, lga = b * lgh + (1 - b) * LG_DEFAULT[0], b * lga + (1 - b) * LG_DEFAULT[1]
        return lgh, lga, rate


class Engine:
    """Walk-forward over chronologically sorted parsed matches of one or several competitions.  Predictions for a date use only
    strictly earlier dates.  With one competition this reproduces the single-league Model exactly (tested)."""
    def __init__(self, priors=(40.0,), team_priors=(10.0,), half_life=None, lineup="actual", adjust=False, lg_half_life=None, adj_team_prior=10.0, team_min=0):
        self.Ps, self.TPs, self.hl, self.lineup, self.adjust, self.lghl = list(priors), list(team_priors), half_life, lineup, adjust, lg_half_life
        self.atp, self.tmin = adj_team_prior, team_min

    def _w(self, now, then):
        return 1.0 if not self.hl else 0.5 ** ((now - then) / self.hl)

    def _team_sums(self, thist, team, day, st):
        """(weighted match count, goals for, goals against) with each entry measured in its own competition's average goals per team."""
        n = gf = ga = 0.0
        for d, f, a, comp in thist.get(team, ()):
            lgh, lga, _ = st(comp)
            avg = (lgh + lga) / 2
            w = self._w(day, d)
            n += w; gf += w * f / avg; ga += w * a / avg
        return n, gf, ga

    def _rates(self, thist, team, day, st, tp):
        n, gf, ga = self._team_sums(thist, team, day, st)
        if n < self.tmin:
            return 1.0, 1.0
        return (gf + tp) / (n + tp), (ga + tp) / (n + tp)

    def run(self, matches, target=None):
        hist = defaultdict(list)            # pid -> [(day, mins, gf, ga, comp)]
        thist = defaultdict(list)           # team id -> [(day, gf, ga, comp)]
        starters = {}                       # team id -> [(pid, group)] of its previous match (any competition in this run)
        ls = {}                             # competition -> LeagueState
        out = []
        i = 0
        while i < len(matches):
            j = i
            while j < len(matches) and matches[j]["date"] == matches[i]["date"]:
                j += 1
            day = d_ord(matches[i]["date"])
            cache = {}

            def st(comp, day=day, cache=cache):
                if comp not in cache:
                    cache[comp] = (ls[comp] if comp in ls else LeagueState(self.lghl)).stats(day)
                return cache[comp]
            for m in matches[i:j]:
                if target is not None and not m.get("_future") and not target(m):
                    continue
                comp = m["comp"]
                lgh, lga, _ = st(comp)
                hid, aid = m["home_id"], m["away_id"]
                rec = {"id": m["id"], "date": m["date"], "season": m["season"], "home": m["home"], "away": m["away"], "home_id": hid, "away_id": aid,
                       "comp": comp, "hg": m["hg"], "ag": m["ag"], "lgh": lgh, "lga": lga, "bh": lgh, "ba": lga}
                ind = {}
                for side, team in (("home", hid), ("away", aid)):
                    if m.get("_future"):
                        ind[side] = list((m.get("_lineup") or {}).get(side) or starters.get(team, []))
                    elif self.lineup == "actual":
                        ind[side] = [(p["id"], p["group"]) for p in m["players"] if p["side"] == side and p["start"] == 0.0 and p["group"]]
                    else:
                        ind[side] = starters.get(team, [])
                att = {P: {} for P in self.Ps}
                dfn = {P: {} for P in self.Ps}
                for side in ("home", "away"):
                    ats = {P: [] for P in self.Ps}
                    dfs = {P: [] for P in self.Ps}
                    nat = ndf = 0
                    for pid, grp in ind[side]:
                        m90 = gfu = gau = 0.0
                        for d, mn, gf, ga, c in hist.get(pid, ()):
                            w = self._w(day, d)
                            rate = st(c)[2]
                            m90 += w * mn; gfu += w * gf / rate; gau += w * ga / rate
                        m90 /= 90.0
                        if grp in ("MF", "FW"):
                            nat += 1
                            for P in self.Ps:
                                ats[P].append((gfu + P) / (m90 + P))
                        elif grp in ("DF", "GK"):
                            ndf += 1
                            for P in self.Ps:
                                dfs[P].append((m90 - gau) / (m90 + P))
                    for P in self.Ps:
                        att[P][side] = sum(ats[P]) / len(ats[P]) if ats[P] else 1.0
                        dfn[P][side] = 1.0 - (sum(dfs[P]) / len(dfs[P]) if dfs[P] else 0.0)
                    rec[side + "_n"] = (nat, ndf)
                rec["x"] = {P: (att[P]["home"] * dfn[P]["away"] * lgh, att[P]["away"] * dfn[P]["home"] * lga) for P in self.Ps}
                rec["t"] = {}
                for tp in self.TPs:
                    ah, dh = self._rates(thist, hid, day, st, tp)
                    aa, da = self._rates(thist, aid, day, st, tp)
                    rec["t"][tp] = (ah * da * lgh, aa * dh * lga)
                rec["known"] = (hid in starters, aid in starters)
                rec["n_prior_home"] = len(thist.get(hid, ()))
                rec["n_prior_away"] = len(thist.get(aid, ()))
                if m.get("_future"):
                    rec["future"] = True
                    rec["lineup_source"] = "confirmed" if (m.get("_lineup") or {}).get("home") and (m.get("_lineup") or {}).get("away") else "previous XI"
                out.append(rec)
            # update with this date's matches (adjustment factors use only information from before this date)
            fac = {}
            for m in matches[i:j]:
                if m.get("_future"):
                    continue
                if self.adjust:
                    lgh, lga, _ = st(m["comp"])
                    avg = (lgh + lga) / 2
                    ah, dh = self._rates(thist, m["home_id"], day, st, self.atp)
                    aa, da = self._rates(thist, m["away_id"], day, st, self.atp)
                    fac[m["id"]] = {"home": (da * lgh / avg, aa * lga / avg), "away": (dh * lga / avg, ah * lgh / avg)}
                else:
                    fac[m["id"]] = {"home": (1.0, 1.0), "away": (1.0, 1.0)}
            for m in matches[i:j]:
                if m.get("_future"):
                    continue
                comp = m["comp"]
                for pid, side, grp, mn, gf, ga, stt in match_rows(m):
                    ff, fa_ = fac[m["id"]][side]
                    hist[pid].append((day, mn, gf / max(ff, 0.2), ga / max(fa_, 0.2), comp))
                thist[m["home_id"]].append((day, m["hg"], m["ag"], comp))
                thist[m["away_id"]].append((day, m["ag"], m["hg"], comp))
                if comp not in ls:
                    ls[comp] = LeagueState(self.lghl)
                ls[comp].add(day, m["hg"], m["ag"], m["end"])
                for side, tid in (("home", m["home_id"]), ("away", m["away_id"])):
                    starters[tid] = [(p["id"], p["group"]) for p in m["players"] if p["side"] == side and p["start"] == 0.0 and p["group"]]
            i = j
        return out


# ------------------------------------------------------------------------------------------------ league-strength correction (Model 2)
def _fit_stage2(rows, nL, lam, use_league, iters=30):
    """Poisson regression with offset log(x0): log lam_home = log x0h + c_h + s[Lh] - s[La];  log lam_away = log x0a + c_a + s[La] - s[Lh].
    c_h, c_a free; s ridge-shrunk toward 0 (lam).  rows: (x0h, x0a, hg, ag, Lh, La).  -> parameter vector."""
    import numpy as np
    n = len(rows)
    p = 2 + (nL if use_league else 0)
    X = np.zeros((2 * n, p))
    off = np.empty(2 * n)
    y = np.empty(2 * n)
    for i, (xh, xa, hg, ag, lh, la) in enumerate(rows):
        X[i, 0] = 1.0
        X[n + i, 1] = 1.0
        if use_league and lh != la:
            X[i, 2 + lh] += 1.0; X[i, 2 + la] -= 1.0
            X[n + i, 2 + la] += 1.0; X[n + i, 2 + lh] -= 1.0
        off[i], off[n + i] = math.log(max(xh, 1e-3)), math.log(max(xa, 1e-3))
        y[i], y[n + i] = hg, ag
    R = np.diag([1e-6, 1e-6] + [lam] * (p - 2))
    beta = np.zeros(p)
    for _ in range(iters):
        mu = np.exp(np.clip(off + X @ beta, -6, 6))
        g = X.T @ (mu - y) + R @ beta
        H = X.T @ (mu[:, None] * X) + R
        step = np.linalg.solve(H, g)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return beta


def stage2(recs, getter, lidx, nL, lam, use_league, only_season=None, min_rows=20):
    """Walk-forward: every prediction uses a fit on Champions League results strictly before its date.  -> list of (xh, xa) or None."""
    out = [None] * len(recs)
    prior = []
    n, k = len(recs), 0
    while k < n:
        j = k
        while j < n and recs[j]["date"] == recs[k]["date"]:
            j += 1
        need = [i for i in range(k, j) if only_season is None or recs[i]["season"] == only_season]
        if need:
            beta = _fit_stage2(prior, nL, lam, use_league) if len(prior) >= min_rows else None
            for i in need:
                xh, xa = getter(recs[i])
                if beta is None:
                    out[i] = (xh, xa); continue
                lh, la = lidx(recs[i]["home_id"]), lidx(recs[i]["away_id"])
                dl = (beta[2 + lh] - beta[2 + la]) if use_league else 0.0
                out[i] = (max(xh, 1e-3) * math.exp(beta[0] + dl), max(xa, 1e-3) * math.exp(beta[1] - dl))
        for i in range(k, j):
            r = recs[i]
            if not r.get("future"):
                xh, xa = getter(r)
                prior.append((xh, xa, r["hg"], r["ag"], lidx(r["home_id"]), lidx(r["away_id"])))
        k = j
    return out


def league_strengths(recs, getter, lidx, nL, lam, names):
    """Fit on all given matches and return {league name: s} (positive = clubs of that league do better in Europe than their stage-1 rating says)."""
    beta = _fit_stage2([(getter(r)[0], getter(r)[1], r["hg"], r["ag"], lidx(r["home_id"]), lidx(r["away_id"])) for r in recs if not r.get("future")], nL, lam, True)
    return {names[i]: float(beta[2 + i]) for i in range(nL)}, float(beta[0]), float(beta[1])


# ------------------------------------------------------------------------------------------------ evaluation
def metrics(recs, pairs, idx=None):
    idx = range(len(recs)) if idx is None else idx
    ll, b25, l25, l12, b12 = [], [], [], [], []
    for i in idx:
        r, (xh, xa) = recs[i], pairs[i]
        ll.append(pois_ll(xh, r["hg"]) + pois_ll(xa, r["ag"]))
        p = p_over(xh + xa, 2.5)
        y = 1.0 if r["hg"] + r["ag"] > 2.5 else 0.0
        b25.append((p - y) ** 2)
        l25.append(-(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9))))
        pr = outcome_probs(xh, xa)
        o = 0 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 2
        l12.append(-math.log(max(pr[o], 1e-9)))
        b12.append(sum((pr[k] - (1.0 if k == o else 0.0)) ** 2 for k in range(3)))
    return {"ll": ll, "b25": b25, "l25": l25, "l12": l12, "b12": b12}


def mean_ll(recs, pairs, idx):
    return sum(pois_ll(pairs[i][0], recs[i]["hg"]) + pois_ll(pairs[i][1], recs[i]["ag"]) for i in idx) / max(len(idx), 1)


def blend(pa, pb, w):
    return [None if (a is None or b is None) else (w * a[0] + (1 - w) * b[0], w * a[1] + (1 - w) * b[1]) for a, b in zip(pa, pb)]


GRID_P = [2, 3, 5, 7, 10, 15, 20, 30, 40, 60, 80, 120, 160]
GRID_TP = [2, 3, 5, 7, 10, 15, 20, 30, 40, 60, 80]
GRID_HL = [None, 730, 365]
GRID_LG = [None, 730, 365, 240, 120]
GRID_LAM = [0.5, 1, 2, 4, 8, 16, 32, 64]


def tune_family(ms, target, tune_season, lg_hl, post, label, log, grid_P=GRID_P, grid_TP=GRID_TP, grid_hl=GRID_HL):
    """Tune player prior / half-life and team prior / half-life of one model on `tune_season` (earlier seasons are burn-in).
    `post(recs, getter, only_season)` turns stage-1 expectations into final ones (identity for Model 1; intercept fit for Model 2)."""
    runs = {}
    for hl in grid_hl:
        runs[hl] = Engine(priors=grid_P, team_priors=grid_TP, half_life=hl, adjust=True, lg_half_life=lg_hl).run(ms, target)
    recs0 = runs[grid_hl[0]]
    idx = [i for i, r in enumerate(recs0) if r["season"] == tune_season]
    rowsP, rowsT = [], []
    for hl in grid_hl:
        recs = runs[hl]
        for P in grid_P:
            pr = post(recs, (lambda r, P=P: r["x"][P]), tune_season)
            rowsP.append((mean_ll(recs, pr, idx), P, hl))
        for TP in grid_TP:
            pr = post(recs, (lambda r, TP=TP: r["t"][TP]), tune_season)
            rowsT.append((mean_ll(recs, pr, idx), TP, hl))
    rowsP.sort(reverse=True); rowsT.sort(reverse=True)
    log(f"\n{label}: tuning on season {tune_season} ({len(idx)} Champions League matches), goals loglik per match")
    log("   player index (adjusted):  " + "   ".join(f"prior {P} hl {hl}: {ll:.4f}" for ll, P, hl in rowsP[:4]))
    log("   team-level baseline:      " + "   ".join(f"prior {P} hl {hl}: {ll:.4f}" for ll, P, hl in rowsT[:4]))
    P, hlp = rowsP[0][1], rowsP[0][2]
    TP, hlt = rowsT[0][1], rowsT[0][2]
    edge = []
    if P in (grid_P[0], grid_P[-1]): edge.append("player prior")
    if TP in (grid_TP[0], grid_TP[-1]): edge.append("team prior")
    if edge:
        log(f"   WARNING: best {' and '.join(edge)} sits at the edge of its grid")
    return {"runs": runs, "P": P, "hl_p": hlp, "TP": TP, "hl_t": hlt, "tune_ll_player": rowsP[0][0], "tune_ll_team": rowsT[0][0]}


def pick_blend(recs, px, pt, idx, log, label):
    best = None
    rows = []
    for k in range(11):
        w = k / 10
        ll = mean_ll(recs, blend(px, pt, w), idx)
        rows.append((ll, w))
        if best is None or ll > best[0]:
            best = (ll, w)
    log(f"   {label} blend weight on the player index (rest = team-level), tuned on the tune season: " + ", ".join(f"{w:.1f}: {ll:.4f}" for ll, w in rows[::2]) + f"   -> w = {best[1]:.1f}")
    return best[1]


IDENT = lambda recs, getter, only=None: [getter(r) if (only is None or r["season"] == only) else None for r in recs]


def lineup_last(ms, target, cfg, lg_hl, which):
    """The same chosen configuration with each team's PREVIOUS starting XI (what is available days ahead)."""
    rp = Engine(priors=[cfg["P"]], team_priors=[cfg["TP"]], half_life=cfg["hl_p"], lineup="last", adjust=True, lg_half_life=lg_hl).run(ms, target)
    rt = rp if cfg["hl_t"] == cfg["hl_p"] else Engine(priors=[cfg["P"]], team_priors=[cfg["TP"]], half_life=cfg["hl_t"], lineup="last", adjust=True, lg_half_life=lg_hl).run(ms, target)
    return rp, rt


# ------------------------------------------------------------------------------------------------ the comparison
def club_leagues(ms):
    """club id -> domestic competition (latest domestic match) and the league index used by the strength correction ('other' = no domestic data)."""
    dom = {}
    for m in ms:
        if m["comp"] != CL_SLUG:
            dom[m["home_id"]] = m["comp"]; dom[m["away_id"]] = m["comp"]
    names = sorted(set(dom.values())) + ["other"]
    ix = {n: i for i, n in enumerate(names)}
    return dom, names, (lambda tid: ix[dom.get(tid, "other")])


def run_models(all_ms, tune_season, log=print, grid_hl=GRID_HL, grid_P=GRID_P, grid_TP=GRID_TP, grid_lam=GRID_LAM):
    ms_all = sorted([m for m in all_ms if m["goals_ok"]], key=lambda m: (m["date"], m["id"]))
    A_ms = [m for m in ms_all if m["comp"] == CL_SLUG]
    target = lambda m: m["comp"] == CL_SLUG                                  # noqa: E731
    dom, names, lidx = club_leagues(ms_all)
    nL = len(names)
    R = {"names": names, "dom": dom}
    # league-average half-life (Champions League baseline, no player model)
    rows = []
    for g in GRID_LG:
        recs = Engine(priors=[1e9], team_priors=[1e9], lg_half_life=g).run(A_ms)
        idx = [i for i, r in enumerate(recs) if r["season"] == tune_season]
        rows.append((mean_ll(recs, [r["x"][1e9] for r in recs], idx), g))
    best_lg = max(rows, key=lambda x: x[0])
    lg = best_lg[1]
    log("league-average half-life (Champions League average goals, tuned on season %s): " % tune_season + ", ".join(f"{g}: {ll:.4f}" for ll, g in rows) + f"   -> {lg}")
    # ---- MODEL 1
    famA = tune_family(A_ms, None, tune_season, lg, IDENT, "MODEL 1 (Champions League matches only)", log, grid_P, grid_TP, grid_hl)
    recsA = famA["runs"][famA["hl_p"]]
    recsAt = famA["runs"][famA["hl_t"]]
    idxA = [i for i, r in enumerate(recsA) if r["season"] == tune_season]
    pxA = [r["x"][famA["P"]] for r in recsA]
    ptA = [r["t"][famA["TP"]] for r in recsAt]
    wA = pick_blend(recsA, pxA, ptA, idxA, log, "Model 1")
    # ---- MODEL 2
    post_int = lambda recs, getter, only=None: stage2(recs, getter, lidx, nL, 1.0, False, only)   # noqa: E731
    famB = tune_family(ms_all, target, tune_season, lg, post_int, "MODEL 2 (all leagues; stage-1 criterion with the intercept fit)", log, grid_P, grid_TP, grid_hl)
    recsB = famB["runs"][famB["hl_p"]]
    recsBt = famB["runs"][famB["hl_t"]]
    idxB = [i for i, r in enumerate(recsB) if r["season"] == tune_season]
    assert [r["id"] for r in recsA] == [r["id"] for r in recsB], "both models must predict the same matches"
    gP = lambda r, P=famB["P"]: r["x"][P]                                    # noqa: E731
    gT = lambda r, TP=famB["TP"]: r["t"][TP]                                 # noqa: E731
    px0 = [gP(r) for r in recsB]
    pt0 = [gT(r) for r in recsBt]
    px1 = stage2(recsB, gP, lidx, nL, 1.0, False)
    pt1 = stage2(recsBt, gT, lidx, nL, 1.0, False)
    lamx = max(grid_lam, key=lambda lam: mean_ll(recsB, stage2(recsB, gP, lidx, nL, lam, True, tune_season), idxB))
    lamt = max(grid_lam, key=lambda lam: mean_ll(recsBt, stage2(recsBt, gT, lidx, nL, lam, True, tune_season), idxB))
    px2 = stage2(recsB, gP, lidx, nL, lamx, True)
    pt2 = stage2(recsBt, gT, lidx, nL, lamt, True)
    log(f"   league-strength ridge (tuned on the tune season): player lam {lamx}, team lam {lamt}")
    wB = pick_blend(recsB, px2, pt2, idxB, log, "Model 2")
    # tuned league baseline (Champions League averages)
    lgA = [(r["bh"], r["ba"]) for r in recsA]
    R.update(dict(lg=lg, famA=famA, famB=famB, wA=wA, wB=wB, lamx=lamx, lamt=lamt, recs=recsA, idx_tune=idxA,
                  actual=dict(base=lgA, A_t=ptA, A_x=pxA, A_z=blend(pxA, ptA, wA), B_t0=pt0, B_t1=pt1, B_t2=pt2, B_x0=px0, B_x1=px1, B_x2=px2, B_z=blend(px2, pt2, wB)),
                  lidx=lidx, nL=nL, gP=gP, gT=gT, recsB=recsB, recsBt=recsBt))
    # previous-XI lineups (deployable days ahead)
    rpA, rtA = lineup_last(A_ms, None, famA, lg, "A")
    rpB, rtB = lineup_last(ms_all, target, famB, lg, "B")
    xa_ = [r["x"][famA["P"]] for r in rpA]; ta_ = [r["t"][famA["TP"]] for r in rtA]
    gP1 = lambda r, P=famB["P"]: r["x"][P]                                    # noqa: E731
    gT1 = lambda r, TP=famB["TP"]: r["t"][TP]                                 # noqa: E731
    xb2 = stage2(rpB, gP1, lidx, nL, lamx, True); tb2 = stage2(rtB, gT1, lidx, nL, lamt, True)
    R["last"] = dict(base=lgA, A_t=ta_, A_x=xa_, A_z=blend(xa_, ta_, wA), B_t2=tb2, B_x2=xb2, B_z=blend(xb2, tb2, wB))
    return R


def _fmt(vs, nd=4):
    m, se = mean_se(vs)
    return f"{m:.{nd}f}"


def _paired(a, b):
    return mean_se([x - y for x, y in zip(a, b)])


TABLE = [("league average (Champions League home/away)", "base"),
         ("MODEL 1 team-level", "A_t"), ("MODEL 1 player index", "A_x"), ("MODEL 1  BLEND  (CL data only)", "A_z"),
         ("MODEL 2 team-level, no league correction", "B_t0"), ("MODEL 2 team-level + intercepts", "B_t1"), ("MODEL 2 team-level + league strengths", "B_t2"),
         ("MODEL 2 player index, no league correction", "B_x0"), ("MODEL 2 player index + intercepts", "B_x1"), ("MODEL 2 player index + league strengths", "B_x2"),
         ("MODEL 2  BLEND  (all leagues + league strengths)", "B_z")]
LAST_TABLE = [t for t in TABLE if t[1] in ("base", "A_t", "A_x", "A_z")] + [("MODEL 2 team-level + league strengths", "B_t2"), ("MODEL 2 player index + league strengths", "B_x2"),
                                                                           ("MODEL 2  BLEND  (all leagues + league strengths)", "B_z")]


def print_table(R, key, table, idx, label, log=print, lead=None):
    recs = R["recs"]
    log(f"\n--- {label}: {len(idx)} Champions League matches ---")
    log(f"{'':52s}{'goals loglik':>13s}{'O/U2.5 Brier':>14s}{'1X2 logloss':>13s}{'1X2 Brier':>11s}")
    M = {}
    for nm, k in table:
        M[k] = metrics(recs, R[key][k], idx)
        log(f"{nm:52s}{_fmt(M[k]['ll']):>13s}{_fmt(M[k]['b25']):>14s}{_fmt(M[k]['l12']):>13s}{_fmt(M[k]['b12']):>11s}")
    return M


def print_pairs(M, pairs, log=print):
    for a, b, lab in pairs:
        if a not in M or b not in M:
            continue
        d, se = _paired(M[a]["ll"], M[b]["ll"])
        d2, se2 = _paired(M[a]["l12"], M[b]["l12"])
        d3, se3 = _paired(M[a]["b25"], M[b]["b25"])
        log(f"  {lab:58s} loglik {d:+.4f} (se {se:.4f})   1X2 logloss {d2:+.4f} (se {se2:.4f})   O/U2.5 Brier {d3:+.4f} (se {se3:.4f})")


def team_total_stats(recs, pairs, idx):
    """Each side's goals over 0.5 / 1.5 / 2.5 (both teams pooled) and both-teams-to-score, from one model's expected goals.
    -> {"over": {line: (mean pred, rate, [per-match Brier])}, "btts": (...), "home15": (pred, rate), "away15": (pred, rate)}"""
    out = {"over": {}}
    for ln in TEAM_LINES:
        pr, y, br = [], [], []
        for i in idx:
            r, (xh, xa) = recs[i], pairs[i]
            ph, pa = p_over(xh, ln), p_over(xa, ln)
            yh, ya = 1.0 if r["hg"] > ln else 0.0, 1.0 if r["ag"] > ln else 0.0
            pr.append((ph + pa) / 2); y.append((yh + ya) / 2); br.append(((ph - yh) ** 2 + (pa - ya) ** 2) / 2)
        out["over"][ln] = (sum(pr) / len(pr), sum(y) / len(y), br)
    pr, y, br = [], [], []
    for i in idx:
        r, (xh, xa) = recs[i], pairs[i]
        p = p_btts(xh, xa); yy = 1.0 if (r["hg"] > 0 and r["ag"] > 0) else 0.0
        pr.append(p); y.append(yy); br.append((p - yy) ** 2)
    out["btts"] = (sum(pr) / len(pr), sum(y) / len(y), br)
    for nm, k in (("home15", 0), ("away15", 1)):
        out[nm] = (sum(p_over(pairs[i][k], 1.5) for i in idx) / len(idx), sum(1 for i in idx if recs[i]["hg" if k == 0 else "ag"] > 1.5) / len(idx))
    return out


def print_team_totals(R, test, log):
    recs = R["recs"]
    S = {k: team_total_stats(recs, R["actual"][k], test) for k in ("base", "A_z", "B_z")}
    log("\nTeam goals over/under (each side, both teams pooled) and both teams to score, test matches (Brier: lower = better):")
    log(f"{'':28s}{'actual':>8s}{'league avg':>12s}{'MODEL 1':>10s}{'MODEL 2':>10s}   predicted mean (league avg / M1 / M2)   MODEL 2 minus MODEL 1 Brier")
    for ln in TEAM_LINES:
        r_ = S["B_z"]["over"][ln]
        d, se = _paired(S["B_z"]["over"][ln][2], S["A_z"]["over"][ln][2])
        log(f"{'team over ' + str(ln):28s}{r_[1]:8.1%}{_fmt(S['base']['over'][ln][2]):>12s}{_fmt(S['A_z']['over'][ln][2]):>10s}{_fmt(S['B_z']['over'][ln][2]):>10s}"
            f"   {S['base']['over'][ln][0]:.1%} / {S['A_z']['over'][ln][0]:.1%} / {S['B_z']['over'][ln][0]:.1%}   {d:+.4f} (se {se:.4f})")
    r_ = S["B_z"]["btts"]
    d, se = _paired(S["B_z"]["btts"][2], S["A_z"]["btts"][2])
    log(f"{'both teams to score':28s}{r_[1]:8.1%}{_fmt(S['base']['btts'][2]):>12s}{_fmt(S['A_z']['btts'][2]):>10s}{_fmt(S['B_z']['btts'][2]):>10s}"
        f"   {S['base']['btts'][0]:.1%} / {S['A_z']['btts'][0]:.1%} / {S['B_z']['btts'][0]:.1%}   {d:+.4f} (se {se:.4f})")
    for nm, lab in (("home15", "home team over 1.5"), ("away15", "away team over 1.5")):
        log(f"   {lab:26s} predicted M1 {S['A_z'][nm][0]:.1%}  M2 {S['B_z'][nm][0]:.1%}  actual {S['B_z'][nm][1]:.1%}")
    d, se = _paired(S["B_z"]["over"][1.5][2], S["base"]["over"][1.5][2])
    log(f"   MODEL 2 vs league average, team over 1.5 Brier: {d:+.4f} (se {se:.4f})  (negative = model better)")


def report(R, tune_season, log=print):
    recs = R["recs"]
    test = [i for i, r in enumerate(recs) if r["season"] > tune_season]
    seasons = sorted({recs[i]["season"] for i in test})
    lidx = R["lidx"]
    M = print_table(R, "actual", TABLE, test, f"TEST seasons {seasons}; lineup = the actual starting XI (known ~1h before kickoff)", log)
    log("\nPaired differences per match (loglik: + = first model better; logloss / Brier: - = first model better):")
    print_pairs(M, [("B_z", "A_z", "MODEL 2 minus MODEL 1  (the two models, final blends)"),
                    ("A_z", "base", "MODEL 1 vs league average"), ("B_z", "base", "MODEL 2 vs league average"),
                    ("A_x", "A_t", "MODEL 1 player index vs its team-level"), ("B_x2", "B_t2", "MODEL 2 player index vs its team-level"),
                    ("B_x0", "A_x", "all-leagues data alone (no correction) vs CL-only, player index"), ("B_t0", "A_t", "all-leagues data alone (no correction) vs CL-only, team-level"),
                    ("B_x1", "B_x0", "intercept correction, player index"), ("B_x2", "B_x1", "league strengths on top of intercepts, player index"),
                    ("B_t2", "B_t1", "league strengths on top of intercepts, team-level")], log)
    # subsets
    log("\nSubsets (MODEL 2 minus MODEL 1, goals loglik / 1X2 logloss):")
    both = [i for i in test if lidx(recs[i]["home_id"]) != R["nL"] - 1 and lidx(recs[i]["away_id"]) != R["nL"] - 1]
    some = [i for i in test if i not in set(both)]
    exp_lo = [i for i in test if min(recs[i]["n_prior_home"], recs[i]["n_prior_away"]) <= 5]
    exp_hi = [i for i in test if i not in set(exp_lo)]
    for nm, ids in (("both clubs have domestic data", both), ("a club without domestic data", some), ("a club with <= 5 prior CL matches in the data (CL-only is thin)", exp_lo), ("both clubs have > 5 prior CL matches", exp_hi)):
        if len(ids) < 8:
            log(f"  {nm}: {len(ids)} matches (too few)"); continue
        Mi = {k: metrics(recs, R["actual"][k], ids) for k in ("B_z", "A_z")}
        d, se = _paired(Mi["B_z"]["ll"], Mi["A_z"]["ll"])
        d2, se2 = _paired(Mi["B_z"]["l12"], Mi["A_z"]["l12"])
        log(f"  {nm:62s} n={len(ids):4d}  loglik {d:+.4f} (se {se:.4f})  1X2 logloss {d2:+.4f} (se {se2:.4f})")
    for s in seasons:
        ids = [i for i in test if recs[i]["season"] == s]
        Mi = {k: metrics(recs, R["actual"][k], ids) for k in ("B_z", "A_z")}
        d, se = _paired(Mi["B_z"]["ll"], Mi["A_z"]["ll"])
        d2, se2 = _paired(Mi["B_z"]["l12"], Mi["A_z"]["l12"])
        log(f"  season {s}: n={len(ids):4d}  loglik {d:+.4f} (se {se:.4f})  1X2 logloss {d2:+.4f} (se {se2:.4f})")
    # calibration of the two final models
    log("\nCalibration of the two final models (test matches):")
    n = len(test)
    for nm, k in (("MODEL 1", "A_z"), ("MODEL 2", "B_z")):
        P = R["actual"][k]
        mh = sum(P[i][0] for i in test) / n; ma = sum(P[i][1] for i in test) / n
        ah = sum(recs[i]["hg"] for i in test) / n; aa = sum(recs[i]["ag"] for i in test) / n
        dr = sum(outcome_probs(*P[i])[1] for i in test) / n
        adr = sum(1 for i in test if recs[i]["hg"] == recs[i]["ag"]) / n
        log(f"  {nm}: expected goals home {mh:.3f} / away {ma:.3f}  (actual {ah:.3f} / {aa:.3f});  draw rate predicted {dr:.3f} vs actual {adr:.3f}")
        bins = [(0, .45), (.45, .52), (.52, .58), (.58, .65), (.65, 1.01)]
        row = []
        for lo, hi in bins:
            s_ = [(p_over(P[i][0] + P[i][1], 2.5), 1.0 if recs[i]["hg"] + recs[i]["ag"] > 2.5 else 0.0) for i in test]
            s_ = [x for x in s_ if lo <= x[0] < hi]
            if s_:
                row.append(f"{lo:.2f}-{hi:.2f}: n={len(s_)} pred {sum(x[0] for x in s_) / len(s_):.2f} act {sum(x[1] for x in s_) / len(s_):.2f}")
        log("     O2.5 calibration: " + " | ".join(row))
    print_team_totals(R, test, log)
    # previous-XI lineups
    M2 = print_table(R, "last", LAST_TABLE, test, "same, lineup = each club's previous starting XI (deployable days ahead)", log)
    log("")
    print_pairs(M2, [("B_z", "A_z", "MODEL 2 minus MODEL 1  (previous-XI lineups)")], log)
    # league strengths
    gP, nL = R["gP"], R["nL"]
    names = R["names"]
    st_, ch, ca = league_strengths(R["recsB"], gP, lidx, nL, R["lamx"], names)
    cnt = defaultdict(int)
    for r in recs:
        cnt[names[lidx(r["home_id"])]] += 1; cnt[names[lidx(r["away_id"])]] += 1
    log(f"\nLeague strengths fitted on ALL Champions League matches (player index, ridge {R['lamx']}); factor = exp(s), > 1 = its clubs beat their domestic-based rating in Europe:")
    log("   " + "   ".join(f"{k} {math.exp(v):.2f} (n={cnt[k]})" for k, v in sorted(st_.items(), key=lambda x: -x[1])))
    log(f"   intercepts: home x{math.exp(ch):.3f}, away x{math.exp(ca):.3f}")
    return M


# ------------------------------------------------------------------------------------------------ market comparison (ESPN / DraftKings lines stored with the match)
def _american(x):
    """American odds (number or string like '+600', '-235', 'EVEN') -> implied probability (with the vig), or None."""
    try:
        if isinstance(x, str):
            x = x.strip().upper().replace("EVEN", "+100")
            x = float(x.replace("+", ""))
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x == 0 or abs(x) < 100:
        return None
    return -x / (-x + 100.0) if x < 0 else 100.0 / (x + 100.0)


def parse_market(raw):
    """Betting lines stored with a Champions League match -> {"ml": (ph, pd, pa) implied incl. vig, "total": (line, p_over, p_under) incl. vig, "src": label} or None.
    Understands ESPN's flat pickcenter entries (homeTeamOdds/awayTeamOdds/drawOdds moneyLine, overUnder/overOdds/underOdds) and the nested
    moneyline/total {open, close} form (the close line is used)."""
    out = {}
    for e in (raw.get("pickcenter") or []):
        if not isinstance(e, dict):
            continue
        prov = (e.get("provider") or {}).get("name", "?")
        ml = e.get("moneyline")
        if "ml" not in out and isinstance(ml, dict):
            try:
                t = tuple(_american(((ml[k] or {}).get("close") or (ml[k] or {}).get("current") or {}).get("odds")) for k in ("home", "draw", "away"))
            except (KeyError, TypeError, AttributeError):
                t = (None,)
            if None not in t:
                out["ml"], out["src"] = t, prov + " (close)"
        if "ml" not in out:
            t = (_american((e.get("homeTeamOdds") or {}).get("moneyLine")), _american((e.get("drawOdds") or {}).get("moneyLine")), _american((e.get("awayTeamOdds") or {}).get("moneyLine")))
            if None not in t:
                out["ml"], out["src"] = t, prov + " (listed)"
        if "total" not in out:
            try:
                line = float(e.get("overUnder"))
            except (TypeError, ValueError):
                line = None
            po, pu = _american(e.get("overOdds")), _american(e.get("underOdds"))
            if line is not None and po is not None and pu is not None:
                out["total"] = (line, po, pu)
        if "ml" in out and "total" in out:
            break
    return out or None


def market_compare(R, root, tune_season, log=print):
    recs = R["recs"]
    test = [i for i, r in enumerate(recs) if r["season"] > tune_season]
    mk = {}
    for i in test:
        p = os.path.join(root, CL_SLUG, "matches", recs[i]["id"] + ".json")
        if os.path.exists(p):
            q = parse_market(json.load(open(p)))
            if q:
                mk[i] = q
    ml = [i for i in test if i in mk and "ml" in mk[i]]
    log("\n=== MARKET COMPARISON (ESPN betting lines stored with the match; mostly DraftKings) ===")
    log(f"test matches with a usable 1X2 line: {len(ml)} of {len(test)}; with a total line: {sum(1 for i in test if i in mk and 'total' in mk[i])}"
        f";  by season {dict(sorted(((s, sum(1 for i in ml if recs[i]['season'] == s)) for s in {recs[j]['season'] for j in test})))}")
    if ml:
        log(f"line source: {', '.join(sorted({mk[i]['src'] for i in ml}))}.  Lines are whatever ESPN showed when the page was stored (probably the final pre-match line, NOT verified).")
    if len(ml) < 20:
        log("too few matches with lines for a comparison"); return
    def devig(t):
        s = sum(t)
        return tuple(x / s for x in t), s - 1.0
    ovr = [devig(mk[i]["ml"])[1] for i in ml]
    log(f"average vig (overround) in the 1X2 lines: {sum(ovr) / len(ovr) * 100:.1f}%")
    res = {"market": [], "m1": [], "m2": [], "mix": [], "base": []}
    b12 = {k: [] for k in res}
    for i in ml:
        r = recs[i]
        o = 0 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 2
        pm = devig(mk[i]["ml"])[0]
        p1 = outcome_probs(*R["actual"]["A_z"][i]); p2 = outcome_probs(*R["actual"]["B_z"][i])
        pb = outcome_probs(*R["actual"]["base"][i])
        mix = tuple(0.5 * a + 0.5 * b for a, b in zip(pm, p2))
        for k, pp in (("market", pm), ("m1", p1), ("m2", p2), ("mix", mix), ("base", pb)):
            res[k].append(-math.log(max(pp[o], 1e-9)))
            b12[k].append(sum((pp[j] - (1.0 if j == o else 0.0)) ** 2 for j in range(3)))
    names = {"market": "market (de-vigged)", "m1": "MODEL 1 (CL only)", "m2": "MODEL 2 (all leagues)", "mix": "50/50 mix of market and MODEL 2", "base": "league average"}
    log(f"\n1X2 on the {len(ml)} matches with lines (lower = better):")
    for k in ("base", "m1", "m2", "market", "mix"):
        log(f"   {names[k]:34s} logloss {sum(res[k]) / len(ml):.4f}   Brier {sum(b12[k]) / len(ml):.4f}")
    for a, b, lab in (("m2", "market", "MODEL 2 minus market"), ("m1", "market", "MODEL 1 minus market"), ("m2", "m1", "MODEL 2 minus MODEL 1"), ("mix", "market", "50/50 mix minus market")):
        d, se = _paired(res[a], res[b]); d2, se2 = _paired(b12[a], b12[b])
        log(f"   {lab:26s} logloss {d:+.4f} (se {se:.4f})   Brier {d2:+.4f} (se {se2:.4f})      (negative = first is better)")
    # average probabilities by outcome: is there a systematic skew between the model and the market (e.g. too much home advantage)?
    n_ = len(ml)
    mh = [devig(mk[i]["ml"])[0] for i in ml]
    m2 = [outcome_probs(*R["actual"]["B_z"][i]) for i in ml]
    fr = [sum(1 for i in ml if (0 if recs[i]["hg"] > recs[i]["ag"] else 1 if recs[i]["hg"] == recs[i]["ag"] else 2) == o) / n_ for o in range(3)]
    log("\naverage probability by outcome on those matches:   home    draw    away")
    log(f"   market                                        {sum(p[0] for p in mh) / n_:6.1%}  {sum(p[1] for p in mh) / n_:6.1%}  {sum(p[2] for p in mh) / n_:6.1%}")
    log(f"   MODEL 2                                       {sum(p[0] for p in m2) / n_:6.1%}  {sum(p[1] for p in m2) / n_:6.1%}  {sum(p[2] for p in m2) / n_:6.1%}")
    log(f"   actual                                        {fr[0]:6.1%}  {fr[1]:6.1%}  {fr[2]:6.1%}")
    hd = [m2[k][0] - mh[k][0] for k in range(n_)]
    fav_h = [k for k in range(n_) if mh[k][0] >= mh[k][2]]
    fav_a = [k for k in range(n_) if mh[k][0] < mh[k][2]]
    for lab, ks in (("market favours the HOME club", fav_h), ("market favours the AWAY club", fav_a)):
        if len(ks) >= 5:
            d_, se_ = mean_se([hd[k] for k in ks])
            log(f"   model minus market home-win probability when {lab:30s} n={len(ks):3d}  {d_ * 100:+.1f} pts (se {se_ * 100:.1f})")
    log("\nwho is right when they differ?  (home / away win rates and 1X2 logloss, by which club the market favours)")
    for lab, ks in (("market favours the HOME club", fav_h), ("market favours the AWAY club", fav_a)):
        if len(ks) < 5:
            continue
        rs = [recs[ml[k]] for k in ks]
        ah = sum(1 for r in rs if r["hg"] > r["ag"]) / len(ks); aa = sum(1 for r in rs if r["hg"] < r["ag"]) / len(ks)
        lm = [-math.log(max(mh[k][0 if recs[ml[k]]["hg"] > recs[ml[k]]["ag"] else 1 if recs[ml[k]]["hg"] == recs[ml[k]]["ag"] else 2], 1e-9)) for k in ks]
        l2 = [-math.log(max(m2[k][0 if recs[ml[k]]["hg"] > recs[ml[k]]["ag"] else 1 if recs[ml[k]]["hg"] == recs[ml[k]]["ag"] else 2], 1e-9)) for k in ks]
        d_, se_ = _paired(l2, lm)
        log(f"   {lab:30s} n={len(ks):3d}  home win: market {sum(mh[k][0] for k in ks) / len(ks):.1%} model {sum(m2[k][0] for k in ks) / len(ks):.1%} actual {ah:.1%}"
            f"  |  away win: market {sum(mh[k][2] for k in ks) / len(ks):.1%} model {sum(m2[k][2] for k in ks) / len(ks):.1%} actual {aa:.1%}  |  model minus market logloss {d_:+.4f} (se {se_:.4f})")
    # totals
    tt = [i for i in test if i in mk and "total" in mk[i] and abs(mk[i]["total"][0] * 2 - round(mk[i]["total"][0] * 2)) < 1e-9 and mk[i]["total"][0] % 1 == 0.5]
    if len(tt) >= 20:
        lines = defaultdict(int)
        for i in tt:
            lines[mk[i]["total"][0]] += 1
        rr = {"market": [], "m1": [], "m2": []}
        for i in tt:
            line, po, pu = mk[i]["total"]
            y = 1.0 if recs[i]["hg"] + recs[i]["ag"] > line else 0.0
            pm = po / (po + pu)
            for k, pp in (("market", pm), ("m1", p_over(sum(R["actual"]["A_z"][i]), line)), ("m2", p_over(sum(R["actual"]["B_z"][i]), line))):
                rr[k].append((pp - y) ** 2)
        log(f"\nOver/under on the {len(tt)} matches with a half-goal total line (lines {dict(sorted(lines.items()))}), Brier (lower = better):")
        for k in ("m1", "m2", "market"):
            log(f"   {names[k]:34s} {sum(rr[k]) / len(tt):.4f}")
        for a, b, lab in (("m2", "market", "MODEL 2 minus market"), ("m1", "market", "MODEL 1 minus market")):
            d, se = _paired(rr[a], rr[b])
            log(f"   {lab:26s} Brier {d:+.4f} (se {se:.4f})")
    # where the models disagree most with the market
    dis = sorted(ml, key=lambda i: -abs(outcome_probs(*R["actual"]["B_z"][i])[0] - devig(mk[i]["ml"])[0][0]))[:5]
    log("\nlargest gaps between MODEL 2 and the market (home win probability): " + "; ".join(
        f"{recs[i]['date']} {recs[i]['home']} v {recs[i]['away']}: model {outcome_probs(*R['actual']['B_z'][i])[0]:.2f} market {devig(mk[i]['ml'])[0][0]:.2f} ({recs[i]['hg']}-{recs[i]['ag']})" for i in dis))


# ------------------------------------------------------------------------------------------------ calibration study: fix the too-low overs?
def nb_pmf(mu, alpha, n=14):
    """Goals pmf with mean mu: Poisson (alpha = 0) or negative binomial with variance mu + alpha*mu^2."""
    mu = max(mu, 1e-6)
    if alpha <= 1e-9:
        return pois_pmf(mu, n)
    r = 1.0 / alpha
    p = [(r / (r + mu)) ** r]
    for k in range(n):
        p.append(p[-1] * (k + r) / (k + 1) * mu / (r + mu))
    return p


def _nb_stats(xh, xa, alpha):
    ph, pa = nb_pmf(xh, alpha), nb_pmf(xa, alpha)
    h = d = a = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            if i > j: h += x * y
            elif i == j: d += x * y
            else: a += x * y
    s = h + d + a
    cdf = lambda p, k: min(1.0, sum(p[:k + 1]))                                # noqa: E731
    tot = [sum(ph[i] * pa[k - i] for i in range(0, min(k, len(ph) - 1) + 1) if k - i < len(pa)) for k in range(0, 6)]
    cum = [sum(tot[:k + 1]) for k in range(6)]
    return {"x12": (h / s, d / s, a / s), "h": [1 - cdf(ph, k) for k in (0, 1, 2)], "a": [1 - cdf(pa, k) for k in (0, 1, 2)],
            "o25": 1 - cum[2], "o35": 1 - cum[3], "btts": (1 - ph[0]) * (1 - pa[0]), "ph": ph, "pa": pa}


def _scaled(recs, pairs, m_pseudo, lo=0.7, hi=1.4):
    """Walk-forward bias correction: each match's expected goals are multiplied by (goals scored + m) / (goals expected + m) over the Champions League
    matches on STRICTLY EARLIER dates (home and away separately).  m = None -> no correction."""
    if m_pseudo is None:
        return list(pairs)
    out = [None] * len(recs)
    sh = sa = xh_ = xa_ = 0.0
    k = 0
    n = len(recs)
    while k < n:
        j = k
        while j < n and recs[j]["date"] == recs[k]["date"]:
            j += 1
        rh = min(hi, max(lo, (sh + m_pseudo) / (xh_ + m_pseudo)))
        ra = min(hi, max(lo, (sa + m_pseudo) / (xa_ + m_pseudo)))
        for i in range(k, j):
            if pairs[i] is not None:
                out[i] = (pairs[i][0] * rh, pairs[i][1] * ra)
        for i in range(k, j):                                        # results become known only after the day
            if pairs[i] is not None:
                sh += recs[i]["hg"]; sa += recs[i]["ag"]; xh_ += pairs[i][0]; xa_ += pairs[i][1]
        k = j
    return out


def _nb_ll(recs, pairs, alpha, idx):
    tot = []
    for i in idx:
        xh, xa = pairs[i]
        tot.append(math.log(max(nb_pmf(xh, alpha, 20)[min(recs[i]["hg"], 20)], 1e-12)) + math.log(max(nb_pmf(xa, alpha, 20)[min(recs[i]["ag"], 20)], 1e-12)))
    return sum(tot) / len(tot)


def _variant_metrics(recs, pairs, alpha, idx):
    M = {k: [] for k in ("ll", "l12", "b25", "b35", "t05", "t15", "t25", "btts")}
    pr = {k: [] for k in ("h15", "a15", "t15", "t25", "btts", "xh", "xa")}
    for i in idx:
        r = recs[i]
        S = _nb_stats(pairs[i][0], pairs[i][1], alpha)
        o = 0 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 2
        M["ll"].append(math.log(max(nb_pmf(pairs[i][0], alpha, 20)[min(r["hg"], 20)], 1e-12)) + math.log(max(nb_pmf(pairs[i][1], alpha, 20)[min(r["ag"], 20)], 1e-12)))
        M["l12"].append(-math.log(max(S["x12"][o], 1e-9)))
        M["b25"].append((S["o25"] - (1.0 if r["hg"] + r["ag"] > 2.5 else 0.0)) ** 2)
        M["b35"].append((S["o35"] - (1.0 if r["hg"] + r["ag"] > 3.5 else 0.0)) ** 2)
        for key, k in (("t05", 0), ("t15", 1), ("t25", 2)):
            M[key].append(((S["h"][k] - (1.0 if r["hg"] > k + 0.5 else 0.0)) ** 2 + (S["a"][k] - (1.0 if r["ag"] > k + 0.5 else 0.0)) ** 2) / 2)
        M["btts"].append((S["btts"] - (1.0 if r["hg"] > 0 and r["ag"] > 0 else 0.0)) ** 2)
        pr["h15"].append((S["h"][1], 1.0 if r["hg"] > 1.5 else 0.0)); pr["a15"].append((S["a"][1], 1.0 if r["ag"] > 1.5 else 0.0))
        pr["t15"].append(((S["h"][1] + S["a"][1]) / 2, ((1.0 if r["hg"] > 1.5 else 0.0) + (1.0 if r["ag"] > 1.5 else 0.0)) / 2))
        pr["t25"].append(((S["h"][2] + S["a"][2]) / 2, ((1.0 if r["hg"] > 2.5 else 0.0) + (1.0 if r["ag"] > 2.5 else 0.0)) / 2))
        pr["btts"].append((S["btts"], 1.0 if r["hg"] > 0 and r["ag"] > 0 else 0.0))
        pr["xh"].append((pairs[i][0], r["hg"])); pr["xa"].append((pairs[i][1], r["ag"]))
    return M, pr


def calibration_study(R, tune_season, root, log=print, tune_ms=(25, 50, 100, 200, 400, None), tune_alphas=(0.0, 0.03, 0.06, 0.10, 0.15, 0.20, 0.30)):
    recs = R["recs"]
    base = R["actual"]["B_z"]
    tune = [i for i, r in enumerate(recs) if r["season"] == tune_season and base[i] is not None]
    test = [i for i, r in enumerate(recs) if r["season"] > tune_season and base[i] is not None]
    # 1. choose the correction on the tune season only
    grid = []
    for m in tune_ms:
        sc = _scaled(recs, base, m)
        for al in tune_alphas:
            grid.append((_nb_ll(recs, sc, al, tune), m, al))
    def best(rows):
        return max(rows, key=lambda x: x[0])
    ll0 = [g for g in grid if g[1] is None and g[2] == 0.0][0][0]
    b_s = best([g for g in grid if g[2] == 0.0])
    b_n = best([g for g in grid if g[1] is None])
    b_sn = best(grid)
    log(f"\n=== CALIBRATION STUDY: can a small walk-forward correction fix the too-low goal overs? (chosen on season {tune_season}: {len(tune)} matches; tested on later seasons: {len(test)} matches) ===")
    log(f"goals loglik per match on the tune season:  uncorrected {ll0:.4f}")
    log(f"   scale only (each side's expected goals x (goals + m)/(expected + m) over earlier CL matches):  best m = {b_s[1]}  -> {b_s[0]:.4f}")
    log(f"   negative binomial only (variance = mean + alpha * mean^2):                                    best alpha = {b_n[2]}  -> {b_n[0]:.4f}")
    log(f"   both:                                                                                        m = {b_sn[1]}, alpha = {b_sn[2]}  -> {b_sn[0]:.4f}")
    log("   top 5 of the whole grid: " + "; ".join(f"m={m}, alpha={al}: {ll:.4f}" for ll, m, al in sorted(grid, key=lambda x: -x[0])[:5]))
    edge = [nm for nm, v, g in (("m", b_sn[1], tune_ms), ("alpha", b_sn[2], tune_alphas)) if v in (g[0], g[-1]) and not (nm == "m" and v is None)]
    if edge:
        log(f"   WARNING: the chosen {' and '.join(edge)} sits at the edge of its grid")
    variants = [("uncorrected (Model 2 as is)", None, 0.0), ("scale only", b_s[1], 0.0), ("negative binomial only", None, b_n[2]), ("scale + negative binomial", b_sn[1], b_sn[2])]
    res = {}
    for nm, m, al in variants:
        res[nm] = _variant_metrics(recs, _scaled(recs, base, m), al, test)
    log(f"\nTEST seasons (actual XI), {len(test)} matches; Brier / logloss, lower is better except goals loglik:")
    log(f"{'':32s}{'goals ll':>10s}{'1X2 ll':>9s}{'O2.5':>8s}{'O3.5':>8s}{'team 0.5':>10s}{'team 1.5':>10s}{'team 2.5':>10s}{'BTTS':>8s}")
    for nm, m, al in variants:
        M = res[nm][0]
        log(f"{nm:32s}{sum(M['ll']) / len(test):10.4f}{sum(M['l12']) / len(test):9.4f}{sum(M['b25']) / len(test):8.4f}{sum(M['b35']) / len(test):8.4f}"
            f"{sum(M['t05']) / len(test):10.4f}{sum(M['t15']) / len(test):10.4f}{sum(M['t25']) / len(test):10.4f}{sum(M['btts']) / len(test):8.4f}")
    log("\nPredicted vs actual on the test matches (mean prediction / actual):")
    log(f"{'':32s}{'home goals':>16s}{'away goals':>16s}{'home >1.5':>14s}{'away >1.5':>14s}{'team >1.5':>14s}{'team >2.5':>14s}{'BTTS':>14s}")
    for nm, m, al in variants:
        pr = res[nm][1]
        f2 = lambda k: f"{sum(x[0] for x in pr[k]) / len(test):.3f}/{sum(x[1] for x in pr[k]) / len(test):.3f}"            # noqa: E731
        f1 = lambda k: f"{sum(x[0] for x in pr[k]) / len(test):.1%}/{sum(x[1] for x in pr[k]) / len(test):.1%}"           # noqa: E731
        log(f"{nm:32s}{f2('xh'):>16s}{f2('xa'):>16s}{f1('h15'):>14s}{f1('a15'):>14s}{f1('t15'):>14s}{f1('t25'):>14s}{f1('btts'):>14s}")
    log("\nPaired differences vs the uncorrected model (goals loglik: + = better; the others: - = better):")
    base_ = res["uncorrected (Model 2 as is)"][0]
    for nm, m, al in variants[1:]:
        M = res[nm][0]
        parts = []
        for key, lab, sign in (("ll", "goals loglik", 1), ("l12", "1X2 logloss", 1), ("b25", "O2.5 Brier", 1), ("t15", "team 1.5 Brier", 1), ("t25", "team 2.5 Brier", 1), ("btts", "BTTS Brier", 1)):
            d, se = _paired(M[key], base_[key]) if key != "ll" else _paired(M[key], base_[key])
            parts.append(f"{lab} {d:+.4f} (se {se:.4f})")
        log(f"  {nm:30s} " + "   ".join(parts))
    # 2. one more check: the same, with each club's previous XI (what the live tool uses), and the 1X2 vs the stored closing line
    lastB = R["last"]["B_z"]
    tl = [i for i in test if lastB[i] is not None]
    b0 = _variant_metrics(recs, _scaled(recs, lastB, None), 0.0, tl)[0]
    b1 = _variant_metrics(recs, _scaled(recs, lastB, b_sn[1]), b_sn[2], tl)[0]
    d, se = _paired(b1["ll"], b0["ll"]); d2, se2 = _paired(b1["l12"], b0["l12"]); d3, se3 = _paired(b1["t15"], b0["t15"])
    log(f"\nprevious-XI lineups, scale + negative binomial minus uncorrected: goals loglik {d:+.4f} (se {se:.4f})   1X2 logloss {d2:+.4f} (se {se2:.4f})   team 1.5 Brier {d3:+.4f} (se {se3:.4f})")
    mk = {}
    for i in test:
        p = os.path.join(root, CL_SLUG, "matches", recs[i]["id"] + ".json")
        if os.path.exists(p):
            q = parse_market(json.load(open(p)))
            if q and q.get("ml"):
                s_ = sum(q["ml"]); mk[i] = [x / s_ for x in q["ml"]]
    ml = [i for i in test if i in mk]
    if len(ml) >= 20:
        def ll12(pairs, al):
            out = []
            for i in ml:
                o = 0 if recs[i]["hg"] > recs[i]["ag"] else 1 if recs[i]["hg"] == recs[i]["ag"] else 2
                out.append(-math.log(max(_nb_stats(pairs[i][0], pairs[i][1], al)["x12"][o], 1e-9)))
            return out
        a0 = ll12(base, 0.0); a1 = ll12(_scaled(recs, base, b_sn[1]), b_sn[2])
        am = [-math.log(max(mk[i][0 if recs[i]["hg"] > recs[i]["ag"] else 1 if recs[i]["hg"] == recs[i]["ag"] else 2], 1e-9)) for i in ml]
        d0, s0 = _paired(a0, am); d1, s1 = _paired(a1, am)
        log(f"1X2 vs the closing line ({len(ml)} matches): market {sum(am) / len(ml):.4f}; uncorrected {sum(a0) / len(ml):.4f} (minus market {d0:+.4f}, se {s0:.4f}); corrected {sum(a1) / len(ml):.4f} (minus market {d1:+.4f}, se {s1:.4f})")
    log("\nRule: adopt the correction only if it improves the goals loglik AND the team 1.5 / 2.5 and BTTS Brier by more than about two se on the TEST seasons without hurting the 1X2; chosen on the tune season, so the test seasons did not choose it.")
    return {"m": b_sn[1], "alpha": b_sn[2], "scale_only_m": b_s[1], "nb_only_alpha": b_n[2]}


def cmd_calib(a, log=print):
    ms = load_matches(a.dir, ([CL_SLUG] + list(a.leagues)) if a.leagues is not None else None)
    if sum(1 for m in ms if m["comp"] == CL_SLUG and m["goals_ok"]) < 100:
        log("too few Champions League matches: run `collect` first"); return 1
    R = run_models(ms, a.tune_season, lambda *x: None)
    out = calibration_study(R, a.tune_season, a.dir, log)
    json.dump(out, open(os.path.join(a.dir, "cl_calibration.json"), "w"))
    return 0


def cmd_backtest(a, log=print):
    ms = load_matches(a.dir, ([CL_SLUG] + list(a.leagues)) if a.leagues is not None else None)
    n_cl = sum(1 for m in ms if m["comp"] == CL_SLUG and m["goals_ok"])
    comps = sorted({m["comp"] for m in ms})
    log(f"{sum(m['goals_ok'] for m in ms)} matches with reconciled goals in {len(comps)} competitions ({n_cl} Champions League); seasons {sorted({m['season'] for m in ms})}")
    if n_cl < 100:
        log("too few Champions League matches: run `collect` (and `check`) first"); return 1
    R = run_models(ms, a.tune_season, log)
    report(R, a.tune_season, log)
    market_compare(R, a.dir, a.tune_season, log)
    cfg = {"tune_season": a.tune_season, "lg": R["lg"], "wA": R["wA"], "wB": R["wB"], "lamx": R["lamx"], "lamt": R["lamt"],
           "A": {k: R["famA"][k] for k in ("P", "hl_p", "TP", "hl_t")}, "B": {k: R["famB"][k] for k in ("P", "hl_p", "TP", "hl_t")}}
    json.dump(cfg, open(os.path.join(a.dir, "cl_config.json"), "w"), indent=1)
    log(f"\nchosen settings saved to {os.path.join(a.dir, 'cl_config.json')} (used by `project`)")
    return 0


# ------------------------------------------------------------------------------------------------ the next slate under both models
# ------------------------------------------------------------------------------------------------ live market prices: Kalshi first, ESPN/DraftKings as the fallback
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_GUESS = ["KXUCLGAME", "KXUCLTOTAL"]


def _kd(m, key):
    """A Kalshi price field in dollars (understands '*_dollars' strings and legacy cents)."""
    v = m.get(key + "_dollars")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = m.get(key)
    try:
        return float(v) / 100.0 if v is not None else None
    except (TypeError, ValueError):
        return None


MAX_SPREAD = 0.08


def kalshi_prices(m):
    """-> (bid, ask, mid) in dollars.  mid is only given for a real two-sided quote (ask - bid <= MAX_SPREAD): empty books show up as bid 0.04 / ask 0.81
    on every outcome and carry no information, and a stale last trade is not used instead."""
    bid, ask = _kd(m, "yes_bid"), _kd(m, "yes_ask")
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and 0 < ask < 1 else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None and ask - bid <= MAX_SPREAD else None
    return bid, ask, mid


KALSHI_WANTED = ("KXUCLGAME", "KXUCLTOTAL")


def kalshi_discover(get):
    """The Champions League men's game and total-goals series.  Exact tickers only (a title search also catches the AFC, FIBA and women's competitions)."""
    found = []
    try:
        d = get(KALSHI + "/series?category=Sports")
        have = {s.get("ticker"): s.get("title") or "" for s in (d.get("series") or [])}
        found = [(t, have[t]) for t in KALSHI_WANTED if t in have]
    except Exception:                                                  # noqa: BLE001
        pass
    return found or [(t, "(guessed)") for t in KALSHI_WANTED]


def kalshi_open_markets(get, series):
    out, cur = [], None
    for _ in range(10):
        url = f"{KALSHI}/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cur}" if cur else "")
        d = get(url)
        out += d.get("markets") or []
        cur = d.get("cursor")
        if not cur:
            break
    return out


def _nm_score(a, b):
    ta, tb = norm(a).split(), norm(b).split()
    if not ta or not tb:
        return 0.0
    if set(ta) <= set(tb) or set(tb) <= set(ta):
        return 1.0
    if any(len(x) >= 4 and y.startswith(x) or len(y) >= 4 and x.startswith(y) for x in ta for y in tb):
        return 0.9                                                   # 'Inter' / 'Internazionale', 'Atletico' / 'Atletico Madrid'
    return difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def _code_date(code):
    mt = re.search(r"(\d{2})([A-Z]{3})(\d{2})", code or "")
    if not mt or mt.group(2) not in _MON:
        return None
    try:
        return dt.date(2000 + int(mt.group(1)), _MON[mt.group(2)], int(mt.group(3)))
    except ValueError:
        return None


def kalshi_slate(get):
    """-> (events, notes).  events: {code: {"game": {sub_title: market}, "total": {line: market}}} grouped by the event ticker's code."""
    ev, notes = {}, []
    for ser, title in kalshi_discover(get):
        try:
            mk = kalshi_open_markets(get, ser)
        except Exception as ex:                                      # noqa: BLE001
            notes.append(f"{ser}: {ex}")
            continue
        notes.append(f"{ser} ({title}): {len(mk)} open markets")
        for m in mk:
            et = m.get("event_ticker") or ""
            code = et.split("-", 1)[1] if "-" in et else et
            e = ev.setdefault(code, {"game": {}, "total": {}})
            if ser.upper().endswith("GAME"):
                e["game"][m.get("yes_sub_title") or m.get("subtitle") or m.get("ticker")] = m
            elif ser.upper().endswith("TOTAL"):
                line = m.get("floor_strike")
                if line is None:
                    mt = re.search(r"(\d+(?:\.\d+)?)", (m.get("yes_sub_title") or "") + " " + (m.get("title") or ""))
                    line = float(mt.group(1)) if mt else None
                if line is not None:
                    e["total"][float(line)] = m
    return ev, notes


def kalshi_for(ev, home, away, date=None):
    """The Kalshi quotes for one fixture, or None.  An event matches when its date code is within a day of the fixture (if both are known) and its two
    team outcomes match the two clubs (one strong name match - the other club then follows by elimination when the date agrees, else it must also look similar)."""
    best = None
    fd = dt.date.fromisoformat(date) if date else None
    for code, e in ev.items():
        subs = list(e["game"])
        if len(subs) != 3:
            continue
        cd = _code_date(code)
        if fd and cd and abs((cd - fd).days) > 1:
            continue
        draw = next((s for s in subs if "tie" in norm(s).split() or "draw" in norm(s).split()), None)
        teams = [s for s in subs if s != draw]
        if draw is None or len(teams) != 2:
            continue
        opts = []
        for s1, s2 in ((teams[0], teams[1]), (teams[1], teams[0])):
            x, y = _nm_score(s1, home), _nm_score(s2, away)
            opts.append((min(x, y) + 2 * max(x, y) if max(x, y) >= 0.85 and min(x, y) >= (0.0 if (fd and cd) else 0.45) else -1, s1, s2))
        sc, sh, sa = max(opts)
        if sc < 0:
            continue
        q = {"code": code}
        for k, s in (("h", sh), ("d", draw), ("a", sa)):
            q[k] = kalshi_prices(e["game"][s])
        q["tot"] = {line: kalshi_prices(m) for line, m in e["total"].items()}
        if best is None or sc > best[0]:
            best = (sc, q)
    return best[1] if best else None


def kfee(p):
    return 0.07 * p * (1.0 - p)


def market_for(q, dk):
    """Pick the price source: Kalshi when it has a full 1X2 quote, otherwise the ESPN/DraftKings line.  -> dict or None.
    1X2 probabilities are de-vigged (normalised); asks are kept for the fee-inclusive edge."""
    if q and all(q[k][2] is not None for k in "hda"):
        mids = [q[k][2] for k in "hda"]
        s = sum(mids)
        out = {"src": "KALSHI", "p": tuple(x / s for x in mids), "ask": tuple(q[k][1] for k in "hda"), "mid": tuple(mids), "over": {}}
        for line, (b, a_, m) in q["tot"].items():
            if m is not None:
                out["over"][line] = (m, a_)
        return out
    if dk and dk.get("ml"):
        s = sum(dk["ml"])
        out = {"src": "DRAFTKINGS", "p": tuple(x / s for x in dk["ml"]), "ask": (None, None, None), "over": {}}
        if dk.get("total"):
            line, po, pu = dk["total"]
            out["over"][line] = (po / (po + pu), None)
        return out
    return None


def cmd_project(a, get=None, today=None, log=print, kget=None, sink=None):
    get = get or get_json
    kget = kget or get_json
    today = today or dt.date.today()
    cp = os.path.join(a.dir, "cl_config.json")
    if not os.path.exists(cp):
        log("run `backtest` first: it tunes the two models and saves their settings"); return 1
    cfg = json.load(open(cp))
    ms = load_matches(a.dir, ([CL_SLUG] + list(a.leagues)) if a.leagues is not None else None)
    ms = sorted([m for m in ms if m["goals_ok"]], key=lambda m: (m["date"], m["id"]))
    evs = []
    for k in range(a.days):
        d = today + dt.timedelta(days=k)
        try:
            evs += [e for e in scoreboard_range(get, CL_SLUG, d, d) if not e["completed"]]
        except Exception as ex:                                      # noqa: BLE001
            log(f"scoreboard {d}: {ex}")
    seen, up = set(), []
    for e in evs:
        if e["id"] not in seen:
            seen.add(e["id"]); up.append(e)
    if not up:
        log(f"no upcoming Champions League matches on the scoreboard in the next {a.days} days"); return 0
    fut = []
    for e in up:
        lu, dk = None, None
        try:
            sm = get(summary_url(CL_SLUG) + e["id"])
            lu = lineups_from_summary(sm)
            dk = parse_market({"pickcenter": sm.get("pickcenter") or []})
        except Exception:                                            # noqa: BLE001
            pass
        fut.append({"id": "f" + e["id"], "date": (e["date"] or "")[:10], "season": season_of(e["date"] or today.isoformat()), "home": e["home"]["name"], "away": e["away"]["name"],
                    "home_id": str(e["home"]["id"]), "away_id": str(e["away"]["id"]), "comp": CL_SLUG, "hg": 0, "ag": 0, "players": [], "goals": [], "end": 90.0,
                    "_future": True, "_lineup": lu, "_dk": dk, "_kick": e["date"], "_eid": e["id"], "goals_ok": True})
    for f in fut:
        f["_lineup"] = {k: [(CL2DOM.get(str(i), str(i)), g) for i, g in v] for k, v in (f["_lineup"] or {}).items()} or None
    fut.sort(key=lambda m: (m["date"], m["id"]))
    futmap = {f["id"]: f for f in fut}
    dom, names, lidx = club_leagues(ms)
    nL = len(names)
    A_ms = [m for m in ms if m["comp"] == CL_SLUG] + fut
    A_ms.sort(key=lambda m: (m["date"], m["id"]))
    pa, ca = cfg["A"], cfg["B"]
    rA = Engine(priors=[pa["P"]], team_priors=[pa["TP"]], half_life=pa["hl_p"], adjust=True, lg_half_life=cfg["lg"]).run(A_ms, lambda m: m["comp"] == CL_SLUG)
    rAt = rA if pa["hl_t"] == pa["hl_p"] else Engine(priors=[pa["P"]], team_priors=[pa["TP"]], half_life=pa["hl_t"], adjust=True, lg_half_life=cfg["lg"]).run(A_ms, lambda m: m["comp"] == CL_SLUG)
    B_ms = sorted(ms + fut, key=lambda m: (m["date"], m["id"]))
    rB = Engine(priors=[ca["P"]], team_priors=[ca["TP"]], half_life=ca["hl_p"], adjust=True, lg_half_life=cfg["lg"]).run(B_ms, lambda m: m["comp"] == CL_SLUG)
    rBt = rB if ca["hl_t"] == ca["hl_p"] else Engine(priors=[ca["P"]], team_priors=[ca["TP"]], half_life=ca["hl_t"], adjust=True, lg_half_life=cfg["lg"]).run(B_ms, lambda m: m["comp"] == CL_SLUG)
    gP = lambda r: r["x"][ca["P"]]                                            # noqa: E731
    gT = lambda r: r["t"][ca["TP"]]                                           # noqa: E731
    xb = stage2(rB, gP, lidx, nL, cfg["lamx"], True)
    tb = stage2(rBt, gT, lidx, nL, cfg["lamt"], True)
    zb = blend(xb, tb, cfg["wB"])
    za = blend([r["x"][pa["P"]] for r in rA], [r["t"][pa["TP"]] for r in rAt], cfg["wA"])
    dkmap = {f["id"]: f.get("_dk") for f in fut}
    ev, knotes = ({}, [])
    if not getattr(a, "no_market", False):
        try:
            ev, knotes = kalshi_slate(kget)
        except Exception as ex:                                      # noqa: BLE001
            knotes = [f"Kalshi not reachable: {ex}"]
    both = getattr(a, "both", False)
    log(f"\nUpcoming Champions League matches ({len(fut)}); MODEL 2 (all leagues + league strengths) vs the market; lineup = confirmed XI when ESPN has published it, else each club's previous XI")
    log("market = Kalshi mid prices (de-vigged) when Kalshi lists the match, otherwise the ESPN/DraftKings line when ESPN has one; 'net' = model probability - (Kalshi ask + fee), cents")
    for n_ in knotes:
        log("   kalshi: " + n_)
    log(f"\n{'match':46s}{'':>10s}{'xG h-a':>12s}{'home':>7s}{'draw':>7s}{'away':>7s}{'O2.5':>7s}{'O1.5':>7s}  lineup")
    for k, r in enumerate(rA):
        if not r.get("future"):
            continue
        j = next(i for i, x in enumerate(rB) if x["id"] == r["id"])
        rows = [("MODEL 2", zb[j])] + ([("MODEL 1", za[k])] if both else [])
        kq_ = kalshi_for(ev, r["home"], r["away"], r["date"]) if ev else None
        mk = market_for(kq_, dkmap.get(r["id"]))
        kwide = bool(kq_) and not all(kq_[x][2] is not None for x in "hda")
        first = True
        for nm, P in rows:
            h, d, aw = outcome_probs(*P)
            log(f"{((r['home'] + ' v ' + r['away'])[:45] if first else ''):46s}{nm:>10s}{P[0]:6.2f}-{P[1]:<5.2f}{h:7.1%}{d:7.1%}{aw:7.1%}{p_over(P[0] + P[1], 2.5):7.1%}{p_over(P[0] + P[1], 1.5):7.1%}  {r['lineup_source'] if first else ''}")
            first = False
        P2 = zb[j]
        log(f"{'':46s}{'team goals':>10s}  over 0.5/1.5/2.5: home " + " ".join(f"{p_over(P2[0], l):5.1%}" for l in TEAM_LINES) + " | away " + " ".join(f"{p_over(P2[1], l):5.1%}" for l in TEAM_LINES)
            + f" | BTTS {p_btts(*P2):.1%} | over 3.5 {p_over(P2[0] + P2[1], 3.5):.1%}")
        if mk:
            P = zb[j]
            m25 = mk["over"].get(2.5)
            m15 = mk["over"].get(1.5)
            f_ = lambda x: f"{x[0]:7.1%}" if x else f"{'-':>7s}"    # noqa: E731
            log(f"{'':46s}{mk['src']:>10s}{'':12s}{mk['p'][0]:7.1%}{mk['p'][1]:7.1%}{mk['p'][2]:7.1%}{f_(m25)}{f_(m15)}")
            mp = outcome_probs(*P)
            gap = [mp[i] - mk["p"][i] for i in range(3)]
            log(f"{'':46s}{'gap pts':>10s}{'':12s}{gap[0] * 100:+7.1f}{gap[1] * 100:+7.1f}{gap[2] * 100:+7.1f}")
            if mk["src"] == "KALSHI" and all(x is not None for x in mk["ask"]):
                net = [mp[i] - (mk["ask"][i] + kfee(mk["ask"][i])) for i in range(3)]
                log(f"{'':46s}{'net c':>10s}{'':12s}{net[0] * 100:+7.1f}{net[1] * 100:+7.1f}{net[2] * 100:+7.1f}")
            for line, lab in sorted((ln, ln) for ln in mk["over"] if ln not in (1.5, 2.5)):
                mo, ask = mk["over"][line]
                log(f"{'':46s}{'O' + str(line):>10s}{'':12s}market {mo:.1%} vs model {p_over(P[0] + P[1], line):.1%}")
        else:
            log(f"{'':46s}{'(no market price found)' + (' - Kalshi lists it but there is no real two-sided quote yet' if kwide else ''):>10s}")
        if mk and kwide:
            log(f"{'':46s}{'(Kalshi lists it but has no real two-sided quote yet; DraftKings shown)':>10s}")
        if sink is not None:
            sink.append({"id": futmap[r["id"]]["_eid"], "kick": futmap[r["id"]]["_kick"], "date": r["date"], "home": r["home"], "away": r["away"],
                         "m2": list(outcome_probs(*zb[j])), "xg": list(zb[j]), "m1": list(outcome_probs(*za[k])), "lineup": r["lineup_source"],
                         "src": mk["src"] if mk else None, "mkt": list(mk["p"]) if mk else None, "ask": list(mk["ask"]) if mk else None,
                         "over": {str(l): [v[0], v[1]] for l, v in mk["over"].items()} if mk else {}, "dk": dkmap.get(r["id"]), "lg": [r["bh"], r["ba"]] if "bh" in r else None})
        warn = []
        if both and (not rA[k]["known"][0] or not rA[k]["known"][1]):
            warn.append("a club has no Champions League match in the data (Model 1 falls back to averages)")
        if lidx(r["home_id"]) == nL - 1 or lidx(r["away_id"]) == nL - 1:
            warn.append("a club has no domestic league collected (Model 2 treats it as 'other')")
        for w in warn:
            log("      note: " + w)
    log("\nReading this: in the backtest Model 2 was level with the closing line (+0.004 logloss, se 0.020), so a gap is as likely the model's noise as the market's; the sample could not show an edge.")
    return 0


# ------------------------------------------------------------------------------------------------ live tracker: log model + price before kickoff, settle, scorecard
TRACK_LOG, TRACK_RES = "cl_track.jsonl", "cl_track_results.json"


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _parse_dt(s):
    try:
        d = dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def track_snapshot(a, get, kget, now, log):
    """Run the projection (a.days ahead) and append one line per not-yet-started match (Model 2 probabilities + the market quote at this moment)."""
    rows = []
    rc = cmd_project(a, get=get, today=now.date(), log=lambda *x: None, kget=kget, sink=rows)
    if rc:
        log("projection failed (run `backtest` first)"); return 0
    if not rows:
        log(f"no upcoming Champions League matches on the scoreboard in the next {a.days} days (the next matchday may be further out: try --days 30); nothing logged")
        return 0
    n = 0
    path = os.path.join(a.dir, TRACK_LOG)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            kick = _parse_dt(r["kick"])
            if kick is None or kick <= now:
                continue
            r["t"] = now.isoformat()
            f.write(json.dumps(r) + "\n")
            n += 1
    with_mkt = sum(1 for r in rows if r["src"] == "KALSHI")
    log(f"logged {n} upcoming matches ({with_mkt} with a Kalshi quote, {sum(1 for r in rows if r['src'] == 'DRAFTKINGS')} with a DraftKings line) to {path}")
    return n


def _load_track(root):
    p = os.path.join(root, TRACK_LOG)
    out = []
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    return out


def track_settle(a, get, now, log):
    """Fetch the final (regulation) score of every logged match that should be over and is not settled yet."""
    rp = os.path.join(a.dir, TRACK_RES)
    res = json.load(open(rp)) if os.path.exists(rp) else {}
    logged = {}
    for r in _load_track(a.dir):
        logged[r["id"]] = r
    todo = [r for r in logged.values() if r["id"] not in res and (_parse_dt(r["kick"]) or now) + dt.timedelta(hours=3) < now]
    got = 0
    for r in todo:
        try:
            d = dt.date.fromisoformat(r["kick"][:10])
            evs = [e for dd in (d, d + dt.timedelta(days=1)) for e in scoreboard_range(get, CL_SLUG, dd, dd) if e["id"] == r["id"]]
            e = evs[0] if evs else None
            if not e or not e["completed"]:
                continue
            raw = compact_summary(get(summary_url(CL_SLUG) + r["id"]), {"id": e["id"], "date": e["date"], "name": e["name"], "home": e["home"], "away": e["away"], "season": 0, "comp": CL_SLUG})
            raw["comp"] = CL_SLUG
            pm = parse_match(raw)
            if pm["goals_ok"]:
                hg, ag, how = pm["hg"], pm["ag"], "regulation"
            else:
                hg, ag, how = int(e["home"]["score"]), int(e["away"]["score"]), "scoreboard (could not verify regulation)"
            res[r["id"]] = {"hg": hg, "ag": ag, "how": how}
            got += 1
        except Exception as ex:                                      # noqa: BLE001
            log(f"settle {r['home']} v {r['away']}: {ex}")
    json.dump(res, open(rp, "w"))
    log(f"settled {got} matches ({len([r for r in logged.values() if r['id'] in res])} of {len(logged)} logged are settled; {len([r for r in logged.values() if r['id'] not in res])} pending)")
    return res


def last_snapshots(rows, res):
    """Per settled match, the latest snapshot taken strictly before kickoff that carries a market price."""
    last = {}
    for r in rows:
        kick, t = _parse_dt(r["kick"]), _parse_dt(r["t"])
        if kick and t and t < kick and r["id"] in res and r.get("mkt"):
            if r["id"] not in last or t > _parse_dt(last[r["id"]]["t"]):
                last[r["id"]] = r
    return list(last.values())


def scorecard_stats(K, res):
    """Model 2 vs the market on settled matches (1X2), as a plain dict (the API and the printed scorecard both use it)."""
    if not K:
        return {"n": 0}
    def oc(r):
        x = res[r["id"]]
        return 0 if x["hg"] > x["ag"] else 1 if x["hg"] == x["ag"] else 2
    L = {"m2": [], "mkt": [], "mix": [], "m1": []}
    B = {k: [] for k in L}
    for r in K:
        o = oc(r)
        mix = [0.5 * x + 0.5 * y for x, y in zip(r["m2"], r["mkt"])]
        for k, p in (("m2", r["m2"]), ("mkt", r["mkt"]), ("mix", mix), ("m1", r["m1"])):
            L[k].append(-math.log(max(p[o], 1e-9)))
            B[k].append(sum((p[j] - (1.0 if j == o else 0.0)) ** 2 for j in range(3)))
    n = len(K)
    srcs = defaultdict(int)
    for r in K:
        srcs[r["src"]] += 1
    out = {"n": n, "sources": dict(srcs), "dates": [min(r["date"] for r in K), max(r["date"] for r in K)],
           "logloss": {k: sum(L[k]) / n for k in L}, "brier": {k: sum(B[k]) / n for k in B}, "paired": {}}
    for x, y, lab in (("m2", "mkt", "model2_minus_market"), ("mix", "mkt", "mix_minus_market")):
        d, se = _paired(L[x], L[y]); d2, se2 = _paired(B[x], B[y])
        out["paired"][lab] = {"logloss": d, "logloss_se": se, "brier": d2, "brier_se": se2}
    fr = [sum(1 for r in K if oc(r) == o) / n for o in range(3)]
    out["avg_prob"] = {"market": [sum(r["mkt"][j] for r in K) / n for j in range(3)], "model2": [sum(r["m2"][j] for r in K) / n for j in range(3)], "actual": fr}
    ka = [r for r in K if r["src"] == "KALSHI" and r.get("ask") and all(x is not None for x in r["ask"])]
    out["bets"] = {"n_matches": len(ka), "rows": []}
    for thr in (0.0, 0.03, 0.05, 0.10):
        pr, cost = [], 0.0
        for r in ka:
            o = oc(r)
            for j in range(3):
                c_ = r["ask"][j] + kfee(r["ask"][j])
                if r["m2"][j] - c_ >= thr:
                    pr.append((1.0 if j == o else 0.0) - c_); cost += c_
        row = {"threshold": thr, "bets": len(pr)}
        if len(pr) >= 3:
            m_, se_ = mean_se(pr)
            row.update({"profit_per_contract": m_, "se": se_, "roi": sum(pr) / cost})
        out["bets"]["rows"].append(row)
    return out


def track_scorecard(a, res, log):
    K = last_snapshots(_load_track(a.dir), res)
    log("\n=== SCORECARD: Model 2 vs the market, last pre-kickoff snapshot of each settled match ===")
    if len(K) < 5:
        log(f"only {len(K)} settled matches with a logged market price so far; run `track` before each matchday and again after it (it logs, settles and prints this)"); return
    S = scorecard_stats(K, res)
    log(f"matches: {S['n']}   price source: {S['sources']}   (dates {S['dates'][0]} to {S['dates'][1]})")
    nm = {"m2": "MODEL 2", "mkt": "market (de-vigged)", "mix": "50/50 mix", "m1": "MODEL 1"}
    for k in ("m1", "m2", "mkt", "mix"):
        log(f"   {nm[k]:22s} 1X2 logloss {S['logloss'][k]:.4f}   Brier {S['brier'][k]:.4f}")
    for key, lab in (("model2_minus_market", "MODEL 2 minus market"), ("mix_minus_market", "50/50 mix minus market")):
        p = S["paired"][key]
        log(f"   {lab:24s} logloss {p['logloss']:+.4f} (se {p['logloss_se']:.4f})   Brier {p['brier']:+.4f} (se {p['brier_se']:.4f})   (negative = first better)")
    ap_ = S["avg_prob"]
    log("   average probability   home    draw    away")
    log(f"      market            {ap_['market'][0]:6.1%}  {ap_['market'][1]:6.1%}  {ap_['market'][2]:6.1%}")
    log(f"      MODEL 2           {ap_['model2'][0]:6.1%}  {ap_['model2'][1]:6.1%}  {ap_['model2'][2]:6.1%}")
    log(f"      actual            {ap_['actual'][0]:6.1%}  {ap_['actual'][1]:6.1%}  {ap_['actual'][2]:6.1%}")
    if S["bets"]["n_matches"]:
        log(f"\n   hypothetical 1-contract bets on Kalshi asks (price + fee 0.07*p*(1-p)), Kalshi-priced matches only (n={S['bets']['n_matches']}); NOT advice, tiny samples are noise:")
        for r in S["bets"]["rows"]:
            if "roi" in r:
                log(f"      net edge >= {r['threshold'] * 100:4.1f}c: {r['bets']:3d} bets, profit per contract {r['profit_per_contract'] * 100:+6.1f}c (se {r['se'] * 100:.1f}c), ROI {r['roi']:+.1%}")
            else:
                log(f"      net edge >= {r['threshold'] * 100:4.1f}c: {r['bets']} bets")
    log("   Read the paired lines: a Model 2 minus market logloss below zero by more than about two se would be real evidence of an edge; anything else is noise.")


def cmd_track(a, get=None, kget=None, now=None, log=print):
    get = get or get_json
    kget = kget or get_json
    now = now or _utc_now()
    if not getattr(a, "no_snapshot", False):
        track_snapshot(a, get, kget, now, log)
    res = {}
    rp = os.path.join(a.dir, TRACK_RES)
    if not getattr(a, "no_settle", False):
        res = track_settle(a, get, now, log)
    elif os.path.exists(rp):
        res = json.load(open(rp))
    track_scorecard(a, res, log)
    return 0


def cmd_kalshi(a, get=None, log=print):
    """Probe: what does Kalshi list for the Champions League, and does it match ESPN's upcoming fixtures?"""
    get = get or get_json
    ev, notes = kalshi_slate(get)
    for n_ in notes:
        log(n_)
    if not ev:
        log("NO Champions League game markets found.  Paste this output back (series list may need a different ticker).")
        try:
            d = get(KALSHI + "/series?category=Sports")
            sr = [(s.get("ticker"), s.get("title")) for s in (d.get("series") or []) if any(w in ((s.get("ticker") or "") + (s.get("title") or "")).lower() for w in ("soccer", "league", "uefa", "champ"))]
            log("sports series that look relevant: " + "; ".join(f"{t} = {ti}" for t, ti in sr[:40]))
        except Exception as ex:                                      # noqa: BLE001
            log(f"series list failed: {ex}")
        return 1
    for code, e in sorted(ev.items(), key=lambda kv: _code_date(kv[0]) or dt.date.max)[:6]:
        log(f"event {code}: game outcomes {list(e['game'])}; total lines {sorted(e['total'])}")
        for s_, m in list(e["game"].items())[:3]:
            log(f"   {s_}: bid/ask/mid {tuple(None if x is None else round(x, 3) for x in kalshi_prices(m))}  fields {sorted(k for k in m if 'yes' in k or 'price' in k or 'strike' in k)}")
    nq = sum(1 for e in ev.values() if len(e["game"]) == 3 and all(kalshi_prices(m)[2] is not None for m in e["game"].values()))
    log(f"{len(ev)} events listed; {nq} with a real two-sided quote on all three outcomes (spread <= {MAX_SPREAD:.2f})")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("probe", "collect", "check"):
        return data_main(argv)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["backtest", "calib", "project", "track", "kalshi", "probe", "collect", "check"])
    ap.add_argument("--dir", default="cl_data")
    ap.add_argument("--leagues", nargs="*", default=None)
    ap.add_argument("--tune-season", type=int, default=None, help="season label used for tuning (default: two seasons before the current one)")
    ap.add_argument("--days", type=int, default=None, help="project / track: how many days ahead to look (default 4 for project, 21 for track)")
    ap.add_argument("--no-snapshot", action="store_true", help="track: only settle and print the scorecard")
    ap.add_argument("--no-settle", action="store_true", help="track: do not look up results")
    ap.add_argument("--both", action="store_true", help="project: also show Model 1 (default: Model 2 only)")
    ap.add_argument("--no-market", action="store_true", help="project: do not fetch Kalshi / use ESPN lines")
    a = ap.parse_args(argv)
    if a.tune_season is None:
        a.tune_season = season_of(dt.date.today()) - 2
    if a.days is None:
        a.days = 21 if a.cmd == "track" else 4
    return {"backtest": cmd_backtest, "calib": cmd_calib, "project": cmd_project, "track": cmd_track, "kalshi": cmd_kalshi}[a.cmd](a) or 0


# ================================================================================================ PLATFORM: backlog, collection loops, live tracker, web page, API
import threading

STATE_FILE, SLATE_FILE, BACKLOG_FILE = "cl_state.json", "cl_slate.json", "cl_backlog.json"
MIN_CL_MATCHES = 100


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _load_json(path, default=None):
    for _ in range(3):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except ValueError:
            time.sleep(0.2)
    return default


def save_config(R, root, tune_season):
    cfg = {"tune_season": tune_season, "lg": R["lg"], "wA": R["wA"], "wB": R["wB"], "lamx": R["lamx"], "lamt": R["lamt"],
           "A": {k: R["famA"][k] for k in ("P", "hl_p", "TP", "hl_t")}, "B": {k: R["famB"][k] for k in ("P", "hl_p", "TP", "hl_t")}}
    _atomic_json(os.path.join(root, "cl_config.json"), cfg)
    return cfg


def walkforward_last(ms, cfg):
    """Walk-forward predictions of both models for EVERY Champions League match, with the chosen settings and each club's previous XI
    (what is knowable days ahead).  -> (recs, model-2 list, model-1 list, lidx, names)."""
    ms_all = sorted([m for m in ms if m["goals_ok"]], key=lambda m: (m["date"], m["id"]))
    A_ms = [m for m in ms_all if m["comp"] == CL_SLUG]
    dom, names, lidx = club_leagues(ms_all)
    nL = len(names)
    famA, famB = cfg["A"], cfg["B"]
    rpA, rtA = lineup_last(A_ms, None, famA, cfg["lg"], "A")
    rpB, rtB = lineup_last(ms_all, lambda m: m["comp"] == CL_SLUG, famB, cfg["lg"], "B")
    za = blend([r["x"][famA["P"]] for r in rpA], [r["t"][famA["TP"]] for r in rtA], cfg["wA"])
    xb = stage2(rpB, lambda r: r["x"][famB["P"]], lidx, nL, cfg["lamx"], True)
    tb = stage2(rtB, lambda r: r["t"][famB["TP"]], lidx, nL, cfg["lamt"], True)
    return rpB, blend(xb, tb, cfg["wB"]), za, lidx, names


def _market_of_file(root, mid):
    p = os.path.join(root, CL_SLUG, "matches", f"{mid}.json")
    try:
        q = parse_market(json.load(open(p))) if os.path.exists(p) else None
    except (ValueError, OSError):
        q = None
    if q and q.get("ml"):
        s = sum(q["ml"])
        return [x / s for x in q["ml"]], q.get("total")
    return None, None


def compute_backlog(ms, cfg, root, season, csv_path=None):
    """Every Champions League match already played in `season`, predicted walk-forward (only earlier matches, previous-XI lineups) vs the result
    and vs ESPN's stored closing line.  -> dict with the per-match rows, a scorecard and a club table; the rows are also written to a CSV."""
    recs, zb, za, lidx, names = walkforward_last(ms, cfg)
    rows = []
    for i, r in enumerate(recs):
        if r["season"] != season:
            continue
        base = (r["bh"], r["ba"])
        P = zb[i] or base
        P1 = za[i] or base
        ph, pd_, pa = outcome_probs(*P)
        mk, tot = _market_of_file(root, r["id"])
        o = 0 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 2
        rows.append({"id": r["id"], "date": r["date"], "home": r["home"], "away": r["away"], "hg": r["hg"], "ag": r["ag"], "outcome": o,
                     "xh": P[0], "xa": P[1], "p": [ph, pd_, pa], "p1": list(outcome_probs(*P1)), "pbase": list(outcome_probs(*base)), "base": list(base),
                     "o25": p_over(P[0] + P[1], 2.5), "o15": p_over(P[0] + P[1], 1.5), "o35": p_over(P[0] + P[1], 3.5), "o45": p_over(P[0] + P[1], 4.5),
                     "th": {str(l): p_over(P[0], l) for l in TEAM_LINES}, "ta": {str(l): p_over(P[1], l) for l in TEAM_LINES}, "btts": p_btts(*P), "mkt": mk, "tot": tot,
                     "fitted": zb[i] is not None})
    out = {"status": "ok", "season": season, "matches": len(rows), "rows": rows, "csv": csv_path}
    if csv_path and rows:
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("date,home,away,home_goals,away_goals,xg_home,xg_away,p_home,p_draw,p_away,p_over_2_5,p_home_over_0_5,p_home_over_1_5,p_home_over_2_5,p_away_over_0_5,p_away_over_1_5,p_away_over_2_5,p_btts,market_home,market_draw,market_away,result\n")
            for r in rows:
                mk = r["mkt"] or [None] * 3
                f.write(",".join([r["date"], '"' + r["home"].replace('"', "") + '"', '"' + r["away"].replace('"', "") + '"', str(r["hg"]), str(r["ag"]), f"{r['xh']:.3f}", f"{r['xa']:.3f}",
                                  f"{r['p'][0]:.4f}", f"{r['p'][1]:.4f}", f"{r['p'][2]:.4f}", f"{r['o25']:.4f}"] + [f"{r['th'][k]:.4f}" for k in ("0.5", "1.5", "2.5")] + [f"{r['ta'][k]:.4f}" for k in ("0.5", "1.5", "2.5")] + [f"{r['btts']:.4f}"] + ["" if x is None else f"{x:.4f}" for x in mk] + ["HDA"[r["outcome"]]]) + "\n")
    if not rows:
        out["scorecard"] = None
        return out
    n = len(rows)
    ll = [-math.log(max(r["p"][r["outcome"]], 1e-9)) for r in rows]
    llb = [-math.log(max(r["pbase"][r["outcome"]], 1e-9)) for r in rows]
    d, se = _paired(ll, llb)
    fr = [sum(1 for r in rows if r["outcome"] == o) / n for o in range(3)]
    sc = {"n": n, "logloss": sum(ll) / n, "logloss_league_avg": sum(llb) / n, "logloss_minus_league_avg": d, "se": se,
          "accuracy": sum(1 for r in rows if max(range(3), key=lambda j: r["p"][j]) == r["outcome"]) / n,
          "pred": [sum(r["p"][j] for r in rows) / n for j in range(3)], "actual": fr,
          "goals": {"home_expected": sum(r["xh"] for r in rows) / n, "away_expected": sum(r["xa"] for r in rows) / n,
                    "home_actual": sum(r["hg"] for r in rows) / n, "away_actual": sum(r["ag"] for r in rows) / n,
                    "mae_total": sum(abs(r["xh"] + r["xa"] - r["hg"] - r["ag"]) for r in rows) / n,
                    "mae_total_league_avg": sum(abs(r["base"][0] + r["base"][1] - r["hg"] - r["ag"]) for r in rows) / n},
          "total_over": {}}
    for ln, key in ((1.5, "o15"), (2.5, "o25"), (3.5, "o35"), (4.5, "o45")):
        ys = [1.0 if r["hg"] + r["ag"] > ln else 0.0 for r in rows]
        sc["total_over"][str(ln)] = {"pred": sum(r[key] for r in rows) / n, "rate": sum(ys) / n,
                                      "brier": sum((r[key] - y) ** 2 for r, y in zip(rows, ys)) / n,
                                      "brier_league_avg": sum((p_over(r["base"][0] + r["base"][1], ln) - y) ** 2 for r, y in zip(rows, ys)) / n}
    sc["team_over"] = {}
    for ln in TEAM_LINES:
        ps = [(r["th"][str(ln)], 1.0 if r["hg"] > ln else 0.0, p_over(r["base"][0], ln)) for r in rows] + [(r["ta"][str(ln)], 1.0 if r["ag"] > ln else 0.0, p_over(r["base"][1], ln)) for r in rows]
        sc["team_over"][str(ln)] = {"pred": sum(x[0] for x in ps) / len(ps), "rate": sum(x[1] for x in ps) / len(ps), "brier": sum((x[0] - x[1]) ** 2 for x in ps) / len(ps),
                                     "brier_league_avg": sum((x[2] - x[1]) ** 2 for x in ps) / len(ps)}
    yb = [1.0 if (r["hg"] > 0 and r["ag"] > 0) else 0.0 for r in rows]
    sc["btts"] = {"pred": sum(r["btts"] for r in rows) / n, "rate": sum(yb) / n, "brier": sum((r["btts"] - y) ** 2 for r, y in zip(rows, yb)) / n,
                  "brier_league_avg": sum((p_btts(*r["base"]) - y) ** 2 for r, y in zip(rows, yb)) / n}
    mrows = [r for r in rows if r["mkt"]]
    if len(mrows) >= 10:
        lm = [-math.log(max(r["mkt"][r["outcome"]], 1e-9)) for r in mrows]
        l2 = [-math.log(max(r["p"][r["outcome"]], 1e-9)) for r in mrows]
        d2, se2 = _paired(l2, lm)
        sc["vs_market_1x2"] = {"n": len(mrows), "logloss_model": sum(l2) / len(mrows), "logloss_market": sum(lm) / len(mrows), "model_minus_market": d2, "se": se2}
    out["scorecard"] = sc
    # club table: points vs expected points, goals vs expected goals (from the walk-forward probabilities)
    tm = {}
    for r in rows:
        for side, nm_ in (("h", r["home"]), ("a", r["away"])):
            t = tm.setdefault(nm_, {"team": nm_, "played": 0, "pts": 0, "exp_pts": 0.0, "gf": 0, "xgf": 0.0, "ga": 0, "xga": 0.0})
            t["played"] += 1
            if side == "h":
                t["pts"] += 3 if r["outcome"] == 0 else 1 if r["outcome"] == 1 else 0
                t["exp_pts"] += 3 * r["p"][0] + r["p"][1]; t["gf"] += r["hg"]; t["ga"] += r["ag"]; t["xgf"] += r["xh"]; t["xga"] += r["xa"]
            else:
                t["pts"] += 3 if r["outcome"] == 2 else 1 if r["outcome"] == 1 else 0
                t["exp_pts"] += 3 * r["p"][2] + r["p"][1]; t["gf"] += r["ag"]; t["ga"] += r["hg"]; t["xgf"] += r["xa"]; t["xga"] += r["xh"]
    out["teams"] = sorted(tm.values(), key=lambda t: (-t["pts"], -(t["gf"] - t["ga"])))
    return out


class CLPlatform:
    def __init__(self, directory="cl_data", now_fn=None, get=None, kget=None, log_fn=None, tune_season=None):
        self.root = directory
        self.tune_season = tune_season
        os.makedirs(directory, exist_ok=True)
        self._now_fn = now_fn
        self.get = get or get_json
        self.kget = kget or get_json
        self._log = log_fn or (lambda *a: print(*a, flush=True))
        self._lock = threading.RLock()
        self._work = threading.Lock()                      # one heavy job (collect / tune / model run) at a time
        self._stop = threading.Event()
        self._ms = None
        self._mkey = None
        self._last_logged = None
        self._errors = {}

    # ------------------------------------------------------------------ helpers
    def _now(self):
        n = self._now_fn() if self._now_fn else dt.datetime.now(dt.timezone.utc)
        return n if n.tzinfo else n.replace(tzinfo=dt.timezone.utc)

    def _p(self, name):
        return os.path.join(self.root, name)

    def _ns(self, **kw):
        class _A: pass
        a = _A()
        a.dir, a.leagues, a.days, a.both, a.no_market, a.tune_season = self.root, None, 21, False, True, self.tune_season or season_of(self._now().date()) - 2
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def _state(self):
        return _load_json(self._p(STATE_FILE), {}) or {}

    def _save_state(self, **kw):
        with self._lock:
            s = self._state()
            s.update(kw)
            _atomic_json(self._p(STATE_FILE), s)

    def _iso(self):
        return self._now().isoformat()

    def _guard(self, name, fn):
        try:
            r = fn()
            self._errors.pop(name, None)
            return r
        except Exception as e:                              # noqa: BLE001 -- a failing refresh must never kill the service
            self._errors[name] = f"{type(e).__name__}: {e}"
            self._log(f"[{name}] {self._errors[name]}")
            return {"status": "error", "warnings": [self._errors[name]]}

    # ------------------------------------------------------------------ stored matches
    def matches(self):
        key = []
        for slug in [CL_SLUG] + DOMESTIC:
            d = os.path.join(self.root, slug, "matches")
            if os.path.isdir(d):
                fs = [f for f in os.listdir(d) if f.endswith(".json")]
                key.append((slug, len(fs), max((os.stat(os.path.join(d, f)).st_mtime_ns for f in fs), default=0)))
        key = tuple(key)
        with self._lock:
            if self._ms is None or key != self._mkey:
                self._ms = load_matches(self.root)
                self._mkey = key
            return self._ms

    def cl_count(self):
        return sum(1 for m in self.matches() if m["comp"] == CL_SLUG and m["goals_ok"])

    def needs_bootstrap(self):
        return self.cl_count() < MIN_CL_MATCHES or not os.path.exists(self._p("cl_config.json"))

    def config(self):
        return _load_json(self._p("cl_config.json"), None)

    # ------------------------------------------------------------------ data collection (resumable, incremental)
    def refresh_data(self, domestic=True, seasons=None, workers=4, log=None):
        with self._work:
            today = self._now().date()
            cur = season_of(today)
            seasons = seasons or [cur]
            slugs = [CL_SLUG] + (DOMESTIC if domestic else [])
            for slug in slugs:
                collect_league(slug, self.root, seasons, workers=workers, today=today, log=log or (lambda *a: None), get=self.get)
            self._save_state(data_refreshed_at=self._iso(), **({"domestic_refreshed_at": self._iso()} if domestic else {}))
            self._ms = None
            return {"status": "ok", "cl_matches": self.cl_count()}

    def tune(self, log=None):
        with self._work:
            ms = self.matches()
            a = self._ns()
            R = run_models(ms, a.tune_season, log or (lambda *x: None))
            cfg = save_config(R, self.root, a.tune_season)
            self._save_state(tuned_at=self._iso(), tuned_on_cl=self.cl_count())
            return cfg

    def bootstrap(self, log=None):
        """First start: download 4 seasons of the Champions League and 14 domestic leagues, tune both models, build the slate and the backlog."""
        self._save_state(bootstrap={"status": "running", "started_at": self._iso()})
        try:
            cur = season_of(self._now().date())
            self.refresh_data(domestic=True, seasons=[cur - 3, cur - 2, cur - 1, cur], log=log or self._log)
            self.tune(log)
            self.refresh_slate()
            self.refresh_backlog()
            self.refresh_prices()
            self._save_state(bootstrap={"status": "done", "finished_at": self._iso()})
        except Exception as e:                              # noqa: BLE001
            self._save_state(bootstrap={"status": "error", "error": f"{type(e).__name__}: {e}"})
            raise
        return {"status": "ok"}

    # ------------------------------------------------------------------ slate: model (heavy, hourly) + prices (light, every few minutes)
    def refresh_slate(self, days=21):
        cfg = self.config()
        if not cfg:
            return {"status": "error", "warnings": ["no tuned settings yet (bootstrap not finished)"]}
        with self._work:
            a = self._ns(days=days)
            rows = []
            rc = cmd_project(a, get=self.get, today=self._now().date(), log=lambda *x: None, kget=lambda u: (_ for _ in ()).throw(RuntimeError("off")), sink=rows)
            if rc:
                return {"status": "error", "warnings": ["projection failed"]}
            _atomic_json(self._p(SLATE_FILE), {"computed_at": self._iso(), "rows": rows, "days": days, "markets": (_load_json(self._p(SLATE_FILE), {}) or {}).get("markets")})
            self._save_state(slate_computed_at=self._iso(), slate_rows=len(rows))
        self.refresh_prices()
        return {"status": "ok", "matches": len(rows)}

    def refresh_prices(self):
        sl = _load_json(self._p(SLATE_FILE), None)
        if not sl:
            return {"status": "error", "warnings": ["no slate yet"]}
        ev, notes = ({}, [])
        try:
            ev, notes = kalshi_slate(self.kget)
        except Exception as e:                              # noqa: BLE001
            notes = [f"Kalshi not reachable: {e}"]
        if not ev and (sl.get("markets") or {}).get("by_id"):
            self._errors["prices"] = "Kalshi returned no usable events; keeping the previous prices (they are flagged as old)"
            return {"status": "error", "warnings": [self._errors["prices"]]}
        mk = {}
        for r in sl["rows"]:
            q = kalshi_for(ev, r["home"], r["away"], r["date"]) if ev else None
            m = market_for(q, r.get("dk"))
            if m:
                mk[r["id"]] = {"src": m["src"], "p": list(m["p"]), "ask": [x for x in m["ask"]], "over": {str(k): list(v) for k, v in m["over"].items()},
                               "kalshi_listed_no_quote": bool(q) and m["src"] != "KALSHI"}
        sl["markets"] = {"fetched_at": self._iso(), "by_id": mk, "notes": notes}
        _atomic_json(self._p(SLATE_FILE), sl)
        self._save_state(prices_refreshed_at=self._iso(), kalshi_events=len(ev))
        self._log_snapshots(sl)
        return {"status": "ok", "priced": len(mk), "kalshi_events": len(ev)}

    def _log_snapshots(self, sl):
        """Append a pre-kickoff snapshot (model + market) for matches within 72h of kickoff; unchanged snapshots are not repeated."""
        now = self._now()
        if self._last_logged is None:
            self._last_logged = {}
            for r in _load_track(self.root)[-2000:]:
                self._last_logged[r["id"]] = self._sig(r)
        by = (sl.get("markets") or {}).get("by_id", {})
        with self._lock, open(self._p(TRACK_LOG), "a", encoding="utf-8") as f:
            for r in sl["rows"]:
                kick = _parse_dt(r["kick"])
                if kick is None or kick <= now or kick - now > dt.timedelta(hours=72):
                    continue
                m = by.get(r["id"])
                rec = dict(r)
                rec.update({"t": now.isoformat(), "src": m["src"] if m else None, "mkt": m["p"] if m else None, "ask": m["ask"] if m else None, "over": m["over"] if m else {}})
                s = self._sig(rec)
                if self._last_logged.get(r["id"]) == s:
                    continue
                f.write(json.dumps(rec) + "\n")
                self._last_logged[r["id"]] = s

    @staticmethod
    def _sig(r):
        return (tuple(round(x, 3) for x in r["m2"]), tuple(round(x, 3) for x in r["mkt"]) if r.get("mkt") else None, r.get("src"), r.get("lineup"))

    def get_slate(self):
        sl = _load_json(self._p(SLATE_FILE), None)
        if not sl:
            return {"status": "ok", "warnings": ["warming up: no slate yet (bootstrap is still running or has not started)"], "events": []}
        now = self._now()
        mk = (sl.get("markets") or {}).get("by_id", {})
        ev = []
        for r in sl["rows"]:
            kick = _parse_dt(r["kick"])
            if kick and kick <= now - dt.timedelta(hours=3):
                continue
            m = mk.get(r["id"])
            P = r["m2"]
            e = {"id": r["id"], "kickoff": r["kick"], "home": r["home"], "away": r["away"], "lineup": r["lineup"], "xg": r["xg"], "model": P,
                 "over": {str(l): p_over(sum(r["xg"]), l) for l in (1.5, 2.5, 3.5, 4.5)},
                 "team_over": {"home": {str(l): p_over(r["xg"][0], l) for l in TEAM_LINES}, "away": {str(l): p_over(r["xg"][1], l) for l in TEAM_LINES}},
                 "btts": p_btts(*r["xg"]), "scores": likeliest_scores(*r["xg"]), "market": None, "warnings": []}
            if m:
                e["market"] = {"source": m["src"], "p": m["p"], "gap": [P[i] - m["p"][i] for i in range(3)], "over": m["over"]}
                if m["src"] == "KALSHI" and all(x is not None for x in m["ask"]):
                    e["market"]["net_cents"] = [100 * (P[i] - (m["ask"][i] + kfee(m["ask"][i]))) for i in range(3)]
                    e["market"]["ask"] = m["ask"]
                if m.get("kalshi_listed_no_quote"):
                    e["warnings"].append("Kalshi lists this match but has no real two-sided quote yet; DraftKings shown")
            else:
                e["warnings"].append("no market price found")
            ev.append(e)
        age = lambda ts: None if _parse_dt(ts) is None else (now - _parse_dt(ts)).total_seconds() / 60    # noqa: E731
        warns = []
        pa = age((sl.get("markets") or {}).get("fetched_at"))
        if pa is None or pa > 20:
            warns.append("market prices are %s old (Kalshi may be unreachable); gaps and net edges are stale" % ("unknown" if pa is None else "%d minutes" % pa))
        return {"status": "ok", "warnings": warns, "events": ev, "model_age_minutes": age(sl.get("computed_at")), "prices_age_minutes": age((sl.get("markets") or {}).get("fetched_at")),
                "notes": (sl.get("markets") or {}).get("notes", []),
                "reading": "Backtest: Model 2 was level with the closing market (logloss +0.004, se 0.020); a gap is as likely the model's noise as the market's."}

    # ------------------------------------------------------------------ results, live scorecard, backlog
    def refresh_results(self):
        res = track_settle(self._ns(), self.get, self._now(), lambda *x: None)
        self._save_state(results_refreshed_at=self._iso())
        return {"status": "ok", "settled": len(res)}

    def get_scorecard(self):
        res = _load_json(self._p(TRACK_RES), {}) or {}
        rows = _load_track(self.root)
        K = last_snapshots(rows, res)
        S = scorecard_stats(K, res)
        ids = {r["id"] for r in rows}
        return {"status": "ok", "warnings": [] if S["n"] >= 5 else [f"only {S['n']} settled matches with a pre-kickoff market price so far: the live scorecard is noise until a few matchdays have been played"],
                "logged_matches": len(ids), "settled": len([i for i in ids if i in res]), "live": S,
                "recent": [{"date": r["date"], "match": f"{r['home']} v {r['away']}", "model": r["m2"], "market": r["mkt"], "source": r["src"], "score": f"{res[r['id']]['hg']}-{res[r['id']]['ag']}"}
                           for r in sorted(K, key=lambda r: r["date"], reverse=True)[:25]]}

    def refresh_backlog(self, season=None):
        cfg = self.config()
        if not cfg:
            return {"status": "error", "warnings": ["no tuned settings yet"]}
        with self._work:
            season = season or season_of(self._now().date())
            b = compute_backlog(self.matches(), cfg, self.root, season, self._p(f"cl_backlog_{season}.csv"))
            _atomic_json(self._p(BACKLOG_FILE), b)
            self._save_state(backlog_at=self._iso(), backlog_matches=b["matches"])
            return b

    def get_backlog(self):
        b = _load_json(self._p(BACKLOG_FILE), None)
        return b or {"status": "ok", "warnings": ["no backlog yet"], "matches": 0, "rows": []}

    def health(self):
        try:
            st = self._state()
            now = self._now()
            age = lambda ts: None if _parse_dt(ts) is None else (now - _parse_dt(ts)).total_seconds() / 60    # noqa: E731
            ms = self.matches()
            return {"status": "ok", "needs_bootstrap": self.needs_bootstrap(), "bootstrap": st.get("bootstrap"), "cl_matches": self.cl_count(), "all_matches": len(ms),
                    "data_through": max((m["date"] for m in ms if m["comp"] == CL_SLUG), default=None),
                    "data_refreshed_minutes_ago": age(st.get("data_refreshed_at")), "slate_computed_minutes_ago": age(st.get("slate_computed_at")),
                    "prices_refreshed_minutes_ago": age(st.get("prices_refreshed_at")), "results_refreshed_minutes_ago": age(st.get("results_refreshed_at")),
                    "tuned_minutes_ago": age(st.get("tuned_at")), "kalshi_events": st.get("kalshi_events"), "logged_snapshots": len(_load_track(self.root)),
                    "settled": len(_load_json(self._p(TRACK_RES), {}) or {}), "errors": dict(self._errors), "warnings": ["warming up"] if self.needs_bootstrap() else []}
        except Exception as e:                              # noqa: BLE001
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    # ------------------------------------------------------------------ background loops
    def start_background_refresh(self, bootstrap=True, cl_minutes=60, domestic_minutes=360, slate_minutes=30, price_minutes=5, results_minutes=60, retune_new_matches=150, first_delays=(20, 60, 120)):
        self._stop.clear()

        def wait(sec):
            self._stop.wait(sec)

        def data_loop():
            if bootstrap and self.needs_bootstrap():
                self._guard("bootstrap", self.bootstrap)
            last_dom = 0.0
            while not self._stop.is_set():
                dom = time.time() - last_dom > domestic_minutes * 60
                self._guard("data", lambda: self.refresh_data(domestic=dom))
                if dom:
                    last_dom = time.time()
                st = self._state()
                if self.config() and self.cl_count() - (st.get("tuned_on_cl") or 0) >= retune_new_matches:
                    self._guard("tune", self.tune)                  # re-tune after enough new Champions League matches
                if self.config():
                    self._guard("backlog", self.refresh_backlog)
                wait(cl_minutes * 60)

        def slate_loop():
            wait(first_delays[0])
            while not self._stop.is_set():
                if self.config():
                    self._guard("slate", self.refresh_slate)
                wait(slate_minutes * 60)

        def price_loop():
            wait(first_delays[1])
            while not self._stop.is_set():
                self._guard("prices", self.refresh_prices)
                wait(price_minutes * 60)

        def result_loop():
            wait(first_delays[2])
            while not self._stop.is_set():
                self._guard("results", self.refresh_results)
                wait(results_minutes * 60)
        for fn, nm_ in ((data_loop, "cl-data"), (slate_loop, "cl-slate"), (price_loop, "cl-prices"), (result_loop, "cl-results")):
            threading.Thread(target=fn, name=nm_, daemon=True).start()

    def stop(self):
        self._stop.set()


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Champions League model</title>
<style>
:root{--bg:#0f1115;--fg:#e8eaed;--mut:#9aa0a6;--card:#171a21;--line:#262b36;--ok:#3ddc97;--bad:#ff6b6b;--warn:#f4b942;--acc:#5aa9ff}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--fg:#14171c;--mut:#5f6773;--card:#fff;--line:#dfe3ea;--ok:#0a8f5b;--bad:#c62828;--warn:#9a6a00;--acc:#1565c0}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}header{padding:14px 16px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--mut);font-size:12px}.banner{margin:10px 16px;padding:8px 12px;border:1px solid var(--warn);border-radius:8px;color:var(--warn);font-size:12px}.banner:empty{display:none}
main{padding:0 16px 40px;max-width:1100px;margin:auto}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:12px 14px}
.hd{display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}.nm{font-weight:600;font-size:15px}.meta{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;margin-top:8px}th,td{text-align:right;padding:3px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500;font-size:12px}
.pos{color:var(--ok)}.neg{color:var(--bad)}.w{color:var(--warn);font-size:12px}h3{margin:10px 0 0;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;cursor:pointer;margin-right:6px}button.on{border-color:var(--acc);color:var(--acc)}
</style></head><body>
<header><h1>UEFA Champions League · Model 2 (all leagues + league strengths) vs the market</h1><div class="sub" id="sub">loading...</div>
<div style="margin-top:8px"><button id="t1" class="on" onclick="tab(1)">Upcoming</button><button id="t2" onclick="tab(2)">Live scorecard</button><button id="t3" onclick="tab(3)">Played this season</button><button onclick="load()">Refresh</button></div></header>
<div class="banner" id="ban"></div><main id="main"></main>
<script>
let T=1;const pc=x=>x==null?'–':(100*x).toFixed(1)+'%',n2=x=>x==null?'–':x.toFixed(2),n4=x=>x==null?'–':x.toFixed(4);
const sg=(x,k)=>x==null?'–':'<span class="'+(x>=0?'pos':'neg')+'">'+(x>=0?'+':'')+(k?x.toFixed(1):(100*x).toFixed(1))+'</span>';
function tab(i){T=i;[1,2,3].forEach(k=>document.getElementById('t'+k).className=k==i?'on':'');load()}
function card(e){const P=e.model,m=e.market;
 let h='<div class="card"><div class="hd"><div class="nm">'+e.home+' vs '+e.away+'</div><div class="meta">'+(e.kickoff?new Date(e.kickoff).toLocaleString():'')+' · lineup: '+e.lineup+'</div></div>';
 (e.warnings||[]).forEach(w=>h+='<div class="w">⚠ '+w+'</div>');
 h+='<div class="meta">Expected goals '+n2(e.xg[0])+' – '+n2(e.xg[1])+'</div><table><tr><th></th><th>Home</th><th>Draw</th><th>Away</th><th>Over 1.5</th><th>Over 2.5</th><th>Over 3.5</th></tr>';
 h+='<tr><td>Model 2</td><td>'+pc(P[0])+'</td><td>'+pc(P[1])+'</td><td>'+pc(P[2])+'</td><td>'+pc(e.over['1.5'])+'</td><td>'+pc(e.over['2.5'])+'</td><td>'+pc(e.over['3.5'])+'</td></tr>';
 if(m){const o=m.over||{};h+='<tr><td>'+m.source+' (de-vigged)</td><td>'+pc(m.p[0])+'</td><td>'+pc(m.p[1])+'</td><td>'+pc(m.p[2])+'</td><td>'+(o['1.5']?pc(o['1.5'][0]):'–')+'</td><td>'+(o['2.5']?pc(o['2.5'][0]):'–')+'</td><td>'+(o['3.5']?pc(o['3.5'][0]):'–')+'</td></tr>';
  h+='<tr><td>Gap (pts)</td><td>'+sg(m.gap[0])+'</td><td>'+sg(m.gap[1])+'</td><td>'+sg(m.gap[2])+'</td><td></td><td></td><td></td></tr>';
  if(m.net_cents)h+='<tr><td>Net of Kalshi ask + fee (¢)</td><td>'+sg(m.net_cents[0],1)+'</td><td>'+sg(m.net_cents[1],1)+'</td><td>'+sg(m.net_cents[2],1)+'</td><td></td><td></td><td></td></tr>'}
 h+='</table>';
 if(e.team_over){h+='<table><tr><th>Team goals · over</th><th>0.5</th><th>1.5</th><th>2.5</th></tr><tr><td>'+e.home+'</td>'+['0.5','1.5','2.5'].map(l=>'<td>'+pc(e.team_over.home[l])+'</td>').join('')+'</tr><tr><td>'+e.away+'</td>'+['0.5','1.5','2.5'].map(l=>'<td>'+pc(e.team_over.away[l])+'</td>').join('')+'</tr></table>'+
  '<div class="meta">Both teams to score '+pc(e.btts)+' · over 4.5 '+pc(e.over['4.5'])+' · likeliest scores: '+e.scores.map(x=>x.score+' ('+pc(x.p)+')').join(', ')+'</div>'}
 return h+'</div>'}
function tbl(title,rows,head){return '<h3>'+title+'</h3><table><tr>'+head.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+x+'</td>').join('')+'</tr>').join('')+'</table>'}
async function load(){
 if(T==1){const j=await(await fetch('/api/cl/slate')).json();
  document.getElementById('sub').textContent=(j.events||[]).length+' upcoming matches · model computed '+Math.round(j.model_age_minutes||0)+' min ago · prices '+Math.round(j.prices_age_minutes||0)+' min ago';
  document.getElementById('ban').textContent=(j.warnings||[]).join(' · ');
  document.getElementById('main').innerHTML=(j.events||[]).map(card).join('')+'<p class="meta">'+(j.reading||'')+'</p>'||'<p class="meta">No upcoming matches.</p>';
 }else if(T==2){const j=await(await fetch('/api/cl/scorecard')).json();const S=j.live||{};
  document.getElementById('sub').textContent='live record · '+(j.logged_matches||0)+' matches logged · '+(j.settled||0)+' settled';
  document.getElementById('ban').textContent=(j.warnings||[]).join(' · ');
  let h='<div class="card">';
  if(!S.n){h+='<div class="meta">No settled matches with a pre-kickoff price yet.</div>'}
  else{const p=S.paired.model2_minus_market;h+='<div class="nm">'+S.n+' settled matches · '+JSON.stringify(S.sources)+'</div>'+tbl('1X2 (lower is better)',['m1','m2','mkt','mix'].map(k=>[{m1:'Model 1',m2:'Model 2',mkt:'Market (de-vigged)',mix:'50/50 mix'}[k],n4(S.logloss[k]),n4(S.brier[k])]),['','Logloss','Brier'])+
   '<div class="meta">Model 2 minus market: logloss '+n4(p.logloss)+' (se '+n4(p.logloss_se)+') · negative = Model 2 better; need about two se for evidence</div>'+
   tbl('Average probability',[['Market'].concat(S.avg_prob.market.map(pc)),['Model 2'].concat(S.avg_prob.model2.map(pc)),['Actual'].concat(S.avg_prob.actual.map(pc))],['','Home','Draw','Away'])+
   (S.bets.n_matches?tbl('Hypothetical 1-contract Kalshi bets (not advice)',S.bets.rows.map(r=>['net edge ≥ '+(100*r.threshold).toFixed(0)+'¢',r.bets,r.roi==null?'–':(100*r.roi).toFixed(1)+'%']),['','Bets','ROI']):'')}
  h+='</div>';if((j.recent||[]).length)h+='<div class="card">'+tbl('Recent settled',j.recent.map(r=>[r.date,r.match,r.model.map(pc).join(' / '),r.market?r.market.map(pc).join(' / '):'–',r.score]),['Date','Match','Model H/D/A','Market H/D/A','Score'])+'</div>';
  document.getElementById('main').innerHTML=h;
 }else{const j=await(await fetch('/api/cl/backlog')).json();const s=j.scorecard;
  document.getElementById('sub').textContent='season '+(j.season||'–')+' · '+(j.matches||0)+' matches played · walk-forward predictions (previous-XI lineups)';
  document.getElementById('ban').textContent=(j.warnings||[]).join(' · ');
  let h='';
  if(s){h+='<div class="card"><div class="nm">Walk-forward record · '+s.n+' matches</div>'+tbl('Results',[['1X2 logloss (model / league average)',n4(s.logloss),n4(s.logloss_league_avg)],['Favourite correct',pc(s.accuracy),'–'],['Home win predicted / actual',pc(s.pred[0]),pc(s.actual[0])],['Draw predicted / actual',pc(s.pred[1]),pc(s.actual[1])],['Away win predicted / actual',pc(s.pred[2]),pc(s.actual[2])],['Goals home expected / actual',n2(s.goals.home_expected),n2(s.goals.home_actual)],['Goals away expected / actual',n2(s.goals.away_expected),n2(s.goals.away_actual)]],['','Model','League avg / actual'])+
   tbl('Team goals over (both sides pooled) · Brier',Object.keys(s.team_over||{}).map(l=>['over '+l,pc(s.team_over[l].pred),pc(s.team_over[l].rate),n4(s.team_over[l].brier),n4(s.team_over[l].brier_league_avg)]),['','Predicted','Actual','Brier','League-avg Brier'])+
   (s.btts?'<div class="meta">Both teams to score: predicted '+pc(s.btts.pred)+' vs actual '+pc(s.btts.rate)+' · Brier '+n4(s.btts.brier)+' (league avg '+n4(s.btts.brier_league_avg)+')</div>':'')+
   tbl('Game total goals over · Brier',Object.keys(s.total_over||{}).map(l=>['over '+l,pc(s.total_over[l].pred),pc(s.total_over[l].rate),n4(s.total_over[l].brier),n4(s.total_over[l].brier_league_avg)]),['','Predicted','Actual','Brier','League-avg Brier'])+
   (s.vs_market_1x2?'<div class="meta">vs closing market ('+s.vs_market_1x2.n+' matches): logloss model '+n4(s.vs_market_1x2.logloss_model)+' vs market '+n4(s.vs_market_1x2.logloss_market)+' · difference '+n4(s.vs_market_1x2.model_minus_market)+' (se '+n4(s.vs_market_1x2.se)+')</div>':'')+'</div>'}
  h+='<div class="card">'+tbl('Every match played',(j.rows||[]).slice().reverse().map(r=>[r.date,r.home+' '+r.hg+'-'+r.ag+' '+r.away,pc(r.p[0])+' / '+pc(r.p[1])+' / '+pc(r.p[2]),r.mkt?r.mkt.map(pc).join(' / '):'–']),['Date','Result','Model H/D/A','Market H/D/A'])+'</div>';
  if((j.teams||[]).length)h+='<div class="card">'+tbl('Clubs: points vs expected points, goals vs expected',j.teams.map(t=>[t.team,t.played,t.pts,t.exp_pts.toFixed(1),t.gf+' / '+t.xgf.toFixed(1),t.ga+' / '+t.xga.toFixed(1)]),['Club','P','Pts','Exp pts','GF / xGF','GA / xGA'])+'</div>';
  document.getElementById('main').innerHTML=h}
}
load();setInterval(load,60000);
</script></body></html>"""


def serve(platform, host="127.0.0.1", port=8056, cors=False, allow_write=False, token=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(PAGE, 200, "text/html; charset=utf-8")
            routes = {"/api/cl/slate": platform.get_slate, "/api/cl/scorecard": platform.get_scorecard, "/api/cl/backlog": platform.get_backlog,
                      "/health": platform.health, "/api/cl/health": platform.health}
            fn = routes.get(path)
            if fn is None:
                return self._send({"status": "error", "warnings": ["not found"]}, 404)
            try:
                self._send(fn())
            except Exception as e:                          # noqa: BLE001
                self._send({"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}, 500)

        def do_POST(self):
            if self.path.split("?")[0] != "/api/cl/refresh":
                return self._send({"status": "error", "warnings": ["not found"]}, 404)
            if not allow_write:
                return self._send({"status": "error", "warnings": ["writes are disabled (start with --allow-write)"]}, 403)
            if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                return self._send({"status": "error", "warnings": ["missing or wrong bearer token"]}, 401)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send({"status": "error", "warnings": ["body is not JSON"]}, 400)
            what = body.get("what", "prices")
            fn = {"data": lambda: platform.refresh_data(domestic=False), "slate": platform.refresh_slate, "prices": platform.refresh_prices,
                  "results": platform.refresh_results, "backlog": platform.refresh_backlog}.get(what)
            if fn is None:
                return self._send({"status": "error", "warnings": ["what must be 'data', 'slate', 'prices', 'results' or 'backlog'"]}, 400)
            self._send(platform._guard(what, fn))

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving on http://{host}:{port}  (/, /api/cl/slate, /api/cl/scorecard, /api/cl/backlog, /health{', POST /api/cl/refresh' if allow_write else ''}). Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


def print_backlog(b):
    s = b.get("scorecard")
    if not s:
        print("no matches played yet this season" if b.get("matches") == 0 else b.get("warnings"))
        return
    print(f"\nSeason {b['season']}: {b['matches']} Champions League matches already played, each predicted WALK-FORWARD (only earlier matches, previous-XI lineups).")
    if b.get("csv"):
        print(f"Every match is in: {b['csv']}")
    print(f"\n1X2 logloss {s['logloss']:.4f} (league-average baseline {s['logloss_league_avg']:.4f}; difference {s['logloss_minus_league_avg']:+.4f} +/- {s['se']:.4f})   favourite correct {s['accuracy']:.1%}")
    print(f"home {s['pred'][0]:.1%} predicted / {s['actual'][0]:.1%} actual   draw {s['pred'][1]:.1%} / {s['actual'][1]:.1%}   away {s['pred'][2]:.1%} / {s['actual'][2]:.1%}")
    g = s["goals"]
    print(f"goals expected home {g['home_expected']:.2f} / away {g['away_expected']:.2f}   actual {g['home_actual']:.2f} / {g['away_actual']:.2f}   MAE of total {g['mae_total']:.2f} (league avg {g['mae_total_league_avg']:.2f})")
    for ln, t in s["total_over"].items():
        print(f"total over {ln}: predicted {t['pred']:.1%}, actual {t['rate']:.1%}, Brier {t['brier']:.4f} (league avg {t['brier_league_avg']:.4f})")
    for ln, t in (s.get("team_over") or {}).items():
        print(f"team over {ln} (each side): predicted {t['pred']:.1%}, actual {t['rate']:.1%}, Brier {t['brier']:.4f} (league avg {t['brier_league_avg']:.4f})")
    if s.get("btts"):
        print(f"both teams to score: predicted {s['btts']['pred']:.1%}, actual {s['btts']['rate']:.1%}, Brier {s['btts']['brier']:.4f} (league avg {s['btts']['brier_league_avg']:.4f})")
    m = s.get("vs_market_1x2")
    if m:
        print(f"vs closing market ({m['n']} matches): logloss model {m['logloss_model']:.4f} vs market {m['logloss_market']:.4f} (model minus market {m['model_minus_market']:+.4f} +/- {m['se']:.4f}; negative = model better)")
    print(f"\n{'club':<28}{'P':>3}{'pts':>5}{'exp pts':>9}{'GF':>5}{'xGF':>7}{'GA':>5}{'xGA':>7}")
    for t in b["teams"]:
        print(f"{t['team']:<28}{t['played']:>3}{t['pts']:>5}{t['exp_pts']:>9.1f}{t['gf']:>5}{t['xgf']:>7.1f}{t['ga']:>5}{t['xga']:>7.1f}")


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def platform_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("backtest", "calib", "project", "track", "kalshi", "probe", "collect", "check"):
        return main(argv)
    ap = argparse.ArgumentParser(description="Champions League models (CL-only vs all-leagues) + live market tracker + web service, one file")
    ap.add_argument("--dir", default=os.environ.get("CL_DATA_DIR", "cl_data"))
    sub = ap.add_subparsers(dest="cmd")
    for name in ("bootstrap", "refresh", "slate", "prices", "results", "tune"):
        sub.add_parser(name)
    bk = sub.add_parser("backlog", help="every Champions League match already played this season: walk-forward prediction vs result -> CSV + summary")
    bk.add_argument("--season", type=int, default=None)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--cors", action="store_true")
    sv.add_argument("--allow-write", action="store_true")
    sv.add_argument("--bootstrap", action="store_true")
    for sp_ in sub.choices.values():
        sp_.add_argument("--dir", default=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.cmd is None:
        ap.print_help()
        return 0
    p = CLPlatform(a.dir)
    if a.cmd == "serve":
        port = a.port or int(os.environ.get("PORT", "8056"))
        host = a.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
        allow = a.allow_write or _env_flag("CL_ALLOW_WRITE")
        token = os.environ.get("CL_API_TOKEN") or None
        if allow and not token and host != "127.0.0.1":
            print("WARNING: writes are enabled on a public address without CL_API_TOKEN.", flush=True)
        p.start_background_refresh(bootstrap=a.bootstrap or _env_flag("CL_BOOTSTRAP"))
        serve(p, host, port, a.cors or _env_flag("CL_CORS"), allow, token)
        return 0
    if a.cmd == "bootstrap":
        p.bootstrap(log=print)
        print(json.dumps(p.health(), indent=1))
    elif a.cmd == "refresh":
        print(p.refresh_data(domestic=True, log=print))
    elif a.cmd == "tune":
        print(json.dumps(p.tune(print), indent=1))
    elif a.cmd == "slate":
        print(p.refresh_slate())
        print(json.dumps(p.get_slate(), indent=1))
    elif a.cmd == "prices":
        print(p.refresh_prices())
    elif a.cmd == "results":
        print(p.refresh_results())
    elif a.cmd == "backlog":
        print_backlog(p.refresh_backlog(a.season))
    return 0


if __name__ == "__main__":
    sys.exit(platform_main())
