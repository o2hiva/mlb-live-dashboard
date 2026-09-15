"""
Force-loads one date's games, probable pitchers, and 1st-inning
predictions into the database RIGHT NOW - useful for checking a day's
slate before the recurring poller (which runs every 60s and looks
SYNC_DAYS_AHEAD days ahead automatically) would otherwise get to it, or
for backfilling a date the poller missed while the app was offline.

Usage (run from the backend/ folder, with your venv active):
    python load_game_day.py --date 2026-09-15
    python load_game_day.py            # defaults to today

This reuses poller._sync_one_date(), so it's the exact same logic the
live app runs every 60 seconds - not a separate code path that could
drift out of sync with it.
"""
import argparse
from datetime import date

from database import SessionLocal, Base, engine
import poller


def main():
    parser = argparse.ArgumentParser(description="Force-load one date's games/pitchers/predictions now")
    parser.add_argument("--date", default=date.today().isoformat(),
                         help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)  # safe no-op if tables already exist

    db = SessionLocal()
    try:
        print(f"Loading {args.date}...")
        poller._sync_one_date(db, args.date)
        db.commit()
        print("Done. Games, probable pitchers, and predictions for that date are now in the database.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
