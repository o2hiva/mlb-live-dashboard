"""
Pitcher K (strikeout) prop - the validated "bf" model from
mlb_k_sheet.py / mlb_k_backtest.py, ported to the live dashboard.
Replaces the earlier Normal-approximation model.

MODEL (per starter, per game):

    kp      = (K + k_pa * lgsk) / (BF + k_pa)               shrunk starter K%
    bf_exp  = recency-weighted mean of last 20 starts' BF   (decay 0.8,
              shrunk toward the league starter BF/start with weight k_bf)
    opp_k   = slot-weighted mean of the 9 lineup batters' shrunk K%
              (k_bat), or - with no lineup yet - the batting team's shrunk
              K% (k_team), blended at alpha_team instead of alpha
    pf      = (venue K% shrunk with k_park) / league K%      park factor
    odds    = logit(kp) + alpha * (logit(opp_k) - logit(lgk))
    p       = expit(odds) * pf ** beta
    mean    = bf_exp * p
    P(>= N) = 1 - PoissonCDF(floor(line), mean)             (frontend)

Pitcher K/BF are STARTS ONLY. Every season counter is carry-weighted:
the current season's totals plus `carry` (0.4) x the prior season's.

KNOWN APPROXIMATIONS vs. the backtest workbook (the dashboard has no
play-by-play history store, so these are rebuilt from MLB season stats):
  * Carry only reaches back ONE season (current + 0.4 x prior). The
    backtest's rolling carry compounds across all earlier seasons.
  * Venue K% = (home team's batting K + pitching K) / (their batting PA +
    pitching BF) in HOME games - i.e. every PA at that park - for the
    current + 0.4 x prior season. Falls back to pf = 1.0 if unavailable.
  * League starter K% / BF-per-start come from the MLB "starter" (sp)
    team-pitching split; if that split is unavailable the backtest's own
    fallback constants (LG0) are used.
Everything degrades gracefully: any failed fetch falls back to the
league average rather than raising.
"""
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

import mlb_client
from database import SessionLocal
from models_db import PitcherKStat, BatterSeasonStat, LineupBatter, Game, SyncState

log = logging.getLogger("pitcher_k_sync")

SEASON = datetime.utcnow().year

# ---- Tuned parameters (verbatim from mlb_k_sheet.py TUNED) -------------
K_PA = 60          # pitcher K% shrink (per BF)
K_BF = 1.0         # BF/start shrink weight
DECAY = 0.8        # recency weight per start back
K_BAT = 60         # batter K% shrink
ALPHA = 1.0        # opponent weight (lineup basis)
ALPHA_TEAM = 0.75  # opponent weight (team-fallback basis)
BETA = 0.5         # park exponent
CARRY = 0.4        # prior-season weight
K_PARK = 3000
K_TEAM = 400
N_HIST = 20
SLOT_PA = [4.65 - (4.65 - 3.78) * i / 8 for i in range(9)]

# Backtest fallback baselines, used when the pooled league sample is tiny.
LG0 = {"k_all": 0.225, "s_k": 0.225, "s_bf": 22.0, "s_outs": 16.0}

LEAGUE_KEY = "kmodel_league_v1"
PITCHER_KEY = "kmodel_pitcher_v1_{}"
BATTER_PRIOR_KEY = "kmodel_batter_prior_v1_{}_{}"
LEAGUE_TTL = timedelta(hours=24)
LEAGUE_TTL_PARTIAL = timedelta(hours=1)
PITCHER_TTL = timedelta(hours=12)
STAT_STALE_AFTER = timedelta(hours=24)
TIMEOUT = mlb_client.TIMEOUT


# ------------------------------------------------------------------ utils
def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _expit(x):
    return 1.0 / (1.0 + math.exp(-x))


def _ew_mean(values_newest_first, decay, shrink_to, k):
    num = 0.0
    den = 0.0
    w = 1.0
    for v in values_newest_first:
        num += w * v
        den += w
        w *= decay
    return (num + k * shrink_to) / (den + k)


def _kv_get(db, key):
    row = db.get(SyncState, key)
    if row is None or not row.value:
        return None
    try:
        return json.loads(row.value)
    except Exception:
        return None


def _kv_set(db, key, obj):
    val = json.dumps(obj)
    row = db.get(SyncState, key)
    if row is None:
        db.add(SyncState(key=key, value=val))
    else:
        row.value = val
    db.commit()


def _fresh(obj, ttl):
    try:
        return obj is not None and datetime.utcnow() - datetime.fromisoformat(obj["ts"]) < ttl
    except Exception:
        return False


# ------------------------------------------------------------ MLB fetches
def _stat_split(path, params):
    """First split's stat dict from an MLB stats endpoint, or None."""
    resp = requests.get(f"{mlb_client.BASE}{path}", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    stats = resp.json().get("stats") or []
    if not stats:
        return None
    splits = stats[0].get("splits") or []
    if not splits:
        return None
    return splits[0].get("stat") or None


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        return None


def _team_season(team_id, season):
    """Everything the model needs from one team-season, each piece
    independently best-effort."""
    out = {}
    h = _safe(_stat_split, f"/v1/teams/{team_id}/stats",
              {"stats": "season", "season": season, "group": "hitting"})
    if h:
        out["hit_k"] = h.get("strikeOuts", 0) or 0
        out["hit_pa"] = h.get("plateAppearances", 0) or 0
    hh = _safe(_stat_split, f"/v1/teams/{team_id}/stats",
               {"stats": "statSplits", "sitCodes": "h", "season": season, "group": "hitting"})
    ph = _safe(_stat_split, f"/v1/teams/{team_id}/stats",
               {"stats": "statSplits", "sitCodes": "h", "season": season, "group": "pitching"})
    if hh and ph:
        out["home_k"] = (hh.get("strikeOuts", 0) or 0) + (ph.get("strikeOuts", 0) or 0)
        out["home_pa"] = (hh.get("plateAppearances", 0) or 0) + (ph.get("battersFaced", 0) or 0)
    sp = _safe(_stat_split, f"/v1/teams/{team_id}/stats",
               {"stats": "statSplits", "sitCodes": "sp", "season": season, "group": "pitching"})
    if sp:
        starts = sp.get("gamesStarted") or sp.get("gamesPlayed") or 0
        out["sp_k"] = sp.get("strikeOuts", 0) or 0
        out["sp_bf"] = sp.get("battersFaced", 0) or 0
        out["sp_outs"] = sp.get("outs") or 0
        out["sp_starts"] = starts
    return out


def _combine(cur, prior, field):
    """current + CARRY x prior; None only if neither season has it."""
    a, b = cur.get(field), prior.get(field)
    if a is None and b is None:
        return None
    return (a or 0) + CARRY * (b or 0)


def _build_league():
    """League baselines + per-team K/PA + per-team park (home) K/PA, all
    carry-weighted. 30 teams x 2 seasons, fetched in parallel."""
    jobs = [(t, s) for t in mlb_client.ALL_TEAM_IDS for s in (SEASON, SEASON - 1)]
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda ts: _team_season(*ts), jobs))
    by = {}
    for (t, s), r in zip(jobs, results):
        by.setdefault(t, {})[s] = r

    teams = {}
    lg_k = lg_pa = 0.0
    s_k = s_bf = s_outs = s_starts = 0.0
    have_hit = 0
    for t in mlb_client.ALL_TEAM_IDS:
        cur, prior = by[t][SEASON], by[t][SEASON - 1]
        entry = {}
        for f in ("hit_k", "hit_pa", "home_k", "home_pa"):
            v = _combine(cur, prior, f)
            if v is not None:
                entry[f] = v
        teams[str(t)] = entry
        if "hit_pa" in entry:
            have_hit += 1
            lg_k += entry["hit_k"]
            lg_pa += entry["hit_pa"]
        for f, name in (("sp_k", "k"), ("sp_bf", "bf"), ("sp_outs", "outs"), ("sp_starts", "starts")):
            v = _combine(cur, prior, f)
            if v is not None:
                if name == "k":
                    s_k += v
                elif name == "bf":
                    s_bf += v
                elif name == "outs":
                    s_outs += v
                else:
                    s_starts += v

    lg = {
        "k_all": (lg_k / lg_pa) if lg_pa > 500 else LG0["k_all"],
        "s_k": LG0["s_k"], "s_bf": LG0["s_bf"], "s_outs": LG0["s_outs"],
    }
    if s_bf > 500 and s_starts > 30:
        lg["s_k"] = s_k / s_bf
        lg["s_bf"] = s_bf / s_starts
        lg["s_outs"] = s_outs / s_starts
    return {"ts": datetime.utcnow().isoformat(), "complete": have_hit >= 28,
            "lg": lg, "teams": teams}


def get_league_state(force: bool = False) -> dict:
    """Cached league baselines + team tables (24h; 1h if the build was
    incomplete). Never raises - falls back to LG0 constants."""
    db = SessionLocal()
    try:
        cached = _kv_get(db, LEAGUE_KEY)
        if not force and cached:
            ttl = LEAGUE_TTL if cached.get("complete") else LEAGUE_TTL_PARTIAL
            if _fresh(cached, ttl):
                return cached
        try:
            state = _build_league()
            _kv_set(db, LEAGUE_KEY, state)
            return state
        except Exception:
            log.exception("league K-model build failed")
            db.rollback()
            if cached:
                return cached
            return {"ts": datetime.utcnow().isoformat(), "complete": False,
                    "lg": dict(LG0), "teams": {}}
    finally:
        db.close()


def get_league_k_rate(force: bool = False) -> float:
    """League K% per PA (all hitters, carry-weighted) - "lgk"."""
    return get_league_state(force)["lg"]["k_all"]


def _fetch_pitcher_gamelog(pitcher_id, season):
    """[(date, bf, outs, k)] for STARTS only, oldest first."""
    resp = requests.get(
        f"{mlb_client.BASE}/v1/people/{pitcher_id}/stats",
        params={"stats": "gameLog", "season": season, "group": "pitching"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    stats = resp.json().get("stats") or []
    splits = (stats[0].get("splits") if stats else None) or []
    starts = []
    for sp in splits:
        st = sp.get("stat") or {}
        if not (st.get("gamesStarted") or 0):
            continue
        outs = st.get("outs")
        if outs is None:
            ip = str(st.get("inningsPitched", "0.0"))
            whole, _, frac = ip.partition(".")
            outs = int(whole or 0) * 3 + int(frac or 0)
        starts.append((sp.get("date") or "", int(st.get("battersFaced", 0) or 0),
                       int(outs or 0), int(st.get("strikeOuts", 0) or 0)))
    starts.sort(key=lambda r: r[0])
    return starts


def _get_pitcher_starts(pitcher_id, force=False):
    """{"cur": [[date,bf,outs,k]...], "prior": [...]} starts-only logs,
    cached 12h. None if both fetches failed and nothing is cached."""
    db = SessionLocal()
    try:
        cached = _kv_get(db, PITCHER_KEY.format(pitcher_id))
        if not force and _fresh(cached, PITCHER_TTL):
            return cached
        cur = _safe(_fetch_pitcher_gamelog, pitcher_id, SEASON)
        # Prior-season log never changes - reuse it if we already have it.
        prior = cached.get("prior") if cached and cached.get("prior") is not None else \
            _safe(_fetch_pitcher_gamelog, pitcher_id, SEASON - 1)
        if cur is None and prior is None:
            return cached
        data = {"ts": datetime.utcnow().isoformat(),
                "cur": [list(r) for r in (cur if cur is not None else (cached or {}).get("cur", []))],
                "prior": [list(r) for r in (prior or [])]}
        try:
            _kv_set(db, PITCHER_KEY.format(pitcher_id), data)
        except Exception:
            db.rollback()
        return data
    finally:
        db.close()


def _batter_prior(batter_id):
    """(k, pa) from the PRIOR season for one batter - fixed once the
    season is over, so cached permanently after the first fetch."""
    db = SessionLocal()
    try:
        key = BATTER_PRIOR_KEY.format(SEASON - 1, batter_id)
        cached = _kv_get(db, key)
        if cached is not None:
            return cached["k"], cached["pa"]
        try:
            totals = mlb_client.get_season_hitting_totals(batter_id, SEASON - 1)
        except Exception:
            return 0, 0
        k, pa = (totals["strikeouts"], totals["pa"]) if totals else (0, 0)
        try:
            _kv_set(db, key, {"k": k, "pa": pa})
        except Exception:
            db.rollback()
        return k, pa
    finally:
        db.close()


# --------------------------------------------------------- sync (existing)
def sync_pitcher_k_stat(pitcher_id: int, pitcher_name: str, force: bool = False):
    """Called at lineup confirmation. Keeps the all-appearances season
    row (PitcherKStat, still shown/used by debug tools) AND warms the
    starts-only log the model actually uses. Safe to call repeatedly."""
    db = SessionLocal()
    try:
        row = db.get(PitcherKStat, pitcher_id)
        if not (row and not force and datetime.utcnow() - row.updated_at < STAT_STALE_AFTER):
            try:
                totals = mlb_client.get_season_pitching_totals(pitcher_id, SEASON)
            except Exception:
                log.exception("Failed to fetch season pitching totals for %s (%s)", pitcher_name, pitcher_id)
                totals = None
            if totals is not None:
                if row is None:
                    db.add(PitcherKStat(
                        pitcher_id=pitcher_id, pitcher_name=pitcher_name,
                        strikeouts=totals["strikeouts"], batters_faced=totals["batters_faced"],
                        games_started=totals["games_started"], outs=totals["outs"],
                    ))
                else:
                    row.pitcher_name = pitcher_name
                    row.strikeouts = totals["strikeouts"]
                    row.batters_faced = totals["batters_faced"]
                    row.games_started = totals["games_started"]
                    row.outs = totals["outs"]
                db.commit()
    finally:
        db.close()
    _get_pitcher_starts(pitcher_id, force=force)


# ------------------------------------------------------------------ model
def _pitcher_inputs(pitcher_id, game_date, lg):
    """(kp, bf_exp, n_starts) from starts strictly BEFORE game_date."""
    data = _get_pitcher_starts(pitcher_id)
    if data is None:
        return None
    cur = [r for r in data["cur"] if not game_date or r[0] < game_date]
    prior = data["prior"]
    k = sum(r[3] for r in cur) + CARRY * sum(r[3] for r in prior)
    bf = sum(r[1] for r in cur) + CARRY * sum(r[1] for r in prior)
    hist = (prior + cur)[-N_HIST:]
    bfs = [r[1] for r in reversed(hist)]  # newest first
    kp = (k + K_PA * lg["s_k"]) / (bf + K_PA)
    bf_exp = _ew_mean(bfs, DECAY, lg["s_bf"], K_BF)
    return kp, bf_exp, len(hist)


def _lineup_k(db, game_pk, side, lgk):
    """Slot-weighted mean of the batting side's shrunk batter K%, or
    None if no lineup rows are stored. Also returns how many of the 9
    slots had real season data."""
    rows = db.query(LineupBatter).filter_by(game_pk=game_pk, team_side=side).all()
    if not rows:
        return None, 0
    by_slot = {}
    for r in rows:
        if r.batting_order and 1 <= r.batting_order <= 9:
            by_slot[r.batting_order - 1] = r
    ids = [r.batter_id for r in by_slot.values()]
    with ThreadPoolExecutor(max_workers=5) as ex:
        priors = dict(zip(ids, ex.map(_batter_prior, ids)))
    num = den = 0.0
    with_data = 0
    for i in range(9):
        r = by_slot.get(i)
        rate = lgk
        if r is not None:
            cur = db.get(BatterSeasonStat, r.batter_id)
            pk, ppa = priors.get(r.batter_id, (0, 0))
            k = (cur.strikeouts if cur else 0) + CARRY * pk
            pa = (cur.plate_appearances if cur else 0) + CARRY * ppa
            if pa > 0:
                with_data += 1
            rate = (k + K_BAT * lgk) / (pa + K_BAT)
        num += SLOT_PA[i] * rate
        den += SLOT_PA[i]
    return num / den, with_data


def compute_pitcher_k_inputs(pitcher_id: int, game_pk: int, batting_team_side: str,
                              la_b13: float | None = None) -> dict | None:
    """
    Returns {"mean", "pitcher_k_index", "opposing_lineup_k_index",
    "park_factor", "bf_exp", "p", "basis"} for one starter. The frontend
    turns `mean` into P(>= N Ks) with a Poisson tail (any line, instantly).

    batting_team_side: the side BATTING against this pitcher (the home
    starter passes "away"). la_b13 is accepted for call-compatibility and
    ignored - the league baseline now comes from get_league_state().
    None if the pitcher's start log couldn't be fetched at all.
    """
    state = get_league_state()
    lg, teams = state["lg"], state["teams"]
    lgk, lgsk = lg["k_all"], lg["s_k"]

    db = SessionLocal()
    try:
        game = db.get(Game, game_pk)
        game_date = game.game_date if game else None
        pin = _pitcher_inputs(pitcher_id, game_date, lg)
        if pin is None:
            return None
        kp, bf_exp, _n = pin

        opp_k, basis, alpha = None, "league", ALPHA_TEAM
        lineup_k, with_data = _lineup_k(db, game_pk, batting_team_side, lgk)
        if lineup_k is not None:
            opp_k, basis, alpha = lineup_k, "lineup", ALPHA
        elif game is not None:
            bat_team = game.away_team_id if batting_team_side == "away" else game.home_team_id
            t = teams.get(str(bat_team)) if bat_team else None
            if t and "hit_pa" in t:
                opp_k = (t["hit_k"] + K_TEAM * lgk) / (t["hit_pa"] + K_TEAM)
                basis = "team"
        if opp_k is None:
            opp_k = lgk

        pf = 1.0
        if game is not None and game.home_team_id:
            t = teams.get(str(game.home_team_id))
            if t and "home_pa" in t:
                pf = ((t["home_k"] + K_PARK * lgk) / (t["home_pa"] + K_PARK)) / lgk

        odds = _logit(kp) + alpha * (_logit(opp_k) - _logit(lgk))
        p = _expit(odds) * pf ** BETA
        mean = bf_exp * p

        return {
            "mean": mean,
            "pitcher_k_index": kp / lgsk if lgsk > 0 else None,
            "opposing_lineup_k_index": opp_k / lgk if lgk > 0 else None,
            "park_factor": pf,
            "bf_exp": bf_exp,
            "p": p,
            "basis": basis,
        }
    finally:
        db.close()
