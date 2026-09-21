"""
Pitcher Hits Allowed prop - ported from core.py's
pitcher_hits_allowed_probability.

NO NEW DATA NEEDED: unlike every other prop's first build, this one
needs zero new fetching. It combines three things already synced at
the same trigger point (lineup confirmation):
  - The pitcher's own hits_allowed (already in PitcherHitsStat)
  - The pitcher's own batters_faced (already in PitcherKStat - fetched
    for the K prop, same pitcher, same season-pitching-totals call)
  - The opposing lineup's real hits/AB per batter (already in
    BatterSeasonStat via LineupBatter - the exact same "9 real
    confirmed batters" blend Pitcher K's own lineup-specific formula
    already uses)

DISTRIBUTION: Poisson (not Binomial/Negative-Binomial/Normal - a new
one for this system). Validated directly against Normal and Negative
Binomial in the backtest; Poisson gave the calibrated result (z=+0.53).

TWO HARDCODED CONSTANTS - DELIBERATE, NOT AN OVERSIGHT: unlike every
other league rate in this system (all live-computed from current
season data), core.py uses two fixed historical values here:
LEAGUE_PITCHER_HIT_RATE_PER_BF and LEAGUE_AVG_HITS_ALLOWED_PER_START.
Ported verbatim rather than "improved" into live averages - that would
silently change the formula's actual structure away from what was
backtested. Worth knowing these two numbers will grow stale over
time in a way nothing else in this system does; only worth revisiting
if the underlying league-wide hitting environment shifts materially.

THRESHOLD SEMANTICS - checked deliberately: core.py's own threshold
handling (`1 - poisson.cdf(int(threshold), mean)`) was written for
half-integer O/U-style lines (int(4.5)=4 correctly gives P(X>=5)). It
silently computes "at least N+1" for a whole-number line - confirmed
directly: for mean=5.02 and a line of 5, the naive port gives 38.8%
(actually P(X>=6)) instead of the correct 56.3% for P(X>=5). Since
this system's UI convention is a whole-number O/U box (matching
Hits/HR/Pitcher K), this uses P(X >= line) directly - poisson_sf(line
- 1, mean) - the same generalized "at least N" fix already applied to
HRR's negative-binomial threshold earlier today.
"""
import logging
import math
from datetime import datetime

from database import SessionLocal
from models_db import PitcherHitsStat, PitcherKStat, BatterSeasonStat, LineupBatter

log = logging.getLogger("pitcher_hits_allowed_sync")

# Verbatim from core.py - deliberately static, see module docstring.
LEAGUE_PITCHER_HIT_RATE_PER_BF = 0.2224
LEAGUE_AVG_HITS_ALLOWED_PER_START = 5.02

# Verbatim from core.py's pitcher_hits_allowed_probability.
MIN_PITCHER_BATTERS_FACED = 50
MIN_LINEUP_BATTERS_WITH_DATA = 5
MIN_BATTER_AB_TO_TRUST_OWN_RATE = 30
DEFAULT_PITCHER_SHRINKAGE_K = 800
DEFAULT_BATTER_SHRINKAGE_K = 100


def get_pitcher_hits_allowed_index(pitcher_id: int | None) -> float | None:
    """This pitcher's own hits-allowed rate (per batter faced) relative
    to the league constant - shown regardless of sample size (purely
    informational)."""
    if not pitcher_id:
        return None
    db = SessionLocal()
    try:
        pitcher_hits = db.get(PitcherHitsStat, pitcher_id)
        pitcher_k = db.get(PitcherKStat, pitcher_id)
        if not pitcher_hits or not pitcher_k or pitcher_k.batters_faced <= 0:
            return None
        return (pitcher_hits.hits_allowed / pitcher_k.batters_faced) / LEAGUE_PITCHER_HIT_RATE_PER_BF
    finally:
        db.close()


def _lineup_hit_factor(db, game_pk: int, batting_team_side: str, la_b9: float) -> tuple:
    """
    The lineup-specific opposing-batters factor - a blend of the real 9
    confirmed batters this pitcher actually faces today, each shrunk by
    their own real at-bat sample. Same structure as pitcher_k_sync's
    own _lineup_k_factor, just hits/AB instead of strikeouts/PA.

    Returns (lineup_hit_index, batters_with_data_count).
    """
    rows = db.query(LineupBatter).filter_by(game_pk=game_pk, team_side=batting_team_side).all()
    batter_indices = []
    for r in rows:
        batter = db.get(BatterSeasonStat, r.batter_id)
        if not batter or batter.ab <= 0:
            continue
        if batter.ab >= MIN_BATTER_AB_TO_TRUST_OWN_RATE:
            shrunk_rate = (batter.hits + DEFAULT_BATTER_SHRINKAGE_K * la_b9) / (batter.ab + DEFAULT_BATTER_SHRINKAGE_K)
        else:
            shrunk_rate = la_b9  # not enough own data - treated as exactly league-average
        batter_indices.append(shrunk_rate / la_b9)

    if not batter_indices:
        return None, 0
    return sum(batter_indices) / len(batter_indices), len(batter_indices)


def compute_pitcher_hits_allowed_inputs(pitcher_id: int, game_pk: int, batting_team_side: str,
                                         la_b9: float) -> dict | None:
    """
    Returns {"mean":, "pitcher_hits_allowed_index":,
    "opposing_lineup_hit_index":} for one starting pitcher - mean feeds
    a Poisson "at least N hits allowed" probability (the frontend
    computes this instantly for any line via poissonSf, same pattern
    as every other prop). None if there isn't enough real sample yet:
    50+ batters faced for the pitcher, AND at least 5 of the opposing
    lineup's 9 confirmed batters need real season data on file.

    la_b9: pass in the caller's already-computed league hit rate to
    avoid a redundant lookup per pitcher - same "once per request, not
    once per row" reasoning as every other prop in this system.

    Returns None only if there's no pitcher data at all yet.
    """
    db = SessionLocal()
    try:
        pitcher_hits = db.get(PitcherHitsStat, pitcher_id)
        pitcher_k = db.get(PitcherKStat, pitcher_id)
        if not pitcher_hits or not pitcher_k:
            return None

        pitcher_hits_allowed_index = get_pitcher_hits_allowed_index(pitcher_id)

        lineup_hit_index, batters_with_data = _lineup_hit_factor(db, game_pk, batting_team_side, la_b9)

        mean = None
        if pitcher_k.batters_faced >= MIN_PITCHER_BATTERS_FACED and \
                batters_with_data >= MIN_LINEUP_BATTERS_WITH_DATA and lineup_hit_index is not None:

            shrunk_pitcher_rate = (pitcher_hits.hits_allowed + DEFAULT_PITCHER_SHRINKAGE_K * LEAGUE_PITCHER_HIT_RATE_PER_BF) / \
                (pitcher_k.batters_faced + DEFAULT_PITCHER_SHRINKAGE_K)
            pitcher_hit_index = shrunk_pitcher_rate / LEAGUE_PITCHER_HIT_RATE_PER_BF

            mean = pitcher_hit_index * lineup_hit_index * LEAGUE_AVG_HITS_ALLOWED_PER_START

        return {
            "mean": mean,
            "pitcher_hits_allowed_index": pitcher_hits_allowed_index,
            "opposing_lineup_hit_index": lineup_hit_index,
        }
    finally:
        db.close()
