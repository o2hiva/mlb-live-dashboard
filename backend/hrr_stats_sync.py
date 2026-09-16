"""
HRR (Hits+Runs+RBI) prop - ported from core.py's
hrr_probability_shrinkage_adjusted, using the SAME lineup/batter/pitcher
data hits_stats_sync.py already keeps in sync (this file just reads it -
no separate lineup-detection or fetch loop of its own).

The model treats total HRR production as approximately Normal (not
binomial like Hits), built from three components - see core.py's own
hrr_probability() docstring for the full explanation:
  1. Your own hit production over your expected at-bats.
  2. You reach base, then the next 3 lineup spots drive you in (a "run").
  3. The previous 3 lineup spots reach base, you drive them in with a hit
     (an "RBI").

Needs three extra league-average rates beyond Hits' single LA_B9 (hit
rate): on-base rate, HR rate, and walk rate - computed live the same
way (summed real counts across all 30 teams), same as Hits did for its
one rate. Also needs the ballpark RUN factor (W17) alongside the HIT
factor (X17) Hits alone uses - see ballpark_factors.py.
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

STAT_STALE_AFTER = timedelta(hours=24)
LEAGUE_RATES_DATE_KEY = "hrr_league_rates_computed_at"


def _excel_round(x, digits=0):
    """Excel's ROUND: half away from zero. Verbatim from core.py."""
    factor = 10 ** digits
    return math.floor(x * factor + 0.5) / factor if x >= 0 else math.ceil(x * factor - 0.5) / factor


def _norm_cdf(x: float, mean: float, sd: float) -> float:
    """
    Standard normal CDF via the math library's erf function - avoids
    adding scipy (a large, slow-to-install dependency) just for this
    one call. Mathematically identical to scipy.stats.norm.cdf.
    """
    if sd <= 0:
        return 1.0 if x < mean else 0.0
    z = (x - mean) / (sd * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def get_league_hrr_rates() -> dict:
    """
    Returns {"obp":, "hr_rate":, "walk_rate":} - real, live-computed
    league averages (summed across all 30 teams), recomputed at most
    once every 24h. LA_B9 (hit rate) is intentionally NOT duplicated
    here - reuses hits_stats_sync.get_league_average_hit_rate()'s own
    cache instead of a second copy of the same number.
    """
    db = SessionLocal()
    try:
        date_row = db.get(SyncState, LEAGUE_RATES_DATE_KEY)
        if date_row:
            computed_at = datetime.fromisoformat(date_row.value)
            if datetime.utcnow() - computed_at < STAT_STALE_AFTER:
                rates = {}
                for key in ("obp", "hr_rate", "walk_rate"):
                    row = db.get(SyncState, f"league_avg_{key}")
                    if row:
                        rates[key] = float(row.value)
                if len(rates) == 3:
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

        if total_ab == 0:
            return {"obp": 0.32, "hr_rate": 0.032, "walk_rate": 0.085}

        pa = total_ab + total_bb
        rates = {
            "obp": (total_hits + total_bb) / pa if pa > 0 else 0.32,
            "hr_rate": total_hr / total_ab,
            "walk_rate": total_bb / pa if pa > 0 else 0.085,
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
        return {"obp": 0.32, "hr_rate": 0.032, "walk_rate": 0.085}
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


def get_pitcher_hrr_index(pitcher_id: int | None) -> float | None:
    """
    Standalone version of the pitcher-side HRR index (doesn't require a
    batter) - used for the game-card header, same role as Hits'
    get_pitcher_hit_index(). A blend of this pitcher's HR-allowed and
    hits-allowed rates vs. league average, shown regardless of sample size.
    """
    if not pitcher_id:
        return None
    db = SessionLocal()
    try:
        pitcher = db.get(PitcherHitsStat, pitcher_id)
        if not pitcher or pitcher.outs <= 0:
            return None
        la_b9 = hits_stats_sync.get_league_average_hit_rate()
        la_b11 = get_league_hrr_rates()["hr_rate"]
        pbf = pitcher.outs / 3 * 4.3
        pitcher_hr_rate = (pitcher.hr_allowed / pbf) / la_b11 if la_b11 > 0 else None
        pitcher_hits_rate = (pitcher.hits_allowed / pbf) / la_b9 if la_b9 > 0 else None
        if pitcher_hr_rate is None or pitcher_hits_rate is None:
            return None
        return 0.5 * pitcher_hr_rate + 0.5 * pitcher_hits_rate
    finally:
        db.close()


def compute_hrr_inputs(batter_id: int, batting_order: int, pitcher_id: int | None,
                        home_team: str | None, game_pk: int, team_side: str) -> dict | None:
    """
    Returns {"mean":, "sd":, "batter_hrr_index":, "pitcher_hrr_index":}
    for one batter. mean/sd are None if there isn't enough real sample
    yet (same 20 AB / 30 outs gates as Hits) - the frontend computes
    "P(HRR >= line)" for any line instantly from (mean, sd) via a normal
    CDF, the same way Hits computes "at least H hits" from (n_ab, p) via
    a binomial - no server round-trip needed when the line changes.

    batter_hrr_index / pitcher_hrr_index are shown regardless of sample
    size (purely informational, like Hits' own hit_index) - a blend of
    HR rate and hit rate, matching the formula's own 0.5/0.5 weighting
    for the HRR-specific factor (own_hrr_factor in core.py).

    Returns None only if there's no batter data at all yet.
    """
    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        if not batter:
            return None

        la_b9 = hits_stats_sync.get_league_average_hit_rate()
        hrr_rates = get_league_hrr_rates()
        la_b10, la_b11, la_b12 = hrr_rates["obp"], hrr_rates["hr_rate"], hrr_rates["walk_rate"]

        batter_hr_rate = (batter.hr / batter.ab) / la_b11 if batter.ab > 0 and la_b11 > 0 else None
        batter_hits_rate = (batter.hits / batter.ab) / la_b9 if batter.ab > 0 else None
        batter_hrr_index = None
        if batter_hr_rate is not None and batter_hits_rate is not None:
            batter_hrr_index = 0.5 * batter_hr_rate + 0.5 * batter_hits_rate

        pitcher = db.get(PitcherHitsStat, pitcher_id) if pitcher_id else None
        pitcher_hrr_index = None
        if pitcher and pitcher.outs > 0:
            pbf = pitcher.outs / 3 * 4.3
            pitcher_hr_rate = (pitcher.hr_allowed / pbf) / la_b11 if la_b11 > 0 else None
            pitcher_hits_rate = (pitcher.hits_allowed / pbf) / la_b9 if la_b9 > 0 else None
            if pitcher_hr_rate is not None and pitcher_hits_rate is not None:
                pitcher_hrr_index = 0.5 * pitcher_hr_rate + 0.5 * pitcher_hits_rate

        mean, sd = None, None
        if pitcher and batter.ab >= MIN_BATTER_AB and pitcher.outs >= MIN_PITCHER_OUTS and la_b9 > 0 and la_b10 > 0:
            pitcher_batters_faced = pitcher.outs / 3 * 4.3
            ballpark = ballpark_factors.get_ballpark_factors(home_team) if home_team else {"runs": 1.0, "hits": 1.0}

            shrunk_F = _shrunk_factor(batter.hits, batter.ab, la_b9, HITS_SHRINKAGE_K)
            shrunk_H = _shrunk_factor(batter.hr, batter.ab, la_b11, HR_SHRINKAGE_K)
            shrunk_pitcher_factor = _shrunk_factor(pitcher.hits_allowed, pitcher_batters_faced, la_b9, HITS_SHRINKAGE_K)
            shrunk_E = _shrunk_factor(batter.bb, batter.ab, la_b12, WALKS_SHRINKAGE_K)
            pitcher_walks_factor = _shrunk_factor(pitcher.bb_allowed, pitcher_batters_faced, la_b12, WALKS_SHRINKAGE_K)

            n = max(1, int(_excel_round(4.073 - 0.0897 * (batting_order - 1))))

            p_own_hit = la_b9 * shrunk_F * shrunk_pitcher_factor * ballpark["hits"]
            p_own_onbase = la_b10 * (0.5 * shrunk_F + 0.5 * shrunk_E * pitcher_walks_factor)
            own_hrr_factor = 0.5 * shrunk_H + 0.5 * shrunk_F

            next3_rows, prev3_rows = _lineup_neighbors(db, game_pk, team_side, batting_order)
            if next3_rows and prev3_rows:
                avg_next3 = sum(
                    0.5 * _neighbor_factor(db, r.batter_id, "hr", la_b11, HR_SHRINKAGE_K) +
                    0.5 * _neighbor_factor(db, r.batter_id, "hits", la_b9, HITS_SHRINKAGE_K)
                    for r in next3_rows
                ) / 3
                avg_prev3 = sum(
                    0.5 * _neighbor_factor(db, r.batter_id, "hits", la_b9, HITS_SHRINKAGE_K) +
                    0.5 * _neighbor_factor(db, r.batter_id, "bb", la_b12, WALKS_SHRINKAGE_K)
                    for r in prev3_rows
                ) / 3
            else:
                avg_next3, avg_prev3 = 1.0, 1.0  # lineup not fully recorded yet - neutral fallback

            term1_mean = n * p_own_hit

            p_reach = 1 - (1 - p_own_onbase) ** n
            p_next3_hit = 1 - (1 - la_b9 * avg_next3) ** 3
            term2_mean = (p_reach * p_next3_hit) * ballpark["runs"]

            p_prev3_reach = 1 - (1 - la_b10 * avg_prev3) ** 3
            p_own_hrr_hit = 1 - (1 - la_b9 * own_hrr_factor) ** n
            term3_mean = p_prev3_reach * p_own_hrr_hit

            mean = term1_mean + term2_mean + term3_mean

            term1_var = n * p_own_hit * (1 - p_own_hit)
            term2_var = term2_mean * (1 - term2_mean)
            term3_var = term3_mean * (1 - term3_mean)
            sd = (term1_var + term2_var + term3_var) ** 0.5

        return {"mean": mean, "sd": sd, "batter_hrr_index": batter_hrr_index, "pitcher_hrr_index": pitcher_hrr_index}
    finally:
        db.close()
