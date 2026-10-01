"""
Registry coverage for the sports wired in beyond NFL/NBA/MLB: NHL and WNBA.
The risk is city-only names (how Kalshi labels teams) colliding across sports in
the flat TEAM_REGISTRY — "Toronto" is Raptors/Blue Jays/Maple Leafs/Tempo
depending on the league — so these assert the SPORT_OVERRIDES disambiguate.
"""

from core.teams import normalize_team_name, order_teams
from fetch.kalshi import _title_teams


def test_nhl_city_overrides_beat_flat_collisions():
    # Each of these cities maps to a DIFFERENT sport's team in the flat registry.
    assert normalize_team_name("Boston", "NHL") == "Bruins"      # flat: Celtics
    assert normalize_team_name("Toronto", "NHL") == "Maple Leafs"  # flat: Raptors
    assert normalize_team_name("Chicago", "NHL") == "Blackhawks"   # flat: Bears
    assert normalize_team_name("Tampa Bay", "NHL") == "Lightning"  # flat: Buccaneers
    assert normalize_team_name("St. Louis", "NHL") == "Blues"      # flat: Cardinals
    assert normalize_team_name("Seattle", "NHL") == "Kraken"       # flat: Seahawks


def test_nhl_full_names_resolve():
    assert normalize_team_name("Edmonton Oilers", "NHL") == "Oilers"
    assert normalize_team_name("Vegas Golden Knights", "NHL") == "Golden Knights"
    # NY is intentionally not city-overridden (two NHL teams) — names must be explicit.
    assert normalize_team_name("NY Rangers", "NHL") == "Rangers"
    assert normalize_team_name("Islanders", "NHL") == "Islanders"


def test_wnba_city_overrides_kalshi_style():
    assert normalize_team_name("Atlanta", "WNBA") == "Dream"        # flat: Falcons
    assert normalize_team_name("Golden State", "WNBA") == "Valkyries"  # flat: Warriors
    assert normalize_team_name("New York", "WNBA") == "Liberty"     # flat: Knicks
    assert normalize_team_name("Toronto", "WNBA") == "Tempo"        # 2026 expansion
    assert normalize_team_name("Portland", "WNBA") == "Fire"        # 2026 expansion
    assert normalize_team_name("Indiana", "WNBA") == "Fever"        # flat: Pacers


def test_wnba_full_names_resolve():
    assert normalize_team_name("Seattle Storm", "WNBA") == "Storm"
    assert normalize_team_name("Golden State Valkyries", "WNBA") == "Valkyries"
    assert normalize_team_name("Las Vegas Aces", "WNBA") == "Aces"
    assert order_teams("Phoenix Mercury", "Seattle Storm", "WNBA") == ("Mercury", "Storm")


def test_concatenated_camelcase_names_normalize():
    # Polymarket emits some teams without the space (seen live on PortlandFire).
    assert normalize_team_name("PortlandFire", "WNBA") == "Fire"
    assert normalize_team_name("GoldenStateValkyries", "WNBA") == "Valkyries"
    assert normalize_team_name("NewYorkLiberty", "WNBA") == "Liberty"
    assert normalize_team_name("KC", "NFL") == "Chiefs"  # all-caps abbrev unaffected


def test_same_city_disambiguates_across_every_sport():
    # The whole point of SPORT_OVERRIDES: one city, four leagues, four teams.
    assert normalize_team_name("Toronto", "NBA") == "Raptors"
    assert normalize_team_name("Toronto", "MLB") == "Blue Jays"
    assert normalize_team_name("Toronto", "NHL") == "Maple Leafs"
    assert normalize_team_name("Toronto", "WNBA") == "Tempo"


def test_kalshi_title_parser_handles_lowercase_winner():
    # Kalshi WNBA titles use lowercase "winner?"; the parser must still split teams.
    assert _title_teams("Seattle vs Phoenix winner?") == ("Seattle", "Phoenix")
    assert _title_teams("Texas vs Boston Winner?") == ("Texas", "Boston")  # MLB casing still works
