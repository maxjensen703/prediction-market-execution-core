"""
config.py — configuration and environment. THE single place to tune this code.
Everything you'd realistically change lives here, grouped by concern; edit a value and
restart your process. Secrets are the ONE exception: they stay in the environment or .env
(loaded below) and are never written here.

The execution core reads only the fee, risk, venue, network, stream and credential settings.
The MAKER_*, TAKER_*, scan, ranking and league settings belong to a strategy layer that is not
included in this repository; they are inert here.

Sections:
  1. SCAN          — what gets fetched (leagues, day window, bet types)
  2. RANKING       — how detected arbs are surfaced for display/analytics
  3. RISK / LIVE   — the live-trading gate, risk rails, and the (unused here) strategy knobs
  4. FEES          — per-venue taker + maker fee models used everywhere costs are computed
  5. VENUES        — which venues are paper vs. real-money tradeable
  6. NETWORK       — fetch endpoints + timeouts/retries/concurrency  (rarely changed)
  7. CREDENTIALS   — env-var NAMES for live keys (values live in .env, never here)
"""

import os


# ── env helpers (used by the gates + .env loading below) ─────────────────────
def _env_flag(name: str) -> bool:
    """True only for explicit truthy env values (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env into the environment (real env vars win; inline
    ' # comments' stripped). No dependency — lets the server/brokers pick up creds."""
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.split(" #", 1)[0].strip())
    except FileNotFoundError:
        pass


_load_dotenv()   # load .env before reading any credential/flag below


def _csv_env(name: str, allowed: list[str], default: list[str]) -> list[str]:
    """Parse a comma-separated env override (e.g. CROSSBOOK_LEAGUES=mlb,wnba); keep only
    known values; fall back to `default` if the override is empty/all-invalid."""
    picks = [x.strip().lower() for x in os.environ.get(name, "").split(",")]
    picks = [x for x in picks if x in allowed]
    return picks or list(default)


# ════════════════════════════ 1. SCAN ═══════════════════════════════════════
# Every league the app CAN pull. "wc" = FIFA World Cup soccer (totals + spreads only,
# full/1H/2H; moneyline is 3-way so it's excluded). WC is fetched via a dedicated path
# (Kalshi KXWC* series, PM US league 'fwc') — see fetch_kalshi_wc_lines / *_us_wc_lines.
LEAGUES = ["nfl", "nba", "mlb", "nhl", "wnba", "wc"]

# The leagues you're CURRENTLY pulling — just edit this list (e.g. ["mlb", "wnba"]). The
# dashboard's Leagues menu can narrow it live, and CROSSBOOK_LEAGUES=mlb,wnba overrides it.
# Scoping to the league(s) you're trading cuts Kalshi requests ~5x for ~0.5s scans.
ACTIVE_LEAGUES = _csv_env("CROSSBOOK_LEAGUES", LEAGUES, ["mlb"])

# How far ahead to look. In-progress games are included (flagged "live"); a game past
# LIVE_LOOKBACK_SECONDS is presumed finished and dropped.
WINDOW_DAYS = 2.0
LIVE_LOOKBACK_SECONDS = 6 * 3600

# Every bet type the app CAN pull.
BET_TYPES = ["moneyline", "totals", "spreads"]

# The bet types you're CURRENTLY pulling — just edit this list (e.g. ["moneyline"] for the
# fastest scans; skips the Kalshi totals/spreads requests). CROSSBOOK_BET_TYPES overrides.
ACTIVE_BET_TYPES = _csv_env("CROSSBOOK_BET_TYPES", BET_TYPES, ["moneyline", "totals", "spreads"])


# ═══════════════════════════ 2. RANKING ═════════════════════════════════════
MIN_RETURN_PCT = 1.5       # only surface arbs whose net return is at least this % (== the fire gate
                           # below, so what the dashboard shows is what the engine can actually fire)
TOP_N = 20                 # cap on ranked opportunities returned/written
DEFAULT_CAPITAL = 100.0   # notional $ used for the displayed stake model (linear in capital)


# ════════════════════════ 3. RISK / LIVE ════════════════════════════════════
# The live-trading gate. LIVE_TRADING_ENABLED=1 (or true/yes/on) in the environment, set by
# a human out-of-band, is required before a live broker sends an order: KalshiBroker and
# PolymarketUSBroker place_order()/unwind() return ERROR and send nothing while it is off.
# Reads, cancels and cancel_all() are not gated, so a kill can always flatten resting orders.
# PaperBroker ignores the flag. This is the only gate in this repository; the kill switch in
# execute/risk.py binds only orders your code routes through RiskManager.check().
LIVE_TRADING_ENABLED = _env_flag("LIVE_TRADING_ENABLED")  # default OFF

# Hard risk rails enforced on EVERY maker placement and hedge (paper or live) — see
# execute/risk.py (check / check_resting; kill-switch always binds).
RISK_LIMITS = {
    "max_capital_per_trade":  10.0,   # $ per hedge/fill cycle — TIGHT for live $10 tests (a typo can't deploy more)
    "max_total_exposure":     40.0,   # $ summed across all fills this session (the day's cap).
                                      # 2026-07-10: 300->40 by owner decision — hard ceiling for the first LIVE
                                      # maker night ("maximum of 40 dollars of capital deployed").
                                      # Resting quotes count against this too (one shared risk book).
    "max_open_maker_orders":     6,   # maker: concurrent resting quotes (risk-side mirror of MAKER_MAX_OPEN_ORDERS;
                                      # 3->6 2026-07-11: a 5-market universe + 1 cancel-pending straggler)
    "max_resting_exposure":   15.0,   # maker: $ cap on price*qty summed across open resting quotes
}

# ── SCAN LOOP (strategy layer, not included) — recording never stops, even fully disarmed ──
# Root cause of the 2026-06-27 logging outage: the scan loop only ran while armed, so a
# safe (disarmed) posture silently recorded nothing. Fetch+match+detect+log are read-only/
# keyless/money-safe, so they run on this cadence REGARDLESS of arm state; the maker acts
# only while armed, inside its own refresh. ONE cadence (the taker's armed fast-scan was
# removed); PASSIVE_SCAN_ENABLED keeps its historical name as the loop gate.
PASSIVE_SCAN_ENABLED = True    # scan loop runs whenever the server is up
SCAN_SECONDS = 15              # scan cadence. 2026-07-10: 20->10->15 — at 10s the scan's
                               # gateway traffic (~11 req/s sustained) consumed PM's shared per-IP pool and
                               # starved AUTHED order calls (429 on the probe/kill-confirm path). 15s + 8
                               # book workers leaves live-order headroom while keeping maker discovery <20s.
# ── end scan loop block ──────────────────────────────────────────────────────

# HEDGE CROSS CAP (values are DOLLARS despite the _CENTS name — naming quirk kept for
# continuity). The strategy layer's Kalshi hedge is a marketable IOC at
# WS ask + SLIPPAGE_CAP_CENTS["kalshi"] — this cap bounds how far the hedge may cross a
# moving book before the 1-tick-worse retry / unwind path takes over. WHY 1c: measured
# live hedge slip is 0-1c (2026-07-05 night); a wider cross just donates the margin.
SLIPPAGE_CAP_CENTS = {"kalshi": 0.01, "polymarket_us": 0.01, "default": 0.01}

# ── STALE-QUOTE TAKER (Strategy #1, 2026-07-18) — TAKE the lagging venue, hedge the other ──
# Fires a two-sided cross-venue capture off WS-truth prices when a deep moneyline shows a real
# net-of-fee edge (the inversion of the passive maker). Ships DARK; money path; reviewed before live.
TAKER_ENABLED = _env_flag("TAKER_ENABLED")   # master switch (env-overridable); default OFF/inert
TAKER_CONTRACTS = 1                          # contracts per fire (tiny while validating)
TAKER_MIN_EDGE_CENTS = 0.015                 # min net-of-fee locked edge to fire ($; 1.5c). DOLLARS.
TAKER_MAX_FIRES_PER_MARKET = 3               # per-market session fire cap (adverse-selection bound)
TAKER_MIN_PM_DEPTH_DOLLARS = 50.0            # only fire where PM has real depth (data: totals/spreads empty)
TAKER_BET_TYPES = ["moneyline"]              # data: moneyline is the only clean two-sided-deep market
TAKER_FASTLOOP_SECONDS = 1.5                 # re-price the scan's matched pairs off LIVE WS books this
                                             # often (0 = off; scan-loop only). WS books cost no REST, so
                                             # this reacts in ~1.5s not 15s WITHOUT touching PM's 20 req/s
                                             # budget (data: 67% of edges die within one 15s scan).

# ── LEG-IN MAKER (strategy layer, not included) — rest the PM US leg post-only, take Kalshi on fill ──
MAKER_ENABLED = _env_flag("MAKER_ENABLED")  # master switch (env-overridable like LIVE_TRADING_ENABLED); default OFF/inert
MAKER_CONTRACTS = 1                    # contracts per resting quote (tiny while the maker path validates)
MAKER_MAX_OPEN_ORDERS = 6              # concurrent resting quotes (structural cap; RISK_LIMITS mirrors it).
                                       # 3->6 2026-07-11 (owner decision): headroom of 1 over the
                                       # N=5 universe for a cancel_pending straggler.
# The maker never chases the displayed ask — its rest price comes from the Kalshi side +
# margin math, and the p <= pm_ask - 1 tick cap keeps it below even a stale-low PM ask —
# so a big displayed edge is NOT a phantom-fill risk here (that is a TAKER concern:
# crossing a fake ask). Edges >10% stay excluded because absurd displayed edges correlate
# with MATCHING ERRORS (wrong line/period/outcome pairing), and a mismatched pair is not
# an arb at all; 10% is the sanity ceiling. Widened from 3.0 (owner decision 2026-07-10).
MAKER_BAND = (1.0, 10.0)               # edge % band for maker quote candidates
# Maker persistence gate (arb_triggered mode only; the always_on universe has its own
# hysteresis). Evidence (paper session 2026-07-02): in-band edges reappeared 60-90s apart,
# never surviving 2 consecutive 20s scans under a stricter gate, so the maker placed ZERO
# quotes all night. Resting is cheap — a transient quote costs a placement (post-only,
# margin-locked), not a bad taker cross.
MAKER_PERSISTENCE_SCANS = 1            # scans an arb must persist before the maker may quote it
MAKER_TARGET_MARGIN_CENTS = 0.02       # locked margin per $1 payout at placement (DOLLARS; _CENTS naming quirk kept)
# Dynamic target margin (owner decision 2026-07-10): scale the locked margin with the
# displayed edge, never beyond the cap — m = min(MAKER_MAX_MARGIN_CENTS,
# max(MAKER_TARGET_MARGIN_CENTS, 0.5 * edge)) where edge = 1 - 1/(1 + return_pct/100)
# (cost = 1/(1+ret/100) buys $1 payout; edge per $1 payout = 1 - cost). Targets 2-5c.
MAKER_MAX_MARGIN_CENTS = 0.05          # ceiling on the dynamic target margin (DOLLARS)
MAKER_MIN_MARGIN_CENTS = 0.01          # cancel/reprice when the locked margin decays below this (DOLLARS)
                                       # R1.2: raised 0.005 -> 0.01 — the old floor sat inside Kalshi's
                                       # fee-rounding error (<=0.99c), so a floor-priced quote could lock
                                       # a NEGATIVE live margin while paper showed positive
MAKER_KALSHI_BUFFER_CENTS = 0.01       # Kalshi-ask buffer priced in (PM-fill->Kalshi-take latency + measured 0-1c slip)
MAKER_REPRICE_MIN_SECONDS = 3          # per-market action throttle (first live session: 5->3 cuts in-play quote
                                       # staleness/pick-off; the 2-tick threshold + token bucket now carry
                                       # anti-churn + rate, so the blunt per-market throttle can relax)
MAKER_QUOTE_TTL_SECONDS = 1800         # stale-quote cancel (edge episodes: median ~24 min)
# In-play quoting (owner decision 2026-07-10, first live night): the evening's data showed
# 100% of in-band arb supply arrives IN-PLAY (19/19 in one 15-min window) — pre-game books
# are near-empty. Risk: informed in-play flow picks off stale bids before the ~15s reprice
# loop reacts. Mitigations while sizes are tiny (1 contract): live quotes price the hedge
# MAKER_LIVE_KALSHI_BUFFER_CENTS deep (absorbs normal drift; demands bigger displayed edge)
# and rest only MAKER_LIVE_QUOTE_TTL_SECONDS (an unfilled in-play bid is stale bait). The
# proper fix is the event-driven repricer (next build). Worst-case pick-off ~dimes/contract.
MAKER_QUOTE_LIVE_GAMES = True          # quote in-play games (was hard-excluded)
MAKER_LIVE_KALSHI_BUFFER_CENTS = 0.03  # hedge buffer for LIVE-game quotes (vs 0.01 pre-game)
MAKER_LIVE_QUOTE_TTL_SECONDS = 180     # rest window for LIVE-game quotes (vs 1800 pre-game)
MAKER_PAPER_FILL_RULE = "ask_crosses"  # paper sim: live PM ask <= our rest price -> synthesize a full-qty fill
# Hedge-depth gate: the hedge must be a drop in the bucket, not the bucket — before
# resting a PM quote, the LIVE Kalshi book must show >= this multiple of our hedge size
# (CONTRACTS) on the hedge side, so a fill's IOC hedge is absorbed without eating the
# book. Placement-only gate, fail-closed (missing book/depth -> don't rest); the
# fill-time hedge of a REAL fill is never gated on this — hedging is mandatory.
MAKER_HEDGE_DEPTH_CONTRACTS_MULT = 5
# Per-market session inventory cap: a correctly hedged pair is settlement-riskless, but
# each fill-cycle carries unwind/legging risk — cap how many times one market can recycle
# per session (contracts total = this x MAKER_CONTRACTS). Survives restart via
# data/maker_state.json; deliberately NOT reset by kill() (an emergency, not a reset).
MAKER_MAX_FILLS_PER_MARKET = 3
# ── end maker block ───────────────────────────────────────────────────────────

# ── EVENT REPRICER (strategy layer, not included) — WS tick -> maker reprice worker ──
# Ships DARK: with the master gate False the strategy's observable behavior is identical to
# the scan-only maker (the stream hook and dirty-queue may run and record; the worker never
# acts).
MAKER_EVENT_REPRICE_ENABLED = False    # master gate for the worker ACTING; False = today's behavior
MAKER_REPRICE_THRESHOLD_TICKS = 2      # min drift (in PM ticks) before cancel-replace; below = churn
                                       # that loses queue priority and burns budget for nothing
# Global reprice budget (token bucket): 4 actions/s ~= 2 replaces/s is the hard backstop —
# PM US shares ~20 rps/IP between fetches and authed order calls (2026-07-10 calibration:
# ~5-10 rps authed headroom at 8 workers / 15s scans), so a wild night must degrade to slower
# repricing, never to 429s starving the kill/hedge path. The bucket meters REPRICE actions
# ONLY — hedges, kill, disarm, TTL/gone cancels never consume from it (named invariant).
MAKER_GLOBAL_ACTIONS_PER_SECOND = 4.0  # bucket refill rate (a replace = cancel + place = 2 actions).
                                       # first live session: sustained demand measured ~1.4/s (median 2/s), so 4/s
                                       # refill sits far under PM's ~5-10rps authed headroom
MAKER_GLOBAL_ACTIONS_BURST = 8.0       # bucket depth. First live session: 9.3% of seconds burst to 4-6 actions when
                                       # all books tick on one game event and hit the old depth-3 ceiling
                                       # (bucket binding 50% of minutes); depth 8 absorbs the measured max 6
# (MAKER_REPRICE_MIN_SECONDS above doubles as the worker's per-market throttle.)
# ── end event repricer block ─────────────────────────────────────────────────

# ── ALWAYS-ON UNIVERSE (strategy layer, not included) — universe-driven quoting ──
# The strategy layer selects the top-N most liquid matched markets each scan and the
# maker quotes THOSE, arb displayed or not. MAKER_MODE="arb_triggered" keeps the
# arb-triggered behavior; "always_on" switches to universe-driven quoting.
MAKER_MODE = "arb_triggered"           # "always_on" = universe-driven quoting
MAKER_UNIVERSE_SIZE = 5                # N quoted markets (owner decision)
MAKER_MIN_PM_DEPTH_DOLLARS = 50.0      # PM top-of-book floor, our outcome's ask side (UNITS: DOLLARS)
MAKER_MAX_PM_SPREAD = 0.10             # max PM spread (our ask - derived bid) for a sane two-sided book
MAKER_KALSHI_ASK_BAND = (0.10, 0.90)   # near-settlement exclusion on the live hedge-side ask
MAKER_UNIVERSE_ENTER_SCANS = 2         # hysteresis: consecutive top-N scans before a market enters (~30s proof)
MAKER_UNIVERSE_EXIT_SCANS = 4          # hysteresis: consecutive missed scans before an incumbent exits.
                                       # first live session: at 2 (~30s) markets flapped in/out (az-lad entered 27x,
                                       # universe only 4.25/5 full) on brief book-quiet spells; 4 (~60s) keeps
                                       # the WATCH-list sticky while placement stays fresh-gated (never quotes
                                       # stale). Immediate-evict SAFETY reasons (settlement/fill-cap) unchanged
MAKER_UNIVERSE_EXIT_SLACK = 2          # incumbents stay while ranked within top N + this
MAKER_UNIVERSE_PM_DEPTH_NORM = 200.0   # rank normalizer: PM depth $ for a 1.0 score component
MAKER_UNIVERSE_K_DEPTH_NORM = 50       # rank normalizer: Kalshi depth CONTRACTS for a 1.0 score component
# Margin tiers (owner decision): 2c pre-game / 3c in-play, each scaling with displayed edge
# via max(tier, 0.5*edge), hard-capped at MAKER_MAX_MARGIN_CENTS (5c).
MAKER_PREGAME_MARGIN_CENTS = 0.02      # locked margin tier for upcoming games (DOLLARS; _CENTS naming quirk kept)
MAKER_INPLAY_MARGIN_CENTS = 0.03       # locked margin tier for live games (informed flow demands more)
# ── end always-on universe block ─────────────────────────────────────────────

# ── PER-MARKET GATING (strategy layer, not included) — freshness of the SPECIFIC books ──
# Default False = today's global-audit gate at every
# read site (THE revert tell — byte-identical behavior). True rewires: the audit's `healthy`
# becomes a circuit breaker on NEW placements only (its systemic-divergence, 26c-class role);
# existing quotes are managed on their OWN books' freshness. Evidence (live night
# 2026-07-10/11): the audit flapped in-play — 6 healthy
# windows of 55-276s over 14 min, gate closed on ~18-30% of candidate scans at peak — and
# every flap abandoned resting quotes on markets whose own books were provably fresh.
MAKER_PER_MARKET_GATING = False        # True = per-market gate + circuit-breaker rewire; False = today's global gate
MAKER_VENUE_TS_MAX_AGE_SECONDS = 10.0  # venue-timestamp freshness bound (venue_ts None passes fail-open;
                                       # wall-clock fresh(STREAM_STALE_SECONDS) always binds regardless)
MAKER_BOOK_STALE_CANCEL_SECONDS = 30.0 # continuous per-market staleness before a "stale_book" protective cancel
# ── end per-market gating block ──────────────────────────────────────────────

# ── MAKER RISK RAILS (strategy layer, not included) — bounds on the quoter's failure modes ──
# DELIBERATE exception to default-to-current-behavior: these default ON — a rail can only stand the engine down, never place an order,
# and a safety rail that ships disabled protects nothing. Each knob's 0/None = off is the
# revert tell.
MAKER_MAX_UNHEDGED_CONTRACTS = 2       # aggregate naked-fill cap (fill-burst rail): at/above it the
                                       # engine sheds load — no new placements, no reprices; hedging
                                       # and cancels continue; auto-resumes below. 0/None = off
MAKER_TRIPWIRE_BAD = 3                 # bad fill cycles in the window -> disarm + cancel everything;
                                       # HUMAN re-arm required ("we are the dumb money tonight"). 0/None = off
MAKER_TRIPWIRE_WINDOW = 5              # rolling fill-cycle window the bad count is judged over
# ── end maker risk rails block ───────────────────────────────────────────────


# ════════════════════════════ 4. FEES ═══════════════════════════════════════
# Real per-trade TAKER fees on both venues are probability-weighted, not flat:
#     fee per contract = theta * p * (1 - p)        (p = price in dollars, 0-1)
# so a leg's true cost per $1 of payout is:
#     eff_ask = p + theta*p*(1-p) = p * (1 + theta*(1 - p))
# Fees peak at p=0.50 and taper toward 0 at the extremes. Maker rebates are ignored —
# an arb crosses the book (taker). theta: Kalshi verified 0.07; PM US 0.06 as of
# 2026-07-01 (verification-pending against a real fill) — see docs.polymarket.us/fees.md.
PLATFORM_FEES = {
    "kalshi":        {"theta": 0.07},  # ~1.75% of notional max
    "polymarket_us": {"theta": 0.06},  # ~1.5% of notional max; was 0.05, raised eff. 2026-07-01
}

# ── MAKER FEES / REBATES (maker θ; prices a resting quote) ────────────────────
PLATFORM_MAKER_FEES = {
    "kalshi":        {"theta": 0.0175},   # PROVISIONAL: 25% of taker per secondary sources; verify on live post-only fill
    "polymarket_us": {"theta": -0.0125},  # REBATE (maker is PAID) per docs.polymarket.us/fees.md eff. 2026-07-01
}


def maker_fee(platform: str, price: float, qty: float) -> float:
    """Modeled maker fee (negative = rebate) = theta*p*(1-p)*qty; NOT clamped at 0."""
    theta = PLATFORM_MAKER_FEES.get(platform, {}).get("theta", 0.0)
    return theta * price * (1.0 - price) * qty
# ── end maker fees block ──────────────────────────────────────────────────────


# ═══════════════════════════ 5. VENUES ══════════════════════════════════════
PAPER_VENUES = ("kalshi", "polymarket_us")  # platforms the engine simulates by default
# Venues with a real live broker — and the legal route for a US person. In LIVE mode the
# engine refuses any leg NOT on one of these (e.g. the int'l "polymarket" display feed).
TRADEABLE_LIVE_VENUES = ("kalshi", "polymarket_us")


# ═══════════════════════════ 6. NETWORK ═════════════════════════════════════
# Read endpoints (public, keyless). Swap to demo/staging hosts here if ever needed.
KALSHI_MARKETS_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
PM_US_GATEWAY = "https://gateway.polymarket.us"

KALSHI_TIMEOUT = 10.0      # HTTP timeout, seconds
KALSHI_PAGE_SIZE = 1000    # markets per request (API max)
KALSHI_MAX_PAGES = 50      # runaway brake for the pagination loop
KALSHI_MAX_RETRIES = 5     # 429 rate-limit retries before giving up
KALSHI_RETRY_BASE = 0.25   # base backoff (s); real reset is sub-second, 1s was wasteful
KALSHI_RETRY_CAP = 5.0     # never honor a Retry-After longer than this

PM_US_TIMEOUT = 15.0       # HTTP timeout, seconds
BOOK_WORKERS = 8           # concurrent PM US order-book fetches per scan. 2026-07-10: 24->8 — the 24-worker
                           # burst spikes to ~50-100 req/s for ~2s, saturating PM's PER-IP pool that the AUTHED
                           # api.polymarket.us calls share (429s hit the kill-confirm on 07-02 and the GTC probe
                           # tonight). 8 smooths the burst (~+2-3s/scan) so live order calls always have headroom.
FETCH_VENUE_WORKERS = 2    # Kalshi + PM US fetched concurrently (one worker each)
CLI_POLL_SECONDS = 60      # default --interval for the standalone fetch CLIs

# ── WEBSOCKET STREAMING (stream/) — continuously-fresh order books ────────────
# Authenticated handshakes (same keys as live trading: Kalshi RSA-PSS, PM US Ed25519).
# Signed string for both is `timestamp + "GET" + path`. Built shadow-first: the stream
# maintains live books and we LOG ws-vs-REST agreement before any fire reads from it.
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"  # probed working host for this key (2026-06-23)
KALSHI_WS_PATH = "/trade-api/ws/v2"            # path signed in the WS handshake
PM_US_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
PM_US_WS_PATH = "/v1/ws/markets"               # path signed in the WS handshake
STREAM_RECONNECT_BASE = 1.0                    # WS reconnect backoff base (s), doubles per failure
STREAM_RECONNECT_CAP = 30.0                    # max reconnect backoff (s)
STREAM_STALE_SECONDS = 3.0                     # a streamed book older than this is treated as stale
STREAM_AUDIT_SECONDS = 20.0                    # continuous WS-vs-REST self-audit cadence (accuracy watchdog)
STREAM_AUDIT_MIN_AGREE = 0.7                   # if < this fraction of sampled books agree w/ REST (<=1c),
                                               # mark the stream UNHEALTHY -> fires fall back to REST.
                                               # 2026-07-11 0.8->0.7: live night measured the audit oscillating
                                               # 0.76-0.97 during heavy in-play movement (1c-vs-REST sampled
                                               # seconds apart on fast books = sampling skew, not corruption),
                                               # gate-blocking real-book maker candidates. The 26c-divergence
                                               # class this audit exists for scores far below 0.7 and still
                                               # fails. Maker live quotes carry a 3c buffer (3x the audit
                                               # tolerance). REVERT TELL: fills at prices far off the WS book,
                                               # or audit readings sitting in 0.3-0.6 -> restore 0.8 + investigate.

# When True the strategy layer runs the WS stream: its placement/reprice reads and its
# display repricing read the live books. FAIL-CLOSED
# everywhere: an untrusted/stale book stands the maker down for NEW exposure and drops
# display rows; hedging a REAL fill is never gated on it (know-nothing worst case priced).
#
# ROLLOUT 2026-06-24: re-enabled after root-causing the 26c divergence (Kalshi seq-gap desync +
# PM US stats-message corruption — both now fixed) and adding the runtime self-audit. SAFE posture:
# BookStore starts UNHEALTHY until the live audit confirms WS matches REST; if WS diverges mid-run
# the audit demotes it again. Shadow-then-trust within the run: starts proven, earns the speed.
STREAM_ENABLED = True

# PM US order-transport knobs — read by PolymarketUSBroker (execute/polymarket_us_broker.py:
# _order_body + place_order). GTC post-only orders bypass them explicitly; every non-GTC
# PM US order built through _order_body flows through them.
PM_US_SYNCHRONOUS_EXECUTION = True              # non-GTC orders: venue blocks on the POST, fill in the response
PM_US_MAX_BLOCK_SECONDS = 3.0                   # maxBlockTime: how long the venue may block for the fill
PM_US_TAKER_ORDER_STYLE = "limit_ioc"           # non-GTC body style; "market" (cash-sized) exists, unverified live

# ── BOOK DEPTH — top-N (price,size) ask levels published, observational only ──
BOOK_DEPTH_LEVELS = 5                          # levels kept in LiveBook.levels_yes/levels_no
# ── end book depth block ──────────────────────────────────────────────────────

# ── PRIVATE ORDER EVENTS WS — authenticated PM US fill notifications ──
PM_US_PRIVATE_WS_URL = "wss://api.polymarket.us/v1/ws/private"
PM_US_PRIVATE_WS_PATH = "/v1/ws/private"       # path signed in the private WS handshake
# ── end private WS block ────────────────────────────────────────────────────


# ═══════════════════════════ 7. CREDENTIALS ═════════════════════════════════
# Credentials are READ FROM THE ENVIRONMENT ONLY — never hardcode secrets (see .env.example).
# Needed only to PLACE live trades; all market-data reads are public/keyless.
CREDENTIAL_ENV = {
    "kalshi": {
        "api_key_id":       "KALSHI_API_KEY_ID",
        "private_key_path": "KALSHI_PRIVATE_KEY_PATH",   # PEM file used for RSA-PSS signing
        "api_base":         "KALSHI_API_BASE",
    },
    "polymarket_us": {
        "api_key":     "POLYMARKET_US_API_KEY",          # the Key ID (X-PM-Access-Key)
        "private_key": "POLYMARKET_US_PRIVATE_KEY",      # the Ed25519 secret key (signs requests)
        "api_base":    "POLYMARKET_US_API_BASE",
    },
}


def active_bet_types(bet_types=None) -> set[str]:
    """Sanitize a bet-type selection to known types; None/empty -> ACTIVE_BET_TYPES."""
    picks = {b for b in (bet_types or ACTIVE_BET_TYPES) if b in BET_TYPES}
    return picks or set(ACTIVE_BET_TYPES)


def taker_fee(platform: str, price: float, qty: float) -> float:
    """Modeled per-order taker fee = theta*p*(1-p)*qty (continuous; see PLATFORM_FEES).
    Kalshi theta 0.07 confirmed against real fills (2026-06-22). PM US theta 0.06 per
    docs.polymarket.us/fees.md, effective 2026-07-01; live-fill verification pending. Live
    brokers round this UP to the next cent and prefer the venue-reported fee when present."""
    theta = PLATFORM_FEES.get(platform, {}).get("theta", 0.0)
    return max(0.0, theta * price * (1.0 - price) * qty)
