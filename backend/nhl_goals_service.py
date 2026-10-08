"""
nhl_goals_service.py

Dashboard-facing wrapper around nhl_platform.py (the user's NHL goals model: team goals + game totals with an overtime /
shootout process, goalie + back-to-back adjustments, and an independent per-period goals model).

What this adds on top of NHLPlatform (which stays UNCHANGED, copied verbatim as backend/nhl_platform.py):
  * games_payload(date)   one JSON blob per date for the NHL "Games" tab:
        - moneyline win probability for each team (regulation + the model's own overtime / shootout process)
        - predicted goals for each team, full probability tables for each team's goals and for the game total
        - period 1 / 2 / 3 combined-goals probability tables (the period model, regulation only)
        - which goalies are expected, rest flags
    Probabilities are sent as pmf tables (P(X = k)), so the browser can price ANY line instantly:
        over L = sum(pmf[k], k > L), under L = sum(pmf[k], k < L), push = pmf[L] on whole-number lines.
  * final_result(game_id) the settled score for bet grading (final goals WITHOUT the phantom shootout goal, because
        sportsbooks settle totals that way; plus the shootout winner and the goals in each period).
  * start_background(): refreshes results/schedule/boxscores on a timer (NHLPlatform.refresh_data).

The platform's own prediction log, Kalshi fetcher and bet suggestions are NOT used here: the dashboard takes the market
price you type in and sizes the Kelly wager itself, like every other prop tab.

Data files live in NHL_DATA_DIR (default: backend/nhl_data). They are seeded from the user's NHL folder
(nhl_goals_<season>.json, nhl_goals_box_<season>.json for the current and previous season, nhl_goals_params.json,
nhl_period_params.json) and kept fresh by refresh_data().
"""
import datetime
import json
import logging
import math
import os
import threading

import nhl_platform as npf

log = logging.getLogger("nhl_goals_service")
nhl = npf.nhl
nper = npf.nper

try:
    from zoneinfo import ZoneInfo
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:       # pragma: no cover - zoneinfo missing on very old pythons
    _PACIFIC = None

DEFAULT_DIR = os.environ.get("NHL_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "nhl_data")
SO_HOME_SHARE = 0.5     # shootout is treated as a coin flip (the platform does the same)


def _r(xs, nd=5):
    return [round(float(x), nd) for x in xs]


def _trim(pmf, tail=1e-5, min_len=4):
    """Drop a negligible tail so the payload stays small; the browser treats missing entries as probability 0."""
    n = len(pmf)
    while n > min_len and sum(pmf[n - 1:]) < tail:
        n -= 1
    return pmf[:n]


class NHLGoals(npf.NHLPlatform):
    _shadow_lock = threading.RLock()

    def today(self):
        now = self._now()          # naive UTC from the platform
        if _PACIFIC is not None:
            return now.replace(tzinfo=datetime.timezone.utc).astimezone(_PACIFIC).date().isoformat()
        return super().today()

    # ------------------------------------------------------------------ model helpers
    def _moneyline(self, combos, params):
        """P(home wins the game) averaged over the goalie scenarios.  Regulation winner, plus ties split by the platform's
        overtime process (q_ot = share of ties decided in OT, w_home = home share of OT wins) and a coin-flip shootout."""
        shape = params.get("r_team")
        q, w = params.get("q_ot", 0.5), params.get("w_home", 0.5)
        tot = 0.0
        for wt, mh, ma in combos:
            ph, pa = nhl.goal_pmf(mh, shape), nhl.goal_pmf(ma, shape)
            win = tie = 0.0
            for i, x in enumerate(ph):
                for j, y in enumerate(pa):
                    if i > j:
                        win += x * y
                    elif i == j:
                        tie += x * y
            tot += wt * (win + tie * (q * w + (1 - q) * SO_HOME_SHARE))
        return tot

    def _period_model(self, season, date):
        """-> {game_id: {periods: [...]}} for the period model, or {} on failure."""
        out = {}
        try:
            pparams, validated = nper.load_params(self.dir)
            pst, n_games, upcoming = self._period_live(season, pparams)
            for u in upcoming:
                if u["date"] != date:
                    continue
                mu_h, mu_a, lg = pst.means(u["home"], u["away"])
                ps, t2 = nper.game_periods(mu_h, mu_a, pparams["shapes"])
                periods = []
                for i in range(3):
                    d = ps[i]
                    periods.append({"period": i + 1, "mu_home": mu_h[i], "mu_away": mu_a[i], "mu_total": mu_h[i] + mu_a[i],
                                    "fair_line": nhl.fair_line(d["total"]), "p_goal": 1 - d["total"][0],
                                    "pmf_total": _r(_trim(d["total"]), 5)})
                out[str(u["id"])] = {"periods": periods, "validated": validated, "games_with_period_data": n_games}
        except Exception as e:      # the period model is optional - never break the game payload over it
            log.exception("period model failed")
            out["_error"] = f"{type(e).__name__}: {e}"
        return out

    # ------------------------------------------------------------------ the dashboard payload
    def games_payload(self, date=None):
        try:
            with self._lock:
                date = date or self.today()
                season = nhl.season_code_for(date)
                params, validated = nhl.load_params(self.dir)
                st, games, upcoming, have_prev = self._live_state(season, params)
                starters = self._read_input(npf.STARTERS_FILE)
                warnings = []
                if not validated:
                    warnings.append("No nhl_goals_params.json: using UNVALIDATED default parameters.")
                per = self._period_model(season, date)
                perr = per.pop("_error", None)
                if perr:
                    warnings.append(f"Period model unavailable: {perr}")
                now = self._now()
                out_games = []
                for u in sorted([x for x in upcoming if x["date"] == date], key=lambda x: x.get("start_utc") or ""):
                    home, away = u["home"], u["away"]
                    pred = nhl.predict_game(st, u, params, starters)
                    mu_h0, mu_a0, lg = st.means(home, away)
                    scn_h = st.scenarios(home, u.get("rest_h") == 1, starters.get(home))
                    scn_a = st.scenarios(away, u.get("rest_a") == 1, starters.get(away))
                    row = {"mu_h0": mu_h0, "mu_a0": mu_a0, "lg": lg, "scn_h": scn_h, "scn_a": scn_a,
                           "rest_h": u.get("rest_h"), "rest_a": u.get("rest_a")}
                    ml_home = self._moneyline(nhl.row_combos(row, params), params)
                    started = False
                    try:
                        started = bool(u.get("start_utc")) and datetime.datetime.strptime(u["start_utc"][:19], "%Y-%m-%dT%H:%M:%S") <= now
                    except ValueError:
                        pass
                    pp = per.get(str(u["id"]))
                    out_games.append({
                        "game_id": u["id"], "date": date, "start_utc": u.get("start_utc"), "started": started, "final": False,
                        "home": home, "away": away, "trusted": pred["trusted"],
                        "home_games": pred["home_games"], "away_games": pred["away_games"],
                        "ml_home": ml_home, "ml_away": 1 - ml_home,
                        "mu_home": pred["mu_home"], "mu_away": pred["mu_away"], "mu_total": pred["mu_total"],
                        "fair_line": pred["fair_line"],
                        "pmf_home": _r(_trim(pred["pmf"]["home"], min_len=6)),
                        "pmf_away": _r(_trim(pred["pmf"]["away"], min_len=6)),
                        "pmf_total": _r(_trim(pred["pmf"]["total"], min_len=8)),
                        "goalies": pred["goalies"], "rest": pred["rest"],
                        "periods": pp["periods"] if pp else None,
                    })
                # games already finished on this date: show the result (no prediction is made for a played game)
                for g in games:
                    if g.get("date") != date:
                        continue
                    out_games.append({
                        "game_id": g["id"], "date": date, "start_utc": g.get("start_utc"), "started": True, "final": True,
                        "home": g["home"], "away": g["away"], "hg": g["hg"], "ag": g["ag"], "ended_in": g.get("period"),
                        "so_winner": g.get("so"), "period_goals": g.get("per"),
                    })
                out_games.sort(key=lambda x: (x.get("start_utc") or "", x["game_id"]))
                if not out_games:
                    warnings.append(f"No NHL games found on {date}.")
                return npf.clean({"status": "ok", "date": date, "season": season, "validated": validated, "games": out_games,
                                  "warnings": warnings, "generated_at": now.isoformat(timespec="seconds") + "Z",
                                  "ml_note": "Moneyline = regulation result plus the model's overtime process and a 50/50 shootout; not yet checked against sportsbook prices."})
        except Exception as e:
            log.exception("games_payload failed")
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"], "games": []}

    # ------------------------------------------------------------------ shadow scorecard
    # Every not-yet-started game's prediction is logged (latest pre-game version wins) in SyncState['nhl_shadow_log']
    # (database, so it survives redeploys; falls back to a file in the data dir). shadow_scorecard() grades the logged
    # predictions against the final scores.  Nothing here touches bets or markets.
    SHADOW_KEY = "nhl_shadow_log"

    def _shadow_load(self):
        try:
            from database import SessionLocal
            from models_db import SyncState
            db = SessionLocal()
            try:
                row = db.get(SyncState, self.SHADOW_KEY)
                if row and row.value:
                    return json.loads(row.value)
                return {}
            finally:
                db.close()
        except Exception:
            try:
                with open(os.path.join(self.dir, "nhl_shadow_log.json")) as f:
                    return json.load(f)
            except Exception:
                return {}

    def _shadow_save(self, data):
        payload = json.dumps(data, separators=(",", ":"))
        try:
            from database import SessionLocal
            from models_db import SyncState
            db = SessionLocal()
            try:
                row = db.get(SyncState, self.SHADOW_KEY)
                if row is None:
                    db.add(SyncState(key=self.SHADOW_KEY, value=payload))
                else:
                    row.value = payload
                db.commit()
                return
            finally:
                db.close()
        except Exception:
            log.exception("shadow log DB save failed; using file")
        try:
            with open(os.path.join(self.dir, "nhl_shadow_log.json"), "w") as f:
                f.write(payload)
        except Exception:
            log.exception("shadow log file save failed")

    def shadow_log(self, dates=None):
        """Log the current pre-game prediction for every not-yet-started, trusted game on `dates` (default today+tomorrow
        Pacific).  Returns the number of games logged."""
        with self._shadow_lock:
            if not dates:
                t = datetime.date.fromisoformat(self.today())
                dates = [t.isoformat(), (t + datetime.timedelta(days=1)).isoformat()]
            data = self._shadow_load()
            n = 0
            for d in dates:
                pay = self.games_payload(d)
                for g in pay.get("games", []):
                    if g.get("final") or g.get("started") or not g.get("trusted"):
                        continue
                    per = g.get("periods") or []
                    data[str(g["game_id"])] = {
                        "d": d, "h": g["home"], "a": g["away"], "ml": round(g["ml_home"], 4),
                        "mh": round(g["mu_home"], 3), "ma": round(g["mu_away"], 3),
                        "ph": [round(x, 4) for x in g["pmf_home"]], "pa": [round(x, 4) for x in g["pmf_away"]],
                        "pt": [round(x, 4) for x in g["pmf_total"]],
                        "pp": [[round(x, 4) for x in q["pmf_total"]] for q in per] if len(per) == 3 else None,
                        "mp": [round(q["mu_total"], 3) for q in per] if len(per) == 3 else None,
                        "at": self._now().isoformat(timespec="seconds"),
                    }
                    n += 1
            if n:
                self._shadow_save(data)
            return n

    def shadow_scorecard(self):
        try:
            data = self._shadow_load()
            rows = []
            for gid, r in data.items():
                res = self.final_result(gid)
                if res:
                    rows.append((r, res))
            out = {"status": "ok", "logged": len(data), "graded": len(rows), "pending": len(data) - len(rows)}
            if not rows:
                out["note"] = "No graded games yet - predictions are logged automatically before each game and graded once it is final."
                return npf.clean(out)

            def ll(pmf, k):
                p = pmf[k] if 0 <= k < len(pmf) else 0.0
                return -math.log(max(p, 1e-4))

            def over(pmf, line):
                return sum(x for i, x in enumerate(pmf) if i > line)

            n = len(rows)
            # moneyline
            br = lg = acc = 0.0
            buckets = {}
            for r, res in rows:
                y = 1.0 if res["winner"] == "home" else 0.0
                p = r["ml"]
                br += (p - y) ** 2
                lg += -math.log(max(p if y else 1 - p, 1e-4))
                fav_home = p >= 0.5
                fp = p if fav_home else 1 - p
                fwin = (y == 1.0) if fav_home else (y == 0.0)
                acc += 1.0 if fwin else 0.0
                lab = "50-55%" if fp < .55 else "55-60%" if fp < .60 else "60-65%" if fp < .65 else "65%+"
                b = buckets.setdefault(lab, [0, 0.0, 0.0])
                b[0] += 1; b[1] += fp; b[2] += 1.0 if fwin else 0.0
            ml = {"n": n, "brier": br / n, "brier_coinflip": 0.25, "log_loss": lg / n, "log_loss_coinflip": math.log(2),
                  "favorite_win_rate": acc / n,
                  "calibration": [{"bucket": k, "n": v[0], "predicted": v[1] / v[0], "actual": v[2] / v[0]}
                                  for k, v in sorted(buckets.items())]}
            # team goals + total
            tg_ll = tt_ll = 0.0
            tg_pred = tg_act = 0.0
            tt_pred = tt_act = 0.0
            lines = {}
            for r, res in rows:
                tg_ll += ll(r["ph"], res["hg"]) + ll(r["pa"], res["ag"])
                tg_pred += r["mh"] + r["ma"]; tg_act += res["hg"] + res["ag"]
                tot = res["hg"] + res["ag"]
                tt_ll += ll(r["pt"], tot)
                tt_pred += r["mh"] + r["ma"]; tt_act += tot
                for L in (4.5, 5.5, 6.5):
                    d = lines.setdefault(L, [0, 0.0, 0.0])
                    d[0] += 1; d[1] += over(r["pt"], L); d[2] += 1.0 if tot > L else 0.0
            goals = {"n": n, "team_log_loss": tg_ll / (2 * n), "total_log_loss": tt_ll / n,
                     "avg_predicted_total": tt_pred / n, "avg_actual_total": tt_act / n,
                     "total_over": [{"line": L, "n": v[0], "predicted": v[1] / v[0], "actual": v[2] / v[0]}
                                    for L, v in sorted(lines.items())]}
            # periods (regulation only)
            per = []
            for i in range(3):
                m = pr = ac = 0
                pg_ = go_p = go_a = o15_p = o15_a = 0.0
                for r, res in rows:
                    pgl = res.get("period_goals")
                    if not r.get("pp") or not pgl or len(pgl.get("home", [])) <= i or len(pgl.get("away", [])) <= i:
                        continue
                    a = pgl["home"][i] + pgl["away"][i]
                    m += 1
                    pr += r["mp"][i]; ac += a
                    go_p += 1 - r["pp"][i][0]; go_a += 1.0 if a >= 1 else 0.0
                    o15_p += over(r["pp"][i], 1.5); o15_a += 1.0 if a >= 2 else 0.0
                if m:
                    per.append({"period": i + 1, "n": m, "avg_predicted": pr / m, "avg_actual": ac / m,
                                "p_goal_predicted": go_p / m, "p_goal_actual": go_a / m,
                                "over15_predicted": o15_p / m, "over15_actual": o15_a / m})
            out.update({"moneyline": ml, "goals": goals, "periods": per})
            return npf.clean(out)
        except Exception as e:
            log.exception("shadow_scorecard failed")
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    # ------------------------------------------------------------------ grading support
    def final_result(self, game_id):
        """Settled result for one NHL game id, or None while it is not final (or not in the saved data)."""
        try:
            gid = int(game_id)
            start = gid // 1000000
            season = f"{start}{start + 1}"
            games, _ = nhl.load_games(self.dir, season)
            for g in games:
                if int(g["id"]) == gid:
                    winner = None
                    if g["hg"] != g["ag"]:
                        winner = "home" if g["hg"] > g["ag"] else "away"
                    elif g.get("so") in ("home", "away"):
                        winner = g["so"]
                    return {"home": g["home"], "away": g["away"], "hg": g["hg"], "ag": g["ag"], "ended_in": g.get("period"),
                            "winner": winner, "period_goals": g.get("per")}
        except Exception:
            log.exception("final_result failed for %s", game_id)
        return None

    def final_result_fresh(self, game_id):
        """final_result(), refreshing the saved data once first if the game is not in it yet."""
        res = self.final_result(game_id)
        if res is None:
            self.refresh_data()
            res = self.final_result(game_id)
        return res


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            os.makedirs(DEFAULT_DIR, exist_ok=True)
            _service = NHLGoals(DEFAULT_DIR, log_predictions=False, min_refresh_interval_s=300)
        return _service


def start_background(interval_minutes=60, first_delay_s=45):
    """Refresh results / schedule / boxscores on a timer (a refresh is ~35 API calls plus any new boxscores)."""
    svc = get_service()

    def _shadow_loop():
        import time as _t
        _t.sleep(first_delay_s + 90)
        while True:
            try:
                svc.shadow_log()
            except Exception:
                log.exception("shadow log loop failed")
            _t.sleep(20 * 60)
    threading.Thread(target=_shadow_loop, name="nhl-shadow-log", daemon=True).start()
    return svc.start_background_refresh(interval_minutes * 60, first_delay_s=first_delay_s, kalshi_interval_s=0, bootstrap=False)
