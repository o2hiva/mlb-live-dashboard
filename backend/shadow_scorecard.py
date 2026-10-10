"""Generic shadow scorecard.

A shadow scorecard logs every model prediction BEFORE a game starts, settles it against the real result
afterwards, and reports whether the model is honest (calibration), informative (Brier / log loss vs the base
rate) and, where a real market line exists, whether its picks would have made money.  No bets are placed.

Adding a sport = register one adapter (see register()).  CFB is registered from main.py; other sports can
follow the same shape:

    register(Adapter(
        sport="nhl", label="NHL",
        collect=lambda db: [record, ...],      # pre-game predictions (see Record below)
        settle=lambda db, recs: {id: result},  # real results for the records that are finished
        grade=lambda item, result, rec: 1/0/None,   # did this item's YES outcome happen? None = push/void
        markets={"ml": "Moneyline", ...},      # display names, in display order
        line_markets={"spread", "total"},      # markets whose line is a REAL posted line (hit rate / ROI make sense)
    ))

Record  = {"id": str, "start": "YYYY-MM-DDTHH:MM:SS" (UTC), "label": "Away @ Home", "season": int, "week": int,
           "ctx": {...adapter data needed to grade...},
           "items": [{"m": market_key, "p": model P(YES), "line": number|None, "seg": str|None}]}
Storage is one JSON blob per sport in SyncState['shadow_<sport>'] (Railway's disk is ephemeral, the DB is not).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

log = logging.getLogger("shadow_scorecard")

MIN_N_VERDICT = 30
BREAKEVEN_110 = 110 / 210  # 52.38%
_lock = threading.RLock()


@dataclass
class Adapter:
    sport: str
    label: str
    collect: Callable          # (db) -> list[Record]
    settle: Callable           # (db, unsettled_records) -> {id: result}
    grade: Callable            # (item, result, record) -> 1 | 0 | None
    markets: dict = field(default_factory=dict)
    line_markets: set = field(default_factory=set)
    notes: dict = field(default_factory=dict)       # optional static text shown with the scorecard (e.g. benchmarks)


REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter):
    REGISTRY[adapter.sport] = adapter


# ---------------------------------------------------------------- storage
def _key(sport):
    return f"shadow_{sport}"


def _load(db, sport) -> dict:
    from models_db import SyncState
    row = db.get(SyncState, _key(sport))
    if row and row.value:
        try:
            return json.loads(row.value) or {}
        except Exception:
            log.exception("shadow log for %s unreadable", sport)
    return {}


def _save(db, sport, data: dict):
    from models_db import SyncState
    payload = json.dumps(data, separators=(",", ":"))
    row = db.get(SyncState, _key(sport))
    if row is None:
        db.add(SyncState(key=_key(sport), value=payload))
    else:
        row.value = payload
    db.commit()


def _parse_utc(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


# ---------------------------------------------------------------- logging + settling
def log_now(db, sport) -> dict:
    """Save pre-game predictions. A game already started (or already settled) is never touched, so the log can
    only ever hold what the model said BEFORE kickoff (the latest pre-kickoff version wins)."""
    ad = REGISTRY[sport]
    with _lock:
        data = _load(db, sport)
        now = datetime.utcnow()
        added = updated = skipped = 0
        for rec in ad.collect(db) or []:
            start = _parse_utc(rec.get("start"))
            if start is None or start <= now or not rec.get("items"):
                skipped += 1
                continue
            old = data.get(rec["id"])
            if old and old.get("result") is not None:
                continue
            if old:
                updated += 1
            else:
                added += 1
            rec = dict(rec)
            rec["logged_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
            rec["result"] = None
            data[rec["id"]] = rec
        _save(db, sport, data)
        return {"added": added, "updated": updated, "skipped": skipped, "total": len(data)}


def settle_now(db, sport) -> dict:
    ad = REGISTRY[sport]
    with _lock:
        data = _load(db, sport)
        now = datetime.utcnow()
        pending = [r for r in data.values() if r.get("result") is None
                   and (_parse_utc(r.get("start")) or now) < now]
        if not pending:
            return {"settled": 0, "pending": 0}
        results = ad.settle(db, pending) or {}
        n = 0
        for rid, res in results.items():
            if rid in data and res is not None:
                data[rid]["result"] = res
                n += 1
        if n:
            _save(db, sport, data)
        return {"settled": n, "pending": len(pending) - n}


# ---------------------------------------------------------------- metrics
def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _slope(ps, ys):
    """Logistic recalibration slope of y on logit(p): 1.0 = honest, 0 = the probabilities carry no information."""
    if len(ps) < 50:
        return None
    xs = [_logit(p) for p in ps]
    a, b = 0.0, 1.0
    for _ in range(25):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            q = 1 / (1 + math.exp(-(a + b * x)))
            w = q * (1 - q)
            g0 += y - q; g1 += (y - q) * x
            h00 += w; h01 += w * x; h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            return None
        da = (h11 * g0 - h01 * g1) / det
        db_ = (-h01 * g0 + h00 * g1) / det
        a += da; b += db_
        if abs(da) + abs(db_) < 1e-8:
            break
    return b if -5 < b < 5 else None


def _metrics(rows, is_line: bool) -> dict:
    """rows = [(p, y)] with y in {0,1}."""
    n = len(rows)
    if not n:
        return {"n": 0}
    ps = [p for p, _ in rows]
    ys = [y for _, y in rows]
    brier = sum((p - y) ** 2 for p, y in rows) / n
    ll = sum(-math.log(max(p if y else 1 - p, 1e-4)) for p, y in rows) / n
    base = sum(ys) / n
    brier_ref = base * (1 - base)
    var = sum(p * (1 - p) for p in ps)
    z = (sum(ys) - sum(ps)) / math.sqrt(var) if var > 0 else 0.0
    out = {"n": n, "brier": brier, "brier_base": brier_ref, "log_loss": ll, "avg_claimed": sum(ps) / n, "actual": base,
           "z": z, "skill": (1 - brier / brier_ref) if brier_ref > 0 else None, "slope": _slope(ps, ys)}
    # calibration buckets by claimed probability
    edges = [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]
    cal = []
    for lo, hi in edges:
        b = [(p, y) for p, y in rows if lo <= p < hi]
        if b:
            cal.append({"bucket": f"{int(lo*100)}-{min(int(hi*100),100)}%", "n": len(b),
                        "predicted": sum(p for p, _ in b) / len(b), "actual": sum(y for _, y in b) / len(b)})
    out["calibration"] = cal
    # picks: back whichever side the model likes (>50%). Only meaningful against a real posted line.
    pk = [(p, y) for p, y in rows if abs(p - 0.5) > 1e-9]
    if pk:
        wins = sum(1 for p, y in pk if (p > .5) == (y == 1))
        out["pick_n"] = len(pk)
        out["pick_hit"] = wins / len(pk)
        if is_line:
            out["roi_110"] = (wins * (100 / 110) - (len(pk) - wins)) / len(pk)
            e = [(p, y) for p, y in pk if abs(p - 0.5) >= 0.03]
            if e:
                ew = sum(1 for p, y in e if (p > .5) == (y == 1))
                out["edge_n"] = len(e)
                out["edge_hit"] = ew / len(e)
                out["edge_roi_110"] = (ew * (100 / 110) - (len(e) - ew)) / len(e)
    out["verdict"] = _verdict(out, is_line)
    return out


def _verdict(m, is_line) -> str:
    n = m["n"]
    if n < MIN_N_VERDICT:
        return f"Too early to judge ({n} graded; need {MIN_N_VERDICT}+)."
    z = m["z"]
    if z <= -2:
        cal = "overconfident (it claims more than happens)"
    elif z >= 2:
        cal = "underconfident (more happens than it claims)"
    else:
        cal = "honest (claimed rates match results)"
    sk = m.get("skill")
    if sk is None:
        info = ""
    elif sk > 0.02:
        info = " It adds real information beyond the base rate."
    elif sk >= -0.005:
        info = " But it is no better than the base rate."
    else:
        info = " And it is WORSE than just using the base rate."
    s = f"Probabilities are {cal}.{info}"
    if is_line and m.get("pick_n", 0) >= MIN_N_VERDICT:
        h = m["pick_hit"]
        s += f" Backing its side hit {h*100:.1f}% (break-even at -110 is {BREAKEVEN_110*100:.1f}%)."
    return s


def scorecard(db, sport) -> dict:
    ad = REGISTRY[sport]
    with _lock:
        data = _load(db, sport)
    graded_games = 0
    by_m: dict[str, list] = {}
    by_seg: dict[tuple, list] = {}
    for rec in data.values():
        res = rec.get("result")
        if res is None:
            continue
        graded_games += 1
        for it in rec.get("items", []):
            try:
                y = ad.grade(it, res, rec)
            except Exception:
                y = None
            if y is None:
                continue
            by_m.setdefault(it["m"], []).append((float(it["p"]), int(y)))
            if it.get("seg"):
                by_seg.setdefault((it["m"], it["seg"]), []).append((float(it["p"]), int(y)))
    markets = []
    order = list(ad.markets) + [m for m in by_m if m not in ad.markets]
    for m in order:
        if m not in by_m:
            continue
        is_line = m in ad.line_markets
        entry = {"market": m, "label": ad.markets.get(m, m), "real_line": is_line}
        entry.update(_metrics(by_m[m], is_line))
        segs = []
        for (mm, sg), rows in sorted(by_seg.items()):
            if mm == m:
                sm = _metrics(rows, is_line)
                sm["seg"] = sg
                segs.append(sm)
        entry["segments"] = segs
        markets.append(entry)
    logged = len(data)
    return {"status": "ok", "sport": sport, "label": ad.label, "logged": logged, "graded": graded_games,
            "pending": logged - graded_games, "markets": markets, "notes": ad.notes,
            "min_n_verdict": MIN_N_VERDICT, "breakeven_110": BREAKEVEN_110}


def sports() -> list:
    return [{"sport": a.sport, "label": a.label} for a in REGISTRY.values()]


# ---------------------------------------------------------------- background loop
def start_background(interval_minutes: int = 20, first_delay_s: int = 150):
    def loop():
        time.sleep(first_delay_s)
        while True:
            for sport in list(REGISTRY):
                db = None
                try:
                    from database import SessionLocal
                    db = SessionLocal()
                    log_now(db, sport)
                    settle_now(db, sport)
                except Exception:
                    log.exception("shadow scorecard loop failed for %s", sport)
                    try:
                        if db is not None:
                            db.rollback()
                    except Exception:
                        pass
                finally:
                    if db is not None:
                        db.close()
            time.sleep(interval_minutes * 60)

    t = threading.Thread(target=loop, name="shadow-scorecard", daemon=True)
    t.start()
    return t


# ====================================================================== CFB adapter
# Same distribution constants the CFB tab already uses for "at least N points" markets.
_TEAM_OD, _TEAM_SHARE = 5.6, 0.25
_GAME_OD, _GAME_SHARE = 5.0, 0.5


def _ncdf(x, mu, sd):
    return 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))


def mix_ge(mean: float, n: int, od: float, share: float) -> float:
    """P(X >= n): share * NegBin + (1-share) * discretised normal (same mean, variance = od*mean)."""
    if n <= 0:
        return 1.0
    p = 1.0 / od
    r = mean * p / (1 - p)
    cdf = 0.0
    lp, lq = math.log(p), math.log(1 - p)
    for k in range(0, n):
        cdf += math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * lp + k * lq)
    nb = max(0.0, 1.0 - cdf)
    sd = math.sqrt(od * mean)
    den = max(1 - _ncdf(-0.5, mean, sd), 1e-12)
    nm = (1 - _ncdf(n - 0.5, mean, sd)) / den
    return share * nb + (1 - share) * nm


def cfb_collect_from_rows(rows: list) -> list:
    """rows = the per-team rows /api/cfb/games returns."""
    by: dict = {}
    for r in rows:
        by.setdefault(r["game_id"], {})["home" if r.get("is_home") else "away"] = r
    out = []
    for gid, pr in by.items():
        h, a = pr.get("home"), pr.get("away")
        if not h or not a or not h.get("start_date_utc"):
            continue
        items = []
        if h.get("moneyline_trusted") and h.get("moneyline_win_prob") is not None:
            items.append({"m": "ml", "p": round(h["moneyline_win_prob"], 5), "line": None})
        if h.get("spread_trusted") and h.get("spread_cover_prob") is not None and h.get("spread") is not None:
            items.append({"m": "spread", "p": round(h["spread_cover_prob"], 5), "line": h["spread"],
                          "seg": "lopsided (13+)" if h.get("spread_lopsided") else "moderate (<13)"})
        hm, am = h.get("predicted_mean"), a.get("predicted_mean")
        if hm is not None and am is not None:
            tot_mean = hm + am
            pt = h.get("posted_total")
            if pt is not None:
                n = int(math.ceil(pt))  # "at least n" == over a half-point total; a whole-number total counts the tie as over
                items.append({"m": "total", "p": round(mix_ge(tot_mean, n, _GAME_OD, _GAME_SHARE), 5), "line": n})
            for side, mean in (("home", hm), ("away", am)):
                for d in (-4, 0, 4):
                    n = max(1, int(round(mean)) + d)
                    items.append({"m": "team_pts", "p": round(mix_ge(mean, n, _TEAM_OD, _TEAM_SHARE), 5),
                                  "line": n, "side": side})
        if not items:
            continue
        out.append({"id": str(gid), "start": str(h["start_date_utc"])[:19],
                    "label": f"{a['team']} @ {h['team']}", "season": None, "week": None,
                    "ctx": {"home": h["team"], "away": a["team"]}, "items": items})
    return out


def cfb_grade(item, result, rec):
    hs, as_ = result.get("home"), result.get("away")
    if hs is None or as_ is None:
        return None
    m = item["m"]
    if m == "ml":
        return None if hs == as_ else (1 if hs > as_ else 0)
    if m == "spread":
        v = (hs - as_) + item["line"]
        return None if v == 0 else (1 if v > 0 else 0)
    if m == "total":
        return 1 if hs + as_ >= item["line"] else 0
    if m == "team_pts":
        return 1 if (hs if item.get("side") == "home" else as_) >= item["line"] else 0
    return None


_fetch_stamp: dict = {}


def cfb_settle(db, recs: list) -> dict:
    """Finals from the app's own saved scores (current week), then one CFBD /games call per older week."""
    from models_db import SyncState
    res: dict = {}
    try:
        row = db.get(SyncState, "cfb_final_scores")
        scores = (json.loads(row.value) or {}).get("scores", {}) if row and row.value else {}
    except Exception:
        scores = {}
    left = []
    for r in recs:
        s = scores.get(r["id"])
        if s:
            res[r["id"]] = {"home": s["home"], "away": s["away"]}
        else:
            left.append(r)
    if left:
        import cfb_points_sync
        seasons_weeks = {(r.get("season"), r.get("week")) for r in left if r.get("season") and r.get("week")}
        for (season, week) in seasons_weeks:
            if time.time() - _fetch_stamp.get((season, week), 0) < 1800:
                continue
            _fetch_stamp[(season, week)] = time.time()
            for stype in ("regular", "postseason"):
                try:
                    games = cfb_points_sync.get_week_games(season, week, stype)
                except Exception:
                    log.exception("CFB settle fetch failed (%s wk %s %s)", season, week, stype)
                    continue
                for g in games or []:
                    if g.get("completed") and g.get("homePoints") is not None and g.get("awayPoints") is not None:
                        res.setdefault(str(g.get("id")), {"home": g["homePoints"], "away": g["awayPoints"]})
    return {r["id"]: res[r["id"]] for r in recs if r["id"] in res}


CFB_NOTES = {
    "benchmark": ("Historical benchmark (2023-25, walk-forward, n=3,028 spread games): spread picks hit 50.8% while "
                  "claiming ~62%, calibration slope ~0.01 (no real edge over the line). Moneyline underdogs were "
                  "over-rated. Team-points and game-total probabilities were calibrated. The live numbers below "
                  "should look like this if nothing has changed."),
    "team_pts": "Team-points rows are 'at least N' lines placed at the model mean -4, 0, +4 (not real sportsbook lines).",
}


def make_cfb_adapter(get_rows: Callable) -> Adapter:
    """get_rows(db) -> (rows, season, week): the output of /api/cfb/games."""

    def collect(db):
        out = get_rows(db)
        rows, season, week = out["games"], out.get("season"), out.get("week")
        recs = cfb_collect_from_rows(rows)
        for r in recs:
            r["season"], r["week"] = season, week
        return recs

    return Adapter(
        sport="cfb", label="CFB", collect=collect, settle=cfb_settle, grade=cfb_grade,
        markets={"spread": "Spread (home covers)", "ml": "Moneyline (home wins)",
                 "total": "Game total (at least posted)", "team_pts": "Team points (at least N)"},
        line_markets={"spread", "total"}, notes=CFB_NOTES)
