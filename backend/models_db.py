from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class Game(Base):
    __tablename__ = "games"

    game_pk = Column(Integer, primary_key=True)  # MLB's own game id
    game_date = Column(String, index=True)        # "YYYY-MM-DD"
    game_datetime_utc = Column(String, nullable=True)  # ISO 8601 UTC, e.g. "2026-09-15T02:10:00Z" - frontend converts to viewer's local time
    home_team = Column(String)
    away_team = Column(String)
    home_team_id = Column(Integer, nullable=True)
    away_team_id = Column(Integer, nullable=True)
    venue_id = Column(Integer, nullable=True)
    venue_name = Column(String, nullable=True)
    home_probable_pitcher = Column(String, nullable=True)
    away_probable_pitcher = Column(String, nullable=True)
    home_probable_pitcher_id = Column(Integer, nullable=True)
    away_probable_pitcher_id = Column(Integer, nullable=True)
    home_lineup_confirmed = Column(Boolean, default=False)
    away_lineup_confirmed = Column(Boolean, default=False)
    status = Column(String)  # Preview / Live / Final / etc.
    home_score = Column(Integer, default=0)
    away_score = Column(Integer, default=0)
    inning = Column(Integer, default=0)
    inning_half = Column(String, nullable=True)  # "top" / "bottom"
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    inning_lines = relationship("InningLine", back_populates="game")
    predictions = relationship("Prediction", back_populates="game")


class InningLine(Base):
    __tablename__ = "inning_lines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_pk = Column(Integer, ForeignKey("games.game_pk"), index=True)
    inning = Column(Integer)
    half = Column(String)  # "top" / "bottom"
    runs = Column(Integer, default=0)

    game = relationship("Game", back_populates="inning_lines")


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_pk = Column(Integer, ForeignKey("games.game_pk"), index=True)
    market = Column(String, default="first_inning_run")  # extend with more markets later
    probability = Column(Float)
    model_version = Column(String, default="placeholder-v0")
    created_at = Column(DateTime, default=datetime.utcnow)
    actual_outcome = Column(Boolean, nullable=True)  # filled in after the fact, for tracking accuracy

    game = relationship("Game", back_populates="predictions")


class TeamInningStat(Base):
    """Real season-to-date per-inning scoring counts for one team, one
    inning. Kept in sync by inning_stats_sync.py - a live-dashboard port
    of fetch_inning_scoring_stats.py, same MLB Stats API data source."""
    __tablename__ = "team_inning_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_name = Column(String, index=True)
    inning = Column(Integer, index=True)
    games = Column(Integer, default=0)
    scored = Column(Integer, default=0)


class PitcherInningStat(Base):
    """Real season-to-date per-inning allowed counts for one starting
    pitcher, one inning. Only tracked for innings 1-3, matching the
    pitcher-level model's validated scope in core.py."""
    __tablename__ = "pitcher_inning_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pitcher_name = Column(String, index=True)
    inning = Column(Integer, index=True)
    starts = Column(Integer, default=0)
    allowed = Column(Integer, default=0)


class SyncState(Base):
    """Simple key-value store for tracking sync progress, e.g. the last
    date successfully processed by inning_stats_sync.py, or the last
    time the live league-average hit rate was recomputed."""
    __tablename__ = "sync_state"

    key = Column(String, primary_key=True)
    value = Column(String)


class BetTrackerSettings(Base):
    """Single-row table (id always 1) for the Bet Tracker tab's
    persistent settings. Bankroll is the SUM of the five platform
    balances below, not entered separately - kept as its own column
    for convenience/history, but always recomputed from the platform
    values on save (see main.py's update endpoint)."""
    __tablename__ = "bet_tracker_settings"

    id = Column(Integer, primary_key=True)
    bankroll = Column(Float, default=0.0)
    kelly_percent = Column(Float, default=25.0)  # quarter-Kelly is a common conservative default
    kalshi_balance = Column(Float, default=0.0)
    polymarket_balance = Column(Float, default=0.0)
    novig_balance = Column(Float, default=0.0)
    fanduel_balance = Column(Float, default=0.0)
    draftkings_balance = Column(Float, default=0.0)


class TrackedBet(Base):
    """A bet you've flagged with a 'Track' checkbox (from the Hits table
    or the 1st-inning market) - a snapshot of the bet as it looked at
    the moment you checked it (market %, wager, potential profit, model
    probability), for a future end-of-day job to grade against what
    actually happened. Grading itself (comparing actual_hits/the actual
    1st-inning result to what was bet, filling in result) is a planned
    end-of-day job, not implemented yet - this table is just the record
    that job will eventually read and update.

    bet_type distinguishes "hits" (batter-level, uses batter_id/
    team_side/batting_order/hits_threshold) from "first_inning_run"
    (game-level, those fields are null - batter_name holds a display
    label like "1st Inning Run" instead of an actual player name)."""
    __tablename__ = "tracked_bets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_pk = Column(Integer, ForeignKey("games.game_pk"), index=True)
    bet_type = Column(String, default="hits")  # "hits" or "first_inning_run"
    batter_id = Column(Integer, nullable=True)
    batter_name = Column(String)  # batter's name for "hits" bets, a display label (e.g. "1st Inning Run") otherwise
    team_side = Column(String, nullable=True)  # "home" / "away" - only meaningful for "hits" bets
    batting_order = Column(Integer, nullable=True)
    hits_threshold = Column(Integer, nullable=True)  # legacy - see `line` below for both bet types
    line = Column(Float, nullable=True)  # the O/U line at time of tracking - "at least H hits" (1.0, 2.0...) or an HRR line (1.5, 2.5...)
    yn = Column(String)  # "yes" / "no"
    model_probability = Column(Float)
    market_probability = Column(Float)  # stored as a percent (0-100), matching what's typed into the Market box
    wager = Column(Float)
    potential_profit = Column(Float)
    placed_at = Column(DateTime, default=datetime.utcnow)

    # Filled in later by the (not-yet-built) end-of-day grading job.
    resolved = Column(Boolean, default=False)
    actual_hits = Column(Integer, nullable=True)
    result = Column(String, nullable=True)  # "win" / "loss" / "push"


class LineupBatter(Base):
    """One confirmed starting batter for one game/side, in real batting
    order. Populated once MLB posts the official lineup (see
    mlb_client.extract_boxscore_lineup) - rows for a game/side are
    replaced wholesale each time that side's lineup is (re-)confirmed,
    not accumulated, so this always reflects the latest known lineup."""
    __tablename__ = "lineup_batters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_pk = Column(Integer, ForeignKey("games.game_pk"), index=True)
    team_side = Column(String)  # "home" / "away"
    batter_id = Column(Integer, index=True)
    batter_name = Column(String)
    batting_order = Column(Integer)  # 1-9


class BatterSeasonStat(Base):
    """Real season-to-date at-bats/hits/HR/walks for one batter, keyed
    by MLB's own person id (not name) - sidesteps the real
    name-collision cases your own fetch script already had to handle
    (e.g. two different "Max Muncy"s on two different teams). HR/BB
    were added for the HRR model - Hits alone only ever needed ab/hits."""
    __tablename__ = "batter_season_stats"

    batter_id = Column(Integer, primary_key=True)
    batter_name = Column(String)
    ab = Column(Integer, default=0)
    hits = Column(Integer, default=0)
    hr = Column(Integer, default=0)
    bb = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PitcherHitsStat(Base):
    """Real season-to-date outs recorded/hits-HR-walks-allowed for one
    pitcher, keyed by person id - used by the Hits AND HRR models'
    pitcher-shrinkage terms. Separate from PitcherInningStat (different
    data, different model) even though both describe the same pitcher."""
    __tablename__ = "pitcher_hits_stats"

    pitcher_id = Column(Integer, primary_key=True)
    pitcher_name = Column(String)
    outs = Column(Integer, default=0)
    hits_allowed = Column(Integer, default=0)
    hr_allowed = Column(Integer, default=0)
    bb_allowed = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
