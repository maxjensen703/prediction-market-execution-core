"""
fetch/kalshi.py — market-data fetch for Kalshi. Pulls every game + odds currently
trading on Kalshi and normalizes them into the canonical MoneyLine/TotalLine/SpreadLine
models (core/schema.py), and fills the side and tick registries the broker reads at order
time. Run: python -m fetch.kalshi (polls
every 60s, Ctrl-C stops; --once / --json / --league mlb).

Source: Kalshi Trade API v2, public, no auth. Markets are binary YES/NO contracts priced
in dollars [0,1] = implied probability. Winner/spread/total live in separate series but
share a game key inside the event ticker, so we group them back into one game record.

Converter section map: normalize_market/build_games (raw display normalization, no bet
type) -> kalshi_market_to_line (moneyline) -> kalshi_market_to_total_line /
kalshi_market_to_spread_line (totals/spreads, need the moneyline series' team map first) ->
kalshi_wc_total_line / kalshi_wc_spread_line (World Cup periods; moneyline excluded, 3-way).
fetch_kalshi_all() is the one-call entry point for all requested bet types.
"""

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

import config
from core import perf
from core.schema import ET, MoneyLine, SpreadLine, TotalLine, et_stamp
from core.teams import _clean as _team_key, normalize_team_name
from core.teams_data import SPORT_OVERRIDES, TEAM_REGISTRY

# Tunables live in config.py (the one place to change them); aliased here for local use.
KALSHI_URL = config.KALSHI_MARKETS_URL
POLL_SECONDS = config.CLI_POLL_SECONDS   # default seconds between pulls (gentle on the API)
PAGE_SIZE = config.KALSHI_PAGE_SIZE      # markets per request (API max)
MAX_PAGES = config.KALSHI_MAX_PAGES      # runaway brake for the pagination loop
MAX_RETRIES = config.KALSHI_MAX_RETRIES  # 429 rate-limit retries before giving up
RETRY_BASE = config.KALSHI_RETRY_BASE    # base backoff (s); real reset is sub-second
RETRY_CAP = config.KALSHI_RETRY_CAP      # never honor a Retry-After longer than this
TIMEOUT = config.KALSHI_TIMEOUT          # HTTP timeout, seconds

SERIES = {  # per league: game winner, spreads, totals series tickers
    "nfl": ["KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"],
    "nba": ["KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL"],
    "mlb": ["KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL"],
    "nhl": ["KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL"],
    "wnba": ["KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL"],
}
TYPE_BY_SUFFIX = {"GAME": "moneyline", "SPREAD": "spreads", "TOTAL": "totals"}
LEAGUES = list(SERIES)           # sports in scope

# ══ REGISTRIES — written on each fetch, read at order time by the brokers via call-time
# imports; a market missing here fails closed (see the README's registries note).
# These dicts live in memory and are empty in a new process: run a fetch, or persist and
# restore them yourself, before placing orders. ══
# ticker -> canonical YES team (the side a 'bid' buys); read by the trade client to orient orders.
kalshi_sides: dict[str, str] = {}

# ticker -> canonical NO label (the side an 'ask' buys: the opponent, "Under", or the other
# Cover/No Cover). The Kalshi broker accepts a side_label only if it equals one of the two
# registered labels exactly; anything else is refused rather than treated as NO.
kalshi_no_sides: dict[str, str] = {}

# ticker -> price tick size (dollars); read by execute.broker.venue_tick for order rounding.
kalshi_ticks: dict[str, float] = {}

# ticker -> the documented per-side tie payout in cents, recorded beside
# kalshi_sides for a market whose rules_secondary carries the exact NFL tie sentence below.
# Nothing reads it yet (reserved for the reconciler; a tie settlement is NOT derivable today).
kalshi_tie_payout_cents: dict[str, int] = {}

# The ONE tie wording the moneyline fallback accepts (real KXNFLGAME page, 2026-09-16, 64 of 64
# markets): a two-way market that pays 50 cents a side on a tie. Deleted verbatim, never matched
# loosely; any other tie/draw/regulation wording in the rules still refuses the market.
TIE_HALF_PAYOUT_SENTENCE = "If the game ends in a tie, the market will resolve to $0.50 for each team."
TIE_HALF_PAYOUT_CENTS = 50


def _market_tick(market: dict, fallback: float = 0.01) -> float:
    """Tick size from the market's price_ranges[0].step ('0.0100' -> 0.01); fallback if
    absent/bad/non-positive (a 0 or negative tick would zero-divide downstream in venue_tick)."""
    try:
        v = float(market["price_ranges"][0]["step"])
    except (KeyError, IndexError, TypeError, ValueError):
        return fallback
    return v if math.isfinite(v) and v > 0 else fallback


# ------------------------------- fetching -----------------------------------

def _retry_delay(resp, attempt: int) -> float:
    """Honor a numeric Retry-After (capped); else a short exponential backoff."""
    ra = (resp.headers.get("retry-after") or "").strip()
    if ra.isdigit():
        return min(float(ra), RETRY_CAP)
    return min(RETRY_BASE * (2 ** attempt), RETRY_CAP)


def fetch_markets(series: str, client: httpx.Client) -> list[dict]:
    """Fetch every open market in one series, following the pagination cursor."""
    markets, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"series_ticker": series, "status": "open", "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(MAX_RETRIES):
            with perf.sample("kalshi_page"):
                resp = client.get(KALSHI_URL, params=params)
            if resp.status_code != 429:
                break
            delay = _retry_delay(resp, attempt)
            if perf.enabled():
                print(f"[perf] kalshi 429 on {series} (attempt {attempt}) -> sleeping {delay:.2f}s")
            with perf.sample("kalshi_backoff"):
                time.sleep(delay)  # back off on rate limits, then retry
        if resp.status_code != 200:
            raise RuntimeError(f"Kalshi API HTTP {resp.status_code}: {resp.text[:300]}")
        page = resp.json()
        markets += page["markets"]
        cursor = page.get("cursor")  # empty string on the last page
        if not cursor:
            return markets
    raise RuntimeError(f"Over {MAX_PAGES} pages for '{series}' — suspect a pagination bug")


# ----------------------------- normalization --------------------------------

def _num(val) -> float | None:
    """Coerce to float (handles Kalshi's '0.4700' dollar strings); None if missing."""
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _align_last(market: dict, a_is_yes: bool) -> float | None:
    """The market's last-trade price expressed in the A-SIDE's terms (best_ask_yes/over/cover),
    so it lines up with our stored ask. Kalshi's last_price_dollars is the YES last; if the A-side
    is the NO side, flip to 1-last. Observational (phantom) signal only."""
    last = _num(market.get("last_price_dollars"))
    if last is None:
        return None
    return round(last if a_is_yes else 1.0 - last, 4)


def market_type(series: str, league: str) -> str:
    """'KXMLBSPREAD' + 'mlb' -> 'spreads' (the series suffix names the market kind)."""
    return TYPE_BY_SUFFIX[series.removeprefix("KX" + league.upper())]


def game_key(event_ticker: str) -> str:
    """'KXMLBGAME-26JUN121940LADCWS' -> '26JUN121940LADCWS', shared across series."""
    return event_ticker.split("-", 1)[1] if "-" in event_ticker else event_ticker


def game_title(question: str) -> str:
    """'Texas vs Boston Winner?' or 'Will X win the A vs B Pro Football game?' -> 'A vs B'."""
    m = re.search(r" the (.+?) Pro \w+ game\?$", question, re.I) or re.search(r"^(.+?) winner\?$", question, re.I)
    return m.group(1) if m else question


def normalize_market(m: dict, mtype: str) -> dict:
    """One binary market: what YES means, its line, and both sides of the book."""
    start = game_start(m)  # true start from the ticker; occurrence_datetime is the game END
    return {
        "ticker": m.get("ticker", ""),
        "event_ticker": m.get("event_ticker", ""),
        "type": mtype,
        "question": m.get("title") or "",
        "side": m.get("yes_sub_title") or "",  # what YES pays on, e.g. 'Texas' or 'Over 8.5 runs'
        "line": _num(m.get("floor_strike")),   # 3.5 spread / 8.5 total; None for moneyline
        "yes_bid": _num(m.get("yes_bid_dollars")),
        "yes_ask": _num(m.get("yes_ask_dollars")),
        "no_bid": _num(m.get("no_bid_dollars")),
        "no_ask": _num(m.get("no_ask_dollars")),
        "last_price": _num(m.get("last_price_dollars")),
        "volume_24h": _num(m.get("volume_24h_fp")),
        "open_interest": _num(m.get("open_interest_fp")),
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None,
    }


def build_games(markets: list[dict], league: str) -> list[dict]:
    """Group normalized markets by their shared game key into one record per game."""
    games: dict[str, dict] = {}
    for m in markets:
        g = games.setdefault(game_key(m["event_ticker"]), {
            "game_key": game_key(m["event_ticker"]),
            "title": None, "league": league, "start_time": m["start_time"], "markets": [],
        })
        g["markets"].append(m)
        if m["type"] == "moneyline":  # winner markets carry the cleanest game title
            g["title"] = game_title(m["question"])
            g["start_time"] = m["start_time"]
    games = {k: g for k, g in games.items() if g["title"]}  # drop spread/total orphans
    return sorted(games.values(), key=lambda g: (g["start_time"] is None, g["start_time"] or ""))


def pull_board(leagues: list[str]) -> dict[str, list[dict]]:
    """All games currently trading, per league: {'mlb': [game, ...]}."""
    with httpx.Client(timeout=TIMEOUT) as client:
        board = {}
        for lg in leagues:
            markets = [normalize_market(m, market_type(s, lg))
                       for s in SERIES[lg] for m in fetch_markets(s, client)]
            board[lg] = build_games(markets, lg)
        return board


# ------------------------- canonical MoneyLine -----------------------------

def _registry_canonical(name: str, sport: str) -> str | None:
    """The registry's canonical name for `name`, or None when the registry has no entry for it.
    core/teams.py hands back the raw input for an unmapped name, and an NFL nickname's canonical
    IS its own spelling ('Lions' -> 'Lions'), so 'resolved' is registry membership of the exact
    cleaned key, never a comparison of input and output. Exact key lookup only."""
    key = _team_key(name)
    if key in SPORT_OVERRIDES.get(sport.upper(), {}) or key in TEAM_REGISTRY:
        return normalize_team_name(name, sport)
    return None


# '<CODE> <Nickname>': an optional leading all-caps code token, one space, then a nickname that
# carries at least one lowercase letter (so a bare code never stands in for a nickname).
_CODED_NAME = re.compile(r"^(?:([A-Z]{2,3}) )?([A-Za-z0-9][A-Za-z0-9 ]*[a-z][A-Za-z0-9 ]*)$")


def _coded_pair(parts: list[str], yes_title: str, sport: str, ticker: str) -> tuple[str, str] | None:
    """Coded NFL pair ('DET Lions vs BUF Bills', 'NY Giants vs LA Rams') ->
    (yes_title, opponent nickname), or None. Every identity step is an EXACT registry lookup of a
    whole token: PREFIX AND SUBSTRING MATCHING ARE FORBIDDEN HERE (never 'NY' -> 'NYG', never a
    ticker suffix compared as a string against a pair name, never startswith/in on team names).
    Three independent sources must name the same YES team or the market is refused: the pair's
    nickname, yes_sub_title's registry resolution, and the ticker suffix's registry resolution."""
    canon, nicknames = [], []
    for part in parts:
        m = _CODED_NAME.match(part)
        if not m:
            return None
        code, nickname = m.group(1), m.group(2).strip()
        c = _registry_canonical(nickname, sport)
        if c is None or c != nickname:
            return None   # the nickname must be a registry entry whose canonical is itself
        if code is not None:
            code_canon = _registry_canonical(code, sport)
            if code_canon is not None and code_canon != c:
                return None   # 'DET Bills': a resolvable code naming another team
        canon.append(c)
        nicknames.append(nickname)
    yes_canon = _nfl_yes_identity(canon, yes_title, sport, ticker)
    if yes_canon is None:
        return None
    return (yes_title, nicknames[1] if canon[0] == yes_canon else nicknames[0])


# The 32 NFL canonical names (core/teams_data.py's NFL block). The flat registry is shared across
# sports ('new york' -> 'Knicks'), so an NFL market may only ever register one of these.
NFL_TEAMS = frozenset({
    "49ers", "Bears", "Bengals", "Bills", "Broncos", "Browns", "Buccaneers", "Cardinals",
    "Chargers", "Chiefs", "Colts", "Commanders", "Cowboys", "Dolphins", "Eagles", "Falcons",
    "Giants", "Jaguars", "Jets", "Lions", "Packers", "Panthers", "Patriots", "Raiders", "Rams",
    "Ravens", "Saints", "Seahawks", "Steelers", "Texans", "Titans", "Vikings",
})


def _nfl_yes_identity(canon: list[str], yes_title: str, sport: str, ticker: str) -> str | None:
    """Both NFL grammars. The YES canonical, or None unless: both pair teams
    are distinct NFL teams; yes_sub_title resolves (exact registry key) to exactly one of them; and
    the ticker suffix resolves (exact registry key, never a prefix of anything) to exactly that
    same team. The suffix and the city/nickname resolution must agree or the market is refused."""
    if len(canon) != 2 or canon[0] == canon[1]:
        return None   # both names one team
    if not all(c in NFL_TEAMS for c in canon):
        return None   # a cross-sport canonical ('Knicks') never registers on an NFL market
    yes_canon = _registry_canonical(yes_title, sport)
    if yes_canon is None or [c == yes_canon for c in canon].count(True) != 1:
        return None   # yes_sub_title must resolve to exactly one of the two pair teams
    segments = (ticker or "").split("-")
    if len(segments) < 3 or not segments[-1]:
        return None   # no ticker suffix to cross-check
    suffix_canon = _registry_canonical(segments[-1], sport)
    hits = [c == suffix_canon for c in canon].count(True) if suffix_canon is not None else 0
    if hits != 1 or suffix_canon != yes_canon:
        return None   # the suffix resolves to neither team, both, or not the YES team
    return yes_canon


def _title_teams(question: str, rules_primary: str = "", yes_title: str = "",
                 sport: str | None = None, rules_secondary: str = "",
                 ticker: str = "") -> tuple[str, str] | None:
    """'Texas vs Boston Winner?' / 'Game 5: New York at San Antonio Winner?' -> the two raw names.
    A coded NFL pair returns (yes_title, the opponent's nickname) instead."""
    t = re.sub(r"^Game \d+: ", "", game_title(question))
    parts = re.split(r" vs | at ", t, maxsplit=1)
    if len(parts) == 2:
        return (parts[0].strip(), parts[1].strip())
    # Venue fact (real KXMLBGAME page, 2026-09-16): Kalshi now titles each
    # team market '<Team> wins' (yes_sub_title = no_sub_title = '<Team>'), and only rules_primary
    # names both teams: 'If San Francisco wins the San Francisco vs Los Angeles D professional
    # baseball game ...'. Fall back to that, per market, and fail closed (None) on anything that
    # is not a clean two-team pair of registry-resolved names containing this market's YES team.
    # Two-way leagues only (sport in SERIES): three-way KXWCGAME never takes this path.
    yes_title = (yes_title or "").strip()
    if not (rules_primary and yes_title and sport and sport.lower() in SERIES):
        return None
    if (question or "").strip() != f"{yes_title} wins":
        return None   # YES must be exactly '<this team> wins', nothing else
    if not rules_primary.startswith(f"If {yes_title} wins the "):
        return None   # the rules must say THIS market's team is the one YES pays on
    # Delete the one whitelisted tie sentence verbatim, then scan the rest
    # with the unchanged tie/draw/regulation scan. Any other tie wording still refuses.
    secondary = (rules_secondary or "").replace(TIE_HALF_PAYOUT_SENTENCE, "")
    if re.search(r"\b(?:tie|tied|draw|regulation)\b", f"{rules_primary}\n{secondary}", re.I):
        return None   # a result other than one team winning exists: NO is not 'opponent wins'
    m = re.search(r"\bthe (.+?) (?:professional|pro) [a-z]+ game\b", rules_primary, re.I)
    if not m:
        return None
    parts = [p.strip() for p in re.split(r" vs | at ", m.group(1), maxsplit=1)]
    if len(parts) != 2 or not all(parts):
        return None
    if any(p.lower() in ("yes", "no", "tie", "draw") for p in parts):
        return None
    if yes_title not in parts:
        # The coded grammar ('DET Lions vs BUF Bills'), NFL only. Every
        # other league keeps 014's refusal of a pair that does not name the YES team verbatim.
        if sport.lower() != "nfl":
            return None
        return _coded_pair(parts, yes_title, sport, ticker)
    canon = [normalize_team_name(p, sport) for p in parts]
    if any(c == p for c, p in zip(canon, parts)) or canon[0] == canon[1]:
        return None   # core/teams.py returns the raw input when unmapped: unresolved
    if sport.lower() == "nfl" and _nfl_yes_identity(canon, yes_title, sport, ticker) is None:
        return None   # city form cross-checks too
    return (parts[0], parts[1])


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def game_start(market: dict, fallback_hours: float = 3.0) -> datetime | None:
    """True game start (UTC). Event tickers encode it in US Eastern time
    ('...-26JUN141920TEXBOS' = Jun 14 2026, 7:20pm ET — verified against
    Polymarket's start stamps); occurrence_datetime is the expected game END,
    so it's the fallback (minus `fallback_hours`) when the ticker has no time.
    Baseball ≈ 3h; World Cup soccer's occurrence is kickoff + ~4h (verified), so
    WC converters pass fallback_hours=4 to land on the real kickoff."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})?(?=[A-Z])", market.get("event_ticker") or "")
    if m and m.group(4) and m.group(2) in _MONTHS:
        yy, mon, dd, hhmm = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)), m.group(4)
        try:
            local = datetime(2000 + yy, mon, dd, int(hhmm[:2]), int(hhmm[2:]),
                             tzinfo=ZoneInfo("America/New_York"))
            return local.astimezone(timezone.utc)
        except ValueError:
            pass  # malformed ticker digits — fall through to the fallback
    end = market.get("occurrence_datetime") or market.get("expected_expiration_time") or ""
    try:
        return datetime.fromisoformat(str(end).replace("Z", "+00:00")) - timedelta(hours=fallback_hours)
    except ValueError:
        return None  # no time anchor at all


def kalshi_market_to_line(market: dict, sport: str) -> MoneyLine | None:
    """Convert one raw Kalshi WINNER market to a canonical MoneyLine; None = skip."""
    # A tie payout is only ever the record of THIS conversion; cleared first
    # so no refusal path below can leave a stale one behind.
    kalshi_tie_payout_cents.pop(market.get("ticker") or "", None)
    yes_title = (market.get("yes_sub_title") or "").strip()
    pair = _title_teams(market.get("title") or "", rules_primary=market.get("rules_primary") or "",
                        yes_title=yes_title, sport=sport,   # '<Team> wins' titles
                        rules_secondary=market.get("rules_secondary") or "",
                        ticker=market.get("ticker") or "")   # coded NFL pairs
    if not yes_title or not pair:
        return None
    opponent = pair[1] if yes_title == pair[0] else pair[0]
    team_yes = normalize_team_name(yes_title, sport)
    team_opp = normalize_team_name(opponent, sport)
    if team_yes == team_opp:
        return None  # both names resolved to the same team — bad parse

    dt = game_start(market)
    if dt is None:
        return None  # cannot match without a time anchor

    try:
        ask_yes_team = float(market["yes_ask_dollars"])  # price to BUY team_yes wins
        ask_opp_team = float(market["no_ask_dollars"])   # price to BUY opponent wins
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < ask_yes_team < 1 and 0 < ask_opp_team < 1):
        return None  # dead or one-sided book — unexecutable

    team_a, team_b = sorted([team_yes, team_opp])
    ask_a, ask_b = (ask_yes_team, ask_opp_team) if team_a == team_yes else (ask_opp_team, ask_yes_team)
    try:
        liquidity = float(market.get("yes_ask_size_fp") or 0.0)  # depth at ask, contracts ≈ USD
    except (TypeError, ValueError):
        liquidity = 0.0

    kalshi_sides[market["ticker"]] = team_yes   # the team this ticker's YES ('bid') buys
    kalshi_no_sides[market["ticker"]] = team_opp   # the team its NO ('ask') buys
    kalshi_ticks[market["ticker"]] = _market_tick(market)
    if TIE_HALF_PAYOUT_SENTENCE in (market.get("rules_secondary") or ""):
        kalshi_tie_payout_cents[market["ticker"]] = TIE_HALF_PAYOUT_CENTS
    start_date, start_et = et_stamp(dt)
    return MoneyLine(
        sport=sport.upper(), team_a=team_a, team_b=team_b,
        start_date=start_date, start_time=int(dt.timestamp()), start_et=start_et,
        platform="kalshi", market_id=market["ticker"],
        best_ask_yes=ask_a, best_ask_no=ask_b, liquidity=liquidity,
        last_price=_align_last(market, team_a == team_yes),
    )


# ------------------- canonical TotalLine / SpreadLine -----------------------
# Market type comes from the SERIES (KX<LG>TOTAL / KX<LG>SPREAD); team names come
# from a game_key -> (team_a, team_b) map built off the winner series (the only
# series whose title carries both teams). game_start() supplies the true start.

def kalshi_market_to_total_line(market: dict, sport: str, teams_by_key: dict) -> TotalLine | None:
    """Convert one raw Kalshi TOTAL market ('Over 8.5 runs scored', floor_strike 8.5)
    to a TotalLine; None = skip. YES = Over, NO = Under."""
    teams = teams_by_key.get(game_key(market.get("event_ticker", "")))
    if not teams:
        return None
    if not (market.get("yes_sub_title") or "").strip().lower().startswith("over"):
        return None
    line_value = _num(market.get("floor_strike"))
    dt = game_start(market)
    if line_value is None or line_value <= 0 or dt is None:
        return None
    try:
        over = float(market["yes_ask_dollars"])
        under = float(market["no_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < over < 1 and 0 < under < 1):
        return None
    kalshi_sides[market["ticker"]] = "Over"   # Kalshi total YES side = Over (the trade client orients off this)
    kalshi_no_sides[market["ticker"]] = "Under"
    kalshi_ticks[market["ticker"]] = _market_tick(market)
    start_date, start_et = et_stamp(dt)
    return TotalLine(
        sport=sport.upper(), team_a=teams[0], team_b=teams[1],
        start_date=start_date, start_time=int(dt.timestamp()), start_et=start_et,
        line_value=line_value, platform="kalshi", market_id=market["ticker"],
        platform_url=f"https://kalshi.com/markets/{market['ticker']}",
        best_ask_over=over, best_ask_under=under,
        liquidity=float(market.get("yes_ask_size_fp") or 0.0),
        last_price=_align_last(market, True),   # Kalshi total YES = Over = the A-side
    )


def kalshi_market_to_spread_line(market: dict, sport: str, teams_by_key: dict) -> SpreadLine | None:
    """Convert one raw Kalshi SPREAD market ('Toronto wins by over 1.5 runs', floor 1.5)
    to a SpreadLine; None = skip. 'wins by over N.5' == an N.5 runline cover for the
    named (favored) team; expressed signed from canonical team_a."""
    teams = teams_by_key.get(game_key(market.get("event_ticker", "")))
    if not teams:
        return None
    team_a, team_b = teams
    m = re.match(r"^(.+?)\s+wins by over\b", market.get("title") or "", re.IGNORECASE)
    mag = _num(market.get("floor_strike"))
    dt = game_start(market)
    if not m or mag is None or mag <= 0 or dt is None:
        return None
    fav = normalize_team_name(m.group(1).strip(), sport)
    try:
        yes = float(market["yes_ask_dollars"])   # 'favorite wins by over mag'
        no = float(market["no_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < yes < 1 and 0 < no < 1):
        return None
    if fav == team_a:        # team_a favored: YES = team_a covers -mag
        line_value, cover, nocover = -mag, yes, no
    elif fav == team_b:      # team_a is the +mag underdog: it covers when fav does NOT (NO)
        line_value, cover, nocover = mag, no, yes
    else:
        return None
    # Kalshi spread YES = the favorite covers; map that to our canonical Cover/No-Cover side
    kalshi_sides[market["ticker"]] = "Cover" if fav == team_a else "No Cover"
    kalshi_no_sides[market["ticker"]] = "No Cover" if fav == team_a else "Cover"
    kalshi_ticks[market["ticker"]] = _market_tick(market)
    start_date, start_et = et_stamp(dt)
    return SpreadLine(
        sport=sport.upper(), team_a=team_a, team_b=team_b,
        start_date=start_date, start_time=int(dt.timestamp()), start_et=start_et,
        line_value=line_value, platform="kalshi", market_id=market["ticker"],
        platform_url=f"https://kalshi.com/markets/{market['ticker']}",
        best_ask_cover=cover, best_ask_nocover=nocover,
        liquidity=float(market.get("yes_ask_size_fp") or 0.0),
        last_price=_align_last(market, fav == team_a),   # cover = YES only when team_a is the favorite
    )


def fetch_kalshi_extra_lines(leagues: list[str]) -> tuple[list[TotalLine], list[SpreadLine]]:
    """Every Kalshi totals + spreads market as TotalLine/SpreadLine lists. Builds the
    game_key -> teams map from the winner series first (it names both teams)."""
    totals: list[TotalLine] = []
    spreads: list[SpreadLine] = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for lg in leagues:
            teams_by_key: dict[str, tuple[str, str]] = {}
            for m in fetch_markets(f"KX{lg.upper()}GAME", client):
                ln = kalshi_market_to_line(m, lg)
                if ln:
                    teams_by_key[game_key(m.get("event_ticker", ""))] = (ln.team_a, ln.team_b)
            for m in fetch_markets(f"KX{lg.upper()}TOTAL", client):
                t = kalshi_market_to_total_line(m, lg, teams_by_key)
                if t:
                    totals.append(t)
            for m in fetch_markets(f"KX{lg.upper()}SPREAD", client):
                s = kalshi_market_to_spread_line(m, lg, teams_by_key)
                if s:
                    spreads.append(s)
    return totals, spreads


def _dedupe_moneyline(lines: list[MoneyLine]) -> list[MoneyLine]:
    """One MoneyLine per game — keep the deepest book.
    Key by START_TIME, not ET date — the two mirror winner markets of ONE
    event share the ticker-decoded start exactly, but a doubleheader's two games do NOT.
    Keying by date collapsed both games into whichever had the deeper book, which let the
    matcher pair game 1 on one venue with game 2 on the other."""
    best: dict[tuple, MoneyLine] = {}
    for line in lines:
        key = (line.sport, line.team_a, line.team_b, line.start_time)
        if key not in best or line.liquidity > best[key].liquidity:
            best[key] = line
    return list(best.values())


def fetch_kalshi_all(leagues: list[str], bet_types=None) -> tuple[list[MoneyLine], list[TotalLine], list[SpreadLine]]:
    """All requested Kalshi bet types in one pass: the GAME series is fetched ONCE per league
    and reused for both the moneylines and the totals/spreads team map (was fetched twice).
    bet_types defaults to config.BET_TYPES; trimming it (e.g. moneyline only) skips the
    matching TOTAL/SPREAD requests. Equivalent to fetch_kalshi_lines + fetch_kalshi_extra_lines."""
    want = config.active_bet_types(bet_types)
    need_extra = bool(want & {"totals", "spreads"})
    money: list[MoneyLine] = []
    totals: list[TotalLine] = []
    spreads: list[SpreadLine] = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for lg in leagues:
            if lg not in SERIES:   # e.g. "wc" — has its own fetch path (fetch_kalshi_wc_lines)
                continue
            teams_by_key: dict[str, tuple[str, str]] = {}
            for m in fetch_markets(f"KX{lg.upper()}GAME", client):  # needed for moneylines and the team map
                ln = kalshi_market_to_line(m, lg)
                if ln:
                    if "moneyline" in want:
                        money.append(ln)
                    teams_by_key[game_key(m.get("event_ticker", ""))] = (ln.team_a, ln.team_b)
            if not need_extra or not teams_by_key:   # no games (or no extra wanted) -> skip those requests
                continue
            if "totals" in want:
                for m in fetch_markets(f"KX{lg.upper()}TOTAL", client):
                    t = kalshi_market_to_total_line(m, lg, teams_by_key)
                    if t:
                        totals.append(t)
            if "spreads" in want:
                for m in fetch_markets(f"KX{lg.upper()}SPREAD", client):
                    s = kalshi_market_to_spread_line(m, lg, teams_by_key)
                    if s:
                        spreads.append(s)
    return _dedupe_moneyline(money), totals, spreads


# ------------------- World Cup (soccer) — totals + spreads ------------------
# WC markets share a game_key across series; the PERIOD is encoded in the series
# prefix (KXWCSPREAD=full, KXWC1HSPREAD=1st half, KXWC2HSPREAD=2nd half, same for
# TOTAL). Teams come from KXWCGAME ("A vs B Winner?", 3-way incl. Tie — used only
# for the name map, never traded). Spread title: "<Fav> wins by more than N.5 goals".
WC_GAME_SERIES = "KXWCGAME"
WC_SERIES = {  # period -> {kind: series ticker}
    "full": {"total": "KXWCTOTAL", "spread": "KXWCSPREAD"},
    "1h":   {"total": "KXWC1HTOTAL", "spread": "KXWC1HSPREAD"},
    "2h":   {"total": "KXWC2HTOTAL", "spread": "KXWC2HSPREAD"},
}


def _wc_teams_by_key(game_markets: list[dict]) -> dict:
    """game_key -> (team_a, team_b) from KXWCGAME titles ('A vs B Winner?')."""
    out: dict[str, tuple[str, str]] = {}
    for m in game_markets:
        pair = _title_teams(m.get("title") or "")
        if not pair:
            continue
        ta, tb = normalize_team_name(pair[0], "wc"), normalize_team_name(pair[1], "wc")
        if ta != tb:
            out[game_key(m.get("event_ticker", ""))] = tuple(sorted([ta, tb]))
    return out


def kalshi_wc_total_line(market: dict, teams_by_key: dict, period: str) -> TotalLine | None:
    """KXWC[1H|2H]TOTAL market -> TotalLine; YES = Over. None = skip."""
    teams = teams_by_key.get(game_key(market.get("event_ticker", "")))
    if not teams or not (market.get("yes_sub_title") or "").strip().lower().startswith("over"):
        return None
    line_value = _num(market.get("floor_strike"))
    dt = game_start(market, fallback_hours=4.0)   # WC occurrence is kickoff + ~4h
    if line_value is None or line_value <= 0 or dt is None:
        return None
    try:
        over, under = float(market["yes_ask_dollars"]), float(market["no_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < over < 1 and 0 < under < 1):
        return None
    kalshi_sides[market["ticker"]] = "Over"
    kalshi_no_sides[market["ticker"]] = "Under"
    kalshi_ticks[market["ticker"]] = _market_tick(market)
    start_date, start_et = et_stamp(dt)
    return TotalLine(
        sport="WC", team_a=teams[0], team_b=teams[1], start_date=start_date,
        start_time=int(dt.timestamp()), start_et=start_et, line_value=line_value, period=period,
        platform="kalshi", market_id=market["ticker"], platform_url=f"https://kalshi.com/markets/{market['ticker']}",
        best_ask_over=over, best_ask_under=under, liquidity=float(market.get("yes_ask_size_fp") or 0.0))


def kalshi_wc_spread_line(market: dict, teams_by_key: dict, period: str) -> SpreadLine | None:
    """KXWC[1H|2H]SPREAD market ('<Fav> wins by more than N.5 goals') -> SpreadLine,
    signed from canonical team_a (negative = team_a favored). None = skip."""
    teams = teams_by_key.get(game_key(market.get("event_ticker", "")))
    if not teams:
        return None
    team_a, team_b = teams
    m = re.match(r"^(.+?)\s+wins\b.*?\bby more than\b", market.get("title") or "", re.IGNORECASE)
    mag = _num(market.get("floor_strike"))
    dt = game_start(market, fallback_hours=4.0)   # WC occurrence is kickoff + ~4h
    if not m or mag is None or mag <= 0 or dt is None:
        return None
    fav = normalize_team_name(m.group(1).strip(), "wc")
    try:
        yes, no = float(market["yes_ask_dollars"]), float(market["no_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < yes < 1 and 0 < no < 1):
        return None
    if fav == team_a:
        line_value, cover, nocover = -mag, yes, no
    elif fav == team_b:
        line_value, cover, nocover = mag, no, yes
    else:
        return None
    kalshi_sides[market["ticker"]] = "Cover" if fav == team_a else "No Cover"
    kalshi_no_sides[market["ticker"]] = "No Cover" if fav == team_a else "Cover"
    kalshi_ticks[market["ticker"]] = _market_tick(market)
    start_date, start_et = et_stamp(dt)
    return SpreadLine(
        sport="WC", team_a=team_a, team_b=team_b, start_date=start_date,
        start_time=int(dt.timestamp()), start_et=start_et, line_value=line_value, period=period,
        platform="kalshi", market_id=market["ticker"], platform_url=f"https://kalshi.com/markets/{market['ticker']}",
        best_ask_cover=cover, best_ask_nocover=nocover, liquidity=float(market.get("yes_ask_size_fp") or 0.0))


def fetch_kalshi_wc_lines(bet_types=None) -> tuple[list[TotalLine], list[SpreadLine]]:
    """Every World Cup totals + spreads line (full/1H/2H) from Kalshi. Moneyline is 3-way
    (win/draw/lose) so it's excluded — not two-way arbitrable."""
    want = config.active_bet_types(bet_types)
    totals: list[TotalLine] = []
    spreads: list[SpreadLine] = []
    if not (want & {"totals", "spreads"}):
        return totals, spreads
    with httpx.Client(timeout=TIMEOUT) as client:
        teams_by_key = _wc_teams_by_key(fetch_markets(WC_GAME_SERIES, client))
        if not teams_by_key:
            return totals, spreads
        for period, ser in WC_SERIES.items():
            if "totals" in want:
                for m in fetch_markets(ser["total"], client):
                    t = kalshi_wc_total_line(m, teams_by_key, period)
                    if t:
                        totals.append(t)
            if "spreads" in want:
                for m in fetch_markets(ser["spread"], client):
                    s = kalshi_wc_spread_line(m, teams_by_key, period)
                    if s:
                        spreads.append(s)
    return totals, spreads


# ------------------------------- display ------------------------------------

def main_market(game: dict, mtype: str) -> dict | None:
    """The headline market of a type: YES price closest to 0.50 (= the main line)."""
    candidates = [m for m in game["markets"] if m["type"] == mtype and m["yes_bid"] is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda m: abs((m["yes_bid"] + m["yes_ask"]) / 2 - 0.5))


def display_et(iso: str | None) -> str:
    """ISO UTC string -> '2026-06-14 19:20 ET' for human display."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except ValueError:
        return "time TBD"


def print_game(g: dict) -> None:
    print(f"  {g['title']}   {display_et(g['start_time'])}   ({len(g['markets'])} markets)")
    for m in [m for m in g["markets"] if m["type"] == "moneyline"]:
        print(f"    moneyline: {m['side']} YES   bid {m['yes_bid']} / ask {m['yes_ask']}")
    for label in ("spreads", "totals"):
        m = main_market(g, label)
        if m:
            print(f"    {label:<9}: {m['side']}   bid {m['yes_bid']} / ask {m['yes_ask']}")


def print_board(board: dict) -> None:
    for league, games in board.items():
        print(f"\n{league.upper()} — {len(games)} game(s) trading")
        for g in games:
            print_game(g)


# --------------------------------- CLI --------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull all games + odds trading on Kalshi.")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds between pulls")
    parser.add_argument("--once", action="store_true", help="single pull, then exit")
    parser.add_argument("--league", action="append", choices=LEAGUES, help="limit leagues (repeatable)")
    parser.add_argument("--json", action="store_true", help="print full raw data as JSON")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        raise ValueError(f"interval must be positive, got {args.interval}")
    leagues = args.league or LEAGUES

    try:
        while True:
            stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
            board = pull_board(leagues)
            if args.json:
                print(json.dumps({"pulled_at": stamp, "source": "kalshi", "games": board}, indent=2))
            else:
                total = sum(len(g) for g in board.values())
                print(f"\n=== {stamp} — {total} game(s) trading on Kalshi ===")
                print_board(board)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
