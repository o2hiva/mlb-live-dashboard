"""
Live Kalshi prices for the CFB tab: moneyline (KXNCAAFGAME), spread, game total and team total markets, matched to the games in
CfbGame and served by /api/cfb/kalshi. Public, read-only Kalshi endpoints (no key, no trading), reusing the helpers in
tennis_platform.py's embedded kalshi_tennis_live module.

HOW MATCHING WORKS. Kalshi names a game with a date + its own team codes (KXNCAAFGAME-26SEP19MSSTSCAR); every series of one game
shares that suffix. The moneyline markets carry full team names, so they are matched to a CfbGame first (date within a day AND both
team names agree); the other series then follow by their shared event suffix, falling back to the team names in their own text.
Prices are dollars per $1 contract: yes_bid / yes_ask / no_ask.

Series names for spread / total / team-total are inferred from Kalshi's naming (KXNCAAFGAME, KXNCAAF1HTOTAL, ...) and discovered at
run time from Kalshi's series list; /api/cfb/kalshi/debug shows what was found and anything that could not be matched.
Everything degrades instead of raising.
"""
import datetime
import logging
import re
import threading
import time
import unicodedata

import tennis_platform as tpf

kal = tpf.kal
log = logging.getLogger("cfb_kalshi")

KNOWN_SERIES = {"KXNCAAFGAME": "ml", "KXNCAAFSPREAD": "spread", "KXNCAAFTOTAL": "total", "KXNCAAFTEAMTOTAL": "team_total"}
PERIOD_RE = re.compile(r"^KXNCAAF(1H|2H|1Q|2Q|3Q|4Q|OT)")
REFRESH_SECONDS = 60
SERIES_REDISCOVER_SECONDS = 6 * 3600


# ------------------------------------------------------------------------------------------------ names
ALIASES = {
    "olemiss": "mississippi", "pittsburgh": "pitt", "connecticut": "uconn", "massachusetts": "umass", "centralflorida": "ucf",
    "southerncalifornia": "usc", "southernmississippi": "southernmiss", "miamifl": "miami", "miamiflorida": "miami",
    "miamioh": "miamioh", "miamiohio": "miamioh", "northcarolinast": "ncst", "ncstate": "ncst", "nebraskaomaha": "nebraska",
    "hawaii": "hawaii", "sanjosest": "sanjosest", "texasam": "texasam", "ulmonroe": "ulmonroe", "louisianamonroe": "ulmonroe",
    "louisianalafayette": "louisiana", "ullafayette": "louisiana", "uab": "uab", "utsa": "utsa", "texassanantonio": "utsa",
    "appalachianst": "appst", "appstate": "appst", "bowlinggreenst": "bgsu", "bowlinggreen": "bgsu", "fiu": "fiu",
    "floridainternational": "fiu", "floridaatlantic": "fau", "fau": "fau", "middletennesseest": "mtsu", "middletennessee": "mtsu",
    "westernkentucky": "wku", "wku": "wku", "eastcarolina": "ecu", "ecu": "ecu", "sanDiegost": "sdsu", "sandiegost": "sdsu",
    "coloradost": "coloradost", "lsu": "lsu", "louisianast": "lsu", "tcu": "tcu", "texaschristian": "tcu", "smu": "smu",
    "southernmethodist": "smu", "byu": "byu", "brighamyoung": "byu", "unlv": "unlv", "nevadalasvegas": "unlv", "utep": "utep",
    "texaselpaso": "utep", "umass": "umass", "uconn": "uconn", "ucf": "ucf", "usc": "usc", "pitt": "pitt",
}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", "and")
    s = re.sub(r"\b(university|the|of|at)\b", " ", s)
    s = re.sub(r"\bstate\b", "st", s)
    s = re.sub(r"\b(saint)\b", "st", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return ALIASES.get(s, s)


def name_score(kalshi_name, cfb_name):
    """2 = same team, 1 = one name is a prefix of the other (e.g. 'Miami' / 'MiamiFL...'), 0 = different."""
    a, b = norm(kalshi_name), norm(cfb_name)
    if not a or not b:
        return 0
    if a == b:
        return 2
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short):
        return 1
    return 0


def text_has_team(text, cfb_name):
    t, n = norm(text), norm(cfb_name)
    return bool(n) and len(n) >= 3 and n in t


# ------------------------------------------------------------------------------------------------ fetching
def discover_series(get=kal.http_get):
    """-> {series_ticker: kind}; always includes KNOWN_SERIES, plus anything Kalshi's series list shows that looks like a full-game
    college football moneyline / spread / total / team-total market."""
    found = dict(KNOWN_SERIES)
    try:
        d = get("/series", category="Sports")
        for s in d.get("series", []) or []:
            t = (s.get("ticker") or "").upper()
            if not t.startswith("KXNCAAF") or PERIOD_RE.match(t):
                continue
            if t.endswith("TEAMTOTAL"):
                found.setdefault(t, "team_total")
            elif t.endswith("TOTAL"):
                found.setdefault(t, "total")
            elif t.endswith("SPREAD"):
                found.setdefault(t, "spread")
            elif t.endswith("GAME"):
                found.setdefault(t, "ml")
    except Exception as e:                           # noqa: BLE001
        log.info("Kalshi series discovery failed (%s): using the known list", e)
    return found


def compact(m, series, kind):
    return {"ticker": m.get("ticker"), "event_ticker": m.get("event_ticker"), "series": series, "kind": kind,
            "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title"), "no_sub_title": m.get("no_sub_title"),
            "floor_strike": kal._num(m.get("floor_strike")),
            "yes_bid": kal.price(m, "yes_bid"), "yes_ask": kal.price(m, "yes_ask"), "no_bid": kal.price(m, "no_bid"),
            "no_ask": kal.price(m, "no_ask"), "last": kal.price(m, "last_price"),
            "volume": kal._num(m.get("volume_fp")) or kal._num(m.get("volume")),
            "close_time": m.get("close_time"), "status": m.get("status"),
            "rules": (m.get("rules_primary") or "")[:300]}


def fetch_open(series_map, get=kal.http_get, max_pages=12):
    out, errors, counts = [], {}, {}
    for s, kind in series_map.items():
        cursor, n = None, 0
        try:
            for _ in range(max_pages):
                d = get("/markets", series_ticker=s, status="open", limit=1000, cursor=cursor)
                for m in d.get("markets", []) or []:
                    if m.get("ticker"):
                        out.append(compact(m, s, kind))
                        n += 1
                cursor = d.get("cursor")
                if not cursor:
                    break
        except Exception as e:                       # noqa: BLE001
            errors[s] = f"{type(e).__name__}: {e}"[:200]
        counts[s] = n
    return out, errors, counts


# ------------------------------------------------------------------------------------------------ matching
def _event_key(m):
    return kal.event_key(m.get("event_ticker"))


def _event_date(m):
    return kal.event_date(m.get("event_ticker"))


def _et_date(start_utc):
    try:
        dt = datetime.datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:                                # noqa: BLE001
        return None


def _date_ok(ev_date, game):
    if ev_date is None:
        return True
    gd = game.get("_date")
    if gd is None:
        return True
    return abs((datetime.date(*ev_date) - gd).days) <= 1


def _view(m):
    return {"ticker": m["ticker"], "yes_bid": m["yes_bid"], "yes_ask": m["yes_ask"], "no_bid": m["no_bid"], "no_ask": m["no_ask"],
            "last": m["last"], "volume": m["volume"]}


def build(markets, games):
    """markets = compact Kalshi markets; games = [{game_id, home, away, start_utc}] -> ({game_id: prices}, unmatched list)."""
    gl = []
    for g in games:
        g = dict(g)
        g["_date"] = _et_date(g.get("start_utc"))
        gl.append(g)
    by_event = {}
    for m in markets:
        by_event.setdefault(_event_key(m), []).append(m)

    def side_of(g, text, strict_exact=False):
        # (how well the whole text equals the name, length of the team name found inside the text): "Miami (OH) wins by ..."
        # must pick Miami (OH), not Miami, so the longer contained name wins a tie
        sh = (name_score(text, g["home"]), len(norm(g["home"])) if text_has_team(text, g["home"]) else 0)
        sa = (name_score(text, g["away"]), len(norm(g["away"])) if text_has_team(text, g["away"]) else 0)
        if sh > sa:
            return "home"
        if sa > sh:
            return "away"
        return None

    # 1) moneyline events -> games (both team names must agree, date within a day)
    event_game, unmatched = {}, []
    for key, ms in by_event.items():
        ml = [m for m in ms if m["kind"] == "ml"]
        if len(ml) < 1:
            continue
        names = [m.get("yes_sub_title") or "" for m in ml]
        ed = _event_date(ml[0])
        best, best_score, tie = None, 0, False
        for g in gl:
            if not _date_ok(ed, g):
                continue
            hs = max((name_score(n, g["home"]) for n in names), default=0)
            as_ = max((name_score(n, g["away"]) for n in names), default=0)
            if hs == 0 or as_ == 0:
                continue
            sc = hs + as_
            if sc > best_score:
                best, best_score, tie = g, sc, False
            elif sc == best_score:
                tie = True
        if best is None or tie:
            unmatched.append({"event": key, "names": names, "reason": "no CFB game with both teams" if best is None else "ambiguous"})
        else:
            event_game[key] = best

    out = {}

    def slot(g):
        return out.setdefault(g["game_id"], {"ml": {}, "spread": [], "total": [], "team_total": {"home": [], "away": []}, "event": None})

    for key, g in event_game.items():
        s = slot(g)
        s["event"] = key
        for m in by_event[key]:
            kind = m["kind"]
            text = " ".join(filter(None, [m.get("yes_sub_title"), m.get("title")]))
            if kind == "ml":
                sd = side_of(g, m.get("yes_sub_title") or "")
                if sd:
                    s["ml"][sd] = _view(m)
            elif kind == "spread" and m.get("floor_strike") is not None:
                sd = side_of(g, m.get("yes_sub_title") or "") or side_of(g, text)
                if sd:
                    s["spread"].append({"team": sd, "strike": m["floor_strike"], **_view(m)})
            elif kind == "total" and m.get("floor_strike") is not None:
                s["total"].append({"strike": m["floor_strike"], **_view(m)})
            elif kind == "team_total" and m.get("floor_strike") is not None:
                sd = side_of(g, m.get("yes_sub_title") or "") or side_of(g, text)
                if sd:
                    s["team_total"][sd].append({"strike": m["floor_strike"], **_view(m)})
    for s in out.values():
        s["spread"].sort(key=lambda r: (r["team"], r["strike"]))
        s["total"].sort(key=lambda r: r["strike"])
        for sd in ("home", "away"):
            s["team_total"][sd].sort(key=lambda r: r["strike"])
    # events with only non-moneyline markets (no sibling ml) are reported, not guessed
    for key, ms in by_event.items():
        if key not in event_game and not any(m["kind"] == "ml" for m in ms):
            unmatched.append({"event": key, "names": [m.get("title") for m in ms[:2]], "reason": "no moneyline market to anchor the game"})
    return out, unmatched


class CfbKalshi:
    def __init__(self, get=None):
        self._get = get or kal.http_get
        self._lock = threading.Lock()
        self._markets = []
        self._errors = {}
        self._counts = {}
        self._series = dict(KNOWN_SERIES)
        self._series_at = 0.0
        self._fetched_at = None
        self._started = False
        self._stop = threading.Event()
        self._built = {}

    def refresh(self):
        try:
            if time.time() - self._series_at > SERIES_REDISCOVER_SECONDS:
                self._series = discover_series(self._get)
                self._series_at = time.time()
            markets, errors, counts = fetch_open(self._series, self._get)
            with self._lock:
                if markets or not self._markets:
                    self._markets = markets
                self._errors, self._counts = errors, counts
                self._fetched_at = datetime.datetime.now(datetime.timezone.utc)
                self._built = {}
            return {"status": "ok", "markets": len(markets), "by_series": counts, "errors": errors}
        except Exception as e:                       # noqa: BLE001
            log.exception("CFB Kalshi refresh failed")
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}

    def start(self):
        if self._started:
            return
        self._started = True

        def loop():
            self._stop.wait(20)
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(REFRESH_SECONDS)
        threading.Thread(target=loop, name="cfb-kalshi", daemon=True).start()

    def payload(self, games):
        """games = [{game_id, home, away, start_utc}] from CfbGame (one entry per game)."""
        with self._lock:
            markets, fetched = list(self._markets), self._fetched_at
            errors, counts = dict(self._errors), dict(self._counts)
        if fetched is None:
            return {"status": "error", "warnings": ["no Kalshi pull yet - the first one runs about 20 seconds after the server starts"], "games": {}}
        sig = (len(games), fetched)
        cached = self._built.get("sig") == sig and self._built.get("payload")
        if cached:
            out = self._built["payload"]
        else:
            by_game, unmatched = build(markets, games)
            out = {"games": by_game, "unmatched": unmatched}
            self._built = {"sig": sig, "payload": out}
        age = (datetime.datetime.now(datetime.timezone.utc) - fetched).total_seconds() / 60
        warns = [f"Kalshi series {s} failed on the last pull: {e}" for s, e in errors.items()]
        return {"status": "ok", "warnings": warns, "fetched_at": fetched.isoformat(), "age_minutes": age,
                "markets": len(markets), "by_series": counts, "n_games_priced": len(out["games"]),
                "games": out["games"], "unmatched": len(out["unmatched"])}

    def debug(self, games):
        with self._lock:
            markets = list(self._markets)
            counts, errors, series = dict(self._counts), dict(self._errors), dict(self._series)
        by_game, unmatched = build(markets, games)
        samples = {}
        for m in markets:
            samples.setdefault(m["series"], [])
            if len(samples[m["series"]]) < 3:
                samples[m["series"]].append({k: m[k] for k in ("ticker", "event_ticker", "title", "yes_sub_title", "floor_strike", "yes_bid", "yes_ask")})
        return {"series": series, "counts": counts, "errors": errors, "samples": samples, "games_in_db": len(games),
                "games_priced": len(by_game), "unmatched": unmatched[:40],
                "priced": {gid: {"ml": sorted(v["ml"]), "spread": len(v["spread"]), "total": len(v["total"]),
                                 "team_total": [len(v["team_total"]["home"]), len(v["team_total"]["away"])]} for gid, v in list(by_game.items())[:60]}}


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = CfbKalshi()
        return _service


def start_background():
    try:
        get_service().start()
    except Exception:
        log.exception("could not start the CFB Kalshi service")
