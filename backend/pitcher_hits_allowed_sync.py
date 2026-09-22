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

TWO BASELINE CONSTANTS - HYBRID, NOT PURELY STATIC: originally ported
as fixed historical values (deliberately, not an oversight -
"improving" them into naive live averages would have silently changed
the formula's structure away from what was backtested). core.py later
added a validated hybrid approach, implemented here in
get_hybrid_baselines(): use these exact static, backtested values
until enough REAL live data has accumulated (30+ distinct pitchers,
each with 50+ batters faced AND a real start count), then switch to
the live-computed equivalent from that same real data, self-correcting
as the season's true conditions evolve rather than staying frozen at
the day this was written. Below that threshold, behavior is IDENTICAL
to the original static-only version - so early-season results don't
change at all. This needed no new data either - PitcherKStat already
has games_started, and this queries the database this system already
keeps (no MLB API calls), so unlike every other league rate in this
system, no staleness-caching layer is needed for it: the underlying
data only grows as more games get displayed, so it's always exactly
as fresh as this dashboard's own accumulated history.

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

MIN_QUALIFYING_PITCHERS_FOR_LIVE_BASELINE = 30

# Verbatim from core.py - deliberately static, see module docstring.
LEAGUE_PITCHER_HIT_RATE_PER_BF = 0.2224
LEAGUE_AVG_HITS_ALLOWED_PER_START = 5.02

# Verbatim from core.py's pitcher_hits_allowed_probability.
MIN_PITCHER_BATTERS_FACED = 50
MIN_LINEUP_BATTERS_WITH_DATA = 5
MIN_BATTER_AB_TO_TRUST_OWN_RATE = 30
DEFAULT_PITCHER_SHRINKAGE_K = 800
DEFAULT_BATTER_SHRINKAGE_K = 100


def get_hybrid_baselines(db) -> tuple:
    """
    Returns (league_pitcher_hit_rate_per_bf, league_avg_hits_allowed_per_start)
    - the live-computed equivalents once 30+ distinct pitchers qualify
    (50+ batters faced AND a real start count on file - "starts" is
    what converts hits_allowed, a season-to-date CUMULATIVE total, into
    a per-start figure), falling back to the validated static constants
    below that threshold, so early behavior is identical to what was
    actually backtested. Pure local DB query - no MLB API call, so
    unlike every other league rate in this system, this needs no
    staleness cache; it naturally reflects whatever real pitcher data
    has already accumulated in PitcherHitsStat/PitcherKStat from every
    game this dashboard has ever displayed (a set that only grows).
    """
    pitchers = db.query(PitcherHitsStat).all()
    qualifying = []
    for ph in pitchers:
        pk = db.get(PitcherKStat, ph.pitcher_id)
        if pk and pk.batters_faced >= MIN_PITCHER_BATTERS_FACED and pk.games_started > 0:
            qualifying.append((ph.hits_allowed, pk.batters_faced, pk.games_started))

    if len(qualifying) < MIN_QUALIFYING_PITCHERS_FOR_LIVE_BASELINE:
        return LEAGUE_PITCHER_HIT_RATE_PER_BF, LEAGUE_AVG_HITS_ALLOWED_PER_START

    total_hits = sum(h for h, bf, gs in qualifying)
    total_bf = sum(bf for h, bf, gs in qualifying)
    total_starts = sum(gs for h, bf, gs in qualifying)

    return total_hits / total_bf, total_hits / total_starts


def get_pitcher_hits_allowed_index(pitcher_id: int | None, league_pitcher_hit_rate: float | None = None) -> float | None:
    """This pitcher's own hits-allowed rate (per batter faced) relative
    to the league baseline, SHRUNK the same way the actual mean
    calculation is - verbatim DEFAULT_PITCHER_SHRINKAGE_K formula, not
    the raw rate. Same fix applied to Pitcher K's own display index
    after a real case (Brady Basso) showed the raw rate can look
    contradictory next to a small-sample pitcher's actual (properly
    regressed) prediction. Shown regardless of sample size (purely
    informational either way - MIN_PITCHER_BATTERS_FACED still
    controls whether a probability gets computed at all).
    league_pitcher_hit_rate: pass in the caller's already-computed
    hybrid baseline to avoid recomputing it per pitcher - same "once
    per request" reasoning as every other prop."""
    if not pitcher_id:
        return None
    db = SessionLocal()
    try:
        if league_pitcher_hit_rate is None:
            league_pitcher_hit_rate = get_hybrid_baselines(db)[0]
        pitcher_hits = db.get(PitcherHitsStat, pitcher_id)
        pitcher_k = db.get(PitcherKStat, pitcher_id)
        if not pitcher_hits or not pitcher_k or pitcher_k.batters_faced <= 0:
            return None
        shrunk_rate = (pitcher_hits.hits_allowed + DEFAULT_PITCHER_SHRINKAGE_K * league_pitcher_hit_rate) / \
            (pitcher_k.batters_faced + DEFAULT_PITCHER_SHRINKAGE_K)
        return shrunk_rate / league_pitcher_hit_rate
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

        league_pitcher_hit_rate, league_avg_hits_per_start = get_hybrid_baselines(db)

        pitcher_hits_allowed_index = get_pitcher_hits_allowed_index(pitcher_id, league_pitcher_hit_rate=league_pitcher_hit_rate)

        lineup_hit_index, batters_with_data = _lineup_hit_factor(db, game_pk, batting_team_side, la_b9)

        mean = None
        if pitcher_k.batters_faced >= MIN_PITCHER_BATTERS_FACED and \
                batters_with_data >= MIN_LINEUP_BATTERS_WITH_DATA and lineup_hit_index is not None:

            shrunk_pitcher_rate = (pitcher_hits.hits_allowed + DEFAULT_PITCHER_SHRINKAGE_K * league_pitcher_hit_rate) / \
                (pitcher_k.batters_faced + DEFAULT_PITCHER_SHRINKAGE_K)
            pitcher_hit_index = shrunk_pitcher_rate / league_pitcher_hit_rate

            mean = pitcher_hit_index * lineup_hit_index * league_avg_hits_per_start

        return {
            "mean": mean,
            "pitcher_hits_allowed_index": pitcher_hits_allowed_index,
            "opposing_lineup_hit_index": lineup_hit_index,
        }
    finally:
        db.close()
