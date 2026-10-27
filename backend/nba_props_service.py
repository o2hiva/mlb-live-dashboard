"""
NBA player props (Points / Rebounds / Assists / PRA) for the dashboard.

Wraps nba_platform.py (embedded, unchanged - see its docstring for the model) and adds what the dashboard
needs on top:

  * PROPS-ONLY refresh loop (collect new games + box scores from ESPN, rebuild the model context when the
    files changed). The platform's game-side work (player-value fits for spreads/moneylines) is skipped.
  * PROJECTED LINEUPS: a game's player predictions no longer wait for ESPN to post the starting lineups. Until both lineups
    (5 flagged starters each) are posted, rosters and starters come from each team's PREVIOUS game (that game's starters, next man
    up for anyone Out) and the game is flagged "projected lineup"; the posted lineups replace the projection when they appear.
  * Full survival curve per (player, stat) so the browser can recompute P(over) for any line instantly,
    exactly like every other prop in the dashboard.
  * Final-box-score lookup used by bet_grading for tracked NBA prop bets.

  * DATA GATE ("collect first, predict later"): the current season is collected from ESPN every 30 minutes. A game's player
    predictions are only released once BOTH teams have played NBA_MIN_TEAM_GAMES (default 1, 0 = off) completed regular-season
    games this season (raise it with that variable to wait for more of this season's data); until then the game is listed as "collecting data" with each team's game count. (Players also need >= 10 games
    of history - last season counts - and the minutes-context features need ~5 team games, so early-season numbers lean on last year.)
  * Minutes context from nba_platform.py (teammates out / blowout risk / back-to-back / late season) is applied automatically when
    nba_prop_params.json has a validated "minutes_context" block; each player row then carries base vs adjusted minutes and flags.

Data lives in NBA_DATA_DIR (default: backend/nba_data). That folder is seeded from git with the box scores for the
last seasons and nba_prop_params.json; the current season is collected on the server.

Everything degrades instead of raising: endpoints always return JSON with a "status" and "warnings".
"""
import datetime
import logging
import os
import threading
import time

import numpy as np

import nba_platform as npf          # installs the embedded model modules (core_nba_player_props, ...) first
import backtest_nba_player_props as bp
import core_nba_player_props as cp
import core_nba_moneyline as mon
import core_nba_spread as cs
import nba_collect_boxscores as bx

log = logging.getLogger("nba_props_service")

DATA_DIR = os.environ.get("NBA_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "nba_data")
REFRESH_SECONDS = 30 * 60
LINEUP_FETCH_LEAD = datetime.timedelta(hours=3)     # don't poll a game's summary until tip-off is this close
LINEUP_TTL_PENDING = 90.0                            # seconds between re-checks while lineups aren't out yet
LINEUP_TTL_CONFIRMED = 600.0                         # seconds between re-checks once confirmed (late scratches)
PAYLOAD_TTL = 20.0
STATS = ("pts", "reb", "ast", "pra")
MAX_SF = 100
MIN_TEAM_GAMES = int(os.environ.get("NBA_MIN_TEAM_GAMES", "1"))     # completed regular-season games each team needs before its games get predictions
DAY_SETTLE_DAYS = 2          # a scoreboard day counts as fully collected only once it is this many (UTC) days old - late West-Coast games


def collect_games_safe(season, sleep=0.15, directory=".", quiet=False):
    """Same job as nba_collect_games.collect_season, with one fix for a server running in UTC: the original marked a day
    "done" as soon as the UTC date rolled over (8 pm US Eastern), so games still in progress that evening - the late
    tip-offs - were never collected. Here a day is marked done only DAY_SETTLE_DAYS later; until then it is simply re-fetched
    (a couple of cheap requests per refresh)."""
    cg = mon.collector
    say = (lambda *a, **k: None) if quiet else print
    path = os.path.join(directory, cg.progress_file_for(season))
    progress = cg.load_progress(path)
    done = set(progress["completed_days"])
    start, end = cg.season_window(season)
    today = datetime.date.today()
    settled_before = today - datetime.timedelta(days=DAY_SETTLE_DAYS - 1)
    days = [d for d in cg.daterange(start, end) if d.isoformat() not in done]
    say(f"{season}: {len(done)} day(s) already collected, {len(days)} to check ({start} .. {end}).")
    for i, d in enumerate(days):
        if d > today:
            break
        try:
            data = cg.get_day(d)
        except Exception as e:                    # network trouble: this day is retried on the next refresh
            say(f"  {d}: error, will retry next run: {e}")
            continue
        for ev in data.get("events") or []:
            g = cg.parse_event(ev)
            if g is None or not g["completed"] or g["home_score"] is None or g["away_score"] is None:
                continue
            progress["games"][g["game_id"]] = g
        if d < settled_before:
            progress["completed_days"].append(d.isoformat())
        if (i + 1) % 30 == 0:
            cg.save_progress(progress, path)
        time.sleep(sleep)
    cg.save_progress(progress, path)
    say(f"{season}: {len(progress['games'])} completed game(s) cached -> {path}")


class NBAProps(npf.NBAPlatform):
    def __init__(self, directory=DATA_DIR, **kw):
        os.makedirs(directory, exist_ok=True)
        kw.setdefault("log_predictions", False)     # shadow-mode log files are not used by the dashboard
        kw.setdefault("collect_games", collect_games_safe)
        super().__init__(directory, **kw)
        self._lineups = {}                          # game_id -> (fetched_epoch, info)
        self._lineup_lock = threading.Lock()
        self._payloads = {}                         # date -> (epoch, payload)
        self._warm_thread = None
        self._gate_lock = threading.Lock()
        self._gate_cache = {}                       # season -> ((mtime, size), {team_id: games played})

    # ---------------------------------------------------------------- refresh (props only)
    def refresh_data(self, season=None, refit=False):
        started = time.time()
        lock = npf.FileLock(os.path.join(self.dir, ".nba_refresh.lock"), timeout=0.0, stale_after=1800)
        try:
            lock.__enter__()
        except npf.LockBusy:
            return {"status": "busy"}
        try:
            season = season or mon.season_for_date(self._today_fn())
            self._collect_games(season, directory=self.dir, quiet=True)
            self._collect_boxes(season, self.dir, quiet=True)
            self._props_context(season)             # rebuilds only if a data file changed
            info = {"status": "ok", "season": season, "duration_s": round(time.time() - started, 1),
                    "finished_at": self._now().isoformat(timespec="seconds")}
        except Exception as e:
            log.exception("NBA refresh failed")
            info = {"status": "error", "error": f"{type(e).__name__}: {e}",
                    "finished_at": self._now().isoformat(timespec="seconds")}
        finally:
            lock.release()
        self._refresh_info["last"] = info
        return info

    def start(self):
        """Background: build the model context once right away, then refresh every 30 minutes."""
        def loop():
            while not self._stop.is_set():
                try:
                    self._props_context(mon.season_for_date(self._today_fn()))
                except Exception:
                    log.exception("NBA initial context build failed")
                self.refresh_data()
                self._stop.wait(REFRESH_SECONDS)
        t = threading.Thread(target=loop, name="nba-props", daemon=True)
        t.start()
        self._warm_thread = t
        return t

    # ---------------------------------------------------------------- lineups
    @staticmethod
    def _starters_from_summary(data):
        """-> {team_id: [starter player ids]} from an ESPN game-summary JSON, plus the game state."""
        out = {}
        for block in (data.get("boxscore") or {}).get("players") or []:
            tid = str((block.get("team") or {}).get("id"))
            ids = []
            for sb in block.get("statistics") or []:
                for r in sb.get("athletes") or []:
                    if r.get("starter"):
                        ids.append(str((r.get("athlete") or {}).get("id")))
            out[tid] = ids
        comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
        state = (((comp.get("status") or {}).get("type") or {}).get("state")) or None
        return out, state

    def _lineup(self, game_id, tip, now):
        """-> {"confirmed": bool, "starters": {team_id: [ids]}, "state": pre|in|post|None, "error": str|None}."""
        epoch = time.time()
        with self._lineup_lock:
            hit = self._lineups.get(game_id)
        if hit:
            ttl = LINEUP_TTL_CONFIRMED if hit[1]["confirmed"] else LINEUP_TTL_PENDING
            if epoch - hit[0] < ttl:
                return hit[1]
        started = bool(tip and now >= tip)
        if not started and tip and tip - now > LINEUP_FETCH_LEAD:
            return {"confirmed": False, "starters": {}, "state": "pre", "error": None}
        info = {"confirmed": False, "starters": {}, "state": None, "error": None}
        try:
            data = bx.get_summary(game_id, retries=1)
            starters, state = self._starters_from_summary(data)
            info.update({"starters": starters, "state": state})
            both = len([t for t, ids in starters.items() if len(ids) >= 5]) >= 2
            info["confirmed"] = bool(both or (state in ("in", "post")))
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
            if hit:
                return hit[1]
            if started:
                info["confirmed"] = True       # can't verify but the game is underway: don't hide it forever
        with self._lineup_lock:
            self._lineups[game_id] = (epoch, info)
        return info

    # ---------------------------------------------------------------- data gate
    def team_game_counts(self, season):
        """-> {team_id: completed regular-season games this season}, from the collected games file."""
        path = os.path.join(self.dir, f"nba_games_{season}.json")
        try:
            st = os.stat(path)
        except OSError:
            return {}
        key = (st.st_mtime_ns, st.st_size)
        with self._gate_lock:
            hit = self._gate_cache.get(season)
            if hit and hit[0] == key:
                return hit[1]
        data = npf.load_json_retry(path, {}) or {}
        counts = {}
        for g in (data.get("games") or {}).values():
            if g.get("season_type") != mon.REGULAR_SEASON_TYPE or not g.get("completed"):
                continue
            for tid in (str(g.get("home_id")), str(g.get("away_id"))):
                counts[tid] = counts.get(tid, 0) + 1
        with self._gate_lock:
            self._gate_cache[season] = (key, counts)
        return counts

    def gate_info(self, season):
        """League-wide progress toward the gate (for the banner and /api/nba/health)."""
        counts = self.team_game_counts(season)
        league = set()
        try:                                        # the 30 real teams = those with a full-ish schedule last season
            prev = npf.load_json_retry(os.path.join(self.dir, f"nba_games_{season - 1}.json"), {}) or {}
            n = {}
            for g in (prev.get("games") or {}).values():
                if g.get("season_type") == mon.REGULAR_SEASON_TYPE:
                    for tid in (str(g.get("home_id")), str(g.get("away_id"))):
                        n[tid] = n.get(tid, 0) + 1
            league = {t for t, c in n.items() if c >= 40}
        except Exception:
            pass
        league = league or set(counts)
        played = [counts.get(t, 0) for t in league] or [0]
        need = MIN_TEAM_GAMES
        return {"min_team_games": need, "teams_total": len(league), "teams_ready": sum(1 for x in played if x >= need),
                "fewest_games": min(played), "most_games": max(played), "season": season,
                "message": ("data gate off" if need <= 0 else
                            ("every team has enough games - predictions are on" if min(played) >= need else
                             f"collecting data: predictions open per game once both teams have played {need} games "
                             f"({sum(1 for x in played if x >= need)}/{len(played)} teams there so far)"))}

    def health(self):
        h = super().health()
        try:
            h["gate"] = self.gate_info(h.get("season") or mon.season_for_date(self._today_fn()))
        except Exception as e:
            h["gate"] = {"error": f"{type(e).__name__}: {e}"}
        return h

    # ---------------------------------------------------------------- survival curves
    @staticmethod
    def _sf(mu, disp):
        """sf[i] = P(X >= i+1) for i in 0..K-1 (so P(X >= k) = sf[k-1])."""
        k_max = int(min(MAX_SF, max(12, mu * 2.2 + 12)))
        ks = np.arange(1, k_max + 1)
        over = bp.over_with(ks - 1, np.full(k_max, float(mu)), disp)      # P(X > k-1) = P(X >= k)
        out = [round(float(x), 4) for x in np.clip(over, 0.0, 1.0)]
        while len(out) > 1 and out[-1] <= 0.0005:
            out.pop()
        return out

    # ---------------------------------------------------------------- payload for the dashboard
    def games_payload(self, date=None):
        """One entry per regular-season game on the date; players only for games whose lineups are out."""
        try:
            d = self._parse_date(date) if date else self._today_fn()
        except Exception:
            return {"status": "error", "warnings": ["bad date - use YYYYMMDD or YYYY-MM-DD"], "games": []}
        key = d.isoformat()
        with self._plock:
            hit = self._payloads.get(key)
        if hit and time.time() - hit[0] < PAYLOAD_TTL:
            return hit[1]

        warnings, now = [], self._now()
        try:
            rows, meta, row_warnings, status = self.props_rows(d)
            warnings += row_warnings
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "nba_prop_params.json" in msg or "FileNotFound" in msg:
                msg = "NBA model files are not on the server yet (nba_prop_params.json / box scores)"
            return {"status": "error", "date": key, "warnings": [msg], "games": []}

        events, _age = self._scoreboard(d, [])
        games = {}
        for ev in events or []:
            g = mon.collector.parse_event(ev)
            if g is None or g["season_type"] != mon.REGULAR_SEASON_TYPE:
                continue
            tip = cs.tip_off({"date": g["date"]})
            games[g["game_id"]] = {
                "game_id": g["game_id"], "tip_off": tip.isoformat(timespec="minutes") if tip else None,
                "away_team": g["away_name"], "home_team": g["home_name"],
                "away_team_id": g["away_id"], "home_team_id": g["home_id"],
                "completed": g["completed"], "started": bool(tip and now >= tip),
                "lineup_confirmed": False, "lineup_basis": None, "players": [],
                "_tip": tip,
            }

        season = meta.get("season") or mon.season_for_date(d)
        counts = self.team_game_counts(season)
        for g in games.values():
            hg, ag = counts.get(str(g["home_team_id"]), 0), counts.get(str(g["away_team_id"]), 0)
            g["team_games"] = {"home": hg, "away": ag, "needed": MIN_TEAM_GAMES}
            g["data_ready"] = bool(MIN_TEAM_GAMES <= 0 or (hg >= MIN_TEAM_GAMES and ag >= MIN_TEAM_GAMES))

        by_game = {}
        for r in rows:
            by_game.setdefault(r["game_id"], {}).setdefault(r["player_id"], []).append(r)

        for gid, g in games.items():
            if g["completed"] or not g["data_ready"]:
                continue                                  # finished, or still collecting this season's data: no lineup polling, no players
            info = self._lineup(gid, g["_tip"], now)
            g["lineup_confirmed"] = bool(info["confirmed"])
            # Predictions no longer wait for the posted lineups: until ESPN publishes them the rosters and starters come from each team's
            # PREVIOUS game (nba_platform: last game's starters, next man up for anyone Out). Once both lineups are posted the real
            # starters replace the projection.
            g["lineup_basis"] = "confirmed" if g["lineup_confirmed"] else "previous game"
            if info.get("error"):
                warnings.append(f"lineup check for game {gid}: {info['error']}")
            starters = {pid for ids in info["starters"].values() for pid in ids} if g["lineup_confirmed"] else None
            for pid, prs in by_game.get(gid, {}).items():
                ref = prs[0]
                props = {}
                for r in prs:
                    disp = r["disp"]
                    props[r["stat"]] = {"projection": r["mu"], "fair_line": r["fair_line"], "last10": r["last10"],
                                        "opp_index": r["opp_index"], "sf": self._sf(r["mu"], disp)}
                if not all(s in props for s in STATS):
                    continue
                g["players"].append({
                    "player_id": pid, "player": ref["player"], "team": ref["team"], "team_id": ref["team_id"],
                    "opp": ref["opp"], "home": bool(ref["home"]),
                    "starter": (pid in starters) if starters is not None else (ref.get("role") == "S"),
                    "started_last_game": ref.get("started_last"), "starts_last5": ref.get("starts_last5"), "minutes_last_game": ref.get("min_last"),
                    "position_group": ref["group"], "projected_minutes": ref["emin"],
                    "projected_minutes_base": ref.get("emin_base"), "context": ref.get("ctx"),
                    "games_of_history": ref["n_prior"], "injury_status": ref["status"],
                    "teammates_out": [o["name"] for o in (ref.get("teammates_out") or [])][:4], "props": props,
                })
            g["players"].sort(key=lambda p: (p["home"], not p["starter"], -p["projected_minutes"]))

        glist = sorted(({k: v for k, v in g.items() if k != "_tip"} for g in games.values()),
                       key=lambda g: (g["tip_off"] or "", g["game_id"]))
        try:
            ctx_on = bool((self._props_context(season) or {}).get("ctx_model"))
        except Exception:
            ctx_on = False
        gate = self.gate_info(season)
        collecting = any(not g["data_ready"] and not g["completed"] for g in glist)
        payload = {"status": status, "date": key, "season": meta.get("season"), "warnings": warnings, "games": glist,
                   "data_through": meta.get("data_through"), "gate": gate, "collecting": collecting,
                   "minutes_context": ctx_on,
                   "note": ("Minutes are adjusted for teammates out, blowout risk and (when validated) back-to-backs / late-season status; "
                            if ctx_on else "Projection assumes the player plays his usual role; teammates being out (usage up) is NOT modeled. ")
                           + "Shadow-mode model: validated for calibration, NOT against real sportsbook lines."}
        with self._plock:
            self._payloads[key] = (time.time(), payload)
        return payload

    def lineup_debug(self, game_id):
        try:
            data = bx.get_summary(game_id, retries=1)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        starters, state = self._starters_from_summary(data)
        return {"game_id": game_id, "state": state, "starters": starters,
                "boxscore_player_blocks": len((data.get("boxscore") or {}).get("players") or []),
                "top_level_keys": sorted(data.keys())}

    # ---------------------------------------------------------------- grading support
    def final_player_stat(self, game_id, player_id):
        """-> ("pending", None) | ("dnp", None) | ("final", {"pts","reb","ast","pra"}) for one ESPN game + player."""
        try:
            data = bx.get_summary(game_id, retries=2)
        except Exception:
            return "pending", None
        comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
        state = (((comp.get("status") or {}).get("type") or {}).get("state")) or None
        if state != "post":
            return "pending", None
        for block in (data.get("boxscore") or {}).get("players") or []:
            for sb in block.get("statistics") or []:
                labels = sb.get("labels") or []
                for r in sb.get("athletes") or []:
                    if str((r.get("athlete") or {}).get("id")) != str(player_id):
                        continue
                    p = bx.parse_player_row(r, labels)
                    if p["dnp"] or not p["min"]:
                        return "dnp", None
                    if p["pts"] is None or p["reb"] is None or p["ast"] is None:
                        return "pending", None
                    return "final", {"pts": p["pts"], "reb": p["reb"], "ast": p["ast"],
                                     "pra": p["pts"] + p["reb"] + p["ast"]}
        return "dnp", None          # final box score exists but the player isn't in it


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = NBAProps()
        return _service


def start_background():
    try:
        get_service().start()
    except Exception:
        log.exception("could not start the NBA props service")
