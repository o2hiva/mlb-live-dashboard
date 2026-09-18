"""
Pitcher K (strikeout) prop - ported from the UPDATED core.py's
pitcher_k_probability_shrinkage_adjusted, which replaced the original
whole-season "opposing team" factor with a genuinely LINEUP-SPECIFIC
blend of the real 9 confirmed batters facing this pitcher today - a
validated improvement (backtest z went from -2.88 with the whole-team
version to -2.27 with this one). No new fetch script was needed for
this - the per-batter strikeouts/PA this needs come from the exact
same MLB API call Hits already makes for each confirmed batter (see
mlb_client.get_season_hitting_totals and hits_stats_sync's
_sync_batter_stat, which now also store these two fields).

DISTRIBUTION: Normal approximation to Binomial(n, p) - unlike HRR, this
formula never got a Negative Binomial correction in core.py, so it's
ported as-is (Normal), matching what's actually validated there.

TWO REAL DATA GAPS, both disclosed honestly rather than silently
guessed at:

  1. "Med IP" (median innings pitched per start) is read from a
     manually-maintained Excel cell in the original workbook - there's
     no direct MLB API equivalent. This uses AVERAGE innings per start
     (total outs / 3 / games started) as a stand-in. Reasonable for a
     pure starter, but a REAL, CONFIRMED PROBLEM for anyone who's also
     pitched in relief: "outs" is total season outs across EVERY
     appearance, while "games_started" only counts starts, so a
     swingman/rookie-callup's relief innings get misattributed as
     extra innings-per-START. Confirmed with real data (Wilber Dotel,
     2 starts but relief innings mixed in, computed to 18.67 "innings
     per start" - impossible for any real start), which produced a
     mean of 16.7 strikeouts and a probability rounding to 100%. Now
     clamped to 3.0-7.5 innings (a range no genuine start falls outside
     of) as a safety net, pending a real fix: fetching starts-only
     innings needs a separate API call this basic season-stats
     endpoint doesn't provide.

  2. Batters with under 30 real PA against this specific matchup
     context are treated as exactly league-average (shrunk rate = la_b13
     itself) rather than excluded - verbatim from core.py, matches what
     was actually validated, not an approximation on my part.

HONEST CALIBRATION NOTE FROM core.py ITSELF: even with this
improvement, Pitcher K remains the least-validated of the shrinkage
models - "not fully under the |z|<2 bar yet" for either version. Worth
knowing, not a reason to skip building it.

NO BALLPARK FACTOR: unlike Hits/HRR/HR, core.py's K formula has no
W17/X17/Y17 term anywhere - strikeout rate isn't park-adjusted in this
system. Confirmed by inspection, not an oversight to fix.
"""
import logging
import math
from datetime import datetime, timedelta

import mlb_client
from database import SessionLocal
from models_db import PitcherKStat, BatterSeasonStat, LineupBatter, SyncState

log = logging.getLogger("pitcher_k_sync")

SEASON = datetime.utcnow().year

# Verbatim from core.py's pitcher_k_probability_shrinkage_adjusted.
MIN_PITCHER_BATTERS_FACED = 50
MIN_LINEUP_BATTERS_WITH_DATA = 5   # of the 9 confirmed batters, at least this many need real data
MIN_BATTER_PA_TO_TRUST_OWN_RATE = 30  # below this, a batter is treated as exactly league-average

# Verbatim from core.py's DEFAULT_PITCHER_K_SHRINKAGE_K / DEFAULT_LINEUP_K_SHRINKAGE_K.
DEFAULT_PITCHER_K_SHRINKAGE_K = 800
DEFAULT_LINEUP_K_SHRINKAGE_K = 75

STAT_STALE_AFTER = timedelta(hours=24)
LEAGUE_K_RATE_KEY = "league_k_rate_per_pa"
LEAGUE_K_RATE_DATE_KEY = "league_k_rate_computed_at"


def get_league_k_rate(force: bool = False) -> float:
    """
    LA_B13: real, live-computed league-average strikeout rate per plate
    appearance (summed across all 30 teams' own hitters), recomputed at
    most once every 24h. This is a league-wide baseline, unrelated to
    the lineup-specific change above - still needed as the reference
    rate every shrinkage calculation compares against. Falls back to a
    rough historical MLB average (~0.225) if the live computation fails.
    """
    db = SessionLocal()
    try:
        rate_row = db.get(SyncState, LEAGUE_K_RATE_KEY)
        date_row = db.get(SyncState, LEAGUE_K_RATE_DATE_KEY)
        if rate_row and date_row and not force:
            computed_at = datetime.fromisoformat(date_row.value)
            if datetime.utcnow() - computed_at < STAT_STALE_AFTER:
                return float(rate_row.value)

        total_k, total_pa = 0, 0
        for team_id in mlb_client.ALL_TEAM_IDS:
            try:
                totals = mlb_client.get_team_season_k_stats(team_id, SEASON)
            except Exception:
                continue
            if totals:
                total_k += totals["strikeouts"]
                total_pa += totals["plate_appearances"]

        if total_pa == 0:
            return 0.225

        rate = total_k / total_pa

        if rate_row is None:
            db.add(SyncState(key=LEAGUE_K_RATE_KEY, value=str(rate)))
        else:
            rate_row.value = str(rate)
        if date_row is None:
            db.add(SyncState(key=LEAGUE_K_RATE_DATE_KEY, value=datetime.utcnow().isoformat()))
        else:
            date_row.value = datetime.utcnow().isoformat()
        db.commit()
        return rate
    except Exception:
        log.exception("get_league_k_rate failed - using fallback")
        return 0.225
    finally:
        db.close()


def sync_pitcher_k_stat(pitcher_id: int, pitcher_name: str, force: bool = False):
    """Fetches and caches one pitcher's real season K/batters-faced/
    starts/outs. Safe to call repeatedly - skips if already fresh."""
    db = SessionLocal()
    try:
        row = db.get(PitcherKStat, pitcher_id)
        if row and not force and datetime.utcnow() - row.updated_at < STAT_STALE_AFTER:
            return
        try:
            totals = mlb_client.get_season_pitching_totals(pitcher_id, SEASON)
        except Exception:
            log.exception("Failed to fetch season pitching totals for %s (%s)", pitcher_name, pitcher_id)
            return
        if totals is None:
            return
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


def get_pitcher_k_index(pitcher_id: int | None, la_b13: float | None = None) -> float | None:
    """This pitcher's own K rate (per batter faced) relative to league
    average - shown regardless of sample size (purely informational)."""
    if not pitcher_id:
        return None
    if la_b13 is None:
        la_b13 = get_league_k_rate()
    if la_b13 <= 0:
        return None
    db = SessionLocal()
    try:
        pitcher = db.get(PitcherKStat, pitcher_id)
        if not pitcher or pitcher.batters_faced <= 0:
            return None
        return (pitcher.strikeouts / pitcher.batters_faced) / la_b13
    finally:
        db.close()


def _lineup_k_factor(db, game_pk: int, batting_team_side: str, la_b13: float) -> tuple:
    """
    The genuinely lineup-specific "opposing batters" factor - a blend
    of the real 9 confirmed batters this pitcher actually faces today,
    each shrunk by their OWN real plate-appearance sample (verbatim
    from core.py's pitcher_k_probability_shrinkage_adjusted).

    batting_team_side: the side that's BATTING against this pitcher
    (e.g. if the pitcher is the home starter, this is "away").

    Returns (team_factor, batters_with_data_count) - the caller decides
    whether the count clears the minimum-sample gate.
    """
    rows = db.query(LineupBatter).filter_by(game_pk=game_pk, team_side=batting_team_side).all()
    batter_factors = []
    for r in rows:
        batter = db.get(BatterSeasonStat, r.batter_id)
        if not batter or batter.plate_appearances <= 0:
            continue
        if batter.plate_appearances >= MIN_BATTER_PA_TO_TRUST_OWN_RATE:
            shrunk_rate = (batter.strikeouts + DEFAULT_LINEUP_K_SHRINKAGE_K * la_b13) / \
                (batter.plate_appearances + DEFAULT_LINEUP_K_SHRINKAGE_K)
        else:
            shrunk_rate = la_b13  # not enough own data - treated as exactly league-average
        batter_factors.append(shrunk_rate / la_b13)

    if not batter_factors:
        return None, 0
    return sum(batter_factors) / len(batter_factors), len(batter_factors)


def compute_pitcher_k_inputs(pitcher_id: int, game_pk: int, batting_team_side: str,
                              la_b13: float | None = None) -> dict | None:
    """
    Returns {"mean":, "sd":, "pitcher_k_index":, "opposing_lineup_k_index":}
    for one starting pitcher - mean/sd feed a Normal-approximation "at
    least K strikeouts" probability (the frontend computes this
    instantly for any line via the same normCdf math used elsewhere).
    None if there isn't enough real sample yet: 50+ batters faced for
    the pitcher, AND at least 5 of the opposing lineup's 9 confirmed
    batters need real season data on file.

    batting_team_side: the side BATTING against this pitcher (e.g. if
    this pitcher started for the home team, pass "away" - that's whose
    lineup he actually faces).

    la_b13: pass in the caller's already-computed league K rate to
    avoid a redundant lookup per pitcher - same "once per request, not
    once per row" reasoning as every other prop in this system.

    Returns None only if there's no pitcher data at all yet.
    """
    if la_b13 is None:
        la_b13 = get_league_k_rate()

    db = SessionLocal()
    try:
        pitcher = db.get(PitcherKStat, pitcher_id)
        if not pitcher:
            return None

        pitcher_k_index = get_pitcher_k_index(pitcher_id, la_b13=la_b13)

        team_factor, batters_with_data = _lineup_k_factor(db, game_pk, batting_team_side, la_b13)
        opposing_lineup_k_index = team_factor  # same number IS the display index - no separate calc needed

        mean, sd = None, None
        if pitcher.batters_faced >= MIN_PITCHER_BATTERS_FACED and \
                batters_with_data >= MIN_LINEUP_BATTERS_WITH_DATA and la_b13 > 0:

            shrunk_pitcher_rate = (pitcher.strikeouts + DEFAULT_PITCHER_K_SHRINKAGE_K * la_b13) / \
                (pitcher.batters_faced + DEFAULT_PITCHER_K_SHRINKAGE_K)
            pitcher_factor = shrunk_pitcher_rate / la_b13

            # "Med IP" stand-in: average innings per start from real
            # season outs, not a true median (see module docstring).
            # Falls back to 5 innings, matching core.py's own blank-value
            # fallback, if this pitcher hasn't started yet.
            #
            # SAFETY CLAMP: "outs" is this pitcher's TOTAL season outs
            # across EVERY appearance (starts AND relief), but
            # "games_started" only counts starts - for a pitcher who's
            # also worked in relief (a common swingman/rookie-callup
            # pattern), dividing total outs by starts-only massively
            # overstates innings-per-START. Confirmed with real data:
            # Wilber Dotel (2 starts, but relief innings mixed into his
            # season outs) computed to 18.67 "innings per start" -
            # physically impossible (no starter throws 18+ innings in
            # one game) - which fed a runaway mean (16.7 strikeouts) and
            # a probability that rounded to 100%. Clamped to a range no
            # genuine MLB start falls outside of, until a starts-only
            # innings figure can be fetched (needs a separate API call
            # this basic season-stats endpoint doesn't provide).
            if pitcher.games_started > 0:
                med_ip = (pitcher.outs / 3) / pitcher.games_started
                med_ip = max(3.0, min(med_ip, 7.5))
            else:
                med_ip = 5

            n = med_ip * 4.3
            p = max(0.01, min(la_b13 * pitcher_factor * team_factor, 0.9))
            mean = n * p
            var = n * p * (1 - p)
            if var > 0:
                sd = var ** 0.5
            else:
                mean = None  # matches core.py's own var<=0 fallback-to-None behavior

        return {
            "mean": mean, "sd": sd,
            "pitcher_k_index": pitcher_k_index,
            "opposing_lineup_k_index": opposing_lineup_k_index,
        }
    finally:
        db.close()
