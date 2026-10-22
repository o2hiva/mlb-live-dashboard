"""
UEFA Champions League for the dashboard: wraps cl_platform.py (two goal models - "CL only" and "all leagues" - ESPN data + lineups, Kalshi prices
with a DraftKings fallback; the embedded models are unchanged, see its docstring).

  * background loops (started once from main.py's lifespan): Champions League results hourly, the 14 domestic leagues every 6 h (the first start
    downloads about four seasons and tunes both models - that takes a long while, the slate says "warming up" until it is done), the model slate
    every 30 minutes, Kalshi prices every 5 minutes (this also logs each pre-match prediction), settlement of the logged predictions hourly
  * slate()        : upcoming matches in the same shape the Bundesliga / EFL / J1 tabs use (probabilities, total_over, team_over, expected_goals,
                     likeliest_scores) plus "kq": the platform's own Kalshi quotes (asks for 1X2 and the listed total-goals lines) - cached 10 s.
                     The tab shows the "all leagues" model (Model 2), the one the platform itself publishes.
  * final_score(id): (home goals, away goals) once a match is settled (regulation score), else None - used for the shadow scorecard and for grading
                     tracked Champions League bets

Data lives in CL_DATA_DIR (default backend/cl_data). Railway's disk is wiped on every deploy, so for production point CL_DATA_DIR at a Railway
Volume (e.g. /data/cl): otherwise every deploy repeats the long first download. Needs numpy (already in requirements.txt).
Everything degrades instead of raising: every call returns JSON with a "status" and "warnings".
"""
import logging
import os
import threading
import time

import cl_platform as bp

log = logging.getLogger("cl_service")

DATA_DIR = os.environ.get("CL_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cl_data")
SLATE_TTL = 10.0


def _f(x):
    try:
        return None if x is None else float(x)
    except Exception:
        return None


class CLService(bp.CLPlatform):
    def __init__(self, directory=DATA_DIR, **kw):
        os.makedirs(directory, exist_ok=True)
        super().__init__(directory, **kw)
        self._slate_cache = None
        self._slate_lock = threading.Lock()
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        self.start_background_refresh(bootstrap=True)
        log.info("cl service started (data dir %s)", self.root)

    # ---- the platform's slate -> the shape the soccer tabs use
    def _adapt(self, raw):
        now = self._now()
        out = []
        for e in raw.get("events") or []:
            try:
                P = e["model"]
                xg = e["xg"]
                kick = bp._parse_dt(e.get("kickoff"))
                mk = e.get("market") or None
                ev = {
                    "id": str(e["id"]), "home": e["home"], "away": e["away"], "kickoff": e.get("kickoff"),
                    "state": "pre" if (kick is None or kick > now) else "in", "status": "Scheduled" if (kick is None or kick > now) else "In progress",
                    "lineup": {"source": "confirmed" if str(e.get("lineup") or "").lower().startswith("conf") else "previous"},
                    "expected_goals": {"home": xg[0], "away": xg[1], "total": xg[0] + xg[1]},
                    "probabilities": {"home": P[0], "draw": P[1], "away": P[2], "btts": e.get("btts")},
                    "total_over": e.get("over") or {}, "team_over": e.get("team_over") or {},
                    "likeliest_scores": e.get("scores") or [], "warnings": list(e.get("warnings") or []),
                    "market": None, "kq": None,
                }
                if mk:
                    p = mk.get("p") or [None, None, None]
                    ov = mk.get("over") or {}
                    o25 = ov.get("2.5") if "2.5" in ov else ov.get(2.5)
                    ev["market"] = {"H": p[0], "D": p[1], "A": p[2], "o25": _f(o25[0]) if o25 else None, "source": mk.get("source")}
                    if mk.get("source") == "KALSHI":
                        ask = mk.get("ask") or [None, None, None]
                        ev["kq"] = {"ask": {"h": _f(ask[0]), "d": _f(ask[1]), "a": _f(ask[2])},
                                    "over": {str(k): {"mid": _f(v[0]), "ask": _f(v[1]) if len(v) > 1 else None} for k, v in ov.items()}}
                out.append(ev)
            except Exception:
                log.exception("could not adapt a Champions League slate row")
        raw = dict(raw)
        raw["events"] = out
        return raw

    def slate(self):
        now = time.time()
        with self._slate_lock:
            if self._slate_cache and now - self._slate_cache[0] < SLATE_TTL:
                return self._slate_cache[1]
        try:
            out = self._adapt(self.get_slate())
        except Exception as ex:
            log.exception("champions league slate failed")
            out = {"status": "error", "warnings": [f"{type(ex).__name__}: {ex}"], "events": []}
        out["needs_bootstrap"] = bool(self.needs_bootstrap())
        out["server_time"] = self._now().isoformat()
        with self._slate_lock:
            self._slate_cache = (now, out)
        return out

    def final_score(self, match_id):
        """-> (home goals, away goals) for a settled match (ESPN event id), else None."""
        mid = str(match_id)
        try:
            res = bp._load_json(self._p(bp.TRACK_RES), {}) or {}
            g = res.get(mid)
            if isinstance(g, dict) and g.get("hg") is not None:
                return int(g["hg"]), int(g["ag"])
            for m in reversed(self.matches()):
                if str(m["id"]) == mid and m.get("comp") == bp.CL_SLUG and m.get("goals_ok"):
                    return int(m["hg"]), int(m["ag"])
        except Exception:
            log.exception("could not look up the result of match %s", mid)
        return None


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = CLService()
        return _service


def start_background():
    try:
        get_service().start()
    except Exception:
        log.exception("could not start the cl service")
