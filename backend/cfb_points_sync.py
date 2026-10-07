"""
cfb_points_sync.py

CFB "Team Points" prop (each team's predicted points scored, with a
Yes/No O/U line) - the live-dashboard port of core_ncaa_points.py /
backtest_ncaa_points_total.py, ported from a local, resumable-JSON
backtest into a live backend sync, same pattern as
nfl_passing_yards_sync.py.

UNLIKE the NFL passing-yards prop, this one needed NO estimation
workaround: CFBD's own /games endpoint (api.collegefootballdata.com)
returns real, final, per-game scores directly (homePoints/awayPoints,
confirmed camelCase fields per backtest_ncaa_points_total.py's own
investigation) - there's no missing-per-game-data problem here at all.
Every completed FBS-vs-FBS game this season is real ground truth.

FORMULA (verbatim from core_ncaa_points.py, validated against the full
2025 season):
    predicted_team_points = team_scoring_index * opp_allowed_index * league_avg_points
    team_scoring_index = shrunk(team's own points-scored history) / league_avg_points
    opp_allowed_index  = shrunk(opponent's points-allowed history) / league_avg_points
    shrinkage_k = 5.0 games (validated via a real k-sweep)
    Negative Binomial overdispersion = 5.2 for a single team's score
    (game-total/combined prop deliberately NOT built yet - team points only, per scope)

POPULATION MATCH: only FBS-vs-FBS games count (mixing in FCS blowouts
would bias both a team's "points allowed" history and the league
average - the same population-mismatch bug class already fixed in the
MLB Hits Allowed prop).

REBUILD STRATEGY: unlike NFL (where a full rebuild needs care around
estimation), this fully rebuilds CfbTeamPointsStat and CfbGame from
scratch every call - walking every week 1..current_week, refetching
each week's real games from CFBD. This is simple, always correct, and
cheap (at most ~15 API calls for a full season) - no incremental/
snapshot state to keep in sync.
"""
import logging
import math
import os
from datetime import datetime, timedelta

import requests

from database import SessionLocal
from models_db import CfbTeamPointsStat, CfbGame, CfbWeekTeamStat

log = logging.getLogger("cfb_points_sync")

API_BASE = "https://api.collegefootballdata.com"
TIMEOUT = 30

MIN_PRIOR_GAMES = 3          # min games of a team's own history before its scoring/allowed avg is trusted
MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE = 20  # min teams w/ >= MIN_PRIOR_GAMES before trusting a LIVE league average

DEFAULT_POINTS_SHRINKAGE_K = 5.0          # validated via 2025 season k-sweep (core_ncaa_points.py)
DEFAULT_TEAM_OVERDISPERSION = 5.2         # validated: sd_z = 0.994 on 1080 real 2025 team-games

# Validated empirical value from ncaa_points_total_progress_2025.json's
# league_points_pool: 1522 real 2025 FBS-vs-FBS team-games, sum/len = 26.239159900131406
LEAGUE_AVG_POINTS_STATIC = 26.24

# Moneyline win-probability layer (ported from core_ncaa_moneyline.py,
# validated out-of-sample: Brier=0.1886, log loss=0.5542, both clearly
# beating the naive "always 50%" baseline of 0.25/0.693 - see
# compute_cfb_moneyline's docstring for the small, accepted, documented
# residual calibration bias noted in that script). Same
# DEFAULT_TEAM_OVERDISPERSION=5.2 constant above doubles as this layer's
# variance term, exactly as the source script uses its own matching
# TEAM_OVERDISPERSION=5.2.
HOME_FIELD_POINTS = 5.0
VARIANCE_SCALE = 1.0

# Spread cover-probability layer (ported from core_ncaa_spread.py, the
# user-supplied production module - validated against REAL sportsbook
# market lines across three seasons, 2023/2024/2025, 1608 games total,
# not just the model's own outcomes). DELIBERATELY uses its OWN
# home-field constant, different from the moneyline one above:
# core_ncaa_spread.py's HOME_FIELD_POINTS=3.19 was re-estimated directly
# against real market lines via a closed-form solve, while the
# moneyline's HOME_FIELD_POINTS=5.0 above was tuned only against the
# model's own outcomes - both are "correct" for their own purpose (see
# that script's docstring for why they diverge; a pooled 3-season
# re-estimate landed at 2.95, close enough to 3.19 to treat as confirmed
# rather than revise again). SPREAD_VARIANCE_SCALE coincides with the
# moneyline VARIANCE_SCALE's value (1.0) but is kept as its own constant
# since the two layers were validated independently.
SPREAD_HOME_FIELD_POINTS = 3.19   # VALIDATED against real market lines (core_ncaa_spread.py) - NOT the same as moneyline's HOME_FIELD_POINTS above
SPREAD_VARIANCE_SCALE = 1.0       # VALIDATED - a global rescale sweep against real market outcomes was tried and found to be a dead end (core_ncaa_spread.py's docstring, Finding 2)

# CONFIRMED (Oct 2026) against real 2025 week-5 CFBD /lines data - see
# core_ncaa_spread.py / backtest_ncaa_spread_probability.py's module
# docstrings for the full sign-convention/provider confirmation.
PREFERRED_PROVIDERS = ["DraftKings", "ESPN Bet", "Bovada"]

# CONFIRMED across 3 real seasons (2023/2024/2025) as where spread
# cover-probability overconfidence concentrates (Finding 3 in
# core_ncaa_spread.py's own findings doc). Games at or above this get
# flagged `lopsided=True`, not suppressed - the frontend decides how to
# caveat them rather than hiding them.
LOPSIDED_THRESHOLD = 13.0

CACHE_TTL = timedelta(hours=6)
_league_avg_cache = {"value": None, "computed_at": None}


def get_api_key() -> str:
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        raise RuntimeError(
            "CFBD_API_KEY environment variable not set. Get a free key at "
            "https://collegefootballdata.com/key and set it in Railway's variables."
        )
    return key


def get_week_games(year: int, week: int, season_type: str = "regular") -> list[dict]:
    """Every game for one week, in CFBD's real, confirmed /games shape
    (camelCase: homeTeam, homePoints, awayTeam, awayPoints, completed,
    homeClassification, awayClassification, id)."""
    resp = requests.get(
        f"{API_BASE}/games",
        params={"year": year, "week": week, "seasonType": season_type},
        headers={"Authorization": f"Bearer {get_api_key()}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_week_lines(year: int, week: int, season_type: str = "regular") -> list[dict]:
    """Every game's real posted lines for one week, from CFBD's /lines
    endpoint - confirmed shape (per core_ncaa_spread.py / backtest_ncaa_
    spread_probability.py): a list of game entries, each with "id"
    (matching the same numeric game id /games returns) and a nested
    "lines" array of {"provider":, "spread":, ...} per sportsbook. A
    game with no book posted yet (common well before kickoff) just has
    an empty/missing "lines" array - handled by pick_provider_line, not
    here."""
    resp = requests.get(
        f"{API_BASE}/lines",
        params={"year": year, "week": week, "seasonType": season_type},
        headers={"Authorization": f"Bearer {get_api_key()}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def pick_provider_line(game: dict, preferred_providers: list[str] = PREFERRED_PROVIDERS) -> tuple[str | None, float | None]:
    """Picks ONE line per game from its nested 'lines' array: the first
    preferred provider (in order) that has a non-null spread, or failing
    that, the first entry in the array with a non-null spread. Returns
    (provider_name, home_spread) or (None, None) if the game has no
    usable line at all yet - ported verbatim from core_ncaa_spread.py."""
    lines = game.get("lines") or []
    by_provider = {l.get("provider"): l for l in lines if l.get("provider")}
    for provider in preferred_providers:
        line = by_provider.get(provider)
        if line is not None and line.get("spread") is not None:
            return provider, float(line["spread"])
    for line in lines:
        if line.get("spread") is not None:
            return line.get("provider"), float(line["spread"])
    return None, None


def is_fbs_vs_fbs(game: dict) -> bool:
    return (
        bool(game.get("completed"))
        and game.get("homeClassification") == "fbs"
        and game.get("awayClassification") == "fbs"
        and game.get("homePoints") is not None
        and game.get("awayPoints") is not None
    )


def _shrunk_avg(sum_val: float, count: int, k: float, league_avg: float) -> float:
    return (sum_val + k * league_avg) / (count + k)


def compute_live_league_avg_points(team_points_counts: dict,
                                    min_qualifying_teams: int = MIN_QUALIFYING_TEAMS_FOR_LIVE_BASELINE,
                                    static_default: float = LEAGUE_AVG_POINTS_STATIC) -> float:
    """Hybrid live/static league-average-points baseline (same pattern as
    every other prop's hybrid baseline in this dashboard). Below
    min_qualifying_teams teams with enough games, falls back to the
    validated static constant from the 2025 season; above it, computes
    the average live from real accumulated data."""
    qualifying = [c for c in team_points_counts.values() if c["games"] >= MIN_PRIOR_GAMES]
    if len(qualifying) < min_qualifying_teams:
        return static_default
    total_points = sum(c["points_scored_sum"] for c in qualifying)
    total_games = sum(c["games"] for c in qualifying)
    if total_games == 0:
        return static_default
    return total_points / total_games


def get_league_avg_points(db) -> float:
    """Cached (6h) live-computed league average, same pattern as
    nfl_passing_yards_sync.get_league_avg_allowed - avoids recomputing
    from every row on every single request."""
    now = datetime.utcnow()
    if _league_avg_cache["value"] is not None and _league_avg_cache["computed_at"] is not None \
            and now - _league_avg_cache["computed_at"] < CACHE_TTL:
        return _league_avg_cache["value"]

    rows = db.query(CfbTeamPointsStat).all()
    team_points_counts = {
        r.team: {"games": r.games, "points_scored_sum": r.points_scored_sum, "points_allowed_sum": r.points_allowed_sum}
        for r in rows
    }
    value = compute_live_league_avg_points(team_points_counts)
    _league_avg_cache["value"] = value
    _league_avg_cache["computed_at"] = now
    return value


def refresh_cfb_points_stats(season: int, current_week: int) -> dict:
    """
    Rebuilds CfbTeamPointsStat (every FBS team's real season-to-date
    points-scored/points-allowed totals, weeks 1..current_week) and
    CfbGame (this week's real matchups, from whatever week=current_week
    games CFBD has posted so far, whether or not they've been played
    yet).

    CFBD's rate limit is MONTHLY (1000 calls/mo on the free tier, per
    https://blog.collegefootballdata.com/api-v2-is-now-in-general-availability/ ),
    not per-minute or per-hour - a burned monthly budget stays burned
    until the next billing cycle, it doesn't cool down. The original
    version of this function re-fetched every week 1..current_week from
    CFBD on every single call (plus a duplicate call for the current
    week's schedule) - a handful of "Refresh Stats" clicks was enough to
    exhaust the free tier for the rest of the month.

    So: every PAST week (< current_week) that's already been fetched
    once is cached team-by-team in CfbWeekTeamStat and reused from then
    on - a finished week's score never changes, so re-fetching it is
    pure waste. Only the current (possibly still in-progress) week is
    always fetched fresh, and that single fetch is reused for both the
    team-totals rebuild and the CfbGame schedule (the old code fetched
    the same week twice). Net effect: 1 CFBD call per refresh once a
    season's past weeks are cached, instead of current_week+1 calls
    every time.
    """
    db = SessionLocal()
    try:
        team_totals: dict[str, dict] = {}
        weeks_fetched = 0
        weeks_from_cache = 0
        this_week_games: list[dict] = []
        errors: list[str] = []   # surfaced to the caller so a failed CFBD fetch (quota, bad key...) is never silent

        cached_weeks = {
            w for (w,) in db.query(CfbWeekTeamStat.week)
                            .filter(CfbWeekTeamStat.season == season, CfbWeekTeamStat.week < current_week)
                            .distinct().all()
        }

        for week in range(1, current_week + 1):
            is_current = (week == current_week)

            if not is_current and week in cached_weeks:
                weeks_from_cache += 1
                for row in db.query(CfbWeekTeamStat).filter_by(season=season, week=week).all():
                    totals = team_totals.setdefault(
                        row.team, {"games": 0, "points_scored_sum": 0, "points_allowed_sum": 0})
                    totals["games"] += row.games
                    totals["points_scored_sum"] += row.points_scored
                    totals["points_allowed_sum"] += row.points_allowed
                continue

            try:
                games = get_week_games(season, week)
            except requests.exceptions.RequestException as e:
                log.exception("Failed to fetch CFB week %s games", week)
                body = ""
                try:
                    body = (e.response.text or "")[:160] if e.response is not None else ""
                except Exception:
                    pass
                errors.append(f"CFBD week {week} fetch failed: {type(e).__name__} {e}{(' - ' + body) if body else ''}")
                continue
            weeks_fetched += 1
            if is_current:
                this_week_games = games  # reused below for CfbGame - avoids a second identical CFBD call

            week_rows: dict[str, dict] = {}
            for game in games:
                if not is_fbs_vs_fbs(game):
                    continue
                home, away = game["homeTeam"], game["awayTeam"]
                home_pts, away_pts = game["homePoints"], game["awayPoints"]

                for team, pts, allowed_pts in ((home, home_pts, away_pts), (away, away_pts, home_pts)):
                    totals = team_totals.setdefault(
                        team, {"games": 0, "points_scored_sum": 0, "points_allowed_sum": 0})
                    totals["games"] += 1
                    totals["points_scored_sum"] += pts
                    totals["points_allowed_sum"] += allowed_pts
                    wrow = week_rows.setdefault(team, {"games": 0, "points_scored": 0, "points_allowed": 0})
                    wrow["games"] += 1
                    wrow["points_scored"] += pts
                    wrow["points_allowed"] += allowed_pts

            if not is_current:
                # A past week that fetched successfully for the first time -
                # cache it so no future refresh ever needs CFBD for it again.
                db.query(CfbWeekTeamStat).filter_by(season=season, week=week).delete()
                for team, wtotals in week_rows.items():
                    db.add(CfbWeekTeamStat(season=season, week=week, team=team, **wtotals))
                db.flush()

        db.query(CfbTeamPointsStat).delete()
        for team, totals in team_totals.items():
            db.add(CfbTeamPointsStat(
                team=team, games=totals["games"],
                points_scored_sum=totals["points_scored_sum"],
                points_allowed_sum=totals["points_allowed_sum"],
            ))

        # Current week's real matchups (may include not-yet-played games -
        # is_fbs_vs_fbs isn't applied here, we want the schedule even
        # before it's final, same as NflGame's own "this week's matchups"
        # table). A team can appear at most once per week in a normal
        # schedule, so team-as-primary-key (like NflGame) is safe here too.
        # Only replace the existing schedule if this call actually got a
        # fresh current-week fetch - if CFBD failed (e.g. quota
        # exhausted), keep whatever CfbGame already has rather than
        # wiping it out to empty.
        if this_week_games:
            # Real posted spread lines for this same week - a SEPARATE CFBD
            # call (one more call per refresh, same monthly-budget caution
            # as everything else here), keyed by CFBD's own numeric game id
            # so each game below can pick its own line. A game with no book
            # posted yet (common well before kickoff) just has no entry -
            # pick_provider_line returns (None, None) for it, which
            # compute_cfb_spread treats as untrusted rather than guessed.
            # A fetch failure here never blocks the rest of the refresh -
            # every game's spread fields simply come back null/untrusted
            # (the points/moneyline predictions are unaffected).
            line_games_by_id: dict = {}
            try:
                for line_game in get_week_lines(season, current_week):
                    line_games_by_id[line_game.get("id")] = line_game
            except requests.exceptions.RequestException as e:
                log.exception("Failed to fetch CFB week %s real spread lines", current_week)
                errors.append(f"CFBD week {current_week} spread lines fetch failed: {type(e).__name__} {e}")

            db.query(CfbGame).delete()
            seen_teams: set = set()   # CfbGame's primary key is the team name: a team listed twice in one week would abort the whole refresh
            for game in this_week_games:
                home, away = game.get("homeTeam"), game.get("awayTeam")
                if not home or not away:
                    continue
                if game.get("homeClassification") != "fbs" or game.get("awayClassification") != "fbs":
                    continue   # FCS/D2/D3 games have no model history - keep the tab FBS-vs-FBS only, as its heading says
                if home in seen_teams or away in seen_teams:
                    errors.append(f"skipped duplicate/second game for {home} vs {away} (id {game.get('id')})")
                    continue
                seen_teams.update((home, away))
                # "startDate" confirmed live (CFBD real field, ISO8601 UTC,
                # e.g. "2026-09-24T23:00:00.000Z") - unlike NFL's schedule
                # API, this one DOES carry a real kickoff time, so CFB Team
                # Points can show/order by actual time, not just date.
                start_date_utc = game.get("startDate")
                # CFBD's real "neutralSite" boolean - a neutral-site game gets
                # no home-field-advantage bump in the moneyline model below
                # (see win_probability's effective_hfa).
                neutral_site = bool(game.get("neutralSite"))
                line_game = line_games_by_id.get(game.get("id"))
                spread_provider, home_spread = pick_provider_line(line_game) if line_game else (None, None)
                db.add(CfbGame(team=home, opponent=away, game_id=game.get("id"), is_home=True,
                                season=season, week=current_week, start_date_utc=start_date_utc,
                                neutral_site=neutral_site, home_spread=home_spread, spread_provider=spread_provider))
                db.add(CfbGame(team=away, opponent=home, game_id=game.get("id"), is_home=False,
                                season=season, week=current_week, start_date_utc=start_date_utc,
                                neutral_site=neutral_site, home_spread=home_spread, spread_provider=spread_provider))

        db.commit()
        _league_avg_cache["value"] = None  # force recompute next read, using the fresh data just written
        games_used = sum(t["games"] for t in team_totals.values()) // 2
        summary = {
            "weeks_fetched": weeks_fetched, "weeks_from_cache": weeks_from_cache,
            "teams": len(team_totals), "games_used": games_used,
            "current_week_matchups": len(this_week_games),
            "errors": errors,
        }
        log.info("refresh_cfb_points_stats complete: %s", summary)
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def compute_cfb_team_points_prediction(team: str, opponent: str, db, league_avg: float | None = None) -> dict | None:
    """Returns {"mean":, "team_index":, "opp_index":, "team_games_sample":,
    "opp_games_sample":} for `team`'s predicted points against `opponent`,
    or None if either side doesn't have MIN_PRIOR_GAMES yet. Mirrors
    nfl_passing_yards_sync.compute_passing_yards_prediction's shape for
    consistency across the dashboard's prop modules."""
    team_row = db.get(CfbTeamPointsStat, team)
    opp_row = db.get(CfbTeamPointsStat, opponent)
    if not team_row or not opp_row or team_row.games < MIN_PRIOR_GAMES or opp_row.games < MIN_PRIOR_GAMES:
        return None

    if league_avg is None:
        league_avg = get_league_avg_points(db)

    shrunk_scored = _shrunk_avg(team_row.points_scored_sum, team_row.games, DEFAULT_POINTS_SHRINKAGE_K, league_avg)
    shrunk_allowed = _shrunk_avg(opp_row.points_allowed_sum, opp_row.games, DEFAULT_POINTS_SHRINKAGE_K, league_avg)

    team_index = shrunk_scored / league_avg
    opp_index = shrunk_allowed / league_avg
    mean = team_index * opp_index * league_avg

    return {
        "mean": mean,
        "team_index": team_index,
        "opp_index": opp_index,
        "team_games_sample": team_row.games,
        "opp_games_sample": opp_row.games,
        "league_avg_points": league_avg,
    }


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    """Pure-stdlib Normal CDF via math.erf - this dashboard avoids scipy/
    numpy/pandas entirely (every validated-model module here uses only
    math.lgamma/math.erf etc.), so core_ncaa_moneyline.py's
    scipy.stats.norm.sf(...) call is ported to this instead of adding a
    new dependency to the Railway deploy."""
    if sigma <= 0:
        return 1.0 if x < mean else 0.0
    z = (x - mean) / (sigma * math.sqrt(2))
    return 0.5 * (1.0 + math.erf(z))


def win_probability(home_predicted: float | None, away_predicted: float | None, neutral_site: bool = False,
                     home_field_points: float = HOME_FIELD_POINTS, variance_scale: float = VARIANCE_SCALE) -> float | None:
    """P(home team wins), ported verbatim (formula-wise) from
    core_ncaa_moneyline.py's win_probability - a Normal approximation to
    the scoring-differential distribution, built on top of each side's
    already-validated predicted points (compute_cfb_team_points_prediction's
    "mean"). Returns None (never 0%/100%) if either side's predicted
    points is missing or non-positive, same "can't trust this one" signal
    the source script calls `trusted`."""
    if home_predicted is None or away_predicted is None or home_predicted <= 0 or away_predicted <= 0:
        return None
    effective_hfa = 0.0 if neutral_site else home_field_points
    predicted_diff = (home_predicted - away_predicted) + effective_hfa
    variance_diff = variance_scale * (DEFAULT_TEAM_OVERDISPERSION * home_predicted + DEFAULT_TEAM_OVERDISPERSION * away_predicted)
    sigma = math.sqrt(variance_diff) if variance_diff > 0 else 1.0
    # P(home wins) = P(diff > 0) = 1 - CDF(0) = norm.sf(0, loc=predicted_diff, scale=sigma)
    return 1.0 - _normal_cdf(0.0, predicted_diff, sigma)


def compute_cfb_moneyline(home_team: str, away_team: str, db, league_avg: float | None = None,
                           neutral_site: bool = False) -> dict:
    """Returns {"home_win_probability":, "away_win_probability":, "trusted":}
    for one matchup, built on top of compute_cfb_team_points_prediction's
    already-validated per-team predicted points (same shrinkage/
    overdispersion constants), adding the win-probability layer from
    core_ncaa_moneyline.py.

    "trusted" is False - and both probabilities are None, NEVER 0% - when
    either side doesn't have MIN_PRIOR_GAMES of its own history yet
    (compute_cfb_team_points_prediction already returns None in that
    case). This is the exact contract the source script calls
    `trusted: False` / `home_win_probability: null` - the frontend must
    render that as "no pick yet", not 0% and not a crash."""
    if league_avg is None:
        league_avg = get_league_avg_points(db)

    home_pred = compute_cfb_team_points_prediction(home_team, away_team, db, league_avg=league_avg)
    away_pred = compute_cfb_team_points_prediction(away_team, home_team, db, league_avg=league_avg)
    if home_pred is None or away_pred is None:
        return {"home_win_probability": None, "away_win_probability": None, "trusted": False}

    home_prob = win_probability(home_pred["mean"], away_pred["mean"], neutral_site=neutral_site)
    if home_prob is None:
        return {"home_win_probability": None, "away_win_probability": None, "trusted": False}

    return {
        "home_win_probability": home_prob,
        "away_win_probability": 1.0 - home_prob,
        "trusted": True,
    }


def spread_cover_probability(home_predicted: float | None, away_predicted: float | None, home_spread: float | None,
                              neutral_site: bool = False, home_field_points: float = SPREAD_HOME_FIELD_POINTS,
                              variance_scale: float = SPREAD_VARIANCE_SCALE) -> float | None:
    """P(home team covers `home_spread`), ported verbatim (formula-wise)
    from core_ncaa_spread.py's spread_cover_probability - the same
    Normal score-differential model as win_probability above, just
    evaluated at an arbitrary threshold (-home_spread) instead of always
    0, and using the spread layer's OWN home-field constant (see
    SPREAD_HOME_FIELD_POINTS's comment - deliberately different from
    win_probability's HOME_FIELD_POINTS). home_spread convention: HOME
    team's perspective, negative = home favored (confirmed against real
    CFBD data). Returns None if either predicted points is missing or
    non-positive, or home_spread itself is None (no real line posted
    yet) - never a guessed probability."""
    if home_predicted is None or away_predicted is None or home_predicted <= 0 or away_predicted <= 0 or home_spread is None:
        return None
    effective_hfa = 0.0 if neutral_site else home_field_points
    predicted_diff = (home_predicted - away_predicted) + effective_hfa
    variance_diff = variance_scale * (DEFAULT_TEAM_OVERDISPERSION * home_predicted + DEFAULT_TEAM_OVERDISPERSION * away_predicted)
    sigma = math.sqrt(variance_diff) if variance_diff > 0 else 1.0
    # P(home covers) = P(diff > -home_spread) = norm.sf(-home_spread, loc=predicted_diff, scale=sigma)
    return 1.0 - _normal_cdf(-home_spread, predicted_diff, sigma)


def compute_cfb_spread(home_team: str, away_team: str, home_spread: float | None, db,
                        league_avg: float | None = None, neutral_site: bool = False) -> dict:
    """Returns {"home_covers_probability":, "away_covers_probability":,
    "trusted":, "lopsided":} for one matchup's REAL market spread line
    (home_spread - already fetched from CFBD's /lines during
    refresh_cfb_points_stats and stored on CfbGame, see
    pick_provider_line), built on top of compute_cfb_team_points_
    prediction's already-validated per-team predicted points.

    Gated independently from the points prediction and from moneyline:
    "trusted" is False - both probabilities None, NEVER 0% - whenever
    EITHER side lacks MIN_PRIOR_GAMES of history yet, OR no sportsbook
    has posted a line for this game yet (home_spread is None, common
    well before kickoff). "lopsided" is True when |home_spread| >=
    LOPSIDED_THRESHOLD - core_ncaa_spread.py's own 3-season-confirmed
    finding that cover probabilities are OVERCONFIDENT on lopsided
    lines; None (not False) whenever not trusted, since "not lopsided"
    would be a claim this function can't back up without a real line.
    The frontend must caveat lopsided=True predictions rather than
    showing them with the same confidence as a moderate line."""
    if league_avg is None:
        league_avg = get_league_avg_points(db)
    if home_spread is None:
        return {"home_covers_probability": None, "away_covers_probability": None, "trusted": False, "lopsided": None}

    home_pred = compute_cfb_team_points_prediction(home_team, away_team, db, league_avg=league_avg)
    away_pred = compute_cfb_team_points_prediction(away_team, home_team, db, league_avg=league_avg)
    if home_pred is None or away_pred is None:
        return {"home_covers_probability": None, "away_covers_probability": None, "trusted": False, "lopsided": None}

    home_cover_prob = spread_cover_probability(home_pred["mean"], away_pred["mean"], home_spread, neutral_site=neutral_site)
    if home_cover_prob is None:
        return {"home_covers_probability": None, "away_covers_probability": None, "trusted": False, "lopsided": None}

    return {
        "home_covers_probability": home_cover_prob,
        "away_covers_probability": 1.0 - home_cover_prob,
        "trusted": True,
        "lopsided": abs(home_spread) >= LOPSIDED_THRESHOLD,
    }
