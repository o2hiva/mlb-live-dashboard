"""
Men's college basketball (NCAA Division I) for the dashboard: wraps cbb_platform.py (the points model - home points-scored index x away
points-allowed index x league average - see cbb-points-model.md; the embedded model is unchanged).

  * background loops (started once from main.py's lifespan): ESPN scoreboards hourly (the first start downloads about six seasons and
    runs the backtest that tunes the parameters - the slate says "warming up" until then), upcoming games + Kalshi prices + logging of each
    pre-game prediction every 15 minutes, settlement of the logged predictions hourly
  * slate()        : upcoming games with model scores, moneyline, spread / total / team-total ladders and the Kalshi prices (cached 10 s)
  * final_score(id): (home points, away points) once a game is final, else None - used for the scorecard and for grading tracked CBB bets

Data lives in CBB_DATA_DIR (default backend/cbb_data). Railway's disk is wiped on every deploy, so for production point CBB_DATA_DIR at a
Railway Volume (e.g. /data/cbb_data): otherwise every deploy repeats the first download and backtest.
Everything degrades instead of raising: every call returns JSON with a "status" and "warnings".
"""
import logging
import os
import threading
import time

import cbb_platform as bp

log = logging.getLogger("cbb_service")

DATA_DIR = os.environ.get("CBB_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cbb_data")
SLATE_TTL = 10.0


class CbbService(bp.Platform):
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
        self.start_background_refresh(data_s=3600, slate_s=900, results_s=3600, bootstrap=True)
        log.info("cbb service started (data dir %s)", self.d)

    def slate(self):
        now = time.time()
        with self._slate_lock:
            if self._slate_cache and now - self._slate_cache[0] < SLATE_TTL:
                return self._slate_cache[1]
        out = self.get_slate()
        try:
            out["needs_bootstrap"] = bool(self.needs_bootstrap())
        except Exception:
            out["needs_bootstrap"] = None
        with self._slate_lock:
            self._slate_cache = (now, out)
        return out

    def refresh_kalshi(self):
        """Re-pull Kalshi and re-price the slate (the slate refresh already does both)."""
        return self.refresh_slate(kalshi=True)

    def final_score(self, game_id):
        """-> (home points, away points) for a finished game (ESPN event id), else None."""
        gid = str(game_id)
        try:
            res = bp.load_json(os.path.join(self.d, bp.RESULTS_FILE), {}) or {}
            r = res.get(gid)
            if r and r.get("hs") is not None and r.get("as") is not None:
                return int(r["hs"]), int(r["as"])
            for g in reversed(self._games or []):
                if str(g["id"]) == gid:
                    return int(g["hs"]), int(g["as"])
        except Exception:
            log.exception("could not look up the result of game %s", gid)
        return None


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = CbbService()
        return _service


def start_background():
    try:
        get_service().start()
    except Exception:
        log.exception("could not start the cbb service")
