"""
nfl_fantasy_service.py

Dashboard-facing wrapper around nfl_fantasy_platform.py (the user's NFL fantasy model; that file is deployed UNCHANGED).

  * Two Yahoo leagues are built into the platform (501858 Family Fantasy Football League, 572561 Otuhiva Family League). Their
    tuned parameter files + league files are committed in backend/ffb_data/.
  * The nflverse data (about 70 MB, 2018-now) is NOT committed: on start the service downloads it in the background (about a
    minute, no tuning needed because the tuned parameter files are already there), then refreshes the current season every
    30 minutes.  Until the download finishes the endpoints answer status "loading".
  * Rosters are kept in the database (SyncState['ffb_rosters']) so they survive redeploys, and are passed to the platform
    per request.  Nothing is written to the platform's own roster file.
"""
import json
import logging
import os
import re
import threading

import nfl_fantasy_platform as nfp

log = logging.getLogger("nfl_fantasy_service")

DEFAULT_DIR = os.environ.get("FFB_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffb_data")
ROSTER_KEY = "ffb_rosters"
MAX_NAMES = 60

_service = None
_lock = threading.Lock()


class FFB(nfp.FantasyPlatform):
    def ready(self):
        d = nfp.fpm.data_dir(self.dir)
        return os.path.exists(os.path.join(d, "games.csv")) and os.path.exists(os.path.join(d, f"week_{self.season()}.csv"))

    def state(self):
        return dict(self._boot)

    # ---- roster storage (database, with a file fallback)
    def load_rosters(self):
        try:
            from database import SessionLocal
            from models_db import SyncState
            db = SessionLocal()
            try:
                row = db.get(SyncState, ROSTER_KEY)
                if row and row.value:
                    return json.loads(row.value)
            finally:
                db.close()
        except Exception:
            try:
                with open(os.path.join(self.dir, "ffb_rosters.json")) as f:
                    return json.load(f)
            except Exception:
                pass
        # nothing saved yet: start from the committed seed roster (backend/ffb_data/ffb_rosters_seed.json)
        try:
            with open(os.path.join(self.dir, "ffb_rosters_seed.json")) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_rosters(self, data):
        payload = json.dumps(data, separators=(",", ":"))
        try:
            from database import SessionLocal
            from models_db import SyncState
            db = SessionLocal()
            try:
                row = db.get(SyncState, ROSTER_KEY)
                if row is None:
                    db.add(SyncState(key=ROSTER_KEY, value=payload))
                else:
                    row.value = payload
                db.commit()
                return True
            finally:
                db.close()
        except Exception:
            log.exception("roster DB save failed; using file")
        try:
            with open(os.path.join(self.dir, "ffb_rosters.json"), "w") as f:
                f.write(payload)
            return True
        except Exception:
            log.exception("roster file save failed")
            return False

    def roster(self, league):
        r = self.load_rosters().get(str(league)) or {}
        return list(r.get("players") or []), list(r.get("taken") or [])

    def put_roster(self, league, players, taken=None):
        try:
            self._league(league)
        except KeyError as e:
            return {"status": "error", "warnings": [str(e).strip("'\"")]}

        def ok(lst):
            return isinstance(lst, list) and len(lst) <= MAX_NAMES and all(isinstance(x, str) and 0 < len(x.strip()) <= 60 for x in lst)
        if not ok(players) or (taken is not None and not ok(taken)):
            return {"status": "error", "warnings": [f"players / taken must be lists of at most {MAX_NAMES} names"]}
        data = self.load_rosters()
        old = data.get(str(league)) or {}
        data[str(league)] = {"players": [x.strip() for x in players],
                             "taken": [x.strip() for x in (taken if taken is not None else old.get("taken", []))]}
        if not self.save_rosters(data):
            return {"status": "error", "warnings": ["could not save the roster"]}
        return {"status": "ok", "league": str(league), "players": len(data[str(league)]["players"]), "taken": len(data[str(league)]["taken"])}

    # ---- reads (all guard on data being downloaded)
    def _loading(self):
        return {"status": "loading", "warnings": ["Fantasy data is downloading (first start after a deploy takes 1-2 minutes). Try again shortly."],
                "bootstrap": self.state()}

    def leagues_payload(self):
        out = self.get_leagues()
        if out.get("status") == "ok":
            r = self.load_rosters()
            for l in out["leagues"]:
                e = r.get(l["id"]) or {}
                l["roster_size"], l["taken_size"] = len(e.get("players") or []), len(e.get("taken") or [])
            out["ready"] = self.ready()
            out["bootstrap"] = self.state()
        return out

    def projections_payload(self, league, week=None, pos=None):
        return self.get_projections(league, week, pos) if self.ready() else self._loading()

    @staticmethod
    def _split_ir(players):
        """Names written as 'IR: Name' (or 'OUT: Name') are on the user's injured reserve: kept on the roster list but never
        started and never counted as a roster spot for waiver math."""
        active, ir = [], []
        for p in players:
            m = re.match(r"^\s*(IR|OUT)\s*:\s*(.+)$", p, re.I)
            (ir if m else active).append(m.group(2).strip() if m else p)
        return active, ir

    def lineup_payload(self, league, week=None):
        if not self.ready():
            return self._loading()
        players, _ = self.roster(league)
        active, ir = self._split_ir(players)
        out = self.get_lineup(league, week, players=active)
        out["roster"] = players
        out["ir"] = ir
        return out

    def waivers_payload(self, league, week=None, top=15):
        if not self.ready():
            return self._loading()
        players, taken = self.roster(league)
        active, ir = self._split_ir(players)
        out = self.get_waivers(league, week, top=top, players=active, taken=taken + ir)
        out["roster"] = players
        return out

    def players_payload(self, q, league=None, week=None):
        return self.search_players(q, league, week) if self.ready() else self._loading()


def get_service():
    global _service
    with _lock:
        if _service is None:
            os.makedirs(DEFAULT_DIR, exist_ok=True)
            _service = FFB(DEFAULT_DIR, min_refresh_interval_s=600)
            _service.ensure_leagues()
        return _service


def start_background(interval_minutes=30):
    """Download the nflverse files if missing (no tuning: the tuned parameter files are committed), then keep the
    current season fresh."""
    svc = get_service()
    svc._stop.clear()

    def loop():
        try:
            d = nfp.fpm.data_dir(svc.dir)
            if not (os.path.exists(os.path.join(d, "games.csv")) and os.path.exists(os.path.join(d, f"week_{svc.season()}.csv"))):
                svc.bootstrap(retune=False)
        except Exception:
            log.exception("fantasy bootstrap failed")
        while not svc._stop.is_set():
            try:
                svc.refresh_data()
            except Exception:
                log.exception("fantasy refresh failed")
            svc._stop.wait(interval_minutes * 60)
    t = threading.Thread(target=loop, name="ffb-refresh", daemon=True)
    t.start()
    return t
