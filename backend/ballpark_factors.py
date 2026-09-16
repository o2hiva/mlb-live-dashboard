"""
Ballpark run/hit factors - real values from Baseball Savant's Statcast
Park Factors leaderboard (index_wOBA view, 2026, rolling 1 year, both
batter hands, all park conditions), pasted in by hand since that page
renders its table via JavaScript and resists automated scraping.

These correspond to core.py's W17 (ballpark RUN factor) and X17
(ballpark HIT factor) - the Excel workbook read these from a specific
cell per park; this is that same table, just as a Python dict. Savant's
own scale is "100 = league average", so every value here is the raw
Savant number divided by 100, matching how the rest of this codebase
already treats "factor" values (1.0 = neutral).

Ballpark factors are a season-level constant, not something that
changes game to game - update this dict once a year (or whenever a
fresh export is available), not on any live schedule.

Keyed by the full MLB team name (matching mlb_client/the Game table),
not the short names Savant's export uses - see TEAM_NAME_MAP below for
the translation.
"""

# Savant's short team names -> the full names MLB's Stats API (and this
# app's Game.home_team/away_team) actually uses.
_TEAM_NAME_MAP = {
    "Angels": "Los Angeles Angels",
    "Astros": "Houston Astros",
    "Athletics": "Athletics",  # MLB's API already uses the bare name post-relocation
    "Blue Jays": "Toronto Blue Jays",
    "Braves": "Atlanta Braves",
    "Brewers": "Milwaukee Brewers",
    "Cardinals": "St. Louis Cardinals",
    "Cubs": "Chicago Cubs",
    "D-backs": "Arizona Diamondbacks",
    "Dodgers": "Los Angeles Dodgers",
    "Giants": "San Francisco Giants",
    "Guardians": "Cleveland Guardians",
    "Mariners": "Seattle Mariners",
    "Marlins": "Miami Marlins",
    "Mets": "New York Mets",
    "Nationals": "Washington Nationals",
    "Orioles": "Baltimore Orioles",
    "Padres": "San Diego Padres",
    "Phillies": "Philadelphia Phillies",
    "Pirates": "Pittsburgh Pirates",
    "Rangers": "Texas Rangers",
    "Rays": "Tampa Bay Rays",
    "Red Sox": "Boston Red Sox",
    "Reds": "Cincinnati Reds",
    "Rockies": "Colorado Rockies",
    "Royals": "Kansas City Royals",
    "Tigers": "Detroit Tigers",
    "Twins": "Minnesota Twins",
    "White Sox": "Chicago White Sox",
    "Yankees": "New York Yankees",
}

# {savant_short_name: {"runs": R/100, "hits": H/100}}, from the uploaded export.
_RAW_FACTORS = {
    "Angels": {"runs": 0.85, "hits": 0.93},
    "Astros": {"runs": 0.98, "hits": 0.94},
    "Athletics": {"runs": 1.28, "hits": 1.11},
    "Blue Jays": {"runs": 0.98, "hits": 1.00},
    "Braves": {"runs": 1.02, "hits": 1.04},
    "Brewers": {"runs": 0.96, "hits": 0.97},
    "Cardinals": {"runs": 0.92, "hits": 1.02},
    "Cubs": {"runs": 1.06, "hits": 1.01},
    "D-backs": {"runs": 1.00, "hits": 1.03},
    "Dodgers": {"runs": 0.96, "hits": 0.96},
    "Giants": {"runs": 0.92, "hits": 0.96},
    "Guardians": {"runs": 1.00, "hits": 1.02},
    "Mariners": {"runs": 0.86, "hits": 0.91},
    "Marlins": {"runs": 0.94, "hits": 0.96},
    "Mets": {"runs": 0.94, "hits": 0.96},
    "Nationals": {"runs": 1.12, "hits": 1.04},
    "Orioles": {"runs": 0.98, "hits": 0.98},
    "Padres": {"runs": 0.88, "hits": 0.92},
    "Phillies": {"runs": 1.06, "hits": 1.04},
    "Pirates": {"runs": 1.08, "hits": 1.05},
    "Rangers": {"runs": 0.94, "hits": 1.00},
    "Rays": {"runs": 0.98, "hits": 1.03},
    "Red Sox": {"runs": 1.06, "hits": 1.06},
    "Reds": {"runs": 0.98, "hits": 0.92},
    "Rockies": {"runs": 1.17, "hits": 1.12},
    "Royals": {"runs": 1.12, "hits": 1.06},
    "Tigers": {"runs": 0.94, "hits": 0.96},
    "Twins": {"runs": 0.96, "hits": 0.99},
    "White Sox": {"runs": 0.98, "hits": 0.98},
    "Yankees": {"runs": 1.04, "hits": 0.98},
}

# Final lookup, keyed by full MLB team name.
BALLPARK_FACTORS = {
    _TEAM_NAME_MAP[short_name]: factors
    for short_name, factors in _RAW_FACTORS.items()
}


def get_ballpark_factors(home_team_name: str) -> dict:
    """
    Returns {"runs": <factor>, "hits": <factor>} for the park a game is
    played in - identified by the HOME team, since that's whose park it
    is (applies equally to both teams' batters/pitchers in that game).
    Falls back to neutral (1.0/1.0) for an unrecognized team name rather
    than crashing - shouldn't happen with a real MLB team name, but
    better a neutral factor than a broken prediction.
    """
    return BALLPARK_FACTORS.get(home_team_name, {"runs": 1.0, "hits": 1.0})
