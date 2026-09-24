"""
Database setup.

Defaults to a local SQLite file so the app runs with zero setup.
For a hosted "live" deployment, set DATABASE_URL to a Postgres URL
(e.g. the one Railway/Render/Fly.io give you for a Postgres addon):

    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"

SQLAlchemy handles the rest without any code changes.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mlb_dashboard.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Force the psycopg2 driver explicitly -- that's the only Postgres driver
# installed via requirements.txt (psycopg2-binary). Some hosts (e.g. Railway)
# hand back DATABASE_URL as "postgresql+psycopg://..." (naming psycopg v3,
# which isn't installed) or as a bare "postgresql://..." which newer
# SQLAlchemy/host combos can also resolve to psycopg v3. Rewriting the prefix
# here pins it to the driver that's actually available, so deploys don't
# crash with "ModuleNotFoundError: No module named 'psycopg'".
if DATABASE_URL.startswith("postgresql+psycopg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
