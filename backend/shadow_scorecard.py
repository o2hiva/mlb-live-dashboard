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
# One JSON blob per (sport, shard) in SyncState. A record's optional "shard" (e.g. "2026-10" for a daily sport)
# keeps each blob small; sports without a shard use the single blob 'shadow_<sport>'.
def _skey(sport, shard=""):
    return f"shadow_{sport}" if not shard else f"shadow_{sport}:{shard}"


def _get(db, key, default):
    from models_db import SyncState
    row = db.get(SyncState, key)
    if row and row.value:
        try:
            return json.loads(row.value)
        except Exception:
            log.exception("shadow log %s unreadable", key)
    return default


def _put(db, key, obj):
    from models_db import SyncState
    payload = json.dumps(obj, separators=(",", ":"))
    row = db.get(SyncState, key)
    if row is None:
        db.add(SyncState(key=key, value=payload))
    else:
        row.value = payload


def _shard_names(db, sport) -> list:
    return sorted(_get(db, f"shadow_{sport}:_shards", []))


def _load(db, sport, shard="") -> dict:
    return _get(db, _skey(sport, shard), {}) or {}


def _load_all(db, sport) -> dict:
    data = dict(_load(db, sport, ""))
    for sh in _shard_names(db, sport):
        data.update(_load(db, sport, sh))
    return data


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
    only ever hold what the model said BEFORE the start (the latest pre-start version wins).
    A record with "merge": True only replaces the markets it carries and keeps the other items already logged."""
    ad = REGISTRY[sport]
    with _lock:
        now = datetime.utcnow()
        incoming = {}
        skipped = 0
        for rec in ad.collect(db) or []:
            start = _parse_utc(rec.get("start"))
            if start is None or start <= now or not rec.get("items"):
                skipped += 1
                continue
            incoming.setdefault(rec.get("shard", ""), []).append(rec)
        added = updated = 0
        names = set(_shard_names(db, sport))
        for shard, recs in incoming.items():
            data = _load(db, sport, shard)
            for rec in recs:
                old = data.get(rec["id"])
                if old and (old.get("result") is not None or old.get("graded") is not None):
                    continue
                rec = dict(rec)
                merge = rec.pop("merge", False)
                if old and merge:
                    keep = [it for it in old.get("items", []) if it["m"] not in {i["m"] for i in rec["items"]}]
                    rec["items"] = keep + rec["items"]
                    rec["ctx"] = {**old.get("ctx", {}), **rec.get("ctx", {})}
                if old:
                    updated += 1
                else:
                    added += 1
                rec["logged_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")
                rec["result"] = None
                data[rec["id"]] = rec
            _put(db, _skey(sport, shard), data)
            if shard:
                names.add(shard)
        if names:
            _put(db, f"shadow_{sport}:_shards", sorted(names))
        db.commit()
        return {"added": added, "updated": updated, "skipped": skipped}


def settle_now(db, sport) -> dict:
    """Grade finished games. Settled records are compacted to just their graded outcomes (market, p, hit, segment)."""
    ad = REGISTRY[sport]
    with _lock:
        now = datetime.utcnow()
        shards = [""] + _shard_names(db, sport)[-3:]   # only recent shards can still hold unsettled games
        settled = pending_total = 0
        for shard in shards:
            data = _load(db, sport, shard)
            pending = [r for r in data.values() if r.get("graded") is None and r.get("result") is None
                       and (_parse_utc(r.get("start")) or now) < now]
            if not pending:
                continue
            results = ad.settle(db, pending) or {}
            n = 0
            for rid, res in results.items():
                rec = data.get(rid)
                if rec is None or res is None:
                    continue
                g = []
                if not res.get("void"):
                    for it in rec.get("items", []):
                        try:
                            y = ad.grade(it, res, rec)
                        except Exception:
                            y = None
                        if y is not None:
                            g.append([it["m"], it["p"], int(y), it.get("seg")])
                rec["graded"] = g
                rec["result"] = {"void": True} if res.get("void") else {"settled": 1}
                rec.pop("items", None)
                rec.pop("ctx", None)
                n += 1
            if n:
                _put(db, _skey(sport, shard), data)
            settled += n
            pending_total += len(pending) - n
        db.commit()
        return {"settled": settled, "pending": pending_total}


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
        data = _load_all(db, sport)
    graded_games = 0
    by_m: dict[str, list] = {}
    by_seg: dict[tuple, list] = {}
    for rec in data.values():
        g = rec.get("graded")
        if g is None:
            continue
        if g:
            graded_games += 1
        for m, p, y, seg in g:
            by_m.setdefault(m, []).append((float(p), int(y)))
            if seg:
                by_seg.setdefault((m, seg), []).append((float(p), int(y)))
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
    pending = sum(1 for r in data.values() if r.get("graded") is None)
    return {"status": "ok", "sport": sport, "label": ad.label, "logged": logged, "graded": graded_games,
            "pending": pending, "markets": markets, "notes": ad.notes,
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


# ====================================================================== MLB adapter
def _poisson_ge(mean: float, n: int) -> float:
    if n <= 0:
        return 1.0
    if not mean or mean <= 0:
        return 0.0
    cdf = sum(math.exp(k * math.log(mean) - mean - math.lgamma(k + 1)) for k in range(n))
    return min(1.0, max(0.0, 1.0 - cdf))


def _nb_ge(r: float, p: float, n: int) -> float:
    if n <= 0:
        return 1.0
    if not r or not p or r <= 0 or p <= 0 or p >= 1:
        return None
    lp, lq = math.log(p), math.log(1 - p)
    cdf = sum(math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * lp + k * lq) for k in range(n))
    return min(1.0, max(0.0, 1.0 - cdf))


def _binom_ge1(n_ab, p) -> float:
    return 1.0 - (1.0 - p) ** max(1, int(round(n_ab)))


def _order_seg(bo):
    try:
        bo = int(bo)
    except Exception:
        return None
    # MLB lineups store batting order as 1-9 (or 100-900 in some feeds)
    if bo >= 100:
        bo //= 100
    return "order 1-3" if bo <= 3 else "order 4-6" if bo <= 6 else "order 7-9"


def mlb_collect_from_bundles(bundles: list) -> list:
    """bundles: [{game: <games/today row>, hits, hrr, hr, k, pha}] (each prop response may be None)."""
    out = []
    for b in bundles:
        g = b["game"]
        start = g.get("game_datetime_utc")
        if not start:
            continue
        items = []
        py = g.get("first_inning_run_yes_probability")
        if py is not None:
            items.append({"m": "yrfi", "p": round(py, 5)})
        pm = g.get("home_moneyline_win_prob")
        if pm is not None:
            items.append({"m": "ml", "p": round(pm, 5)})
        full = bool(b.get("full", True))
        if full:
            for key, mk, fn in (("hits", "hits1", lambda x: _binom_ge1(x["n_ab"], x["p"]) if x.get("n_ab") and x.get("p") is not None else None),
                                ("hr", "hr1", lambda x: _binom_ge1(x["n_ab"], x["p"]) if x.get("n_ab") and x.get("p") is not None else None),
                                ("hrr", "hrr2", lambda x: _nb_ge(x.get("r"), x.get("p"), 2) if x.get("r") is not None and x.get("p") is not None else None)):
                resp = b.get(key) or {}
                for side in ("home", "away"):
                    for x in resp.get(f"{side}_batters", []) or []:
                        pr = fn(x)
                        if pr is not None and x.get("batter_id"):
                            items.append({"m": mk, "p": round(pr, 5), "pid": x["batter_id"], "side": side,
                                          "seg": _order_seg(x.get("batting_order"))})
            for key, mk in (("k", "pitcher_k"), ("pha", "pitcher_ha")):
                resp = b.get(key) or {}
                for side in ("home", "away"):
                    pit = resp.get(f"{side}_pitcher") or {}
                    mean = pit.get("mean")
                    if mean and pit.get("pitcher_id"):
                        base = max(1, int(round(mean)))
                        for d in (-1, 0, 1):
                            n = base + d
                            if n >= 1:
                                items.append({"m": mk, "p": round(_poisson_ge(mean, n), 5), "line": n,
                                              "pid": pit["pitcher_id"], "side": side})
        if not items:
            continue
        both = bool(g.get("home_lineup_confirmed") and g.get("away_lineup_confirmed"))
        out.append({"id": str(g["game_pk"]), "start": str(start)[:19], "shard": str(g.get("game_date", ""))[:7] or "unknown",
                    "label": f"{g.get('away_team')} @ {g.get('home_team')}", "merge": not full,
                    "ctx": ({"home": g.get("home_team"), "away": g.get("away_team"), "locked": both} if full else {}), "items": items})
    return out


def mlb_grade(item, result, rec):
    m = item["m"]
    if result.get("void"):
        return None
    if m == "yrfi":
        v = result.get("yrfi")
        return None if v is None else int(bool(v))
    if m == "ml":
        h, a = result.get("home"), result.get("away")
        return None if h is None or a is None or h == a else int(h > a)
    key = f"{item.get('side')}:{item.get('pid')}"
    if m in ("hits1", "hr1", "hrr2"):
        row = (result.get("b") or {}).get(key)      # [hits, hr, hrr, played]
        if not row or not row[3]:
            return None
        return int({"hits1": row[0] >= 1, "hr1": row[1] >= 1, "hrr2": row[2] >= 2}[m])
    if m in ("pitcher_k", "pitcher_ha"):
        row = (result.get("p") or {}).get(key)      # [strikeouts, hits allowed, played]
        if not row or not row[2]:
            return None
        return int((row[0] if m == "pitcher_k" else row[1]) >= item["line"])
    return None


def make_mlb_adapter(get_bundles: Callable) -> Adapter:
    """get_bundles(db, skip_heavy_ids:set[str]) -> bundles for games starting soon (see mlb_collect_from_bundles).
    A game whose lineups were both confirmed when last logged is 'locked': only its cheap game-level markets
    (YRFI, moneyline) are refreshed afterwards."""

    def collect(db):
        locked = set()
        for sh in _shard_names(db, "mlb")[-2:]:
            for rid, r in _load(db, "mlb", sh).items():
                if r.get("graded") is None and (r.get("ctx") or {}).get("locked"):
                    locked.add(rid)
        return mlb_collect_from_bundles(get_bundles(db, locked))

    def settle(db, recs):
        import mlb_client
        from models_db import Game, InningLine
        out = {}
        for r in recs:
            try:
                game = db.get(Game, int(r["id"]))
                start = _parse_utc(r.get("start")) or datetime.utcnow()
                final = bool(game and ((game.abstract_status or "") == "Final" or str(game.status or "").startswith("Final")))
                if not final:
                    if (datetime.utcnow() - start).total_seconds() > 3 * 86400:
                        out[r["id"]] = {"void": True}      # postponed / never finished
                    continue
                lines = db.query(InningLine).filter_by(game_pk=game.game_pk, inning=1).all()
                res = {"home": game.home_score, "away": game.away_score,
                       "yrfi": (sum(l.runs for l in lines) > 0) if lines else None, "b": {}, "p": {}}
                need = [it for it in r.get("items", []) if it.get("pid")]
                if need:
                    box = mlb_client.get_boxscore(game.game_pk)
                    for it in need:
                        side, pid = it["side"], it["pid"]
                        pd = (box.get("teams", {}).get(side, {}).get("players", {}) or {}).get(f"ID{pid}", {})
                        st = pd.get("stats") or {}
                        bt, pt = st.get("batting") or {}, st.get("pitching") or {}
                        k = f"{side}:{pid}"
                        if it["m"] in ("hits1", "hr1", "hrr2") and k not in res["b"]:
                            pa = bt.get("plateAppearances")
                            if pa is None:
                                pa = sum(bt.get(x, 0) or 0 for x in ("atBats", "baseOnBalls", "hitByPitch", "sacFlies", "sacBunts"))
                            res["b"][k] = [bt.get("hits", 0), bt.get("homeRuns", 0),
                                           bt.get("hits", 0) + bt.get("runs", 0) + bt.get("rbi", 0), 1 if pa else 0]
                        if it["m"] in ("pitcher_k", "pitcher_ha") and k not in res["p"]:
                            try:
                                ip = float(pt.get("inningsPitched", 0) or 0)
                            except Exception:
                                ip = 0.0
                            res["p"][k] = [pt.get("strikeOuts", 0), pt.get("hits", 0), 1 if (ip > 0 or pt.get("battersFaced", 0)) else 0]
                out[r["id"]] = res
            except Exception:
                log.exception("MLB settle failed for %s", r.get("id"))
        return out

    return Adapter(
        sport="mlb", label="MLB", collect=collect, settle=settle, grade=mlb_grade,
        markets={"yrfi": "First-inning run (YRFI)", "ml": "Moneyline (home wins)", "hits1": "Batter: 1+ hit",
                 "hr1": "Batter: 1+ home run", "hrr2": "Batter: 2+ hits+runs+RBI",
                 "pitcher_k": "Pitcher strikeouts (at least N)", "pitcher_ha": "Pitcher hits allowed (at least N)"},
        line_markets=set(),
        notes={"team_pts": "No sportsbook prices are stored with these picks, so this measures whether the probabilities are honest and informative, not betting profit. Pitcher lines sit at the model mean -1, 0, +1. Players who did not play are voided."})


# ====================================================================== NFL adapter
_NFL_OD = 4.4          # same constants as the dashboard: team points = discretised Normal, game total = Negative Binomial


def nfl_collect_from_payload(payload: dict) -> list:
    rows, season, week = payload.get("games") or [], payload.get("season"), payload.get("week")
    by: dict = {}
    for r in rows:
        by.setdefault(r["game_id"], {})["home" if r.get("is_home") else "away"] = r
    out = []
    for pair, pr in by.items():
        h, a = pr.get("home"), pr.get("away")
        if not h or not a:
            continue
        start = h.get("kickoff_utc") or (f"{h['gameday']}T00:00:00" if h.get("gameday") else None)
        if not start:
            continue
        items = []
        if h.get("moneyline_trusted") and h.get("moneyline_win_prob") is not None:
            items.append({"m": "ml", "p": round(h["moneyline_win_prob"], 5)})
        hm, am = h.get("predicted_mean"), a.get("predicted_mean")
        if hm is not None and am is not None:
            for side, mean in (("home", hm), ("away", am)):
                for d in (-3, 0, 3):
                    n = max(1, int(round(mean)) + d)
                    items.append({"m": "team_pts", "p": round(mix_ge(mean, n, _NFL_OD, 0.0), 5), "line": n, "side": side})
            tm = hm + am
            for d in (-3, 0, 3):
                n = max(1, int(round(tm)) + d)
                items.append({"m": "total", "p": round(mix_ge(tm, n, _NFL_OD, 1.0), 5), "line": n})
        if items:
            out.append({"id": f"{season}-{week}-{pair}", "start": str(start)[:19], "season": season, "week": week,
                        "label": f"{a['team']} @ {h['team']}", "ctx": {"home": h["team"], "away": a["team"]}, "items": items})
    return out


def nfl_grade(item, result, rec):
    hs, as_ = result.get("home"), result.get("away")
    if hs is None or as_ is None:
        return None
    m = item["m"]
    if m == "ml":
        return None if hs == as_ else int(hs > as_)
    if m == "team_pts":
        return int((hs if item.get("side") == "home" else as_) >= item["line"])
    if m == "total":
        return int(hs + as_ >= item["line"])
    return None


def make_nfl_adapter(get_payload: Callable) -> Adapter:
    def collect(db):
        return nfl_collect_from_payload(get_payload(db))

    def settle(db, recs):
        import nfl_points_sync
        out, cache = {}, {}
        for r in recs:
            sw = (r.get("season"), r.get("week"))
            if not all(sw):
                continue
            if sw not in cache:
                try:
                    cache[sw] = nfl_points_sync.get_week_games(*sw)
                except Exception:
                    log.exception("NFL settle fetch failed %s", sw)
                    cache[sw] = []
            for g in cache[sw]:
                if nfl_points_sync.is_completed_reg(g) and g.get("home_team") == r["ctx"]["home"] \
                        and g.get("away_team") == r["ctx"]["away"]:
                    out[r["id"]] = {"home": g["home_score"], "away": g["away_score"]}
            if r["id"] not in out and (datetime.utcnow() - (_parse_utc(r.get("start")) or datetime.utcnow())).days > 4:
                out[r["id"]] = {"void": True}   # postponed / not a regular-season game
        return out

    return Adapter(
        sport="nfl", label="NFL", collect=collect, settle=settle, grade=nfl_grade,
        markets={"ml": "Moneyline (home wins)", "team_pts": "Team points (at least N)", "total": "Game total (at least N)"},
        line_markets=set(),
        notes={"team_pts": "No sportsbook lines are stored for the NFL tab, so team-points and total rows are 'at least N' lines placed at the model mean -3, 0, +3. This measures honesty and information, not betting profit."})


# ====================================================================== NPB adapter
def make_npb_adapter(get_rows: Callable) -> Adapter:
    """get_rows(db) -> [{id, start (UTC 'YYYY-MM-DDTHH:MM:SS'), date 'YYYYMMDD', p_yrfi, heuristic, home, away}]"""
    def collect(db):
        out = []
        for g in get_rows(db):
            if g.get("p_yrfi") is None or not g.get("start"):
                continue
            seg = "presumed starters" if g.get("heuristic") else "known starters"
            d = str(g["date"])
            out.append({"id": str(g["id"]), "start": g["start"], "shard": f"{d[:4]}-{d[4:6]}",
                        "label": f"{g.get('away')} @ {g.get('home')}", "ctx": {}, "items": [
                            {"m": "yrfi", "p": round(g["p_yrfi"], 5), "seg": seg}]})
        return out

    def settle(db, recs):
        from models_db import NpbGame
        out = {}
        for r in recs:
            g = db.get(NpbGame, r["id"])
            if g is not None and g.visitor_scored_1st is not None and g.home_scored_1st is not None:
                out[r["id"]] = {"yrfi": bool(g.visitor_scored_1st or g.home_scored_1st)}
            elif (datetime.utcnow() - (_parse_utc(r.get("start")) or datetime.utcnow())).days > 3:
                out[r["id"]] = {"void": True}
        return out

    def grade(item, result, rec):
        v = result.get("yrfi")
        return None if v is None else int(bool(v))

    return Adapter(sport="npb", label="NPB", collect=collect, settle=settle, grade=grade,
                   markets={"yrfi": "First-inning run (YRFI)"}, line_markets=set(),
                   notes={"team_pts": "NPB never publishes starters ahead of time, so most picks use presumed starters; the segment rows show whether that matters."})


# ====================================================================== NBA adapter
_NBA_STEP = {"pts": 2, "reb": 1, "ast": 1, "pra": 3}


def nba_collect_from_payloads(payloads: list) -> list:
    out = []
    for pl in payloads:
        for g in pl.get("games") or []:
            if g.get("completed") or g.get("started") or not g.get("players") or not g.get("tip_off"):
                continue
            tip = str(g["tip_off"])[:16] + ":00"
            items = []
            for p in g["players"]:
                for stat, step in _NBA_STEP.items():
                    pr = (p.get("props") or {}).get(stat)
                    if not pr or not pr.get("sf"):
                        continue
                    sf = pr["sf"]
                    base = max(1, int(round(pr["projection"])))
                    for d in (-1, 0, 1):
                        n = base + d * step
                        if n < 1:
                            continue
                        prob = sf[n - 1] if n - 1 < len(sf) else 0.0
                        items.append({"m": stat, "p": round(float(prob), 5), "line": n, "pid": str(p["player_id"]),
                                      "seg": "starter" if p.get("starter") else "bench"})
            if items:
                out.append({"id": str(g["game_id"]), "start": tip, "shard": str(pl.get("date", ""))[:7] or "unknown",
                            "label": f"{g.get('away_team')} @ {g.get('home_team')}", "ctx": {}, "items": items})
    return out


def make_nba_adapter(get_payloads: Callable) -> Adapter:
    def collect(db):
        return nba_collect_from_payloads(get_payloads(db))

    def settle(db, recs):
        import nba_props_service as nps
        out = {}
        for r in recs:
            try:
                data = nps.bx.get_summary(r["id"], retries=2)
            except Exception:
                data = None
            comp = (((data or {}).get("header") or {}).get("competitions") or [{}])[0]
            state = (((comp.get("status") or {}).get("type") or {}).get("state")) or None
            if state != "post":
                if (datetime.utcnow() - (_parse_utc(r.get("start")) or datetime.utcnow())).days > 3:
                    out[r["id"]] = {"void": True}
                continue
            players = {}
            for block in (data.get("boxscore") or {}).get("players") or []:
                for sb in block.get("statistics") or []:
                    labels = sb.get("labels") or []
                    for row in sb.get("athletes") or []:
                        pp = nps.bx.parse_player_row(row, labels)
                        if pp["dnp"] or not pp["min"] or pp["pts"] is None or pp["reb"] is None or pp["ast"] is None:
                            continue
                        players[str(pp["id"])] = {"pts": pp["pts"], "reb": pp["reb"], "ast": pp["ast"],
                                                  "pra": pp["pts"] + pp["reb"] + pp["ast"]}
            out[r["id"]] = {"p": players}
        return out

    def grade(item, result, rec):
        row = (result.get("p") or {}).get(str(item.get("pid")))
        return None if not row else int(row[item["m"]] >= item["line"])     # did-not-play = void

    return Adapter(sport="nba", label="NBA", collect=collect, settle=settle, grade=grade,
                   markets={"pts": "Points (at least N)", "reb": "Rebounds (at least N)", "ast": "Assists (at least N)",
                            "pra": "Pts+Reb+Ast (at least N)"}, line_markets=set(),
                   notes={"team_pts": "Lines sit at the projection -1, 0, +1 steps (2 pts / 1 reb / 1 ast / 3 PRA). Player props only. Players who did not play are voided. Measures honesty, not betting profit."})


# ====================================================================== Tennis adapter
def tennis_collect_from_events(events: list) -> list:
    """events = the tennis slate's `events` list. One record per match: the favourite's moneyline probability plus every Kalshi
    total-games / total-sets rung (so each logged prediction is a real, settleable Kalshi market, graded by its ticker)."""
    out = []
    for ev in events or []:
        if ev.get("status") != "ok" or ev.get("maybe_in_play") or not ev.get("scheduled_start"):
            continue
        try:
            ml = ev["moneyline"]
            who = "a" if ml["model"]["a"] >= 0.5 else "b"
            k = (ml.get("kalshi") or {}).get(who) or {}
            seg_bo = f"best of {ev.get('best_of')}"
            items = []
            if k.get("ticker"):
                items.append({"m": "ml", "p": round(float(ml["model"][who]), 5), "tk": k["ticker"], "seg": ev.get("surface")})
            for kind, rungs in (("games", ev["games"]["kalshi_rungs"]), ("sets", ev["sets"]["kalshi_rungs"])):
                for r in rungs:
                    tk = (r.get("kalshi") or {}).get("ticker")
                    if tk and r.get("model_over") is not None:
                        items.append({"m": kind, "p": round(float(r["model_over"]), 5), "line": r["line"], "tk": tk, "seg": seg_bo})
            if not items:
                continue
            start = str(ev["scheduled_start"])[:19]
            out.append({"id": str(ev["event"]), "start": start, "shard": start[:7],
                        "label": f"{ev['players']['a']['name']} vs {ev['players']['b']['name']}", "ctx": {}, "items": items})
        except Exception:
            continue
    return out


def make_tennis_adapter(get_events: Callable, get_result: Callable) -> Adapter:
    """get_events(db) -> slate events; get_result(ticker) -> 'yes' | 'no' | 'void' | None (Kalshi's settled result)."""
    def collect(db):
        return tennis_collect_from_events(get_events(db))

    def settle(db, recs):
        out = {}
        now = datetime.utcnow()
        for r in recs:
            start = _parse_utc(r.get("start")) or now
            age = now - start
            if age.total_seconds() < 90 * 60:
                continue                                    # a match cannot be over yet
            res = {}
            for it in r.get("items", []):
                try:
                    v = get_result(it["tk"])
                except Exception:
                    v = None
                if v in ("yes", "no"):
                    res[it["tk"]] = v
            ml_done = any(it["m"] == "ml" and it["tk"] in res for it in r.get("items", []))
            if ml_done or (res and age.days >= 1):
                out[r["id"]] = {"r": res}
            elif age.days > 3:
                out[r["id"]] = {"void": True}                # never settled: postponed / cancelled
        return out

    def grade(item, result, rec):
        v = (result.get("r") or {}).get(item.get("tk"))
        return None if v is None else int(v == "yes")

    return Adapter(sport="tennis", label="Tennis", collect=collect, settle=settle, grade=grade,
                   markets={"ml": "Moneyline - the favourite wins", "games": "Total games Over (Kalshi lines)",
                            "sets": "Total sets Over (Kalshi lines)"}, line_markets=set(),
                   notes={"team_pts": "ATP only. Every Kalshi line is logged before the scheduled start and graded from Kalshi's own settlement. "
                                      "Against Kalshi's prices the model has not shown an edge in backtests: this card is the live check. Retired / walkover matches are graded the way Kalshi settles them."})


# ====================================================================== Bundesliga adapter
BL_LINES = {"o15": 1.5, "o25": 2.5, "o35": 3.5, "o45": 4.5}
BL_TEAM_LINES = {"05": 0.5, "15": 1.5, "25": 2.5}


def bundesliga_collect_from_events(events: list) -> list:
    """events = the Bundesliga slate's `events` list. One record per match that has not kicked off: home / draw / away, both
    teams to score, total goals over 1.5-4.5 and each team's goals over 0.5-2.5 - every market the tab shows."""
    out = []
    for ev in events or []:
        try:
            if ev.get("state") not in (None, "pre") or not ev.get("kickoff"):
                continue
            pr, tot, team = ev["probabilities"], ev["total_over"], ev["team_over"]
            seg = (ev.get("lineup") or {}).get("source") or "previous XI"
            items = [{"m": "h", "p": round(float(pr["home"]), 5), "seg": seg}, {"m": "d", "p": round(float(pr["draw"]), 5), "seg": seg},
                     {"m": "a", "p": round(float(pr["away"]), 5), "seg": seg}, {"m": "btts", "p": round(float(pr["btts"]), 5), "seg": seg}]
            for k, ln in BL_LINES.items():
                if str(ln) in tot:
                    items.append({"m": k, "p": round(float(tot[str(ln)]), 5), "line": ln, "seg": seg})
            for side, pre in (("home", "th"), ("away", "ta")):
                for k, ln in BL_TEAM_LINES.items():
                    if str(ln) in team.get(side, {}):
                        items.append({"m": pre + k, "p": round(float(team[side][str(ln)]), 5), "line": ln, "seg": seg})
            ko = str(ev["kickoff"]).replace("Z", "")
            start = (ko[:16] + ":00") if len(ko) <= 16 else ko[:19]
            out.append({"id": str(ev["id"]), "start": start, "shard": start[:7], "label": f"{ev['home']} vs {ev['away']}", "ctx": {}, "items": items})
        except Exception:
            continue
    return out


def make_bundesliga_adapter(get_events: Callable, get_score: Callable, sport: str = "bundesliga", label: str = "Bundesliga", benchmark: str = None) -> Adapter:
    """get_events(db) -> slate events; get_score(match_id) -> (home goals, away goals) | None. (Also used for the Championship.)"""
    def collect(db):
        return bundesliga_collect_from_events(get_events(db))

    def settle(db, recs):
        out = {}
        now = datetime.utcnow()
        for r in recs:
            start = _parse_utc(r.get("start")) or now
            age = now - start
            if age.total_seconds() < 100 * 60:
                continue                                    # a match cannot be over yet
            try:
                sc = get_score(r["id"])
            except Exception:
                sc = None
            if sc is not None:
                out[r["id"]] = {"hg": int(sc[0]), "ag": int(sc[1])}
            elif age.days > 4:
                out[r["id"]] = {"void": True}                # never settled: postponed / abandoned
        return out

    def grade(item, result, rec):
        hg, ag, m = result["hg"], result["ag"], item["m"]
        if m == "h":
            return int(hg > ag)
        if m == "d":
            return int(hg == ag)
        if m == "a":
            return int(hg < ag)
        if m == "btts":
            return int(hg > 0 and ag > 0)
        if m in BL_LINES:
            return int(hg + ag > BL_LINES[m])
        if m[:2] in ("th", "ta") and m[2:] in BL_TEAM_LINES:
            return int((hg if m[1] == "h" else ag) > BL_TEAM_LINES[m[2:]])
        return None

    markets = {"h": "Home win", "d": "Draw", "a": "Away win", "btts": "Both teams to score"}
    markets.update({k: f"Total goals Over {ln}" for k, ln in BL_LINES.items()})
    markets.update({f"th{k}": f"Home goals Over {ln}" for k, ln in BL_TEAM_LINES.items()})
    markets.update({f"ta{k}": f"Away goals Over {ln}" for k, ln in BL_TEAM_LINES.items()})
    bench = benchmark or ("Research model: in its own backtest (351 matches) it beats the league average, ties a tuned team-level model, and does NOT beat the closing market. "
                          "This card is the live out-of-sample check: every prediction is saved before kickoff and graded on the final score (90 minutes + stoppage). Calibration, not betting profit.")
    return Adapter(sport=sport, label=label, collect=collect, settle=settle, grade=grade, markets=markets, line_markets=set(),
                   notes={"benchmark": bench,
                          "team_pts": "Lines are fixed (total 1.5-4.5, team 0.5-2.5), not posted lines."})


def make_efl_adapter(get_events: Callable, get_score: Callable) -> Adapter:
    """English Championship: same markets, same grading (final score) as the Bundesliga card."""
    return make_bundesliga_adapter(
        get_events, get_score, sport="efl", label="EFL Championship",
        benchmark="Research model: in its own backtest (351 matches) it beats the league average, ties a tuned team-level model, and does NOT beat the closing market. "
                  "This card is the live out-of-sample check: every prediction is saved before kickoff and graded on the final score (90 minutes + stoppage). Calibration, not betting profit.")


def make_j1_adapter(get_events: Callable, get_score: Callable) -> Adapter:
    """J1 League (Japan): same markets, same grading (final score) as the Bundesliga card."""
    return make_bundesliga_adapter(
        get_events, get_score, sport="j1", label="J1 League",
        benchmark="Research model, NOT yet backtested on J1 data. Football-data.co.uk lists only 1X2 closing odds for J1, so there is no market line to beat on totals. "
                  "This card is the live out-of-sample check: every prediction is saved before kickoff and graded on the final score (90 minutes + stoppage). Calibration, not betting profit.")
