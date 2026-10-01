"""
Tests for fetchers/pm/kalshi.py. No network: HTTP is faked with httpx.MockTransport
and the CLI tests monkeypatch pull_board. Live proof the real API works:
    python -m fetch.kalshi --once
"""

import json

import httpx
import pytest

from fetch import kalshi as main

# Trimmed copies of real Kalshi markets — winner, spread, total share a game key.
WINNER_TEX = {
    "ticker": "KXMLBGAME-26JUN141920TEXBOS-TEX", "event_ticker": "KXMLBGAME-26JUN141920TEXBOS",
    "title": "Texas vs Boston Winner?", "yes_sub_title": "Texas",
    "occurrence_datetime": "2026-06-15T02:20:00Z",
    "yes_bid_dollars": "0.4700", "yes_ask_dollars": "0.5100",
    "no_bid_dollars": "0.4900", "no_ask_dollars": "0.5300",
    "last_price_dollars": "0.5400", "volume_24h_fp": "28.11", "open_interest_fp": "28.11",
}
WINNER_BOS = dict(WINNER_TEX, ticker="KXMLBGAME-26JUN141920TEXBOS-BOS", yes_sub_title="Boston",
                  yes_bid_dollars="0.4900", yes_ask_dollars="0.5300")
SPREAD_TEX = {
    "ticker": "KXMLBSPREAD-26JUN141920TEXBOS-TEX2", "event_ticker": "KXMLBSPREAD-26JUN141920TEXBOS",
    "title": "Texas wins by over 1.5 runs?", "yes_sub_title": "Texas wins by over 1.5 runs",
    "floor_strike": 1.5, "occurrence_datetime": "2026-06-15T02:20:00Z",
    "yes_bid_dollars": "0.3000", "yes_ask_dollars": "0.3400",
}
TOTAL_9 = {
    "ticker": "KXMLBTOTAL-26JUN141920TEXBOS-9", "event_ticker": "KXMLBTOTAL-26JUN141920TEXBOS",
    "title": "Texas vs Boston Total Runs?", "yes_sub_title": "Over 8.5 runs scored",
    "floor_strike": 8.5, "occurrence_datetime": "2026-06-15T02:20:00Z",
    "yes_bid_dollars": "0.5000", "yes_ask_dollars": "0.5200",
}
TOTAL_12 = dict(TOTAL_9, ticker="KXMLBTOTAL-26JUN141920TEXBOS-12",
                yes_sub_title="Over 11.5 runs scored", floor_strike=11.5,
                yes_bid_dollars="0.1000", yes_ask_dollars="0.1400")


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# fetching ---------------------------------------------------------------

def test_fetch_follows_cursor_and_sends_documented_params():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"markets": [{"ticker": "A"}], "cursor": "tok"})
        return httpx.Response(200, json={"markets": [{"ticker": "B"}], "cursor": ""})

    markets = main.fetch_markets("KXMLBGAME", mock_client(handler))
    assert [m["ticker"] for m in markets] == ["A", "B"]
    assert seen[0] == {"series_ticker": "KXMLBGAME", "status": "open", "limit": "1000"}
    assert seen[1]["cursor"] == "tok"


def test_fetch_retries_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    assert main.fetch_markets("KXMLBGAME", mock_client(handler)) == []
    assert len(calls) == 2


def test_fetch_error_surfaces_server_message():
    client = mock_client(lambda r: httpx.Response(404, json={"error": "unknown series"}))
    with pytest.raises(RuntimeError, match="404.*unknown series"):
        main.fetch_markets("KXBAD", client)


def test_fetch_runaway_pagination_brake():
    client = mock_client(lambda r: httpx.Response(200, json={"markets": [], "cursor": "again"}))
    with pytest.raises(RuntimeError, match="pagination"):
        main.fetch_markets("KXMLBGAME", client)


# normalization ----------------------------------------------------------

def _normalized():
    raw = [(WINNER_TEX, "moneyline"), (WINNER_BOS, "moneyline"),
           (SPREAD_TEX, "spreads"), (TOTAL_9, "totals"), (TOTAL_12, "totals")]
    return [main.normalize_market(m, t) for m, t in raw]


def test_series_suffix_maps_to_market_type():
    assert main.market_type("KXMLBGAME", "mlb") == "moneyline"
    assert main.market_type("KXNBASPREAD", "nba") == "spreads"
    assert main.market_type("KXNFLTOTAL", "nfl") == "totals"


def test_game_title_handles_both_observed_formats():
    assert main.game_title("Texas vs Boston Winner?") == "Texas vs Boston"
    assert main.game_title("Will New England win the New England vs Seattle Pro Football game?") \
        == "New England vs Seattle"
    assert main.game_title("Something unrecognized") == "Something unrecognized"


def test_markets_group_into_one_game_across_series():
    games = main.build_games(_normalized(), "mlb")
    assert len(games) == 1
    g = games[0]
    assert g["game_key"] == "26JUN141920TEXBOS"  # shared by all three series
    assert g["title"] == "Texas vs Boston"        # ' Winner?' stripped
    assert g["start_time"] == "2026-06-14T23:20:00Z"  # true start from ticker (Jun 14, 7:20pm ET)
    assert len(g["markets"]) == 5


def test_market_carries_everything_arbitrage_needs():
    m = main.normalize_market(WINNER_TEX, "moneyline")
    assert (m["side"], m["type"], m["line"]) == ("Texas", "moneyline", None)
    assert (m["yes_bid"], m["yes_ask"]) == (0.47, 0.51)  # dollar strings -> floats
    assert (m["no_bid"], m["no_ask"]) == (0.49, 0.53)
    assert m["ticker"] == "KXMLBGAME-26JUN141920TEXBOS-TEX"
    assert main.normalize_market(SPREAD_TEX, "spreads")["line"] == 1.5


def test_orphan_spreads_without_a_winner_market_are_dropped():
    games = main.build_games([main.normalize_market(SPREAD_TEX, "spreads")], "mlb")
    assert games == []


def test_main_market_picks_line_closest_to_even():
    g = main.build_games(_normalized(), "mlb")[0]
    assert main.main_market(g, "totals")["line"] == 8.5  # 0.51 mid beats 0.12 mid
    assert main.main_market(g, "spreads")["line"] == 1.5


# CLI ---------------------------------------------------------------------

@pytest.fixture
def fake_board(monkeypatch):
    calls = []

    def fake_pull(leagues):
        calls.append(leagues)
        return {lg: (main.build_games(_normalized(), lg) if lg == "mlb" else []) for lg in leagues}

    monkeypatch.setattr(main, "pull_board", fake_pull)
    return calls


def test_cli_once_prints_board(fake_board, capsys):
    assert main.main(["--once"]) == 0
    out = capsys.readouterr().out
    assert "Texas vs Boston" in out
    assert fake_board == [main.LEAGUES]  # default leagues (all in SERIES)


def test_cli_json_is_one_parseable_document(fake_board, capsys):
    main.main(["--once", "--json", "--league", "mlb"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["source"] == "kalshi"
    assert doc["games"]["mlb"][0]["markets"][0]["ticker"].startswith("KXMLBGAME")


def test_cli_loop_sleeps_interval_and_ctrl_c_exits_cleanly(fake_board, monkeypatch):
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    assert main.main([]) == 0
    assert sleeps == [60, 60]


def test_cli_rejects_nonpositive_interval(fake_board):
    with pytest.raises(ValueError, match="interval"):
        main.main(["--interval", "0"])


def test_align_last_orientation():
    from fetch.kalshi import _align_last
    m = {"last_price_dollars": "0.70"}   # Kalshi last is always the YES price
    assert _align_last(m, True) == 0.70    # A-side = YES  -> raw
    assert _align_last(m, False) == 0.30   # A-side = NO   -> 1-raw
    assert _align_last({}, True) is None   # no last -> no signal


# ── per-market tick alignment ────────────────────────────────────────────────

def test_market_tick_parses_price_ranges_step():
    m = dict(WINNER_TEX, price_ranges=[{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])
    assert main._market_tick(m) == pytest.approx(0.01)


def test_market_tick_falls_back_when_missing_or_unparseable():
    assert main._market_tick({}) == 0.01                              # no price_ranges at all
    assert main._market_tick({"price_ranges": []}) == 0.01            # empty list
    assert main._market_tick({"price_ranges": [{"step": "oops"}]}) == 0.01  # unparseable step


def test_market_tick_falls_back_when_zero_or_negative():
    # A "0.0000" step (or a corrupt negative) must never pass through — it would zero-divide
    # downstream in execute.broker.venue_tick's floor.
    assert main._market_tick({"price_ranges": [{"step": "0.0000"}]}) == 0.01
    assert main._market_tick({"price_ranges": [{"step": "-0.0100"}]}) == 0.01


def test_kalshi_market_to_line_populates_kalshi_ticks():
    main.kalshi_ticks.clear()
    m = dict(WINNER_TEX, price_ranges=[{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])
    ln = main.kalshi_market_to_line(m, "mlb")
    assert ln is not None
    assert main.kalshi_ticks[m["ticker"]] == pytest.approx(0.01)


def test_kalshi_total_market_to_line_populates_kalshi_ticks():
    # m3: a NON-moneyline converter must also feed kalshi_ticks (execute.broker.venue_tick
    # reads this dict regardless of bet type).
    main.kalshi_ticks.clear()
    teams_by_key = {main.game_key(TOTAL_9["event_ticker"]): ("Texas", "Boston")}
    m = dict(TOTAL_9, no_ask_dollars="0.5000",
             price_ranges=[{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])
    ln = main.kalshi_market_to_total_line(m, "mlb", teams_by_key)
    assert ln is not None
    assert main.kalshi_ticks[m["ticker"]] == pytest.approx(0.01)


# ── dedupe keys by game identity, not ET date ──────────────────────────────────

def test_dedupe_moneyline_keeps_both_doubleheader_games():
    from core.schema import MoneyLine

    def _ml(mid, start_time, liquidity):
        return MoneyLine(sport="MLB", team_a="Rangers", team_b="Red Sox",
                         start_date="2026-06-14", start_time=start_time,
                         start_et="2026-06-14 13:10 ET", platform="kalshi", market_id=mid,
                         best_ask_yes=0.5, best_ask_no=0.5, liquidity=liquidity)

    gap = int(3.5 * 3600)
    # game 1: two mirror markets of ONE event (same decoded start) -> collapse to deepest;
    # game 2: same matchup, same DATE, different start -> must SURVIVE (the old date key
    # collapsed it into game 1's line — the cross-game pairing seed).
    out = main._dedupe_moneyline([
        _ml("K-G1-RANGERS", 1781461800, 50), _ml("K-G1-REDSOX", 1781461800, 200),
        _ml("K-G2-RANGERS", 1781461800 + gap, 90),
    ])
    assert {l.market_id for l in out} == {"K-G1-REDSOX", "K-G2-RANGERS"}
