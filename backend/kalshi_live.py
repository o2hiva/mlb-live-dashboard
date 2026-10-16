"""
Live Kalshi prices for every sport the dashboard bets on (MLB, NBA, NHL, NFL), served raw-but-compact so the browser can default
each bet's Market box to the live Kalshi price. Public, read-only Kalshi endpoints (no key, no trading), reusing the helpers in
tennis_platform.py's embedded kalshi_tennis_live module. (CFB and tennis have their own services: cfb_kalshi.py, tennis_service.py.)

  * discovers Kalshi's series for each sport from Kalshi's own series list (game winner, spread, totals, player props ...),
    always including the well-known ones;
  * pulls every OPEN market of those series; the pull only runs while somebody is looking (a request in the last 15 minutes),
    every 75 seconds, so an idle server makes no Kalshi calls;
  * each market is reduced to: kind (the series name minus the sport prefix: game, total, ks, hit, pts ...), ticker, event key
    (Kalshi's date+teams code), the date in it, the "at least N" number at the end of player/total tickers, the strike, the
    yes/no sub-titles, and yes/no bid/ask in dollars.

Matching a market to a bet (team names, player names, lines) happens in the browser, where the bet's own names are known.
/api/kalshi/debug lists what was discovered so a series with an unexpected name can be fixed in one place.
"""
import datetime
import logging
import re
import threading
import time

import tennis_platform as tpf

kal = tpf.kal
log = logging.getLogger("kalshi_live")

PREFIXES = {"KXMLB": "mlb", "KXNBA": "nba", "KXNHL": "nhl", "KXNFL": "nfl", "KXBUNDESLIGA": "bl"}
KNOWN = {
    "mlb": ["GAME", "SPREAD", "TOTAL", "TEAMTOTAL", "HIT", "HR", "HRR", "TB", "RBI", "KS", "F5", "F5TOTAL", "F5SPREAD", "RFI"],
    "nba": ["GAME", "SPREAD", "TOTAL", "TEAMTOTAL", "PTS", "REB", "AST", "3PT", "STL", "BLK"],
    "nhl": ["GAME", "SPREAD", "TOTAL", "TEAMTOTAL", "GOAL", "PTS", "AST", "SAVES", "1PTOTAL", "2PTOTAL", "3PTOTAL"],
    "nfl": ["GAME", "SPREAD", "TOTAL", "TEAMTOTAL", "ANYTD", "PASSYDS", "RUSHYDS", "RSHYDS", "RECYDS", "RECS", "PASSTDS"],
    "bl": ["GAME", "SPREAD", "TOTAL", "BTTS", "TEAMTOTAL"],       # Bundesliga (soccer): 1X2 winner/draw, totals, both teams to score
}
SUFFIX_OK = re.compile(r"^(GAME|SPREAD|TOTAL|TEAMTOTAL|BTTS|HIT|HR|HRR|TB|RBI|KS|PTS|REB|AST|3PT|STL|BLK|GOAL|SAVES|ANYTD|PASSYDS|RUSHYDS|RSHYDS|RECYDS|RECS|"
                       r"PASSTDS|F5\w*|1INN\w*|RFI|YRFI|NRFI|[123]P\w*|[12]H\w*|[1-4]Q\w*)$")
EVENT_RE = re.compile(r"^(\d\d)([A-Z]{3})(\d\d)(\d{4})?(.*)$")
REFRESH_SECONDS = 75
ACTIVE_WINDOW = 15 * 60
RERESCOVER = 6 * 3600


def parse_ticker(ticker):
    """'KXMLBKS-26JUL272145MILSF-SFTMAHLE54-9' -> (event_key, date 'YYYY-MM-DD' | None, player_seg | None, n | None)"""
    parts = (ticker or "").split("-")
    ev = parts[1] if len(parts) > 1 else ""
    date = None
    m = EVENT_RE.match(ev)
    if m and m.group(2) in kal.MONTHS:
        try:
            date = datetime.date(2000 + int(m.group(1)), kal.MONTHS[m.group(2)], int(m.group(3))).isoformat()
        except ValueError:
            date = None
    n = None
    if len(parts) >= 3 and parts[-1].isdigit():
        n = int(parts[-1])
    seg = parts[2] if len(parts) >= 4 else None
    return ev, date, seg, n


def compact(m, sport, kind):
    ev, date, seg, n = parse_ticker(m.get("ticker"))
    return {"k": kind, "t": m.get("ticker"), "e": ev, "d": date, "p": seg, "n": n, "s": kal._num(m.get("floor_strike")),
            "ys": m.get("yes_sub_title"), "ti": m.get("title"),
            "yb": kal.price(m, "yes_bid"), "ya": kal.price(m, "yes_ask"), "nb": kal.price(m, "no_bid"), "na": kal.price(m, "no_ask"),
            "v": kal._num(m.get("volume_fp")) or kal._num(m.get("volume"))}


def series_to_kind(series):
    for pre, sport in PREFIXES.items():
        if series.startswith(pre):
            return sport, series[len(pre):].lower()
    return None, None


def discover(get=kal.http_get):
    """-> {series_ticker: (sport, kind)}"""
    found = {}
    for sport, sufs in KNOWN.items():
        pre = next(p for p, s in PREFIXES.items() if s == sport)
        for suf in sufs:
            found[pre + suf] = (sport, suf.lower())
    try:
        try:
            d = get("/series", category="Sports")
        except Exception:                            # noqa: BLE001
            d = get("/series")
        for s in d.get("series", []) or []:
            t = (s.get("ticker") or "").upper()
            sport, kind = series_to_kind(t)
            if sport and SUFFIX_OK.match(kind.upper()):
                found.setdefault(t, (sport, kind))
    except Exception as e:                           # noqa: BLE001
        log.info("Kalshi series discovery failed (%s): using the known list", e)
    return found


def fetch_series(series, sport, kind, get=kal.http_get, max_pages=15):
    out, cursor = [], None
    for _ in range(max_pages):
        d = get("/markets", series_ticker=series, status="open", limit=1000, cursor=cursor)
        for m in d.get("markets", []) or []:
            if m.get("ticker"):
                out.append(compact(m, sport, kind))
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


class KalshiLive:
    def __init__(self, get=None):
        self._get = get or kal.http_get
        self._lock = threading.Lock()
        self._by_sport = {s: [] for s in KNOWN}
        self._fetched = None
        self._series = {}
        self._series_at = 0.0
        self._errors = {}
        self._counts = {}
        self._last_touch = 0.0
        self._thread = None
        self._busy = False

    def refresh(self):
        if self._busy:
            return {"status": "ok", "warnings": ["a refresh is already running"]}
        self._busy = True
        try:
            if not self._series or time.time() - self._series_at > RERESCOVER:
                self._series = discover(self._get)
                self._series_at = time.time()
            by_sport = {s: [] for s in KNOWN}
            errors, counts = {}, {}
            for series, (sport, kind) in self._series.items():
                try:
                    ms = fetch_series(series, sport, kind, self._get)
                    by_sport[sport].extend(ms)
                    counts[series] = len(ms)
                except Exception as e:               # noqa: BLE001
                    errors[series] = f"{type(e).__name__}: {e}"[:160]
            with self._lock:
                for s, ms in by_sport.items():
                    if ms or not self._by_sport.get(s):
                        self._by_sport[s] = ms
                self._errors, self._counts = errors, counts
                self._fetched = datetime.datetime.now(datetime.timezone.utc)
            return {"status": "ok", "counts": counts, "errors": errors}
        except Exception as e:                       # noqa: BLE001
            log.exception("Kalshi live refresh failed")
            return {"status": "error", "warnings": [f"{type(e).__name__}: {e}"]}
        finally:
            self._busy = False

    def touch(self):
        """Called on every API request: (re)starts the pull loop while somebody is looking."""
        self._last_touch = time.time()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="kalshi-live", daemon=True)
            self._thread.start()

    def _loop(self):
        while time.time() - self._last_touch < ACTIVE_WINDOW:
            self.refresh()
            time.sleep(REFRESH_SECONDS)

    def payload(self, sport, kinds=None, since=None):
        self.touch()
        with self._lock:
            fetched = self._fetched
            ms = list(self._by_sport.get(sport, []))
            avail = {}
            for m in ms:
                avail[m["k"]] = avail.get(m["k"], 0) + 1
        if fetched is None:
            return {"status": "error", "warnings": ["first Kalshi pull is still running - try again in a few seconds"], "markets": []}
        v = fetched.isoformat()
        age = (datetime.datetime.now(datetime.timezone.utc) - fetched).total_seconds() / 60
        if since and since == v:
            return {"status": "ok", "unchanged": True, "v": v, "age_minutes": age}
        if kinds:
            want = set(kinds)
            ms = [m for m in ms if m["k"] in want]
        return {"status": "ok", "v": v, "age_minutes": age, "sport": sport, "kinds_available": avail, "markets": ms,
                "warnings": [f"Kalshi series {s} failed: {e}" for s, e in list(self._errors.items())[:5]]}

    def debug(self):
        with self._lock:
            counts, errors = dict(self._counts), dict(self._errors)
            samples = {}
            for sport, ms in self._by_sport.items():
                for m in ms:
                    key = f"{sport}:{m['k']}"
                    if len(samples.setdefault(key, [])) < 2:
                        samples[key].append({x: m[x] for x in ("t", "e", "d", "p", "n", "s", "ys", "ti", "ya", "na")})
        return {"series": {k: list(v) for k, v in self._series.items()}, "counts": counts, "errors": errors, "samples": samples,
                "fetched": self._fetched.isoformat() if self._fetched else None}


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = KalshiLive()
        return _service
