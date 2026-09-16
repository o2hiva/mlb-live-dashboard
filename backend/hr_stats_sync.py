"""
HR (home run) prop - ported from core.py's hr_probability_shrinkage_adjusted,
using the SAME batter/pitcher HR data hits_stats_sync.py already keeps in
sync (this file just reads it - no separate lineup-detection or fetch
loop of its own). Threshold is always "at least 1 HR" - core.py's own
hr_probability_for_row docstring: "threshold is fixed at 0 ... matching
Player model's own formulas exactly - they don't read an adjustable
threshold cell the way Hit Prob does." No line selector needed.

BALLPARK FACTOR: core.py's base formula (p_hr = LA_B11 * H * H_pitcher *
Y17) includes the ballpark HR factor, but hr_probability_shrinkage_adjusted
itself OMITS it entirely - the same class of gap independently found and
fixed in Hits' own shrinkage-adjusted formula. Added back in here.

MINIMUM SAMPLE: 45 AB / 45 outs, not the 20/30 used elsewhere - validated
separately for HR specifically, since HR's much lower base rate (~3% per
AB vs ~24% for Hits) means the same 20 AB carries much less real
information about a player's true HR rate.

NOT IMPLEMENTED: platoon adjustment (swapping in a batter's HR rate
specifically vs the actual opposing pitcher's throwing hand) - core.py's
hr_probability_platoon_adjusted does this and is real/validated, but it
needs pitcher-handedness and batter-platoon-split data this dashboard
doesn't fetch yet. Worth adding later as its own data source; the
shrinkage-adjusted version here uses real season totals only, no splits.
"""
import logging
import math

import ballpark_factors
import hrr_stats_sync
from database import SessionLocal
from models_db import BatterSeasonStat, PitcherHitsStat

log = logging.getLogger("hr_stats_sync")

# Higher than Hits/HRR's 20/30 - validated separately for HR (see module docstring).
MIN_BATTER_AB = 45
MIN_PITCHER_OUTS = 45

# Verbatim from core.py's DEFAULT_HR_LIVE_SHRINKAGE_K.
DEFAULT_HR_SHRINKAGE_K = 280


def _shrunk_factor(count: float, denom: float, league_rate: float, shrinkage_k: float) -> float:
    """A shrunk rate, converted to a FACTOR (ratio to league average).
    Falls back to 1.0 (neutral) for missing/insufficient data."""
    if denom is None or denom <= 0 or not league_rate or league_rate <= 0:
        return 1.0
    return ((count + shrinkage_k * league_rate) / (denom + shrinkage_k)) / league_rate


def get_pitcher_hr_index(pitcher_id: int | None, la_hr_rate: float | None = None) -> float | None:
    """This pitcher's HR-allowed rate relative to league average - shown
    regardless of sample size (purely informational). la_hr_rate: pass
    in the caller's already-computed value to skip a redundant lookup -
    see hrr_stats_sync's identical pattern/reasoning."""
    if not pitcher_id:
        return None
    if la_hr_rate is None:
        la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]
    if la_hr_rate <= 0:
        return None
    db = SessionLocal()
    try:
        pitcher = db.get(PitcherHitsStat, pitcher_id)
        if not pitcher or pitcher.outs <= 0:
            return None
        pbf = pitcher.outs / 3 * 4.3
        return (pitcher.hr_allowed / pbf) / la_hr_rate
    finally:
        db.close()


def get_batter_hr_index(batter_id: int, la_hr_rate: float | None = None) -> float | None:
    """This batter's own HR rate relative to league average - shown
    regardless of sample size. la_hr_rate: same pass-in pattern."""
    if la_hr_rate is None:
        la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]
    if la_hr_rate <= 0:
        return None
    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        if not batter or batter.ab <= 0:
            return None
        return (batter.hr / batter.ab) / la_hr_rate
    finally:
        db.close()


def _excel_round(x, digits=0):
    """Excel's ROUND: half away from zero. Verbatim from core.py."""
    factor = 10 ** digits
    return math.floor(x * factor + 0.5) / factor if x >= 0 else math.ceil(x * factor - 0.5) / factor


def compute_hr_inputs(batter_id: int, batting_order: int, pitcher_id: int | None, home_team: str | None,
                       la_hr_rate: float | None = None) -> dict | None:
    """
    Returns {"n_ab":, "p":, "hr_index":} for one batter - n_ab/p feed a
    binomial "at least 1 HR" probability (matching Hits' own pattern,
    computed once and left to the frontend/caller to turn into a
    percentage), None if there isn't enough real sample yet (45 AB / 45
    outs - see module docstring). hr_index is shown regardless of
    sample size (purely informational).

    la_hr_rate: pass in the caller's already-computed league HR rate to
    avoid a redundant lookup per batter - same reasoning as HRR's own
    functions; see hrr_stats_sync's docstrings for the full explanation
    of why this matters (a cold cache otherwise means dozens of MLB API
    calls PER BATTER instead of once per request).

    Returns None only if there's no batter data at all yet.
    """
    if la_hr_rate is None:
        la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]

    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        if not batter:
            return None

        hr_index = get_batter_hr_index(batter_id, la_hr_rate=la_hr_rate)

        pitcher = db.get(PitcherHitsStat, pitcher_id) if pitcher_id else None

        n_ab, p = None, None
        if pitcher and batter.ab >= MIN_BATTER_AB and pitcher.outs >= MIN_PITCHER_OUTS and la_hr_rate > 0:
            pitcher_batters_faced = pitcher.outs / 3 * 4.3
            ballpark = ballpark_factors.get_ballpark_factors(home_team) if home_team else {"hr": 1.0}
            y17 = ballpark.get("hr", 1.0)

            shrunk_batter_rate = _shrunk_factor(batter.hr, batter.ab, la_hr_rate, DEFAULT_HR_SHRINKAGE_K)
            shrunk_pitcher_rate = _shrunk_factor(pitcher.hr_allowed, pitcher_batters_faced, la_hr_rate, DEFAULT_HR_SHRINKAGE_K)

            # Same lineup-position at-bats estimate as Hits/HRR - verbatim
            # formula from core.py, batters lower in the order get fewer
            # expected at-bats.
            n_ab = max(1, int(_excel_round(4.073 - 0.0897 * (batting_order - 1))))

            p = la_hr_rate * shrunk_batter_rate * shrunk_pitcher_rate * y17
            p = max(0.001, min(p, 0.3))  # same tighter clamp as core.py (HR rates run much lower than Hits)

        return {"n_ab": n_ab, "p": p, "hr_index": hr_index}
    finally:
        db.close()
