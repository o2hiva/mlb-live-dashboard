"""
Hits prop: lineup detection + real batter/pitcher season stats + the
shrinkage-adjusted probability math, ported from core.py's
hits_probability_shrinkage_adjusted.

Three pieces:
  1. Lineup detection (check_and_sync_lineups) - once MLB posts a
     game's official lineup (via the live-feed boxscore, see
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


def get_league_average_hit_rate() -> float:
    """
    Real, live-computed league-average hit rate (total hits / total
    at-bats across all 30 teams), recomputed at most once every 24h.
    Falls back to a rough historical MLB average (~0.245) if the live
    computation fails for any reason, rather than crashing predictions.
    """
    db = SessionLocal()
    try:
        rate_row = db.get(SyncState, LEAGUE_RATE_KEY)
        date_row = db.get(SyncState, LEAGUE_RATE_DATE_KEY)
        if rate_row and date_row:
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


def _sync_batter_stat(db, batter_id: int, batter_name: str):
    row = db.get(BatterSeasonStat, batter_id)
    if row and datetime.utcnow() - row.updated_at < STAT_STALE_AFTER:
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
                                 ab=totals["ab"], hits=totals["hits"]))
    else:
        row.batter_name = batter_name
        row.ab = totals["ab"]
        row.hits = totals["hits"]


def _sync_pitcher_hits_stat(db, pitcher_id: int, pitcher_name: str):
    row = db.get(PitcherHitsStat, pitcher_id)
    if row and datetime.utcnow() - row.updated_at < STAT_STALE_AFTER:
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
                                outs=totals["outs"], hits_allowed=totals["hits_allowed"]))
    else:
        row.pitcher_name = pitcher_name
        row.outs = totals["outs"]
        row.hits_allowed = totals["hits_allowed"]


def check_and_sync_lineups(db, game: Game, feed: dict):
    """
    Checks one game's live-feed boxscore for a newly-confirmed lineup on
    either side. If found: replaces that side's LineupBatter rows and
    syncs real season stats for each of the 9 batters plus the opposing
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
        confirmed, batters = mlb_client.extract_boxscore_lineup(feed, side)
        was_confirmed = getattr(game, confirmed_flag_attr)
        setattr(game, confirmed_flag_attr, confirmed)

        if not confirmed:
            continue
        if was_confirmed:
            continue  # already recorded this lineup, nothing new to sync

        db.query(LineupBatter).filter_by(game_pk=game.game_pk, team_side=side).delete()
        for b in batters:
            db.add(LineupBatter(game_pk=game.game_pk, team_side=side,
                                 batter_id=b["id"], batter_name=b["name"],
                                 batting_order=b["batting_order"]))
            _sync_batter_stat(db, b["id"], b["name"])

        if opposing_pitcher_id:
            _sync_pitcher_hits_stat(db, opposing_pitcher_id, opposing_pitcher_name or "")


def compute_batter_hits_inputs(batter_id: int, batting_order: int, pitcher_id: int | None) -> dict | None:
    """
    Returns {"n_ab": <int>, "p": <float>} for one batter, or None if
    there isn't enough real season data yet to trust a shrunk rate
    (matches core.py's own minimum-sample gates: 20 AB / 30 outs).

    n_ab: estimated at-bats for this game, from lineup position alone
    (verbatim formula from core.py's load_hits_raw_inputs: batters
    lower in the order get fewer expected at-bats).

    p: shrinkage-adjusted per-at-bat hit probability, verbatim math
    from core.py's hits_probability_shrinkage_adjusted, using a live
    computed league rate instead of the Excel LA_B9 cell.

    The final "at least H hits" probability (for any H) is left to the
    caller/frontend - it's a simple binomial calc from (n_ab, p), cheap
    enough to redo instantly client-side whenever the viewer changes H.
    """
    db = SessionLocal()
    try:
        batter = db.get(BatterSeasonStat, batter_id)
        pitcher = db.get(PitcherHitsStat, pitcher_id) if pitcher_id else None
        if not batter or not pitcher:
            return None
        if batter.ab < MIN_BATTER_AB or pitcher.outs < MIN_PITCHER_OUTS:
            return None

        league_rate = get_league_average_hit_rate()

        n_ab = _excel_round(4.073 - 0.0897 * (batting_order - 1))
        n_ab = max(1, int(n_ab))

        shrunk_batter_rate = (batter.hits + DEFAULT_LIVE_SHRINKAGE_K * league_rate) / \
            (batter.ab + DEFAULT_LIVE_SHRINKAGE_K)
        pitcher_batters_faced = pitcher.outs / 3 * 4.3
        shrunk_pitcher_rate = (pitcher.hits_allowed + DEFAULT_LIVE_SHRINKAGE_K * league_rate) / \
            (pitcher_batters_faced + DEFAULT_LIVE_SHRINKAGE_K)

        p = league_rate * (shrunk_batter_rate / league_rate) * (shrunk_pitcher_rate / league_rate)
        p = max(0.01, min(p, 0.7))  # same sanity clamp as core.py

        return {"n_ab": n_ab, "p": p}
    finally:
        db.close()
