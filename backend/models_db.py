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
    date successfully processed by inning_stats_sync.py."""
    __tablename__ = "sync_state"

    key = Column(String, primary_key=True)
    value = Column(String)
