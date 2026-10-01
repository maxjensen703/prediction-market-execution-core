"""PM US fetcher converter tests — real shapes captured from gateway.polymarket.us.
No network: feeds a recorded market + event + order book into the converter.
Pricing rule: long-team ask = best offer; short-team ask = 1 - best bid."""

import pytest

from core.teams import normalize_team_name
from fetch.polymarket_us import polymarket_us_market_to_line, _book_quote

EVENT = {"slug": "mlb-laa-ath-2026-06-21", "ticker": "mlb-laa-ath-2026-06-21",
         "startTime": "2026-06-21T20:05:00Z", "ended": False, "live": False}

# moneyline market: instrument is "long" = Los Angeles Angels; "short" = Athletics
MARKET = {"id": "108482", "slug": "aec-mlb-laa-ath-2026-06-21", "sportsMarketType": "moneyline", "line": None,
          "marketSides": [{"long": True, "team": {"name": "Los Angeles Angels"}},
                          {"long": False, "team": {"name": "Athletics"}}],
          "bestBidQuote": {"value": "0.4500"}, "bestAskQuote": {"value": "0.4550"}}

BOOK = {"offers": [{"px": {"value": "0.4500"}, "qty": "61072.0000"}],
        "bids":   [{"px": {"value": "0.4450"}, "qty": "4186.0000"}]}


def test_book_quote_reads_best_offer_and_bid():
    best_ask, best_bid, ask_usd, bid_usd = _book_quote(BOOK)
    assert best_ask == 0.45 and best_bid == 0.445
    assert ask_usd > bid_usd > 0   # both sides have depth


def test_moneyline_converter_prices_both_sides():
    line = polymarket_us_market_to_line(MARKET, EVENT, "mlb", BOOK)
    la = normalize_team_name("Los Angeles Angels", "mlb")    # long side
    ath = normalize_team_name("Athletics", "mlb")            # short side
    ta, tb = sorted([la, ath])
    assert (line.team_a, line.team_b) == (ta, tb)
    assert line.platform == "polymarket_us"
    assert line.market_id == "aec-mlb-laa-ath-2026-06-21"    # slug, used by book/order endpoints
    long_ask, short_ask = 0.45, round(1 - 0.445, 4)          # 0.45 / 0.555
    exp_yes = long_ask if ta == la else short_ask
    exp_no = short_ask if tb == ath else long_ask
    assert line.best_ask_yes == exp_yes and line.best_ask_no == exp_no


def test_falls_back_to_cached_quote_when_book_empty():
    line = polymarket_us_market_to_line(MARKET, EVENT, "mlb", {})   # no live book
    assert line is not None                                          # used bestBid/AskQuote
    assert 0 < line.best_ask_yes < 1 and 0 < line.best_ask_no < 1


def test_skips_non_moneyline_and_missing_sides():
    assert polymarket_us_market_to_line({**MARKET, "sportsMarketType": "spreads"}, EVENT, "mlb", BOOK) is None
    assert polymarket_us_market_to_line({**MARKET, "marketSides": []}, EVENT, "mlb", BOOK) is None


def test_skips_when_no_usable_quote():
    bare = {**MARKET, "bestBidQuote": None, "bestAskQuote": None}
    assert polymarket_us_market_to_line(bare, EVENT, "mlb", {}) is None


# ── totals + spreads (real PM US shapes; cached-quote path, no book) ───────────

XEVENT = {"slug": "mlb-nym-phi-2026-06-21", "startTime": "2026-06-21T22:45:00Z", "ended": False,
          "teams": [{"name": "New York Mets"}, {"name": "Philadelphia Phillies"}],
          "title": "New York Mets vs. Philadelphia Phillies"}

TOTAL_MARKET = {"id": "114963", "slug": "tsc-mlb-nym-phi-2026-06-21-7pt5", "sportsMarketType": "totals",
                "line": 7.5, "bestBidQuote": {"value": "0.8800"}, "bestAskQuote": {"value": "0.9000"},
                "marketSides": [{"id": "229564", "long": True, "description": "Over"},
                                {"id": "229565", "long": False, "description": "Under"}]}

SPREAD_MARKET = {"id": "114960", "slug": "asc-mlb-nym-phi-2026-06-21-pos-4pt5", "sportsMarketType": "spreads",
                 "line": 4.5, "bestBidQuote": {"value": "0.4150"}, "bestAskQuote": {"value": "0.4500"},
                 "marketSides": [{"id": "229558", "long": True, "team": {"name": "New York Mets"}, "description": "+4.50"},
                                 {"id": "229559", "long": False, "team": {"name": "Philadelphia Phillies"}, "description": "-4.50"}]}


def test_pm_us_total_converter_over_under():
    from fetch.polymarket_us import polymarket_us_market_to_total_line, polymarket_us_sides
    ln = polymarket_us_market_to_total_line(TOTAL_MARKET, XEVENT, "mlb")   # cached quote, no book
    assert ln.platform == "polymarket_us" and ln.line_value == 7.5
    assert ln.best_ask_over == 0.90                       # Over (long) = best ask
    assert ln.best_ask_under == pytest.approx(0.12)       # Under (short) = 1 - best bid
    assert ln.market_id == "tsc-mlb-nym-phi-2026-06-21-7pt5"
    roles = polymarket_us_sides[ln.market_id]
    assert roles["Over"]["long"] is True and roles["Under"]["long"] is False


def test_total_converter_fails_closed_on_renamed_descriptions():
    # The venue renames the side descriptions ("O 7.5") -> the old code
    # silently GUESSED Over=long; when wrong, the resting order lands on the wrong
    # instrument. Now: unparseable orientation = skip, never guess.
    from fetch.polymarket_us import polymarket_us_market_to_total_line
    drifted = {**TOTAL_MARKET,
               "marketSides": [{"id": "229564", "long": True, "description": "O 7.5"},
                               {"id": "229565", "long": False, "description": "U 7.5"}]}
    assert polymarket_us_market_to_total_line(drifted, XEVENT, "mlb") is None


def test_total_converter_fails_closed_on_degenerate_long_flags():
    # Both sides claiming long (or both short) makes orientation unknowable -> skip.
    from fetch.polymarket_us import polymarket_us_market_to_total_line
    both_long = {**TOTAL_MARKET,
                 "marketSides": [{"id": "229564", "long": True, "description": "Over"},
                                 {"id": "229565", "long": True, "description": "Under"}]}
    assert polymarket_us_market_to_total_line(both_long, XEVENT, "mlb") is None


def test_moneyline_converter_fails_closed_on_degenerate_long_flags():
    # Long flags missing on both sides (bool(None) == False twice) used to pick an
    # arbitrary long team — prices assigned to the wrong side's book. Now: skip.
    flagless = {**MARKET,
                "marketSides": [{"team": {"name": "Los Angeles Angels"}},
                                {"team": {"name": "Athletics"}}]}
    assert polymarket_us_market_to_line(flagless, EVENT, "mlb", BOOK) is None
    both_long = {**MARKET,
                 "marketSides": [{"long": True, "team": {"name": "Los Angeles Angels"}},
                                 {"long": True, "team": {"name": "Athletics"}}]}
    assert polymarket_us_market_to_line(both_long, EVENT, "mlb", BOOK) is None


def test_pm_us_spread_converter_signs_to_team_a():
    from fetch.polymarket_us import polymarket_us_market_to_spread_line, polymarket_us_sides
    from core.teams import normalize_team_name
    ln = polymarket_us_market_to_spread_line(SPREAD_MARKET, XEVENT, "mlb")
    phils = normalize_team_name("Philadelphia Phillies", "mlb")   # the -4.5 favorite
    assert ln.platform == "polymarket_us" and abs(ln.line_value) == 4.5
    roles = polymarket_us_sides[ln.market_id]
    if ln.team_a == phils:        # team_a is the favorite -> -4.5; covers via the short side
        assert ln.line_value == -4.5 and ln.best_ask_cover == pytest.approx(0.585) and roles["Cover"]["long"] is False
    else:                          # team_a is the +4.5 underdog; covers via the long side
        assert ln.line_value == 4.5 and ln.best_ask_cover == 0.45 and roles["Cover"]["long"] is True


def test_align_last_orientation():
    from fetch.polymarket_us import _align_last
    md = {"stats": {"lastTradePx": {"value": "0.70"}}}   # PM US last is the LONG-side price
    assert _align_last(md, True) == 0.70    # A-side long  -> raw
    assert _align_last(md, False) == 0.30   # A-side short -> 1-raw
    assert _align_last({}, True) is None and _align_last(None, True) is None


# ── per-market tick alignment ────────────────────────────────────────────────

def test_market_tick_reads_order_price_min_tick_size():
    from fetch.polymarket_us import _market_tick
    assert _market_tick({"orderPriceMinTickSize": 0.001}) == pytest.approx(0.001)


def test_market_tick_falls_back_when_missing():
    from fetch.polymarket_us import _market_tick
    assert _market_tick({}) == 0.005
    assert _market_tick({"orderPriceMinTickSize": None}) == 0.005


def test_market_tick_falls_back_when_zero_or_negative():
    # A reported 0 (or corrupt negative) must never pass through — it would zero-divide
    # downstream in execute.broker.venue_tick's floor.
    from fetch.polymarket_us import _market_tick
    assert _market_tick({"orderPriceMinTickSize": 0}) == 0.005
    assert _market_tick({"orderPriceMinTickSize": -0.001}) == 0.005


def test_moneyline_converter_populates_polymarket_us_ticks():
    from fetch.polymarket_us import polymarket_us_ticks
    polymarket_us_ticks.clear()
    m = {**MARKET, "orderPriceMinTickSize": 0.001}
    line = polymarket_us_market_to_line(m, EVENT, "mlb", BOOK)
    assert line is not None
    assert polymarket_us_ticks[line.market_id] == pytest.approx(0.001)


def test_spread_converter_populates_polymarket_us_ticks():
    # m3: a NON-moneyline converter must also feed polymarket_us_ticks (execute.broker.venue_tick
    # reads this dict regardless of bet type).
    from fetch.polymarket_us import polymarket_us_market_to_spread_line, polymarket_us_ticks
    polymarket_us_ticks.clear()
    m = {**SPREAD_MARKET, "orderPriceMinTickSize": 0.001}
    ln = polymarket_us_market_to_spread_line(m, XEVENT, "mlb")
    assert ln is not None
    assert polymarket_us_ticks[ln.market_id] == pytest.approx(0.001)
