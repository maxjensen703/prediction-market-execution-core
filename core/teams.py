"""
core/teams.py — shared foundation (core/), consumed by every market-data converter in
fetch/kalshi.py and fetch/polymarket_us.py, so team names line up when lines from the two
venues are compared by (sport, team_a, team_b). Team name registry data itself
lives in core/teams_data.py (TEAM_REGISTRY, SPORT_OVERRIDES); this file is the lookup logic.

All team names from all platforms are normalized through this registry
before being stored in a MoneyLine. Normalization steps:
    1. Lowercase
    2. Strip leading/trailing whitespace
    3. Strip punctuation (periods, hyphens, apostrophes)
    4. Lookup in SPORT_OVERRIDES[sport] first (city-only names like Kalshi's
       "Boston" mean different teams in different sports), then TEAM_REGISTRY
    5. If not found, log to unmapped_teams.log and return the cleaned input

The canonical name is always a short-form proper noun: "Chiefs" not
"Kansas City Chiefs". This is the form used in team_a and team_b fields.

team_a is always alphabetically first (a < b), team_b alphabetically second.
This ensures "Chiefs vs Eagles" and "Eagles vs Chiefs" produce identical records.
"""

import re
import logging
from pathlib import Path

from core.teams_data import TEAM_REGISTRY, SPORT_OVERRIDES

# ── Unmapped team logger ───────────────────────────────────────────────────
# Any team name that passes through normalize_team_name() and is NOT found
# in the registry gets written here for manual review and registry expansion.

_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "unmapped_teams.log"
unmapped_logger = logging.getLogger("unmapped_teams")
if not unmapped_logger.handlers:  # avoid duplicate handlers on re-import
    _LOG_PATH.parent.mkdir(exist_ok=True)
    unmapped_handler = logging.FileHandler(_LOG_PATH)
    unmapped_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    unmapped_logger.addHandler(unmapped_handler)
unmapped_logger.setLevel(logging.WARNING)


def _clean(name: str) -> str:
    """Lowercase, strip whitespace, remove punctuation for registry lookup."""
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)  # split CamelCase: PortlandFire -> Portland Fire
    name = name.lower().strip()
    name = re.sub(r"[.\-'’]", "", name)    # remove periods, hyphens, apostrophes
    name = re.sub(r"\s+", " ", name)       # collapse multiple spaces
    return name


def normalize_team_name(raw_name: str, sport: str | None = None) -> str:
    """
    Resolve any raw team name string to its canonical short form.

    Checks SPORT_OVERRIDES[sport] first (when sport is given), then the flat
    TEAM_REGISTRY. If not found anywhere, logs the raw name to
    unmapped_teams.log and returns the stripped input so the pipeline
    does not crash.

    Always call this before storing team_a or team_b on a MoneyLine.
    """
    cleaned = _clean(raw_name)
    canonical = None
    if sport:
        canonical = SPORT_OVERRIDES.get(sport.upper(), {}).get(cleaned)
    if canonical is None:
        canonical = TEAM_REGISTRY.get(cleaned)

    if canonical is None:
        unmapped_logger.warning(f"UNMAPPED TEAM: '{raw_name}' (cleaned: '{cleaned}', sport: {sport})")
        return raw_name.strip()  # Return cleaned original — do not crash

    return canonical


def order_teams(name_1: str, name_2: str, sport: str | None = None) -> tuple[str, str]:
    """
    Given two raw team names, return them as (team_a, team_b)
    where team_a is alphabetically first.

    This ensures consistent ordering regardless of which team is listed
    first in any platform's API response.

    Example:
        order_teams("Eagles", "Chiefs") -> ("Chiefs", "Eagles")
        order_teams("Chiefs", "Eagles") -> ("Chiefs", "Eagles")
    """
    normalized_1 = normalize_team_name(name_1, sport)
    normalized_2 = normalize_team_name(name_2, sport)
    return tuple(sorted([normalized_1, normalized_2]))
