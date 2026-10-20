"""
English Championship (EFL) for the dashboard: wraps efl_platform.py (player plus-minus goals model, ESPN data + lineups, football-data.co.uk odds -
the embedded model is unchanged, see its docstring).

  * background loops (started once from main.py's lifespan): ESPN results hourly (the first start downloads about three seasons,
    ~15 minutes; the slate says "warming up" until then), fixtures + confirmed lineups every 15 minutes (this also logs each
    pre-match prediction), football-data odds every 3 hours, settlement of the logged predictions hourly
  * slate()        : upcoming matches with model probabilities (1X2, BTTS, total goals, team goals) - cached 10 s
  * final_score(id): (home goals, away goals) once a match is finished, else None - used for the shadow scorecard and for grading
                     tracked Championship bets

Data lives in EFL_DATA_DIR (default backend/efl_data). Railway's disk is wiped on every deploy, so for production point
EFL_DATA_DIR at a Railway Volume (e.g. /data): otherwise every deploy repeats the ~15 minute first download.
Everything degrades instead of raising: every call returns JSON with a "status" and "warnings".
"""
import logging
import os
import threading
import time

import efl_platform as bp

log = logging.getLogger("efl_service")

DATA_DIR = os.environ.get("EFL_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "efl_data")
SLATE_TTL = 10.0


class EFLService(bp.ChampionshipPlatform):
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
        self.start_background_refresh(data_s=3600, fixtures_s=900, odds_s=10800, results_s=3600, bootstrap=True)
        log.info("efl service started (data dir %s)", self.dir)

    def slate(self):
        now = time.time()
        with self._slate_lock:
            if self._slate_cache and now - self._slate_cache[0] < SLATE_TTL:
                return self._slate_cache[1]
        out = self.get_slate()
        out["needs_bootstrap"] = bool(self.needs_bootstrap())
        out["server_time"] = self._now().isoformat()
        with self._slate_lock:
            self._slate_cache = (now, out)
        return out

    def final_score(self, match_id):
        """-> (home goals, away goals) for a finished match (ESPN event id), else None."""
        mid = str(match_id)
        try:
            res = bp.load_json_retry(self._p(bp.RESULTS_FILE), {}) or {}
            g = res.get(mid)
            if isinstance(g, list) and len(g) == 2:
                return int(g[0]), int(g[1])
            for m in reversed(self.matches()):
                if str(m["id"]) == mid:
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
            _service = EFLService()
        return _service


def start_background():
    try:
        get_service().start()
    except Exception:
        log.exception("could not start the efl service")
