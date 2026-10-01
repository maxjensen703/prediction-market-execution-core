"""
fetch/polymarket_us.py — market-data fetch for Polymarket US (QCX)
— the keyless market-data gateway (gateway.polymarket.us), the TRADEABLE Polymarket for a
US person. Emits canonical MoneyLine/TotalLine/SpreadLine (core/schema.py) with
platform="polymarket_us" and the PM US market SLUG as market_id, so execute/
polymarket_us_broker.py can place orders against them.

Converter section map: polymarket_us_market_to_line (moneyline) / _to_total_line /
_to_spread_line (full-game; team roles from marketSides' long/short) ->
polymarket_us_wc_*_line (World Cup periods; soccer REVERSES the spread's long/short
convention — favorite is derived from the description sign, not long/short).
fetch_polymarket_us_all() is the one-call entry point for all requested bet types.
"""

import argparse
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

import config
from core import perf
from core.schema import ET, MoneyLine, SpreadLine, TotalLine, et_stamp
from core.teams import normalize_team_name, order_teams

# Tunables live in config.py (the one place to change them); aliased here for local use.
BASE = config.PM_US_GATEWAY      # keyless market-data gateway (reads only)
LEAGUES = list(config.LEAGUES)
TIMEOUT = config.PM_US_TIMEOUT
POLL_SECONDS = config.CLI_POLL_SECONDS

# ══ REGISTRIES — written on each fetch, read at order time by the brokers via call-time
# imports; a market missing here fails closed (see the README's registries note).
# These dicts live in memory and are empty in a new process: run a fetch, or persist and
# restore them yourself, before placing orders. ══
# Direct event URLs captured during conversion, keyed by market_id (slug); for display.
polymarket_us_urls: dict[str, str] = {}

# Tradeable side id per canonical team, keyed by market_id (slug): {slug: {team: side_id}}.
# Captured at fetch time from marketSides; the trade client reads it to place an order.
polymarket_us_sides: dict[str, dict] = {}

# slug -> price tick size (dollars), from the market's orderPriceMinTickSize; read by
# execute.broker.venue_tick for order rounding.
polymarket_us_ticks: dict[str, float] = {}


def _market_tick(market: dict, fallback: float = 0.005) -> float:
    """Tick size from orderPriceMinTickSize; fallback if absent/unparseable/non-positive
    (a 0 or negative tick would zero-divide downstream in venue_tick)."""
    v = _num(market.get("orderPriceMinTickSize"))
    return v if v is not None and math.isfinite(v) and v > 0 else fallback


# ------------------------------- fetching -----------------------------------

def fetch_events(league: str, client: httpx.Client) -> list[dict]:
    """Every event for one league from the PM US gateway (keyless)."""
    with perf.sample("pm_us_events"):
        r = client.get(f"{BASE}/v2/leagues/{league}/events")
    if r.status_code != 200:
        raise RuntimeError(f"PM US gateway HTTP {r.status_code}: {r.text[:200]}")
    return (r.json() or {}).get("events", [])


def fetch_book(slug: str, client: httpx.Client) -> dict | None:
    """Live order book for one market slug -> marketData dict ({bids, offers, ...}); None on error."""
    try:
        with perf.sample("pm_us_book"):
            r = client.get(f"{BASE}/v1/markets/{slug}/book")
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("marketData") or {}
    except httpx.HTTPError:
        return None


# ----------------------------- normalization --------------------------------

def _num(val) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _pmus_bet_type(market: dict) -> str | None:
    """Canonical bet type for a PM US market. Handles BOTH the old generic sportsMarketType
    ('moneyline'/'totals'/'spreads') and the new sport-prefixed names PM US switched to
    ('baseball_team_full_game_winner'/'_total'/'_spread', likewise for other sports). Returns
    None for anything we don't trade as a full-game line (e.g. 'first_five' / period markets)."""
    t = (market.get("sportsMarketType") or "").lower()
    if t == "moneyline" or t.endswith("_full_game_winner"):
        return "moneyline"
    if t == "totals" or t.endswith("_full_game_total"):
        return "totals"
    if t == "spreads" or t.endswith("_full_game_spread"):
        return "spreads"
    return None


def _px(level: dict) -> float | None:
    return _num((level.get("px") or {}).get("value"))


def _book_quote(md: dict) -> tuple:
    """(best_ask, best_bid, ask_usd_depth, bid_usd_depth) from a marketData book.
    best ask = top offer (buy long here); best bid = top bid (buy short = 1 - bid)."""
    offers, bids = md.get("offers") or [], md.get("bids") or []
    best_ask = _px(offers[0]) if offers else None
    best_bid = _px(bids[0]) if bids else None
    ask_usd = best_ask * (_num(offers[0].get("qty")) or 0.0) if (offers and best_ask) else 0.0
    bid_usd = best_bid * (_num(bids[0].get("qty")) or 0.0) if (bids and best_bid) else 0.0
    return best_ask, best_bid, ask_usd, bid_usd


def _align_last(md: dict | None, a_is_long: bool) -> float | None:
    """The book's last-trade price in the A-SIDE's terms (best_ask_yes/over/cover). PM US quotes the
    LONG instrument, so lastTradePx is the long-side cost; if the A-side is the short side, flip to
    1-last. None when there's no book/last (observational phantom signal only)."""
    v = ((md or {}).get("stats") or {}).get("lastTradePx")
    raw = _num(v.get("value")) if isinstance(v, dict) else _num(v)
    return None if raw is None else round(raw if a_is_long else 1.0 - raw, 4)


def _ml_sides(market: dict, sport: str) -> list | None:
    """[(canon_team, side_id, is_long), ...] for the two moneyline sides; None unless exactly two."""
    out = []
    for s in market.get("marketSides") or []:
        name = (s.get("team") or {}).get("name") or s.get("description")
        if not name:
            continue
        out.append((normalize_team_name(name, sport), str(s.get("id") or ""), bool(s.get("long"))))
    return out if len(out) == 2 else None


def _start_dt(event: dict, market: dict) -> datetime | None:
    start = event.get("startTime") or market.get("gameStartTime") or ""
    try:
        return datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None


def _over_long(sides: list) -> bool | None:
    """Orientation is NEVER guessed. Exactly one side must parse as
    "over" and one as "under", with OPPOSITE long flags — anything else (venue renamed the
    descriptions, degenerate flags) returns None and the market is skipped. The old
    default-True guess could put a real resting order on the WRONG instrument."""
    overs = [s for s in sides if (s.get("description") or "").strip().lower() == "over"]
    unders = [s for s in sides if (s.get("description") or "").strip().lower() == "under"]
    if len(overs) != 1 or len(unders) != 1:
        return None
    over_long = bool(overs[0].get("long"))
    if over_long == bool(unders[0].get("long")):
        return None   # both long / both short: orientation unknowable
    return over_long


def polymarket_us_market_to_line(market: dict, event: dict, sport: str, market_data: dict | None) -> MoneyLine | None:
    """Convert one PM US MONEYLINE market (+ its book) to a canonical MoneyLine; None = skip."""
    if _pmus_bet_type(market) != "moneyline":
        return None
    sides = _ml_sides(market, sport)
    if not sides:
        return None
    (t1, id1, long1), (t2, id2, long2) = sides
    if bool(long1) == bool(long2):
        return None   # degenerate long flags (schema drift) — orientation unknowable, skip
    long_team = t1 if long1 else t2
    short_team = t2 if long1 else t1
    if long_team == short_team:
        return None

    best_ask, best_bid, ask_usd, bid_usd = _book_quote(market_data or {})
    if best_ask is None:                                   # fall back to the cached quote on the market
        best_ask = _num((market.get("bestAskQuote") or {}).get("value"))
    if best_bid is None:
        best_bid = _num((market.get("bestBidQuote") or {}).get("value"))
    if best_ask is None or best_bid is None:
        return None
    long_ask = round(best_ask, 4)                          # buy the long instrument at the ask
    short_ask = round(1.0 - best_bid, 4)                   # buy the short side = 1 - best bid
    if not (0 < long_ask < 1 and 0 < short_ask < 1):
        return None

    dt = _start_dt(event, market)
    market_id = market.get("slug") or str(market.get("id", ""))
    if dt is None or not market_id:
        return None

    team_a, team_b = sorted([long_team, short_team])
    if team_a == long_team:
        ask_a, ask_b, liq_a, liq_b = long_ask, short_ask, ask_usd, bid_usd
    else:
        ask_a, ask_b, liq_a, liq_b = short_ask, long_ask, bid_usd, ask_usd

    # remember each team's side (id + long/short role) so the trade client can place the order
    role = {t1: {"side_id": id1, "long": long1}, t2: {"side_id": id2, "long": long2}}
    polymarket_us_sides[market_id] = {team_a: role[team_a], team_b: role[team_b]}
    polymarket_us_ticks[market_id] = _market_tick(market)
    slug = event.get("slug", "")
    if slug:
        polymarket_us_urls[market_id] = f"https://polymarket.us/event/{slug}"
    start_date, start_et = et_stamp(dt)
    return MoneyLine(
        sport=sport.upper(), team_a=team_a, team_b=team_b,
        start_date=start_date, start_time=int(dt.timestamp()), start_et=start_et,
        platform="polymarket_us", market_id=market_id,
        best_ask_yes=ask_a, best_ask_no=ask_b, liquidity=round(min(liq_a, liq_b), 2),
        last_price=_align_last(market_data, team_a == long_team),
    )


def fetch_polymarket_us_lines(leagues: list[str]) -> list[MoneyLine]:
    """Every PM US game moneyline as a MoneyLine (platform='polymarket_us'), book-priced."""
    lines: list[MoneyLine] = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for lg in leagues:
            for e in fetch_events(lg, client):
                if e.get("ended") is True:
                    continue
                for m in (e.get("markets") or []):
                    if _pmus_bet_type(m) != "moneyline":
                        continue
                    md = fetch_book(m.get("slug", ""), client)
                    line = polymarket_us_market_to_line(m, e, lg, md)
                    if line:
                        lines.append(line)
    return lines


# ----------------- canonical TotalLine / SpreadLine -------------------------
# Totals: the long side is "Over". Spreads: the long side is the +X underdog, the
# short side the -X favorite (`line` is the unsigned magnitude). We re-express the
# spread signed from canonical team_a, mirroring fetch/kalshi.py + fetch/polymarket.py.

def _event_teams(event: dict, sport: str) -> tuple | None:
    """Canonical (team_a, team_b) from the PM US event (teams list, else 'A vs. B' title)."""
    names = [t.get("name") for t in (event.get("teams") or []) if t.get("name")]
    if len(names) < 2:
        parts = re.split(r"\s+vs\.?\s+", event.get("title") or "", maxsplit=1)
        names = [p.strip() for p in parts] if len(parts) == 2 else []
    if len(names) < 2:
        return None
    ta, tb = order_teams(names[0], names[1], sport)
    return (ta, tb) if ta != tb else None


def _quote(market: dict, market_data: dict | None):
    """(best_ask, best_bid, ask_usd, bid_usd): the live book if given, else the market's
    cached bestAskQuote/bestBidQuote (no extra request — keeps the totals/spreads sweep fast)."""
    best_ask, best_bid, ask_usd, bid_usd = _book_quote(market_data or {})
    if best_ask is None:
        best_ask = _num((market.get("bestAskQuote") or {}).get("value"))
    if best_bid is None:
        best_bid = _num((market.get("bestBidQuote") or {}).get("value"))
    return best_ask, best_bid, ask_usd, bid_usd


def polymarket_us_market_to_total_line(market: dict, event: dict, sport: str, market_data: dict | None = None) -> TotalLine | None:
    """Convert one PM US TOTALS market (+ optional book) to a TotalLine; None = skip."""
    if _pmus_bet_type(market) != "totals":
        return None
    line_value = _num(market.get("line"))
    teams = _event_teams(event, sport)
    if line_value is None or line_value <= 0 or not teams:
        return None
    best_ask, best_bid, ask_usd, bid_usd = _quote(market, market_data)
    if best_ask is None or best_bid is None:
        return None
    over_long = _over_long(market.get("marketSides") or [])
    if over_long is None:
        return None   # fail closed on unparseable orientation, never guess
    over_ask = round(best_ask if over_long else 1.0 - best_bid, 4)        # buy Over (long) at the ask
    under_ask = round((1.0 - best_bid) if over_long else best_ask, 4)     # buy Under (short) = 1 - bid
    dt = _start_dt(event, market)
    market_id = market.get("slug") or str(market.get("id", ""))
    if not (0 < over_ask < 1 and 0 < under_ask < 1) or dt is None or not market_id:
        return None
    polymarket_us_sides[market_id] = {"Over": {"long": over_long}, "Under": {"long": not over_long}}
    polymarket_us_ticks[market_id] = _market_tick(market)
    if event.get("slug"):
        polymarket_us_urls[market_id] = f"https://polymarket.us/event/{event['slug']}"
    start_date, start_et = et_stamp(dt)
    return TotalLine(
        sport=sport.upper(), team_a=teams[0], team_b=teams[1],
        start_date=start_date, start_time=int(dt.timestamp()), start_et=start_et,
        line_value=line_value, platform="polymarket_us", market_id=market_id,
        platform_url=polymarket_us_urls.get(market_id, ""),
        best_ask_over=over_ask, best_ask_under=under_ask, liquidity=round(min(ask_usd, bid_usd), 2),
        last_price=_align_last(market_data, over_long),
    )


def polymarket_us_market_to_spread_line(market: dict, event: dict, sport: str, market_data: dict | None = None) -> SpreadLine | None:
    """Convert one PM US SPREADS market (+ optional book) to a SpreadLine; None = skip.
    Signed from canonical team_a (negative = team_a favored)."""
    if _pmus_bet_type(market) != "spreads":
        return None
    mag = _num(market.get("line"))   # unsigned magnitude, e.g. 4.5
    sides = market.get("marketSides") or []
    fav = next((s for s in sides if not s.get("long")), None)   # short side, desc "-X" = favorite
    dog = next((s for s in sides if s.get("long")), None)        # long side, desc "+X" = underdog
    if mag is None or mag <= 0 or not fav or not dog:
        return None
    fav_team = normalize_team_name((fav.get("team") or {}).get("name") or "", sport)
    dog_team = normalize_team_name((dog.get("team") or {}).get("name") or "", sport)
    if not fav_team or not dog_team or fav_team == dog_team:
        return None
    best_ask, best_bid, ask_usd, bid_usd = _quote(market, market_data)
    if best_ask is None or best_bid is None:
        return None
    dog_cover_ask = round(best_ask, 4)         # buy underdog cover = long instrument
    fav_cover_ask = round(1.0 - best_bid, 4)   # buy favorite cover = short instrument
    dt = _start_dt(event, market)
    market_id = market.get("slug") or str(market.get("id", ""))
    if not (0 < dog_cover_ask < 1 and 0 < fav_cover_ask < 1) or dt is None or not market_id:
        return None
    team_a, team_b = sorted([fav_team, dog_team])
    if team_a == fav_team:        # team_a favored: -mag; team_a covers = favorite (short side)
        line_value, cover, nocover, cover_long = -mag, fav_cover_ask, dog_cover_ask, False
    else:                          # team_a is the +mag underdog; covers = underdog (long side)
        line_value, cover, nocover, cover_long = mag, dog_cover_ask, fav_cover_ask, True
    polymarket_us_sides[market_id] = {"Cover": {"long": cover_long}, "No Cover": {"long": not cover_long}}
    polymarket_us_ticks[market_id] = _market_tick(market)
    if event.get("slug"):
        polymarket_us_urls[market_id] = f"https://polymarket.us/event/{event['slug']}"
    start_date, start_et = et_stamp(dt)
    return SpreadLine(
        sport=sport.upper(), team_a=team_a, team_b=team_b,
        start_date=start_date, start_time=int(dt.timestamp()), start_et=start_et,
        line_value=line_value, platform="polymarket_us", market_id=market_id,
        platform_url=polymarket_us_urls.get(market_id, ""),
        best_ask_cover=cover, best_ask_nocover=nocover, liquidity=round(min(ask_usd, bid_usd), 2),
        last_price=_align_last(market_data, cover_long),
    )


BOOK_WORKERS = config.BOOK_WORKERS   # concurrent book fetches (keyless reads, different host from Kalshi)


def _fetch_books_parallel(slugs: list[str], client: httpx.Client) -> dict[str, dict | None]:
    """Fetch many order books concurrently -> {slug: marketData}. Same result as fetching
    each sequentially (fetch_book swallows errors to None); just faster."""
    uniq = [s for s in dict.fromkeys(slugs) if s]
    if not uniq:
        return {}
    out: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=min(BOOK_WORKERS, len(uniq))) as ex:
        for slug, md in zip(uniq, ex.map(lambda s: fetch_book(s, client), uniq)):
            out[slug] = md
    return out


def fetch_polymarket_us_all(leagues: list[str], bet_types=None) -> tuple[list[MoneyLine], list[TotalLine], list[SpreadLine]]:
    """All requested PM US bet types in one pass: events are fetched ONCE per league (was twice)
    and moneyline books are fetched concurrently. bet_types defaults to config.BET_TYPES;
    trimming it skips that work (e.g. no moneyline -> no book fetches). Equivalent to
    fetch_polymarket_us_lines + fetch_polymarket_us_extra_lines."""
    want = config.active_bet_types(bet_types)
    money: list[MoneyLine] = []
    totals: list[TotalLine] = []
    spreads: list[SpreadLine] = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for lg in leagues:
            if lg == "wc":   # World Cup has its own path (fetch_polymarket_us_wc_lines, league 'fwc')
                continue
            events = [e for e in fetch_events(lg, client) if e.get("ended") is not True]
            if "moneyline" in want:
                ml = [(e, m) for e in events for m in (e.get("markets") or [])
                      if _pmus_bet_type(m) == "moneyline"]
                books = _fetch_books_parallel([m.get("slug", "") for _, m in ml], client)
                for e, m in ml:
                    line = polymarket_us_market_to_line(m, e, lg, books.get(m.get("slug", "")))
                    if line:
                        money.append(line)
            if not (want & {"totals", "spreads"}):
                continue
            # totals/spreads: fetch each book too (real depth for the sizing cap), like moneyline
            xjobs = [(e, m, t) for e in events for m in (e.get("markets") or [])
                     for t in [_pmus_bet_type(m)]
                     if (t == "totals" and "totals" in want) or (t == "spreads" and "spreads" in want)]
            xbooks = _fetch_books_parallel([m.get("slug", "") for _, m, _ in xjobs], client)
            for e, m, t in xjobs:
                md = xbooks.get(m.get("slug", ""))
                if t == "totals":
                    ln = polymarket_us_market_to_total_line(m, e, lg, md)
                    if ln:
                        totals.append(ln)
                else:
                    ln = polymarket_us_market_to_spread_line(m, e, lg, md)
                    if ln:
                        spreads.append(ln)
    return money, totals, spreads


def fetch_polymarket_us_extra_lines(leagues: list[str]) -> tuple[list[TotalLine], list[SpreadLine]]:
    """Every PM US totals + spreads market as TotalLine/SpreadLine. Uses the market's
    cached bestBid/bestAsk quote (no per-market book request, to keep this fast)."""
    totals: list[TotalLine] = []
    spreads: list[SpreadLine] = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for lg in leagues:
            for e in fetch_events(lg, client):
                if e.get("ended") is True:
                    continue
                for m in (e.get("markets") or []):
                    t = _pmus_bet_type(m)
                    if t == "totals":
                        ln = polymarket_us_market_to_total_line(m, e, lg, None)
                        if ln:
                            totals.append(ln)
                    elif t == "spreads":
                        ln = polymarket_us_market_to_spread_line(m, e, lg, None)
                        if ln:
                            spreads.append(ln)
    return totals, spreads


# ------------------- World Cup (soccer) — totals + spreads ------------------
# PM US league slug = "fwc". Period is encoded in sportsMarketType. NOTE the spread
# sign convention DIFFERS from MLB: for soccer the favorite ("-X") is the LONG side,
# so we derive favorite/underdog from the description SIGN, not from long/short.
WC_LEAGUE = "fwc"
WC_TYPE_MAP = {  # sportsMarketType -> (kind, period)
    "soccer_team_full_game_total": ("total", "full"),
    "soccer_team_first_half_total": ("total", "1h"),
    "soccer_team_second_half_total": ("total", "2h"),
    "soccer_team_full_game_spread": ("spread", "full"),
    "soccer_team_first_half_spread": ("spread", "1h"),
    "soccer_team_second_half_spread": ("spread", "2h"),
}


def polymarket_us_wc_total_line(market: dict, event: dict, period: str, market_data: dict | None = None) -> TotalLine | None:
    """PM US soccer game total -> TotalLine (Over = long). None = skip."""
    line_value = _num(market.get("line"))
    teams = _event_teams(event, "wc")
    if line_value is None or line_value <= 0 or not teams:
        return None
    best_ask, best_bid, ask_usd, bid_usd = _quote(market, market_data)
    if best_ask is None or best_bid is None:
        return None
    over_long = _over_long(market.get("marketSides") or [])
    if over_long is None:
        return None   # fail closed on unparseable orientation, never guess
    over_ask = round(best_ask if over_long else 1.0 - best_bid, 4)
    under_ask = round((1.0 - best_bid) if over_long else best_ask, 4)
    dt = _start_dt(event, market)
    market_id = market.get("slug") or str(market.get("id", ""))
    if not (0 < over_ask < 1 and 0 < under_ask < 1) or dt is None or not market_id:
        return None
    polymarket_us_sides[market_id] = {"Over": {"long": over_long}, "Under": {"long": not over_long}}
    polymarket_us_ticks[market_id] = _market_tick(market)
    if event.get("slug"):
        polymarket_us_urls[market_id] = f"https://polymarket.us/event/{event['slug']}"
    start_date, start_et = et_stamp(dt)
    return TotalLine(
        sport="WC", team_a=teams[0], team_b=teams[1], start_date=start_date,
        start_time=int(dt.timestamp()), start_et=start_et, line_value=line_value, period=period,
        platform="polymarket_us", market_id=market_id, platform_url=polymarket_us_urls.get(market_id, ""),
        best_ask_over=over_ask, best_ask_under=under_ask, liquidity=round(min(ask_usd, bid_usd), 2))


def polymarket_us_wc_spread_line(market: dict, event: dict, period: str, market_data: dict | None = None) -> SpreadLine | None:
    """PM US soccer goal spread -> SpreadLine signed from canonical team_a. Favorite is the
    side whose description starts '-' (NOT the short side — soccer reverses that). None = skip."""
    sides = market.get("marketSides") or []
    fav = dog = None   # (canonical_team, is_long)
    for s in sides:
        name = (s.get("team") or {}).get("name")
        desc = (s.get("description") or "").strip()
        if not name:
            continue
        if desc.startswith("-"):
            fav = (normalize_team_name(name, "wc"), bool(s.get("long")))
        elif desc.startswith("+"):
            dog = (normalize_team_name(name, "wc"), bool(s.get("long")))
    mag = abs(_num(market.get("line")) or 0.0)
    if not fav or not dog or fav[0] == dog[0] or mag <= 0:
        return None
    best_ask, best_bid, ask_usd, bid_usd = _quote(market, market_data)
    if best_ask is None or best_bid is None:
        return None
    buy = lambda is_long: round(best_ask if is_long else 1.0 - best_bid, 4)   # long buys at ask, short at 1-bid
    fav_team, fav_long = fav
    dog_team, dog_long = dog
    team_a, team_b = sorted([fav_team, dog_team])
    if team_a == fav_team:        # team_a favored: -mag; team_a covers = the favorite side
        line_value, cover, nocover, cover_long = -mag, buy(fav_long), buy(dog_long), fav_long
    else:                          # team_a is the +mag underdog
        line_value, cover, nocover, cover_long = mag, buy(dog_long), buy(fav_long), dog_long
    dt = _start_dt(event, market)
    market_id = market.get("slug") or str(market.get("id", ""))
    if not (0 < cover < 1 and 0 < nocover < 1) or dt is None or not market_id:
        return None
    polymarket_us_sides[market_id] = {"Cover": {"long": cover_long}, "No Cover": {"long": not cover_long}}
    polymarket_us_ticks[market_id] = _market_tick(market)
    if event.get("slug"):
        polymarket_us_urls[market_id] = f"https://polymarket.us/event/{event['slug']}"
    start_date, start_et = et_stamp(dt)
    return SpreadLine(
        sport="WC", team_a=team_a, team_b=team_b, start_date=start_date,
        start_time=int(dt.timestamp()), start_et=start_et, line_value=line_value, period=period,
        platform="polymarket_us", market_id=market_id, platform_url=polymarket_us_urls.get(market_id, ""),
        best_ask_cover=cover, best_ask_nocover=nocover, liquidity=round(min(ask_usd, bid_usd), 2))


def fetch_polymarket_us_wc_lines(bet_types=None) -> tuple[list[TotalLine], list[SpreadLine]]:
    """Every World Cup totals + spreads line (full/1H/2H) from PM US (league 'fwc'). Fetches
    each market's live order book (concurrently) so prices AND depth are real — depth feeds the
    engine's top-of-book sizing cap (cached quotes give no depth -> would size blind)."""
    want = config.active_bet_types(bet_types)
    totals: list[TotalLine] = []
    spreads: list[SpreadLine] = []
    if not (want & {"totals", "spreads"}):
        return totals, spreads
    with httpx.Client(timeout=TIMEOUT) as client:
        jobs = []   # (event, market, kind, period) for every wanted WC market
        for e in fetch_events(WC_LEAGUE, client):
            if e.get("ended") is True:
                continue
            for m in (e.get("markets") or []):
                kp = WC_TYPE_MAP.get(m.get("sportsMarketType"))
                if kp and ((kp[0] == "total" and "totals" in want) or (kp[0] == "spread" and "spreads" in want)):
                    jobs.append((e, m, kp[0], kp[1]))
        books = _fetch_books_parallel([m.get("slug", "") for _, m, _, _ in jobs], client)
        for e, m, kind, period in jobs:
            md = books.get(m.get("slug", ""))
            if kind == "total":
                ln = polymarket_us_wc_total_line(m, e, period, md)
                if ln:
                    totals.append(ln)
            else:
                ln = polymarket_us_wc_spread_line(m, e, period, md)
                if ln:
                    spreads.append(ln)
    return totals, spreads


# --------------------------------- CLI --------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull moneyline odds trading on Polymarket US (QCX).")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds between pulls")
    parser.add_argument("--once", action="store_true", help="single pull, then exit")
    parser.add_argument("--league", action="append", choices=LEAGUES, help="limit leagues (repeatable)")
    args = parser.parse_args(argv)
    leagues = args.league or LEAGUES
    try:
        while True:
            stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
            lines = fetch_polymarket_us_lines(leagues)
            print(f"\n=== {stamp} — {len(lines)} PM US moneyline(s) ===")
            for ln in sorted(lines, key=lambda l: l.start_time):
                print(f"  {ln.sport:<4} {ln.team_a} vs {ln.team_b:<22} {ln.start_et}  "
                      f"ask {ln.best_ask_yes:.3f}/{ln.best_ask_no:.3f}  depth ${ln.liquidity:,.0f}  [{ln.market_id}]")
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
