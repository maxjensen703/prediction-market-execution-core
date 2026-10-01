"""The Kalshi moneyline converter and the venue's '<Team> wins' titles.

Venue fact (real keyless KXMLBGAME page, 2026-09-16, committed as
tests/fixtures/kalshi_markets_list/KXMLBGAME_real_2026-09-16.json): each team market is titled
'<Team> wins', yes_sub_title = no_sub_title = '<Team>', and only rules_primary names both teams.
fetch.kalshi._title_teams falls back to rules_primary for that shape and fails closed on anything
else. No network: every page here is a file."""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path

import pytest

import fetch.kalshi as kalshi
from core.teams import normalize_team_name

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kalshi_markets_list"
REAL = FIXTURES / "KXMLBGAME_real_2026-09-16.json"
CONSTRUCTED = FIXTURES / "KXMLBGAME_constructed.json"


@pytest.fixture(autouse=True)
def _clean_registries():
    sides, ticks = dict(kalshi.kalshi_sides), dict(kalshi.kalshi_ticks)
    ties = dict(kalshi.kalshi_tie_payout_cents)
    kalshi.kalshi_sides.clear()
    kalshi.kalshi_ticks.clear()
    kalshi.kalshi_tie_payout_cents.clear()
    yield
    kalshi.kalshi_sides.clear()
    kalshi.kalshi_sides.update(sides)
    kalshi.kalshi_ticks.clear()
    kalshi.kalshi_ticks.update(ticks)
    kalshi.kalshi_tie_payout_cents.clear()
    kalshi.kalshi_tie_payout_cents.update(ties)


def _page(path: Path) -> list[dict]:
    return json.loads(path.read_text())["markets"]


def _real_market(ticker_suffix: str = "-SF") -> dict:
    return copy.deepcopy(next(m for m in _page(REAL)
                              if m["ticker"] == "KXMLBGAME-26SEP182215SFLAD" + ticker_suffix))


def _new_shape(team: str, rules_pair: str, sport_word: str = "baseball", **over) -> dict:
    m = _real_market()
    m.update(title=f"{team} wins", yes_sub_title=team, no_sub_title=team,
             ticker=f"KXMLBGAME-26SEP182215SFLAD-{team[:3].upper()}",
             rules_primary=(f"If {team} wins the {rules_pair} professional {sport_word} game "
                            f"originally scheduled for Sep 18, 2026 at 10:15 PM EDT, then the "
                            f"market resolves to Yes."))
    m.update(over)
    return m


# ── the real page ────────────────────────────────────────────────────────────

def test_real_page_every_market_converts_and_registers_a_canonical_yes_side():
    markets = _page(REAL)
    assert len(markets) == 78
    lines = [kalshi.kalshi_market_to_line(m, "mlb") for m in markets]
    assert all(ln is not None for ln in lines)
    assert len(kalshi.kalshi_sides) == 78
    for m, ln in zip(markets, lines):
        side = kalshi.kalshi_sides[m["ticker"]]
        assert side == normalize_team_name(m["yes_sub_title"], "mlb") != m["yes_sub_title"]
        assert side.lower() not in ("yes", "no", "tie")
        assert side in (ln.team_a, ln.team_b)
        assert ln.team_a < ln.team_b
        assert ln.market_id == m["ticker"] and ln.platform == "kalshi" and ln.sport == "MLB"


@pytest.mark.parametrize("raw, canonical", [("New York Y", "Yankees"), ("Chicago WS", "White Sox"),
                                            ("A's", "Athletics"), ("San Francisco", "Giants"),
                                            ("Los Angeles D", "Dodgers")])
def test_real_page_spot_checks(raw, canonical):
    markets = [m for m in _page(REAL) if m["yes_sub_title"] == raw]
    assert markets, raw
    for m in markets:
        assert kalshi.kalshi_market_to_line(m, "mlb") is not None
        assert kalshi.kalshi_sides[m["ticker"]] == canonical


def test_real_page_each_events_two_markets_name_each_other():
    by_event: dict[str, list] = defaultdict(list)
    for m in _page(REAL):
        by_event[m["event_ticker"]].append((m, kalshi.kalshi_market_to_line(m, "mlb")))
    assert len(by_event) == 39
    for event, pair in by_event.items():
        assert len(pair) == 2, event
        (m1, l1), (m2, l2) = pair
        s1, s2 = kalshi.kalshi_sides[m1["ticker"]], kalshi.kalshi_sides[m2["ticker"]]
        assert s1 != s2
        assert (l1.team_a, l1.team_b) == (l2.team_a, l2.team_b) == tuple(sorted([s1, s2]))
        assert l1.start_time == l2.start_time
        # each market's YES team is the other market's opponent: the asks mirror
        a1 = l1.best_ask_yes if l1.team_a == s1 else l1.best_ask_no
        assert a1 == float(m1["yes_ask_dollars"])


def test_real_page_fills_the_team_map_through_fetch_kalshi_all(monkeypatch):
    page = _page(REAL)
    monkeypatch.setattr(kalshi, "fetch_markets",
                        lambda series, client: page if series == "KXMLBGAME" else [])
    money, totals, spreads = kalshi.fetch_kalshi_all(["mlb"], bet_types=["moneyline"])
    assert len(money) == 39 and totals == [] and spreads == []
    assert len(kalshi.kalshi_sides) == 78


# ── the old title shapes and the constructed page are unchanged ─────────────

def test_old_title_shapes_parse_identically():
    assert kalshi._title_teams("Texas vs Boston Winner?") == ("Texas", "Boston")
    assert kalshi._title_teams("Game 5: New York at San Antonio Winner?") == ("New York", "San Antonio")
    # old titles never consult rules_primary, even when it disagrees
    assert kalshi._title_teams("Texas vs Boston Winner?", rules_primary="the A vs B pro baseball game",
                               yes_title="Texas", sport="mlb") == ("Texas", "Boston")


def test_old_title_market_converts_as_before():
    m = _new_shape("Texas", "Texas vs Boston", title="Texas vs Boston Winner?", rules_primary="")
    ln = kalshi.kalshi_market_to_line(m, "mlb")
    assert ln is not None and (ln.team_a, ln.team_b) == ("Rangers", "Red Sox")
    assert kalshi.kalshi_sides[m["ticker"]] == "Rangers"


def test_constructed_page_still_converts():
    markets = _page(CONSTRUCTED)
    assert all(kalshi.kalshi_market_to_line(m, "mlb") is not None for m in markets)
    assert {kalshi.kalshi_sides[m["ticker"]] for m in markets} == {
        "Yankees", "Red Sox", "Dodgers", "White Sox"}


def test_a_new_shape_market_with_at_in_rules_parses():
    m = _new_shape("San Francisco", "San Francisco at Los Angeles D")
    ln = kalshi.kalshi_market_to_line(m, "mlb")
    assert ln is not None and (ln.team_a, ln.team_b) == ("Dodgers", "Giants")


# ── fail closed ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case, market", [
    ("no rules_primary", _new_shape("San Francisco", "San Francisco vs Los Angeles D", rules_primary="")),
    ("rules with no game phrase", _new_shape("San Francisco", "x",
                                             rules_primary="If San Francisco wins, resolves Yes.")),
    ("rules with one team", _new_shape("San Francisco", "San Francisco")),
    ("pair lacks the YES team", _new_shape("San Francisco", "Boston vs Los Angeles D")),
    ("unresolvable opponent", _new_shape("San Francisco", "San Francisco vs Springfield Isotopes")),
    ("unresolvable YES team", _new_shape("Springfield Isotopes", "Springfield Isotopes vs Boston")),
    ("both names one team", _new_shape("San Francisco", "San Francisco vs SF Giants")),
    ("literal yes", _new_shape("Yes", "Yes vs Boston")),
    ("literal no", _new_shape("No", "Boston vs No")),
    ("literal tie", _new_shape("Tie", "Boston vs Tie")),
    ("title is not '<Team> wins'", _new_shape("San Francisco", "San Francisco vs Los Angeles D",
                                             title="San Francisco wins by over 1.5 runs")),
    ("empty yes_sub_title", _new_shape("San Francisco", "San Francisco vs Los Angeles D",
                                       yes_sub_title="")),
])
def test_fail_closed_cases_return_none_and_register_nothing(case, market):
    if case == "both names one team":
        assert normalize_team_name("SF Giants", "mlb") == normalize_team_name("San Francisco", "mlb")
    assert kalshi.kalshi_market_to_line(market, "mlb") is None, case
    assert kalshi.kalshi_sides == {}


def test_unmapped_name_is_treated_as_unresolved_even_though_teams_returns_it():
    # the fail-open trap: core/teams.py hands back the raw input for an unmapped name
    assert normalize_team_name("Springfield Isotopes", "mlb") == "Springfield Isotopes"
    assert kalshi._title_teams("San Francisco wins", yes_title="San Francisco", sport="mlb",
                               rules_primary="If San Francisco wins the San Francisco vs Springfield "
                                             "Isotopes professional baseball game") is None


@pytest.mark.parametrize("sport", [None, "", "wc", "epl"])
def test_the_fallback_is_two_way_leagues_only(sport):
    m = _real_market()
    assert kalshi._title_teams(m["title"], rules_primary=m["rules_primary"],
                               yes_title=m["yes_sub_title"], sport=sport) is None


def test_rules_naming_the_opposite_winner_fail_closed():
    # adversary FIX-FIRST 1: title and yes_sub_title say San Francisco, the rules pay on the Dodgers
    m = _new_shape("San Francisco", "San Francisco vs Los Angeles D")
    m["rules_primary"] = m["rules_primary"].replace("If San Francisco wins", "If Los Angeles D wins")
    assert kalshi.kalshi_market_to_line(m, "mlb") is None
    assert kalshi.kalshi_sides == {}


def test_every_real_market_names_its_own_yes_team_first_in_the_rules():
    for m in _page(REAL):
        assert m["rules_primary"].startswith(f"If {m['yes_sub_title']} wins the "), m["ticker"]


NFL_PAIR = "Kansas City vs Philadelphia"


def _nfl_event(tie_in_primary: bool) -> list[dict]:
    """A three-market NFL-style event with RESOLVABLE teams, so the refusal under test is the tie
    wording, not an unmapped name."""
    assert normalize_team_name("Kansas City", "nfl") != "Kansas City"
    assert normalize_team_name("Philadelphia", "nfl") != "Philadelphia"
    tie_line = "If the game ends in a tie, the market resolves to No."
    event = []
    for team, code in (("Kansas City", "KC"), ("Philadelphia", "PHI")):
        # NFL city form cross-checks the ticker suffix, so give each market
        # its real code; the refusal under test stays the tie wording
        m = _new_shape(team, NFL_PAIR, "football", ticker=f"KXNFLGAME-26SEP182215KCPHI-{code}")
        if tie_in_primary:
            m["rules_primary"] += " " + tie_line
        else:
            m["rules_secondary"] = f"The following market refers to the {NFL_PAIR} game. {tie_line}"
        event.append(m)
    event.append(_new_shape("Tie", NFL_PAIR, "football",
                            rules_primary=f"If the {NFL_PAIR} professional football game ends in "
                                          f"a tie, then the market resolves to Yes."))
    return event


def test_resolvable_teams_convert_without_tie_wording():
    # control: the same NFL teams convert when no rules text admits a tie, so the refusals
    # below are the tie refusal and nothing else
    m = _new_shape("Kansas City", NFL_PAIR, "football", ticker="KXNFLGAME-26SEP182215KCPHI-KC")
    assert kalshi.kalshi_market_to_line(m, "nfl") is not None


@pytest.mark.parametrize("tie_in_primary", [True, False], ids=["rules_primary", "rules_secondary"])
def test_a_three_market_event_whose_rules_admit_a_tie_fails_closed(tie_in_primary):
    # NOTE: conversion is per market, so it cannot see a sibling Tie market. If a two-way league
    # ever lists a Tie market while the team markets' rules say nothing about a tie, those team
    # markets would still convert. That gap predates this order (old titles have it too); this
    # test covers only rules text that admits a tie, plus the Tie market itself.
    for m in _nfl_event(tie_in_primary):
        assert kalshi.kalshi_market_to_line(m, "nfl") is None, m["title"]
    assert kalshi.kalshi_sides == {}


def test_world_cup_never_takes_the_fallback_and_never_pairs_a_team_with_tie():
    pair = "Brazil vs Argentina"
    event = [_new_shape("Brazil", pair, "soccer"), _new_shape("Argentina", pair, "soccer"),
             _new_shape("Tie", pair, "soccer")]
    for m in event:   # refused by the two-way-league gate, before any name lookup
        assert kalshi._title_teams(m["title"], rules_primary=m["rules_primary"],
                                   yes_title=m["yes_sub_title"], sport="wc") is None
        assert kalshi.kalshi_market_to_line(m, "wc") is None
    # the World Cup team map shares _title_teams and gets no fallback: no '(Team, Tie)' pair
    assert kalshi._wc_teams_by_key(event) == {}
    assert kalshi.kalshi_sides == {}


def test_a_market_whose_rules_allow_a_tie_or_draw_fails_closed():
    base = "San Francisco vs Los Angeles D"
    for extra in (" If the game ends in a tie, the market resolves to No.",
                  " A draw resolves to No.", " Decided in regulation only."):
        m = _new_shape("San Francisco", base)
        m["rules_primary"] += extra
        assert kalshi.kalshi_market_to_line(m, "mlb") is None, extra
        m = _new_shape("San Francisco", base)
        m["rules_secondary"] += extra
        assert kalshi.kalshi_market_to_line(m, "mlb") is None, extra
    assert kalshi.kalshi_sides == {}


# ── The real KXNFLGAME page, two grammars, the 50-cent tie sentence ──────
# Venue fact (real keyless KXNFLGAME page, 2026-09-16, committed as
# tests/fixtures/kalshi_markets_list/KXNFLGAME_real_2026-09-16.json): 64 markets, 32 events.
# City form: 'If Philadelphia wins the Philadelphia vs Chicago Pro Football game ...' (014's path).
# Coded form: 'If Detroit wins the DET Lions vs BUF Bills Pro Football game ...', yes_sub_title
# 'Detroit', ticker suffix DET. Every rules_secondary carries the exact tie sentence.

NFL_REAL = FIXTURES / "KXNFLGAME_real_2026-09-16.json"
DETBUF = "KXNFLGAME-26SEP17DETBUF"
TIE = kalshi.TIE_HALF_PAYOUT_SENTENCE

# the 32 ticker suffixes on the page and the canonical YES team each registers
NFL_SUFFIX_CANONICAL = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers",
    "CHI": "Bears", "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos",
    "DET": "Lions", "GB": "Packers", "HOU": "Texans", "IND": "Colts", "JAC": "Jaguars",
    "KC": "Chiefs", "LAC": "Chargers", "LAR": "Rams", "LV": "Raiders", "MIA": "Dolphins",
    "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants", "NYJ": "Jets",
    "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks", "SF": "49ers", "TB": "Buccaneers",
    "TEN": "Titans", "WAS": "Commanders",
}


def _nfl_market(ticker: str) -> dict:
    return copy.deepcopy(next(m for m in _page(NFL_REAL) if m["ticker"] == ticker))


def _coded(yes: str, pair: str, suffix: str, **over) -> dict:
    """The real coded DET market, re-pointed at another YES team / pair / ticker suffix."""
    m = _nfl_market(f"{DETBUF}-DET")
    m.update(title=f"{yes} wins", yes_sub_title=yes, no_sub_title=yes, ticker=f"{DETBUF}-{suffix}",
             rules_primary=(f"If {yes} wins the {pair} Pro Football game originally scheduled for "
                            f"Sep 17, 2026, then the market resolves to Yes."))
    m.update(over)
    return m


def _rules_pair(m: dict) -> list[str]:
    import re
    found = re.search(r" wins the (.+?) Pro Football game ", m["rules_primary"]).group(1)
    return found.split(" vs ")


def test_nfl_real_page_every_market_converts_across_both_grammars():
    markets = _page(NFL_REAL)
    assert len(markets) == 64
    coded = [m for m in markets if m["yes_sub_title"] not in _rules_pair(m)]
    assert len(coded) == 32   # 16 coded events, 16 city-form events
    assert all(TIE in m["rules_secondary"] for m in markets)
    lines = [kalshi.kalshi_market_to_line(m, "nfl") for m in markets]
    assert all(ln is not None for ln in lines)
    assert len(kalshi.kalshi_sides) == 64
    for m, ln in zip(markets, lines):
        side = kalshi.kalshi_sides[m["ticker"]]
        assert side == NFL_SUFFIX_CANONICAL[m["ticker"].rsplit("-", 1)[1]]
        assert side == normalize_team_name(m["yes_sub_title"], "nfl") != m["yes_sub_title"]
        assert side in (ln.team_a, ln.team_b) and ln.team_a < ln.team_b
        assert ln.market_id == m["ticker"] and ln.platform == "kalshi" and ln.sport == "NFL"
        assert kalshi.kalshi_tie_payout_cents[m["ticker"]] == 50


@pytest.mark.parametrize("ticker, canonical, opponent", [
    ("KXNFLGAME-26SEP17DETBUF-DET", "Lions", "Bills"), ("KXNFLGAME-26SEP17DETBUF-BUF", "Bills", "Lions"),
    ("KXNFLGAME-26SEP21NYGLAR-NYG", "Giants", "Rams"), ("KXNFLGAME-26SEP21NYGLAR-LAR", "Rams", "Giants"),
    ("KXNFLGAME-26SEP20GBNYJ-NYJ", "Jets", "Packers"), ("KXNFLGAME-26SEP20LVLAC-LAC", "Chargers", "Raiders"),
    ("KXNFLGAME-26SEP27TENNYG-NYG", "Giants", "Titans"), ("KXNFLGAME-26SEP27LARDEN-LAR", "Rams", "Broncos"),
    ("KXNFLGAME-26SEP27NYJDET-NYJ", "Jets", "Lions"), ("KXNFLGAME-26SEP27LACBUF-LAC", "Chargers", "Bills"),
])
def test_nfl_real_page_spot_checks(ticker, canonical, opponent):
    ln = kalshi.kalshi_market_to_line(_nfl_market(ticker), "nfl")
    assert ln is not None
    assert kalshi.kalshi_sides[ticker] == canonical
    assert (ln.team_a, ln.team_b) == tuple(sorted([canonical, opponent]))


def test_nfl_real_page_each_events_two_markets_name_each_other():
    by_event: dict[str, list] = defaultdict(list)
    for m in _page(NFL_REAL):
        by_event[m["event_ticker"]].append((m, kalshi.kalshi_market_to_line(m, "nfl")))
    assert len(by_event) == 32
    for event, pair in by_event.items():
        assert len(pair) == 2, event
        (m1, l1), (m2, l2) = pair
        s1, s2 = kalshi.kalshi_sides[m1["ticker"]], kalshi.kalshi_sides[m2["ticker"]]
        assert s1 != s2
        assert (l1.team_a, l1.team_b) == (l2.team_a, l2.team_b) == tuple(sorted([s1, s2]))
        a1 = l1.best_ask_yes if l1.team_a == s1 else l1.best_ask_no
        assert a1 == float(m1["yes_ask_dollars"])


def test_nfl_real_page_fills_the_team_map_through_fetch_kalshi_all(monkeypatch):
    page = _page(NFL_REAL)
    monkeypatch.setattr(kalshi, "fetch_markets",
                        lambda series, client: page if series == "KXNFLGAME" else [])
    money, totals, spreads = kalshi.fetch_kalshi_all(["nfl"], bet_types=["moneyline"])
    assert len(money) == 32 and totals == [] and spreads == []
    assert len(kalshi.kalshi_sides) == 64


def test_mlb_page_records_no_tie_payout():
    assert all(kalshi.kalshi_market_to_line(m, "mlb") is not None for m in _page(REAL))
    assert len(kalshi.kalshi_sides) == 78 and kalshi.kalshi_tie_payout_cents == {}


def test_a_reconverted_market_without_the_sentence_drops_its_tie_payout():
    m = _nfl_market("KXNFLGAME-26SEP17DETBUF-DET")
    assert kalshi.kalshi_market_to_line(m, "nfl") is not None
    assert kalshi.kalshi_tie_payout_cents == {m["ticker"]: 50}
    m["rules_secondary"] = m["rules_secondary"].replace(TIE, "")
    assert kalshi.kalshi_market_to_line(m, "nfl") is not None
    assert kalshi.kalshi_tie_payout_cents == {}


@pytest.mark.parametrize("breakage", ["tie wording", "dead book", "no title"])
def test_a_reconverted_market_that_now_refuses_drops_its_tie_payout(breakage):
    # adversary FIX-FIRST 3: every refusal path, early or late, clears the tie payout
    m = _nfl_market("KXNFLGAME-26SEP28PHICHI-PHI")
    assert kalshi.kalshi_market_to_line(m, "nfl") is not None
    assert kalshi.kalshi_tie_payout_cents == {m["ticker"]: 50}
    if breakage == "tie wording":
        m["rules_secondary"] = m["rules_secondary"].replace(TIE, TIE + " A draw resolves to No.")
    elif breakage == "dead book":
        m["yes_ask_dollars"] = "0"
    else:
        m["title"] = ""
    assert kalshi.kalshi_market_to_line(m, "nfl") is None
    assert kalshi.kalshi_tie_payout_cents == {}


def test_nfl_teams_are_exactly_the_32_codes_on_the_page():
    assert kalshi.NFL_TEAMS == frozenset(NFL_SUFFIX_CANONICAL.values())
    assert len(kalshi.NFL_TEAMS) == 32


def test_every_city_form_market_on_the_page_has_its_suffix_resolve_to_its_yes_team():
    city = [m for m in _page(NFL_REAL) if m["yes_sub_title"] in _rules_pair(m)]
    assert len(city) == 32
    for m in city:
        suffix = m["ticker"].rsplit("-", 1)[1]
        assert kalshi._registry_canonical(suffix, "nfl") == NFL_SUFFIX_CANONICAL[suffix]
        assert kalshi.kalshi_market_to_line(m, "nfl") is not None
        assert kalshi.kalshi_sides[m["ticker"]] == NFL_SUFFIX_CANONICAL[suffix]


@pytest.mark.parametrize("ticker", ["KXNFLGAME-26SEP28PHICHI-CHI", "KXNFLGAME-26SEP28PHICHI-DAL",
                                    "KXNFLGAME-26SEP28PHICHI-XYZ", "KXNFLGAME-26SEP28PHICHI-",
                                    "KXNFLGAME-PHI", "KXNFLGAME-26SEP28PHICHI-NYK"],
                         ids=["opponent", "third-team", "unresolvable", "empty", "no-suffix",
                              "cross-sport-code"])
def test_a_city_form_nfl_market_whose_suffix_is_not_its_yes_team_refuses(ticker):
    # adversary FIX-FIRST 1: the real PHI market re-tickered; before the fix '-CHI' registered Eagles
    m = _nfl_market("KXNFLGAME-26SEP28PHICHI-PHI")
    m["ticker"] = ticker
    _refused(m)


@pytest.mark.parametrize("yes, pair, suffix", [
    ("New York", "New York vs Chicago", "NYG"),
    ("New York", "New York vs Chicago", "NYJ"),
    ("New York", "New York vs Chicago", "NY"),
    ("New York", "New York vs Chicago", "NYK"),
    ("Chicago", "New York vs Chicago", "CHI"),
    ("Los Angeles", "Los Angeles vs Denver", "LAR"),
])
def test_a_bare_shared_city_on_an_nfl_market_refuses(yes, pair, suffix):
    # adversary FIX-FIRST 2: 'new york' is Knicks in the flat registry; an NFL market never
    # registers a cross-sport canonical, even when the other names check out
    assert normalize_team_name("New York", "nfl") not in kalshi.NFL_TEAMS
    _refused(_coded(yes, pair, suffix))


def test_a_cross_sport_canonical_refuses_even_with_an_agreeing_suffix(monkeypatch):
    # isolate the NFL-team guard: a registry where the suffix agrees with a non-NFL canonical
    monkeypatch.setitem(kalshi.TEAM_REGISTRY, "zzq", "Knicks")
    _refused(_coded("New York", "New York vs Chicago", "ZZQ"))


def test_mlb_city_form_is_not_suffix_checked():
    # other leagues stay on 014 unchanged: the MLB fixture helper's '-SAN' suffix resolves to nothing
    m = _new_shape("San Francisco", "San Francisco vs Los Angeles D")
    assert m["ticker"].endswith("-SAN")
    assert kalshi.kalshi_market_to_line(m, "mlb") is not None


def test_coded_control_converts():
    # control for the refusals below: the helper's own output converts when nothing is wrong
    m = _coded("New York G", "NY Giants vs NY Jets", "NYG")
    ln = kalshi.kalshi_market_to_line(m, "nfl")
    assert ln is not None and (ln.team_a, ln.team_b) == ("Giants", "Jets")
    assert kalshi.kalshi_sides[m["ticker"]] == "Giants"


def _refused(m: dict, sport: str = "nfl"):
    assert kalshi.kalshi_market_to_line(m, sport) is None, m["ticker"]
    assert kalshi.kalshi_sides == {} and kalshi.kalshi_tie_payout_cents == {}


@pytest.mark.parametrize("wording", [
    "If the game ends in a tie, the market will resolve to $0.50 for each side.",
    "If the game ends in a tie, the market will resolve to No.",
    "If the game ends in a tie, the market will resolve to $0.50 for each team",   # no period
    "if the game ends in a tie, the market will resolve to $0.50 for each team.",  # case
])
@pytest.mark.parametrize("ticker", ["KXNFLGAME-26SEP17DETBUF-DET", "KXNFLGAME-26SEP28PHICHI-PHI"],
                         ids=["coded", "city"])
def test_a_different_tie_wording_still_refuses(wording, ticker):
    m = _nfl_market(ticker)
    m["rules_secondary"] = m["rules_secondary"].replace(TIE, wording)
    _refused(m)


@pytest.mark.parametrize("extra", [" A draw resolves to No.", " Decided in regulation only.",
                                   " If the game is tied after overtime, No."])
def test_the_whitelisted_sentence_does_not_cover_other_tie_wording_beside_it(extra):
    m = _nfl_market("KXNFLGAME-26SEP17DETBUF-DET")
    m["rules_secondary"] = m["rules_secondary"].replace(TIE, TIE + extra)
    _refused(m)


@pytest.mark.parametrize("suffix", ["KC", "XYZ", "DETX", "BUF"],
                         ids=["another-team", "unresolvable", "not-a-code", "the-opponent"])
def test_a_coded_suffix_that_is_not_the_yes_team_refuses(suffix):
    _refused(_coded("Detroit", "DET Lions vs BUF Bills", suffix))


def test_a_coded_market_with_no_ticker_suffix_refuses():
    _refused(_coded("Detroit", "DET Lions vs BUF Bills", "DET", ticker="KXNFLGAME-DET"))


@pytest.mark.parametrize("yes, pair, suffix", [
    ("New York G", "NY Giants vs NY Jets", "NY"),
    ("New York J", "NY Giants vs NY Jets", "NY"),
    ("Los Angeles R", "LA Rams vs LA Chargers", "LA"),
    ("Los Angeles C", "LA Rams vs LA Chargers", "LA"),
])
def test_same_city_teams_with_an_ambiguous_suffix_refuse(yes, pair, suffix):
    _refused(_coded(yes, pair, suffix))


@pytest.mark.parametrize("pair", ["DET Lions vs BUF Buffaloes", "DET Lionz vs BUF Bills",
                                  "DET Lions vs Buffaloes", "Lionz vs BUF Bills",
                                  "DET Detroit vs BUF Bills", "DET vs BUF Bills",
                                  "DET Detroit Lions vs BUF Bills"])
def test_a_nickname_that_does_not_resolve_refuses(pair):
    _refused(_coded("Detroit", pair, "DET"))


@pytest.mark.parametrize("yes, pair, suffix", [
    ("Los Angeles C", "NY Giants vs LA Rams", "LAC"),
    ("Los Angeles C", "NY Giants vs LA Rams", "LAR"),
    ("Buffalo", "DET Lions vs KC Chiefs", "BUF"),
    ("Springfield", "DET Lions vs BUF Bills", "DET"),
])
def test_a_yes_city_that_is_not_a_coded_team_refuses(yes, pair, suffix):
    _refused(_coded(yes, pair, suffix))


def test_a_code_token_naming_another_team_refuses():
    _refused(_coded("Detroit", "BUF Lions vs DET Bills", "DET"))


@pytest.mark.parametrize("pair", ["DET Lions vs DET Lions", "DET Lions vs Lions"])
def test_two_coded_names_for_one_team_refuse(pair):
    m = _coded("Detroit", pair, "DET")
    # refused inside the fallback itself, not only by the converter's later same-team check
    assert kalshi._title_teams(m["title"], rules_primary=m["rules_primary"],
                               yes_title=m["yes_sub_title"], sport="nfl",
                               rules_secondary=m["rules_secondary"], ticker=m["ticker"]) is None
    _refused(m)


def test_the_coded_grammar_is_nfl_only():
    m = _coded("Detroit", "DET Lions vs BUF Bills", "DET")
    assert kalshi.kalshi_market_to_line(m, "nfl") is not None
    for sport in ("mlb", "nba", "nhl", "wnba"):
        assert kalshi._title_teams(m["title"], rules_primary=m["rules_primary"],
                                   yes_title=m["yes_sub_title"], sport=sport,
                                   rules_secondary=m["rules_secondary"], ticker=m["ticker"]) is None


def test_mlb_pair_with_code_style_names_still_refuses():
    # 014's MLB behaviour is unchanged: a pair that does not name the YES team verbatim refuses
    _refused(_new_shape("San Francisco", "SF Giants vs LA Dodgers"), "mlb")
