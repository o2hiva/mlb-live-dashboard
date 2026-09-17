"""
Platoon (batter-vs-pitcher-hand) data - the two pieces core.py's
hr_probability_platoon_adjusted / hits_probability_platoon_adjusted
need beyond what Hits/HRR/HR already track:

  1. Pitcher hand (fetch_pitcher_hands.py) - stable, cached indefinitely.
  2. Batter platoon splits (fetch_batter_platoon_splits.py) - REAL season
     Hits/HR rate specifically vs LHP and vs RHP, as factors (ratio to
     league average). That script's own author flags this endpoint as
     untested against live data ("the most speculative part... paste
     back exactly what happened") - see /api/debug/platoon-split/{name}
     in main.py to verify it before this gets wired into any formula's
     actual math.

Minimum 15 AB against a given hand to trust a split - verbatim from
fetch_batter_platoon_splits.py's own factor() gate.
"""
import logging
from datetime import datetime, timedelta

import mlb_client
from database import SessionLocal
from models_db import PitcherHand, BatterPlatoonSplit

log = logging.getLogger("platoon_stats_sync")

MIN_SPLIT_AB = 15

# Pitcher hand never changes - a long "staleness" window is really just
# a safety net against a data-entry correction, not a real refresh need.
HAND_STALE_AFTER = timedelta(days=180)
SPLIT_STALE_AFTER = timedelta(hours=24)


def get_pitcher_hand(pitcher_id: int, pitcher_name: str, force: bool = False) -> str | None:
    """Returns 'L'/'R' for this pitcher, fetching+caching on first ask
    (or if force=True). Returns None if the lookup fails - callers
    should treat that as "hand unknown", not an error."""
    db = SessionLocal()
    try:
        row = db.get(PitcherHand, pitcher_id)
        if row and not force and datetime.utcnow() - row.updated_at < HAND_STALE_AFTER:
            return row.hand
        try:
            hand = mlb_client.get_pitch_hand(pitcher_id)
        except Exception:
            log.exception("Failed to fetch pitch hand for %s (%s)", pitcher_name, pitcher_id)
            return row.hand if row else None
        if hand is None:
            return row.hand if row else None
        if row is None:
            db.add(PitcherHand(pitcher_id=pitcher_id, pitcher_name=pitcher_name, hand=hand))
        else:
            row.pitcher_name = pitcher_name
            row.hand = hand
        db.commit()
        return hand
    finally:
        db.close()


def get_batter_hand(batter_id: int, batter_name: str, force: bool = False) -> str | None:
    """
    Returns 'L'/'R'/'S' (switch-hitter) for this batter, fetching+
    caching on first ask (or if force=True). Stored on the same
    BatterPlatoonSplit row as the vs-L/vs-R factors, but with its OWN
    much-longer staleness window (bat_side never changes, unlike those
    factors which refresh every 24h) - checked via bat_side_updated_at,
    not the row's own updated_at, so this doesn't get needlessly
    re-fetched just because the split factors happen to be refreshing.
    Returns None if the lookup fails - callers should treat that as
    "side unknown", not an error.
    """
    db = SessionLocal()
    try:
        row = db.get(BatterPlatoonSplit, batter_id)
        if row and row.bat_side and not force and row.bat_side_updated_at and \
                datetime.utcnow() - row.bat_side_updated_at < HAND_STALE_AFTER:
            return row.bat_side
        try:
            side = mlb_client.get_bat_side(batter_id)
        except Exception:
            log.exception("Failed to fetch bat side for %s (%s)", batter_name, batter_id)
            return row.bat_side if row else None
        if side is None:
            return row.bat_side if row else None
        if row is None:
            row = BatterPlatoonSplit(batter_id=batter_id, batter_name=batter_name,
                                      bat_side=side, bat_side_updated_at=datetime.utcnow())
            db.add(row)
        else:
            row.batter_name = batter_name
            row.bat_side = side
            row.bat_side_updated_at = datetime.utcnow()
        db.commit()
        return side
    finally:
        db.close()


def _factor(split_stat: dict | None, count_key: str, league_rate: float | None) -> float | None:
    """Verbatim from fetch_batter_platoon_splits.py's factor() - None if
    under 15 AB against this hand (too small a sample to trust)."""
    if not split_stat or not league_rate:
        return None
    ab = split_stat.get("atBats", 0) or 0
    if ab < MIN_SPLIT_AB:
        return None
    count = split_stat.get(count_key, 0) or 0
    return round((count / ab) / league_rate, 3)


def sync_batter_platoon_split(batter_id: int, batter_name: str, season: int,
                               la_b9: float, la_hr_rate: float, force: bool = False) -> BatterPlatoonSplit | None:
    """Fetches and caches one batter's real vs-L/vs-R Hits and HR
    factors. Safe to call repeatedly - skips the fetch if already fresh
    within SPLIT_STALE_AFTER, unless force=True."""
    db = SessionLocal()
    try:
        row = db.get(BatterPlatoonSplit, batter_id)
        if row and not force and datetime.utcnow() - row.updated_at < SPLIT_STALE_AFTER:
            return row

        try:
            vl = mlb_client.get_batter_platoon_split(batter_id, season, "vl")
            vr = mlb_client.get_batter_platoon_split(batter_id, season, "vr")
        except Exception:
            log.exception("Failed to fetch platoon splits for %s (%s)", batter_name, batter_id)
            return row

        values = {
            "hits_factor_vs_l": _factor(vl, "hits", la_b9),
            "hits_factor_vs_r": _factor(vr, "hits", la_b9),
            "hr_factor_vs_l": _factor(vl, "homeRuns", la_hr_rate),
            "hr_factor_vs_r": _factor(vr, "homeRuns", la_hr_rate),
        }
        if row is None:
            row = BatterPlatoonSplit(batter_id=batter_id, batter_name=batter_name, **values)
            db.add(row)
        else:
            row.batter_name = batter_name
            for k, v in values.items():
                setattr(row, k, v)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def get_batter_platoon_factor(batter_id: int, hand: str | None, stat: str) -> float | None:
    """stat: 'hits' or 'hr'. Returns None if no split on file, no
    opposing-pitcher hand known, or that split didn't meet the 15 AB
    minimum - callers should fall back to the blended (non-platoon)
    factor in every one of those cases, same as core.py does."""
    if not hand or hand not in ("L", "R"):
        return None
    db = SessionLocal()
    try:
        row = db.get(BatterPlatoonSplit, batter_id)
        if not row:
            return None
        return getattr(row, f"{stat}_factor_vs_{hand.lower()}", None)
    finally:
        db.close()
