"""
ATP tennis for the dashboard: wraps tennis_platform.py (embedded model + live Kalshi pull, unchanged - see its docstring).

  * background loops (started once from main.py's lifespan):
      - ATP results download (first start builds the ratings: ~11 small CSVs, about a minute), then every 3 hours
      - Kalshi open ATP markets (moneyline / total games / total sets) every 60 seconds -> the live prices in the Tennis tab
      - settlement of the platform's own prediction log (hourly)
  * slate()  : every open Kalshi ATP match with model probabilities next to Kalshi's bid / ask (cached 10 s)
  * final_market_result(ticker): the settled YES/NO of one Kalshi market, used by bet_grading for tracked tennis bets

Data lives in TENNIS_DATA_DIR (default backend/tennis_data). Everything degrades instead of raising: every call returns JSON
with a "status" and "warnings".
"""
import logging
import os
import threading
import time

import tennis_platform as tpf

log = logging.getLogger("tennis_service")

DATA_DIR = os.environ.get("TENNIS_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tennis_data")
KALSHI_SECONDS = int(os.environ.get("TENNIS_KALSHI_SECONDS", "60"))
SLATE_TTL = 10.0


class TennisService(tpf.TennisPlatform):
    def __init__(self, directory=DATA_DIR, **kw):
        os.makedirs(directory, exist_ok=True)
        super().__init__(directory, **kw)
        self._slate_cache = {}
        self._slate_lock = threading.Lock()
        self._started = False
        self._result_cache = {}

    def start(self):
        if self._started:
            return
        self._started = True
        self.start_background_refresh(data_interval_s=3 * 3600, kalshi_interval_s=KALSHI_SECONDS, results_interval_s=3600, bootstrap=True)
        log.info("tennis service started (data dir %s)", self.dir)

    def slate(self, date=None):
        key = date or ""
        now = time.time()
        with self._slate_lock:
            hit = self._slate_cache.get(key)
            if hit and now - hit[0] < SLATE_TTL:
                return hit[1]
        out = self.get_slate(date)
        out["needs_bootstrap"] = bool(out.get("needs_bootstrap"))
        out["server_time"] = self._now().isoformat()
        with self._slate_lock:
            self._slate_cache[key] = (now, out)
            if len(self._slate_cache) > 20:
                self._slate_cache.clear()
        return out

    def final_market_result(self, ticker):
        """-> "yes" | "no" once Kalshi has settled this market, "void" if it settled to neither, None while still open / unreachable."""
        if not ticker:
            return None
        if ticker in self._result_cache:
            return self._result_cache[ticker]
        try:
            m = tpf.kal.fetch_market(ticker, self._kget)
        except Exception:
            log.exception("Kalshi lookup failed for %s", ticker)
            return None
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        if status in ("finalized", "settled", "determined"):
            if result in ("yes", "no"):
                self._result_cache[ticker] = result
                return result
            if result in ("void", "scalar", "voided", "cancelled", "canceled"):
                self._result_cache[ticker] = "void"
                return "void"
        return None


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = TennisService()
        return _service


def start_background():
    try:
        get_service().start()
    except Exception:
        log.exception("could not start the tennis service")
