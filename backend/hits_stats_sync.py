"""
Hits prop: lineup detection + real batter/pitcher season stats + the
shrinkage-adjusted probability math, ported from core.py's
hits_probability_shrinkage_adjusted.

Three pieces:
  1. Lineup detection (check_and_sync_lineups) - once MLB posts a
     game's official lineup (via the dedicated boxscore endpoint, see
     mlb_client.extract_boxscore_lineup), records the 9 confirmed
     batters and fetches each one's + the opposing pitcher's real
     season stats.
  2. League-average hit rate (get_league_average_hit_rate) - core.py
     reads this from an Excel cell (League Averages!B9) that this
     live dashboard has no equivalent source for, so it's computed
     directly from real data instead: total hits / total at-bats
     summed across all 30 teams. Cached for 24h at a time.
  3. The probability math itself (compute_batter_hits_inputs) - same
     shrinkage formula as core.py, just returns (n_ab, p) rather than
     a single probability, so the frontend can compute "at least H
     hits" for any H instantly without a server round-trip.
"""
import logging
import math
from datetime import datetime, timedelta

import mlb_client
import ballpark_factors
from database import SessionLocal
from models_db import Game, LineupBatter, BatterSeasonStat, PitcherHitsStat, SyncState

log = logging.getLogger("hits_stats_sync")

SEASON = datetime.utcnow().year

# Same minimum-sample gates as core.py's hits_probability_shrinkage_adjusted.
MIN_BATTER_AB = 20
MIN_PITCHER_OUTS = 30

# Same shrinkage constant as core.py's DEFAULT_LIVE_SHRINKAGE_K.
DEFAULT_LIVE_SHRINKAGE_K = 840

STAT_STALE_AFTER = timedelta(hours=24)
LEAGUE_RATE_KEY = "league_avg_hit_rate"
LEAGUE_RATE_DATE_KEY = "league_avg_hit_rate_computed_at"


def _excel_round(x, digits=0):
    """Excel's ROUND: half away from zero, unlike Python's banker's-rounding round(). Verbatim from core.py."""
    factor = 10 ** digits
    return math.floor(x * factor + 0.5) / factor if x >= 0 else math.ceil(x * factor - 0.5) / factor


def get_league_average_hit_rate(force: bool = False) -> float:
    """
    Real, live-computed league-average hit rate (total hits / total
    at-bats across all 30 teams), recomputed at most once every 24h
    unless force=True (used by the end-of-day job to guarantee a fresh
    pull once all of a day's games are final, rather than waiting for
    the cache to naturally expire). Falls back to a rough historical
    MLB average (~0.245) if the live computation fails for any reason,
    rather than crashing predictions.
    """
    db = SessionLocal()
    try:
        rate_row = db.get(SyncState, LEAGUE_RATE_KEY)
        date_row = db.get(SyncState, LEAGUE_RATE_DATE_KEY)
        if not force and rate_row and date_row:
            computed_at = datetime.fromisoformat(date_row.value)
            if datetime.utcnow() - computed_at < STAT_STALE_AFTER:
                return float(rate_row.value)

        total_ab, total_hits = 0, 0
        for team_id in mlb_client.ALL_TEAM_IDS:
            try:
                totals = mlb_client.get_team_season_hitting_totals(team_id, SEASON)
            except Exception:
                continue
            if totals:
                total_ab += totals["ab"]
                total_hits += totals["hits"]

        if total_ab == 0:
            return 0.245  # rough historical MLB-average fallback

        rate = total_hits / total_ab

        if rate_row is None:
            db.add(SyncState(key=LEAGUE_RATE_KEY, value=str(rate)))
        else:
            rate_row.value = str(rate)
        if date_row is None:
            db.add(SyncState(key=LEAGUE_RATE_DATE_KEY, value=datetime.utcnow().isoformat()))
        else:
            date_row.value = datetime.utcnow().isoformat()
        db.commit()
        return rate
    except Exception:
        log.exception("get_league_average_hit_rate failed - using fallback")
        return 0.245
    finally:
        db.close()


def _sync_batter_stat(db, batter_id: int, batter_name: str, force: bool = False):
    row = db.get(BatterSeasonStat, batter_id)
    if not force and row and datetime.utcnow() - row.updated_at < STAT_STALE_AFTER:
        return
    try:
        totals = mlb_client.get_season_hitting_totals(batter_id, SEASON)
    except Exception:
        log.exception("Failed to fetch season hitting totals for %s (%s)", batter_name, batter_id)
        return
    if totals is None:
        return
    if row is None:
        db.add(BatterSeasonStat(batter_id=batter_id, batter_name=batter_name,
                                 ab=totals["ab"], hits=totals["hits"],
                                 hr=totals["hr"], bb=totals["bb"],
                                 strikeouts=totals["strikeouts"], plate_appearances=totals["pa"]))
    else:
        row.batter_name = batter_name
        row.ab = totals["ab"]
        row.hits = totals["hits"]
        row.hr = totals["hr"]
        row.bb = totals["bb"]
        row.strikeouts = totals["strikeouts"]
        row.plate_appearances = totals["pa"]


def _sync_pitcher_hits_stat(db, pitcher_id: int, pitcher_name: str, force: bool = False):
    row = db.get(PitcherHitsStat, pitcher_id)
    if not force and row and datetime.utcnow() - row.updated_at < STAT_STALE_AFTER:
        return
    try:
        totals = mlb_client.get_season_pitching_totals(pitcher_id, SEASON)
    except Exception:
        log.exception("Failed to fetch season pitching totals for %s (%s)", pitcher_name, pitcher_id)
        return
    if totals is None:
        return
    if row is None:
        db.add(PitcherHitsStat(pitcher_id=pitcher_id, pitcher_name=pitcher_name,
                                outs=totals["outs"], hits_allowed=totals["hits_allowed"],
                                hr_allowed=totals["hr_allowed"], bb_allowed=totals["bb_allowed"],
                                runs_allowed=totals.get("runs_allowed", 0)))
    else:
        row.pitcher_name = pitcher_name
        row.outs = totals["outs"]
        row.hits_allowed = totals["hits_allowed"]
        row.hr_allowed = totals["hr_allowed"]
        row.bb_allowed = totals["bb_allowed"]
        row.runs_allowed = totals.get("runs_allowed", 0)


def refresh_all_stats_for_game(db, game: Game, force: bool = False) -> int:
    """
    Force-refreshes season stats for every batter in this game's
    confirmed lineups (both sides) plus both probable pitchers. Used by
    the end-of-day job (see end_of_day.py) to guarantee fresh numbers
    once a day's games are final, rather than waiting for the lazy
    per-lineup-confirmation path's 24h staleness cache to naturally
    expire on its own. Returns how many batter/pitcher rows were touched.
    """
    count = 0
    batters = db.query(LineupBatter).filter_by(game_pk=game.game_pk).all()
    for b in batters:
        _sync_batter_stat(db, b.batter_id, b.batter_name, force=force)
        _sync_batter_platoon(db, b.batter_id, b.batter_name)
        count += 1
    for pid, pname in ((game.home_probable_pitcher_id, game.home_probable_pitcher),
                        (game.away_probable_pitcher_id, game.away_probable_pitcher)):
        if pid:
            _sync_pitcher_hits_stat(db, pid, pname or "", force=force)
            _sync_pitcher_hand(pid, pname or "")
            _sync_pitcher_k(pid, pname or "", force=force)
            count += 1
    return count


def check_and_sync_lineups(db, game: Game, boxscore: dict):
    """
    Checks one game's boxscore for a newly-confirmed lineup on either
    side. If found: replaces that side's LineupBatter rows and syncs
    real season stats for each of the 9 batters plus the opposing
    starting pitcher. Call this once per not-yet-started game per sync
    cycle (see poller.py) - cheap no-op once a side is already confirmed
    and its stats are fresh (within STAT_STALE_AFTER).

    Uses the CALLER'S db session (same one poller.py's sync_schedule is
    already using) rather than opening its own - avoids two sessions
    both holding uncommitted changes to the same Game row.
    """
    for side, opposing_pitcher_id, opposing_pitcher_name, confirmed_flag_attr in (
        ("home", game.away_probable_pitcher_id, game.away_probable_pitcher, "home_lineup_confirmed"),
        ("away", game.home_probable_pitcher_id, game.home_probable_pitcher, "away_lineup_confirmed"),
    ):
        confirmed, batters = mlb_client.extract_boxscore_lineup(boxscore, side)
        setattr(game, confirmed_flag_attr, confirmed)

        if not confirmed:
            continue

        # Skip based on whether batters are ACTUALLY STORED, not just the
        # confirmed flag - a game confirmed under an earlier, buggy
        # extraction could have the flag set to True with zero batters
        # ever recorded. Checking the flag alone would permanently skip
        # re-syncing those games after a parsing fix; checking real
        # stored rows self-heals them on the very next cycle instead.
        already_has_batters = db.query(LineupBatter).filter_by(
            game_pk=game.game_pk, team_side=side
        ).count() > 0
        if already_has_batters:
            continue

        db.query(LineupBatter).filter_by(game_pk=game.game_pk, team_side=side).delete()
        for b in batters:
            db.add(LineupBatter(game_pk=game.game_pk, team_side=side,
                                 batter_id=b["id"], batter_name=b["name"],
                                 batting_order=b["batting_order"]))
            _sync_batter_stat(db, b["id"], b["name"])
            _sync_batter_platoon(db, b["id"], b["name"])

        if opposing_pitcher_id:
            _sync_pitcher_hits_stat(db, opposing_pitcher_id, opposing_pitcher_name or "")
            _sync_pitcher_hand(opposing_pitcher_id, opposing_pitcher_name or "")
            _sync_pitcher_k(opposing_pitcher_id, opposing_pitcher_name or "")


def _sync_batter_platoon(db, batter_id: int, batter_name: str):
    """Syncs this batter's real vs-L/vs-R Hits/HR factors AND their bat
    side (see platoon_stats_sync.py), used by compute_batter_hits_inputs
    and hr_stats_sync.compute_hr_inputs to swap in a hand-specific rate
    when available. Best-effort: a failure here doesn't block the
    batter's own season-stat sync above. Local imports avoid a circular
    import (hrr_stats_sync imports this module at its own top level)."""
    try:
        import platoon_stats_sync
        import hrr_stats_sync
        la_b9 = get_league_average_hit_rate()
        la_hr_rate = hrr_stats_sync.get_league_hrr_rates()["hr_rate"]
        platoon_stats_sync.sync_batter_platoon_split(batter_id, batter_name, SEASON, la_b9, la_hr_rate)
        platoon_stats_sync.get_batter_hand(batter_id, batter_name)
    except Exception:
        log.exception("Failed to sync platoon split for %s (%s)", batter_name, batter_id)


def _sync_pitcher_hand(pitcher_id: int, pitcher_name: str):
    """Syncs this pitcher's throwing hand (see platoon_stats_sync.py) -
    a one-time fetch per pitcher in practice, since it's cached
    indefinitely (a hand never changes)."""
    try:
        import platoon_stats_sync
        platoon_stats_sync.get_pitcher_hand(pitcher_id, pitcher_name)
    except Exception:
        log.exception("Failed to sync pitch hand for %s (%s)", pitcher_name, pitcher_id)


def _sync_pitcher_k(pitcher_id: int, pitcher_name: str, force: bool = False):
    """Syncs this pitcher's real season K/batters-faced/starts/outs
    (see pitcher_k_sync.py) - separate table from PitcherHitsStat
    (different data, different model) even though it's the same pitcher."""
    try:
        import pitcher_k_sync
        pitcher_k_sync.sync_pitcher_k_stat(pitcher_id, pitcher_name, force=force)
    except Exception:
        log.exception("Failed to sync pitcher K stat for %s (%s)", pitcher_name, pitcher_id)


def get_pitcher_hit_index(pitcher_id: int | None) -> float | None:
    """
    A pitcher's hits-allowed rate relative to league average - e.g. 1.10
    means this pitcher allows hits 10% more often than a league-average
    pitcher; 0.90 means 10% less often. Shown regardless of sample size
    (purely informational), unlike the shrinkage-adjusted probability
    which requires a minimum real sample before trusting it.
    """
    if not pitcher_id:
        return None
    db = SessionLocal()
    try:
        pitcher = db.get(PitcherHitsStat, pitcher_id)
        if not pitcher or pitcher.outs <= 0:
            return None
        league_rate = get_league_average_hit_rate()
        pitcher_batters_faced = pitcher.outs / 3 * 4.3
        pitcher_rate = pitcher.hits_allowed / pitcher_batters_faced
        return pitcher_rate / league_rate
    finally:
        db.close()


def compute_batter_hits_inputs(batter_id: int, batting_order: int, pitcher_id: int | None,
                                home_team: str | None = None) -> dict | None:
    """
    Returns {"n_ab":, "p":, "hit_index":} for one batter:

      - hit_index: this batter's own hit rate (hits/AB) relative to
        league average - e.g. 1.15 means hitting 15% more often than a
        league-average batter. Shown as soon as the batter has ANY
        at-bats on file, independent of the shrinkage minimum-sample
        gate below (it's just a descriptive stat, not a probability).

      - n_ab / p: shrinkage-adjusted inputs for "at least H hits", None
        if there isn't yet enough real sample to trust them (verbatim
        gates from core.py: 20 AB / 30 outs) OR the opposing pitcher's
        stats haven't synced yet. Now includes the ballpark HIT factor
        (core.py's X17) for the game's actual venue - home_team
        identifies which park (the home team's), applying to BOTH
        teams' batters since it's about the park, not the team.

    Returns None only if there's no batter data at all yet (stats
    haven't synced for this player).
    """
    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        if not batter:
            return None

        league_rate = get_league_average_hit_rate()
        hit_index = (batter.hits / batter.ab) / league_rate if batter.ab > 0 else None

        pitcher = db.get(PitcherHitsStat, pitcher_id) if pitcher_id else None
        ballpark_hit_factor = ballpark_factors.get_ballpark_factors(home_team)["hits"] if home_team else 1.0

        n_ab, p = None, None
        if pitcher and batter.ab >= MIN_BATTER_AB and pitcher.outs >= MIN_PITCHER_OUTS:
            pitcher_batters_faced = pitcher.outs / 3 * 4.3
            n_ab = _excel_round(4.073 - 0.0897 * (batting_order - 1))
            n_ab = max(1, int(n_ab))

            shrunk_batter_rate = (batter.hits + DEFAULT_LIVE_SHRINKAGE_K * league_rate) / \
                (batter.ab + DEFAULT_LIVE_SHRINKAGE_K)
            shrunk_pitcher_rate = (pitcher.hits_allowed + DEFAULT_LIVE_SHRINKAGE_K * league_rate) / \
                (pitcher_batters_faced + DEFAULT_LIVE_SHRINKAGE_K)

            p = league_rate * (shrunk_batter_rate / league_rate) * (shrunk_pitcher_rate / league_rate) * \
                ballpark_hit_factor
            p = max(0.01, min(p, 0.7))  # same sanity clamp as core.py

        return {"n_ab": n_ab, "p": p, "hit_index": hit_index}
    finally:
        db.close()
