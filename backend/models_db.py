from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class Game(Base):
    __tablename__ = "games"

    game_pk = Column(Integer, primary_key=True)  # MLB's own game id
    game_date = Column(String, index=True)        # "YYYY-MM-DD"
    home_team = Column(String)
    away_team = Column(String)
    home_probable_pitcher = Column(String, nullable=True)
    away_probable_pitcher = Column(String, nullable=True)
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
