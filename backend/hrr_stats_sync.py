"""
HRR (Hits+Runs+RBI) prop - ported from core.py's
hrr_probability_shrinkage_adjusted + hrr_probability_negbinom_4term,
using the SAME lineup/batter/pitcher data hits_stats_sync.py already
keeps in sync (this file just reads it - no separate lineup-detection
or fetch loop of its own).

DISTRIBUTION: Negative Binomial, NOT Normal - confirmed against 497
real tracked outcomes (25.6% were exactly zero; a Normal built from
this formula's own mean/variance predicted only ~4.6% chance of zero,
over 5x too low - backtested z=-5.18, severely miscalibrated). Negative
Binomial with overdispersion=2.09 backtested at z=-0.07, the
best-calibrated result in the whole system. Implemented here via
math.lgamma (standard library) rather than scipy.stats.nbinom, to
avoid a large/slow dependency for one function - mathematically
identical.

FOUR TERMS, not three:
  1. Your own hit production over your expected at-bats.
  2. You reach base, the next 3 lineup spots drive you in (a "run").
  3. The previous 3 lineup spots reach base, you drive them in (an "RBI").
  4. Your own guaranteed self-driven run via home run - the original
     3-term model had NO mechanism for this at all.

Needs three extra league-average rates beyond Hits' single LA_B9 (hit
rate): on-base rate, HR rate, and a runs-allowed rate (for the real
Pitcher HRR+ index) - all computed live the same way Hits' one rate is
(summed real counts across all 30 teams).
"""
import logging
import math
from datetime import datetime, timedelta

import mlb_client
import ballpark_factors
import hits_stats_sync
from database import SessionLocal
from models_db import LineupBatter, BatterSeasonStat, PitcherHitsStat, SyncState

log = logging.getLogger("hrr_stats_sync")

SEASON = datetime.utcnow().year

# Same minimum-sample gates as Hits (and core.py's shrinkage functions generally).
MIN_BATTER_AB = 20
MIN_PITCHER_OUTS = 30

# HRR-specific shrinkage constants, deliberately different from Hits'
# own (840) - verbatim from core.py's DEFAULT_HRR_*_SHRINKAGE_K.
HITS_SHRINKAGE_K = 1200
HR_SHRINKAGE_K = 400
WALKS_SHRINKAGE_K = 250

# Validated overdispersion for the Negative Binomial fit - verbatim
# from core.py's hrr_probability_negbinom_4term default.
OVERDISPERSION = 2.09

STAT_STALE_AFTER = timedelta(hours=24)
LEAGUE_RATES_DATE_KEY = "hrr_league_rates_computed_at"


def _excel_round(x, digits=0):
    """Excel's ROUND: half away from zero. Verbatim from core.py."""
    factor = 10 ** digits
    return math.floor(x * factor + 0.5) / factor if x >= 0 else math.ceil(x * factor - 0.5) / factor


def _nbinom_sf(threshold_int: int, r: float, p: float) -> float:
    """
    1 - CDF(threshold_int) for a Negative Binomial(r, p) - r ("number of
    successes") is continuous here, not integer, so the PMF uses the
    Gamma function generalization of the binomial coefficient rather
    than a factorial-based one. Verbatim math to scipy.stats.nbinom,
    just computed via math.lgamma instead of importing scipy.
    """
    if r <= 0 or p <= 0 or p >= 1:
        return 0.0
    log_p, log_1mp = math.log(p), math.log(1 - p)
    cdf = 0.0
    for k in range(threshold_int + 1):
        log_pmf = math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * log_p + k * log_1mp
        cdf += math.exp(log_pmf)
    return max(0.0, min(1.0, 1 - cdf))


def get_league_hrr_rates(force: bool = False) -> dict:
    """
    Returns {"obp":, "hr_rate":, "walk_rate":, "runs_allowed_rate":} -
    real, live-computed league averages (summed across all 30 teams),
    recomputed at most once every 24h unless force=True. LA_B9 (hit
    rate) is intentionally NOT duplicated here - reuses
    hits_stats_sync.get_league_average_hit_rate()'s own cache instead
    of a second copy of the same number.
    """
    db = SessionLocal()
    try:
        date_row = db.get(SyncState, LEAGUE_RATES_DATE_KEY)
        keys = ("obp", "hr_rate", "walk_rate", "runs_allowed_rate")
        if not force and date_row:
            computed_at = datetime.fromisoformat(date_row.value)
            if datetime.utcnow() - computed_at < STAT_STALE_AFTER:
                rates = {}
                for key in keys:
                    row = db.get(SyncState, f"league_avg_{key}")
                    if row:
                        rates[key] = float(row.value)
                if len(rates) == len(keys):
                    return rates

        total_ab, total_hits, total_hr, total_bb = 0, 0, 0, 0
        for team_id in mlb_client.ALL_TEAM_IDS:
            try:
                totals = mlb_client.get_team_season_hitting_totals(team_id, SEASON)
            except Exception:
                continue
            if totals:
                total_ab += totals["ab"]
                total_hits += totals["hits"]
                total_hr += totals["hr"]
                total_bb += totals["bb"]

        total_pitch_outs, total_runs_allowed = 0, 0
        for team_id in mlb_client.ALL_TEAM_IDS:
            try:
                totals = mlb_client.get_team_season_pitching_totals(team_id, SEASON)
            except Exception:
                continue
            if totals:
                total_pitch_outs += totals["outs"]
                total_runs_allowed += totals["runs_allowed"]

        if total_ab == 0:
            return {"obp": 0.32, "hr_rate": 0.032, "walk_rate": 0.085, "runs_allowed_rate": 0.12}

        pa = total_ab + total_bb
        total_batters_faced = total_pitch_outs / 3 * 4.3 if total_pitch_outs > 0 else 0
        rates = {
            "obp": (total_hits + total_bb) / pa if pa > 0 else 0.32,
            "hr_rate": total_hr / total_ab,
            "walk_rate": total_bb / pa if pa > 0 else 0.085,
            "runs_allowed_rate": total_runs_allowed / total_batters_faced if total_batters_faced > 0 else 0.12,
        }
        for key, val in rates.items():
            row = db.get(SyncState, f"league_avg_{key}")
            if row is None:
                db.add(SyncState(key=f"league_avg_{key}", value=str(val)))
            else:
                row.value = str(val)
        if date_row is None:
            db.add(SyncState(key=LEAGUE_RATES_DATE_KEY, value=datetime.utcnow().isoformat()))
        else:
            date_row.value = datetime.utcnow().isoformat()
        db.commit()
        return rates
    except Exception:
        log.exception("get_league_hrr_rates failed - using fallback")
        return {"obp": 0.32, "hr_rate": 0.032, "walk_rate": 0.085, "runs_allowed_rate": 0.12}
    finally:
        db.close()


def _shrunk_factor(count: float, denom: float, league_rate: float, shrinkage_k: float) -> float:
    """A shrunk rate, converted to a FACTOR (ratio to league average).
    Falls back to 1.0 (neutral) for missing/insufficient data, matching
    core.py's own fallback behavior rather than crashing or skipping."""
    if denom is None or denom < 20 or not league_rate or league_rate <= 0:
        return 1.0
    return ((count + shrinkage_k * league_rate) / (denom + shrinkage_k)) / league_rate


def _neighbor_factor(db, batter_id: int, stat: str, league_rate: float, shrinkage_k: float) -> float:
    """Shrunk factor for a lineup neighbor's own stat, by id. Falls back
    to 1.0 if that neighbor's stats aren't synced yet or their sample is
    too small - matches core.py's _shrunk_neighbor_factor exactly."""
    row = db.get(BatterSeasonStat, batter_id)
    if not row or row.ab < MIN_BATTER_AB:
        return 1.0
    count = getattr(row, stat, None)
    if count is None:
        return 1.0
    return _shrunk_factor(count, row.ab, league_rate, shrinkage_k)


def _lineup_neighbors(db, game_pk: int, team_side: str, batting_order: int):
    """The 3 lineup spots after and before this one, wrapping around
    (9's next3 wraps to 1,2,3; 1's prev3 wraps to 9,8,7). Returns
    (next3_rows, prev3_rows), both empty if the full 9-batter lineup
    isn't recorded yet (shouldn't happen once confirmed, but safer to
    check than assume)."""
    rows = (
        db.query(LineupBatter)
        .filter_by(game_pk=game_pk, team_side=team_side)
        .order_by(LineupBatter.batting_order)
        .all()
    )
    if len(rows) < 9:
        return [], []
    idx = batting_order - 1
    next3 = [rows[(idx + 1 + i) % 9] for i in range(3)]
    prev3 = [rows[(idx - 1 - i) % 9] for i in range(3)]
    return next3, prev3


def get_pitcher_hrr_index(pitcher_id: int | None, la_b9: float | None = None, hrr_rates: dict | None = None) -> float | None:
    """
    The REAL "Pitcher HRR+" index from core.py: a weighted average of
    the pitcher's four "allowed" factors - Walks, Hits, Runs, and HR -
    with HR double-weighted (a HR always guarantees a hit, a run, AND
    at least one RBI simultaneously). Divided by 5 (1+1+1+2 weight units).
    Shown regardless of sample size (purely informational).

    la_b9/hrr_rates: pass these in (computed ONCE per request by the
    caller) to skip redundant cache lookups - calling this per batter/
    pitcher without passing them means each call re-checks the cache
    independently, which is slow even when warm and can be genuinely
    expensive (dozens of MLB API calls) on a cold one. Only omit these
    for a one-off standalone call.
    """
    if not pitcher_id:
        return None
    if la_b9 is None:
        la_b9 = hits_stats_sync.get_league_average_hit_rate()
    if hrr_rates is None:
        hrr_rates = get_league_hrr_rates()

    db = SessionLocal()
    try:
        pitcher = db.get(PitcherHitsStat, pitcher_id)
        if not pitcher or pitcher.outs <= 0:
            return None

        la_b11, la_b12 = hrr_rates["hr_rate"], hrr_rates["walk_rate"]
        la_runs_allowed = hrr_rates["runs_allowed_rate"]

        pbf = pitcher.outs / 3 * 4.3
        walks_factor = (pitcher.bb_allowed / pbf) / la_b12 if la_b12 > 0 else None
        hits_factor = (pitcher.hits_allowed / pbf) / la_b9 if la_b9 > 0 else None
        hr_factor = (pitcher.hr_allowed / pbf) / la_b11 if la_b11 > 0 else None
        runs_factor = (pitcher.runs_allowed / pbf) / la_runs_allowed if la_runs_allowed > 0 else None

        if None in (walks_factor, hits_factor, hr_factor, runs_factor):
            return None
        return (walks_factor + hits_factor + runs_factor + 2 * hr_factor) / 5
    finally:
        db.close()


def get_batter_hrr_index(batter_id: int, la_b9: float | None = None, la_hr_rate: float | None = None) -> float | None:
    """
    core.py's own file only defines a composite "HRR+" index for
    PITCHERS - there's no equivalent batter-side formula to port
    faithfully. This is an analogous construction (Hits + 2x HR
    factors, same HR-double-weighting logic), NOT something pulled
    from your validated system - flagged here in case you want it
    changed to match a specific intent.

    la_b9/la_hr_rate: same "pass in what the caller already computed"
    pattern as get_pitcher_hrr_index - see that function's docstring.
    """
    if la_b9 is None:
        la_b9 = hits_stats_sync.get_league_average_hit_rate()
    if la_hr_rate is None:
        la_hr_rate = get_league_hrr_rates()["hr_rate"]

    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        if not batter or batter.ab <= 0:
            return None
        if la_hr_rate <= 0:
            return None
        hits_factor = (batter.hits / batter.ab) / la_b9
        hr_factor = (batter.hr / batter.ab) / la_hr_rate
        return (hits_factor + 2 * hr_factor) / 3
    finally:
        db.close()


def compute_hrr_inputs(batter_id: int, batting_order: int, pitcher_id: int | None,
                        home_team: str | None, game_pk: int, team_side: str,
                        la_b9: float | None = None, hrr_rates: dict | None = None) -> dict | None:
    """
    Returns {"r":, "p":, "batter_hrr_index":, "pitcher_hrr_index":} for
    one batter - r/p are the Negative Binomial parameters, None if
    there isn't enough real sample yet (same 20 AB / 30 outs gates as
    Hits). The frontend computes "P(HRR >= line)" for any line
    instantly from (r, p) via a Negative Binomial survival function -
    same "compute once, adjust instantly client-side" pattern Hits uses.

    la_b9/hrr_rates: same "pass in what the caller already computed"
    pattern as get_pitcher_hrr_index - IMPORTANT when calling this once
    per batter in a lineup (9-18 times per request): without passing
    these, each call independently re-derives them, which is slow even
    when cached and can be a genuine timeout risk on a cold cache
    (dozens of MLB API calls, repeated per batter instead of once).

    Returns None only if there's no batter data at all yet.
    """
    if la_b9 is None:
        la_b9 = hits_stats_sync.get_league_average_hit_rate()
    if hrr_rates is None:
        hrr_rates = get_league_hrr_rates()

    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        if not batter:
            return None

        batter_hrr_index = get_batter_hrr_index(batter_id, la_b9=la_b9, la_hr_rate=hrr_rates["hr_rate"])
        pitcher_hrr_index = get_pitcher_hrr_index(pitcher_id, la_b9=la_b9, hrr_rates=hrr_rates)

        la_b10, la_b11 = hrr_rates["obp"], hrr_rates["hr_rate"]
        la_b12 = hrr_rates["walk_rate"]

        pitcher = db.get(PitcherHitsStat, pitcher_id) if pitcher_id else None

        r, p = None, None
        if pitcher and batter.ab >= MIN_BATTER_AB and pitcher.outs >= MIN_PITCHER_OUTS and la_b9 > 0 and la_b10 > 0 and la_b11 > 0:
            pitcher_batters_faced = pitcher.outs / 3 * 4.3
            # Y17: real HR-specific ballpark factor, now available.
            ballpark = ballpark_factors.get_ballpark_factors(home_team) if home_team else {"runs": 1.0, "hits": 1.0, "hr": 1.0}
            y17 = ballpark.get("hr", 1.0)

            shrunk_F = _shrunk_factor(batter.hits, batter.ab, la_b9, HITS_SHRINKAGE_K)
            shrunk_H = _shrunk_factor(batter.hr, batter.ab, la_b11, HR_SHRINKAGE_K)
            shrunk_pitcher_factor = _shrunk_factor(pitcher.hits_allowed, pitcher_batters_faced, la_b9, HITS_SHRINKAGE_K)
            shrunk_pitcher_hr = _shrunk_factor(pitcher.hr_allowed, pitcher_batters_faced, la_b11, HR_SHRINKAGE_K)
            shrunk_E = _shrunk_factor(batter.bb, batter.ab, la_b12, WALKS_SHRINKAGE_K)
            pitcher_walks_factor = _shrunk_factor(pitcher.bb_allowed, pitcher_batters_faced, la_b12, WALKS_SHRINKAGE_K)

            n = max(1, int(_excel_round(4.073 - 0.0897 * (batting_order - 1))))

            p_own_hit = la_b9 * shrunk_F * shrunk_pitcher_factor * ballpark["hits"]
            p_own_onbase = la_b10 * (0.5 * shrunk_F + 0.5 * shrunk_E * pitcher_walks_factor)
            own_hrr_factor = 0.5 * shrunk_H + 0.5 * shrunk_F
            p_own_hr = la_b11 * shrunk_H * shrunk_pitcher_hr * y17

            next3_rows, prev3_rows = _lineup_neighbors(db, game_pk, team_side, batting_order)
            if next3_rows and prev3_rows:
                avg_next3 = sum(
                    0.5 * _neighbor_factor(db, r2.batter_id, "hr", la_b11, HR_SHRINKAGE_K) +
                    0.5 * _neighbor_factor(db, r2.batter_id, "hits", la_b9, HITS_SHRINKAGE_K)
                    for r2 in next3_rows
                ) / 3
                avg_prev3 = sum(
                    0.5 * _neighbor_factor(db, r2.batter_id, "hits", la_b9, HITS_SHRINKAGE_K) +
                    0.5 * _neighbor_factor(db, r2.batter_id, "bb", la_b12, WALKS_SHRINKAGE_K)
                    for r2 in prev3_rows
                ) / 3
            else:
                avg_next3, avg_prev3 = 1.0, 1.0  # lineup not fully recorded yet - neutral fallback

            term1_mean = n * p_own_hit
            p_reach = 1 - (1 - p_own_onbase) ** n
            p_next3_hit = 1 - (1 - la_b9 * avg_next3) ** 3
            term2_mean = p_reach * p_next3_hit * ballpark["runs"]
            p_prev3_reach = 1 - (1 - la_b10 * avg_prev3) ** 3
            p_own_hrr_hit = 1 - (1 - la_b9 * own_hrr_factor) ** n
            term3_mean = p_prev3_reach * p_own_hrr_hit
            term4_mean = 1 - (1 - p_own_hr) ** n  # guaranteed self-driven run via HR

            mean = term1_mean + term2_mean + term3_mean + term4_mean

            if OVERDISPERSION and OVERDISPERSION > 1:
                p = 1 / OVERDISPERSION
                r = mean / (OVERDISPERSION - 1)
            else:
                # Degenerate case (no overdispersion configured) - falls
                # straight back to a plain Poisson-equivalent, expressed
                # as a very large r with r*(1-p)/p == mean.
                p = 0.999999
                r = mean * p / (1 - p)

        return {"r": r, "p": p, "batter_hrr_index": batter_hrr_index, "pitcher_hrr_index": pitcher_hrr_index}
    finally:
        db.close()
