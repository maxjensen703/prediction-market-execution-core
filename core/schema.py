"""
Canonical data model for the Crossbook Arbitrage Engine.
Every record that flows into the matcher is a MoneyLine. Two field groups
only: matching fields (which game is this?) and arb fields (what does it
cost to buy each side?). Anything else belongs in raw logs, not here.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, model_validator

ET = ZoneInfo("America/New_York")  # Eastern local time (EDT/EST as appropriate)


def et_stamp(dt: datetime) -> tuple[str, str]:
    """UTC datetime -> (ET date 'YYYY-MM-DD', readable 'YYYY-MM-DD HH:MM ET')."""
    local = dt.astimezone(ET)
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:%M ET")


class MoneyLine(BaseModel):
    """
    Canonical representation of a single tradable market line.
    Fields are divided into two groups: matching fields and arb fields.
    No other fields belong here. Debugging data goes to raw logs only.
    """

    # ── Matching fields ────────────────────────────────────────────────────
    # Used to identify that two records refer to the same real-world game.

    sport: str
    # Normalized sport label, always uppercase. Covered today: "NFL", "NBA",
    # "MLB", "NHL", "WNBA" (the leagues both venues share).

    team_a: str
    # Canonical team short name, alphabetically first of the two teams.
    # Always resolved through the team name registry before storage.
    # Examples: "Bears", "Celtics", "Arsenal"

    team_b: str
    # Canonical team short name, alphabetically second of the two teams.
    # Always resolved through the team name registry before storage.

    start_date: str
    # EASTERN (ET) date of game start. Format: "YYYY-MM-DD"
    # ET is the date these markets list games under — a 10pm ET game keeps its
    # real date instead of rolling over to the next UTC day. Stored separately
    # from start_time to allow date-level matching across platforms.

    start_time: int
    # Unix timestamp (integer seconds, timezone-less) of scheduled game start.
    # Used with start_date to disambiguate doubleheaders and series games.
    # Matching tolerance: +/- 4 hours (14400 seconds).

    start_et: str
    # Human-readable game start in Eastern time: "2026-06-14 19:20 ET".
    # Display/manual-testing convenience — derived from start_time, never parsed.

    # ── Arb fields ─────────────────────────────────────────────────────────
    # Used to calculate whether a guaranteed profit exists across platforms.

    platform: str
    # Source platform for this line, lowercase: "kalshi" or "polymarket_us".

    market_id: str
    # Platform-native identifier for this specific market.
    # Kalshi: market ticker string (e.g. "NFL-2024-KC-PHI-Y")
    # Polymarket US: market slug id

    best_ask_yes: float
    # The current lowest price to BUY the team_a wins outcome right now.
    # Expressed as implied probability / cost per $1 of payout: 0.0 to 1.0
    # This is the executable price, not the mid. Use ask, never mid.
    # Kalshi: yes_ask_dollars parsed to float
    # Polymarket US: best ask from the gateway order book

    best_ask_no: float
    # The current lowest price to BUY the team_b wins outcome right now.
    # Expressed as implied probability / cost per $1 of payout: 0.0 to 1.0
    # Kalshi: no_ask_dollars parsed to float
    # Polymarket US: gateway short-side ask (1 - best bid), or 1 - best_ask_yes if only one side available

    liquidity: float
    # Depth in USD at the best ask. Both Kalshi and Polymarket US report this.
    # 0.0 = unknown; never None or omitted.

    last_price: float | None = None
    # Last-trade price ALIGNED to best_ask_yes' outcome (0-1 cost to buy that side); None if no
    # recent trade. OBSERVATIONAL phantom signal only (not used in arb math): a quote far from the
    # last actual fill is suspect. best_ask_no's last = 1 - last_price. See docs/Data_Layer_Audit.md.

    status: str = "upcoming"
    # "live" if the game's scheduled start has passed (in progress), else
    # "upcoming". Stamped at fetch time by pipeline.within_window (which knows now).


# ── TotalLine ───────────────────────────────────────────────────────────────

class TotalLine(BaseModel):
    """
    One over/under (total) market on one platform: will the combined score be
    over or under `line_value`? Same matching/price conventions as MoneyLine,
    plus `line_value` (an EXACT-match key — 8.5 never matches 9.0).

    Carries `best_ask_over`/`best_ask_under` (0-1 asks). team_a/team_b are kept
    only to identify the game; neither "wins" this market.
    """

    # ── Matching fields (ET, identical convention to MoneyLine) ────────────
    sport: str
    team_a: str
    team_b: str
    start_date: str          # ET date "YYYY-MM-DD" (display/grouping)
    start_time: int          # unix seconds of scheduled start (±4h match tolerance)
    start_et: str            # readable ET, e.g. "2026-06-16 18:45 ET"
    line_value: float        # the total, e.g. 8.5 — EXACT MATCH ONLY
    period: str = "full"     # "full" | "1h" | "2h" — EXACT-match key (1h total never matches full)

    # ── Identity ───────────────────────────────────────────────────────────
    platform: str
    market_id: str
    platform_url: str = ""

    # ── Executable asks (cost to buy each side, 0-1) ───────────────────────
    best_ask_over: float
    best_ask_under: float

    liquidity: float = 0.0
    last_price: float | None = None   # last-trade cost of the OVER side (0-1); Under's = 1-last_price. Observational.
    status: str = "upcoming"   # "live" (in progress) or "upcoming"; stamped at fetch time

    @model_validator(mode="after")
    def _validate(self) -> "TotalLine":
        if self.line_value <= 0:
            raise ValueError(f"line_value must be positive, got {self.line_value}")
        if not (0.0 < self.best_ask_over < 1.0 and 0.0 < self.best_ask_under < 1.0):
            raise ValueError("asks must be in (0, 1)")
        return self


# ── SpreadLine ──────────────────────────────────────────────────────────────

class SpreadLine(BaseModel):
    """
    One spread / runline market on one platform, expressed from team_a's
    perspective: `line_value` is the handicap team_a must beat. Negative = team_a
    favored (must win by more than |line_value|); positive = team_a getting points.
    Because team_a is alphabetically first, the sign is consistent across platforms
    regardless of which team is favored. EXACT-match key (-1.5 != +1.5 != -2.5).

    "Cover" = team_a beats the spread. Carries `best_ask_cover`/`best_ask_nocover`
    (0-1 asks).
    """

    # ── Matching fields ────────────────────────────────────────────────────
    sport: str
    team_a: str
    team_b: str
    start_date: str
    start_time: int
    start_et: str
    line_value: float        # signed, team_a's perspective — EXACT MATCH ONLY
    period: str = "full"     # "full" | "1h" | "2h" — EXACT-match key (1h spread never matches full)

    # ── Identity ───────────────────────────────────────────────────────────
    platform: str
    market_id: str
    platform_url: str = ""

    # ── Executable asks (cost to buy each side, 0-1) ───────────────────────
    best_ask_cover: float
    best_ask_nocover: float

    liquidity: float = 0.0
    last_price: float | None = None   # last-trade cost of the COVER side (0-1); No-Cover's = 1-last_price. Observational.
    status: str = "upcoming"   # "live" (in progress) or "upcoming"; stamped at fetch time

    @model_validator(mode="after")
    def _validate(self) -> "SpreadLine":
        if not (0.0 < self.best_ask_cover < 1.0 and 0.0 < self.best_ask_nocover < 1.0):
            raise ValueError("asks must be in (0, 1)")
        return self
