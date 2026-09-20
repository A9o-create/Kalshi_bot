"""
Central configuration for the trading bot.

ENVIRONMENT controls where the bot points and whether it can place real orders.
This is the ONLY place you should need to touch when moving demo -> paper -> live.

    "backtest"    -> synthetic data, no network calls at all
    "paper_prod"  -> real Kalshi production market data, orders are logged only (no real fills)
    "live_prod"   -> real Kalshi production market data, real orders placed

Start at "backtest", move to "paper_prod" once signal logic is validated,
only move to "live_prod" after you've watched paper_prod run and agree with its calls.
"""

import os

ENVIRONMENT = os.environ.get("ENVIRONMENT", "backtest")  # "backtest" | "paper_prod" | "live_prod"

# --- Kalshi API (only used in paper_prod / live_prod) ---
# Verified against docs.kalshi.com (Sep 2026). Market data (markets/trades/
# candlesticks) works unauthenticated. Only portfolio + order endpoints need
# the API Key ID + RSA-signed request headers (KALSHI-ACCESS-KEY / -SIGNATURE
# / -TIMESTAMP). Get keys from your Kalshi account settings; never hardcode
# the private key -- load it from a file path via env var.
KALSHI_API_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"        # production
KALSHI_DEMO_API_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"  # demo, if you want a standalone (non-MCP) demo run
KALSHI_API_KEY_ID_ENV = "KALSHI_API_KEY_ID"
KALSHI_PRIVATE_KEY_PATH_ENV = "KALSHI_PRIVATE_KEY_PATH"

# --- Universe ---
# KXBTCD: "Bitcoin price on {date}?" -- confirmed live against real Kalshi
# ticker instances (e.g. KXBTCD-26APR2117-T79999.99, KXBTCD-26APR2109-T76399.99),
# resolving on whether CF Benchmarks' BRTI average is ABOVE the ticker's
# threshold at close. Unlike KXBTC15M (one market per window), KXBTCD lists
# MULTIPLE simultaneous threshold ("strike") markets per close time -- a
# ladder, not a single up/down bet. combined_runner.py's
# _select_nearest_strike_market() picks whichever strike sits closest to
# real spot price each cycle as the best proxy for the trend signal.
# CONFIRMED: the ticker/threshold structure, via real examples. NOT fully
# confirmed: exact recurrence cadence -- only two real close times observed
# (9 AM and 5 PM EDT, 8 hours apart), not a verified full schedule.
CRYPTO_SERIES = ["KXBTCD"]
# KXITFWMATCH confirmed live/active against Kalshi's real market data.
# KXITFMMATCH (men's) follows the same naming convention as the confirmed
# tickers (KXATPMATCH/KXWTAMATCH -> KXITFWMATCH for women) but is INFERRED,
# not independently verified. If wrong, it fails soft: get_markets() for an
# invalid series just returns empty/errors, which the runner already logs
# as a [warn] and skips -- watch the first few poll cycles' logs to confirm.
TENNIS_SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXITFWMATCH", "KXITFMMATCH"]

# --- Leg 1: Crypto momentum ---
MOMENTUM_VOLUME_SPIKE_MULTIPLE = 3.0     # current window volume vs trailing window avg
MOMENTUM_PRICE_MOVE_CENTS = 2            # min price move (cents) in the current window
MOMENTUM_MAX_SPREAD_CENTS = 4            # don't chase if spread wider than this
# Compressed to fit a KXBTC15M market's 15-minute total lifespan (was 30/5
# when scanning longer-lived hourly/daily BTC range markets). 6+2=8 minutes
# of required history leaves roughly a 7-minute window near the end of each
# market's life where a signal could actually fire and still have time to
# reach take-profit before the market forces resolution -- tight, but real.
MOMENTUM_TRAILING_WINDOW_MINUTES = 6
MOMENTUM_CURRENT_WINDOW_MINUTES = 2

# Real session data (Sep 16, 5 full KXBTC15M windows observed continuously)
# showed the original Kalshi-volume-spike trigger structurally never fires
# on this market: trailing volume was zero in EVERY check, every window, no
# exceptions -- the market just doesn't have baseline liquidity for a spike
# to stand out from. Since the contract resolves against an external index
# (CF Benchmarks BRTI) anyway, the trigger now fires directly off Coinbase's
# real BTC price movement instead of requiring Kalshi-side volume at all.
# Threshold is a first guess (0.15% over the current-window lookback), not
# yet calibrated against real fire rate -- watch and adjust.
MOMENTUM_BTC_TREND_THRESHOLD_PCT = 0.0015

# Decoupled from MOMENTUM_CURRENT_WINDOW_MINUTES (a Kalshi-candle concept
# that's now largely unused for this leg's trigger) so it can be tuned
# independently going forward.
MOMENTUM_BTC_LOOKBACK_MINUTES = 2

# Removing the Kalshi volume requirement also removed our only signal about
# whether THIS specific 15-min window has anyone actually willing to trade
# right now. A genuinely empty book would still get a simulated fill in
# paper mode, which isn't realistic -- this skips the trade instead if
# fewer than this many contracts are resting at the relevant price level.
MOMENTUM_MIN_LIQUIDITY_CONTRACTS = 5
# Kalshi's taker fee peaks at ~1.75c/contract at a 50c price and is charged
# on BOTH open and close -- worst case ~3.5c/contract round trip. Thresholds
# below were widened from the original 8c/4c so a win still clears the fee
# floor with real margin. This is a judgment call, not a re-optimization --
# paper_prod is what actually validates these numbers.
MOMENTUM_TAKE_PROFIT_CENTS = 15
MOMENTUM_STOP_LOSS_CENTS = 6

# --- Leg 2: Tennis mean reversion ---
REVERSION_SPIKE_THRESHOLD_CENTS = 15     # price move within window that counts as "overreaction"
REVERSION_SPIKE_WINDOW_SECONDS = 180     # 3 minutes
REVERSION_CONFIRM_PULLBACK_CENTS = 2     # wait for this much reversal before entering
REVERSION_CONFIRM_WINDOW_SECONDS = 300   # 5 minutes to see the pullback
REVERSION_TAKE_PROFIT_CENTS = 18         # widened from 10c -- same fee-floor reasoning as leg 1
REVERSION_STOP_LOSS_CENTS = 8            # widened from 6c

# --- Leg 3: value entry (buy cheap early, take profit on a % move) ---
# "Beginning of the match" is a proxy: within this many minutes of the
# market's first observed trade. Kalshi doesn't expose match clock/score.
VALUE_ENTRY_MAX_MARKET_AGE_MINUTES = 15
VALUE_ENTRY_TAKE_PROFIT_MIN_PCT = 0.25   # was 0.20, +25% -- modest bump, requested directly, not yet backed by real win-rate data
VALUE_ENTRY_TAKE_PROFIT_MAX_PCT = 0.375  # was 0.30, +25% -- same proportional increase, keeps the min-to-max spread ratio unchanged
VALUE_ENTRY_STOP_LOSS_PCT = 0.15         # left as-is: the risk side of the ratio, not the fee side

# --- Entry price favorability ---
# Prices near 50c carry the highest fee (fee = 0.07 * P * (1-P), peaks at
# P=0.5) and are a coin flip by construction. Deep longshots (<35c) are
# usually thin/illiquid on Kalshi's sports and crypto markets. The 35-50c
# band is the sweet spot: meaningfully cheaper fees than 50c, while avoiding
# longshot illiquidity. This multiplier biases position sizing toward that
# band -- it does NOT gate entries outside it, just sizes them smaller.
FAVORABLE_ENTRY_PRICE_MIN_CENTS = 35.0
FAVORABLE_ENTRY_PRICE_MAX_CENTS = 50.0
FAVORABLE_ENTRY_SIZE_MULTIPLIER = 1.25

# --- Risk & sizing (applies to both legs) ---
MAX_KELLY_FRACTION = 0.25                # fraction of full Kelly to actually use
MAX_POSITION_PCT_OF_BALANCE = 0.03       # hard cap per trade regardless of Kelly
DAILY_LOSS_CAP_PCT = 0.10                # halt bot for the day if breached
MAX_CONCURRENT_POSITIONS = 10  # was 5 -- up to 30% of balance deployed at once now, vs 15% before
MAX_POSITIONS_PER_EVENT = 1

# --- Kalshi request pacing / 429 handling ---
# Kalshi's own docs put the lowest AUTHENTICATED tier (Basic, granted just
# for signing up) at 20 reads/sec -- our requests are unauthenticated, so
# the real limit we're subject to is likely lower and undocumented. Real
# session data (Sep 15) showed near-constant 429s even with 0.15s spacing +
# retries, across ~5 series x up to 10 markets each every 60s -- clearly
# too fast and too broad for whatever the actual anonymous limit is.
# Slowed down substantially rather than guessing again.
KALSHI_MIN_REQUEST_INTERVAL_SECONDS = 1.0    # was 0.15 -- ~10x more conservative
KALSHI_MAX_429_RETRIES = 3                    # was 2 -- one more attempt before giving up
KALSHI_BACKOFF_BASE_SECONDS = 2.0             # was 1.0 -- more patient between retries
KALSHI_BACKOFF_JITTER_SECONDS = 1.0           # was 0.5

# How long a get_markets() result is reused before re-fetching. Market
# listings for a tennis series don't meaningfully change minute to minute --
# re-listing every single 60s cycle across 5 series was pure waste driving
# up request volume for no signal benefit. Candlesticks/trades (the actual
# price data) are NOT cached -- those genuinely need to be fresh.
MARKETS_CACHE_TTL_SECONDS = 240
# Caps how many markets per series get scanned for candlesticks/trades each
# cycle (those calls, unlike the listing itself, can't be cached -- they
# need fresh data). At KALSHI_MIN_REQUEST_INTERVAL_SECONDS=1.0s, 10 markets
# x ~5 series was up to ~50s of pure request spacing per cycle, dangerously
# close to the 60s poll interval. Reduced so the slower pacing still fits
# comfortably within a cycle.
MAX_MARKETS_PER_SERIES = 4

# --- Re-entry cooldown ---
# Real session data (Sep 16) showed the bot getting whipsawed by a single
# volatile match: no cooldown meant it re-entered the same ticker the very
# next cycle after closing, over and over, as price swung wildly -- ~15
# round trips in 35 minutes, paying fees on every single one, net losing
# money even though some individual trades were profitable. This blocks
# re-entry on the same match (not just the same ticker -- also covers the
# paired-market case) for a cooldown window after any close.
REENTRY_COOLDOWN_SECONDS = 300  # 5 minutes

# --- Watchlist: prioritize specific players via sizing, not exclusion ---
# Every market is still scanned and tradeable as normal -- this does NOT
# restrict the universe. When a signal's market title matches one of these
# names (case-insensitive substring), its position size gets boosted by
# WATCHLIST_SIZE_MULTIPLIER, same "boost not gate" pattern as the 35-50c
# favorability band. Still subordinate to the hard 3% risk cap either way.
WATCHLIST_PLAYERS = [
    "Baptiste", "Townsend", "Gauff",           # WTA
    "Rybakina", "Sabalenka",                   # WTA
    "Zverev", "Shelton", "Khachanov", "Alcaraz",  # ATP
]
WATCHLIST_SIZE_MULTIPLIER = 1.5
# For a watchlisted player specifically (not just whichever side is
# cheapest generically): take her side once her own win probability rises
# above this floor, buying as low as possible above it. Below this floor
# she's too much of a longshot -- thin, illiquid, not a genuine value bet,
# just noise. E.g. Baptiste at 31c YES clears the floor and gets taken;
# Baptiste at 12c YES does not.
WATCHLIST_MIN_ENTRY_PCT = 29

# --- Paper trading starting balance (backtest & paper_prod only) ---
PAPER_STARTING_BALANCE_DOLLARS = 1000.0
# Paper mode otherwise assumes a perfect fill at the quoted price. This adds
# random unfavorable slippage (uniform, 0 to this many cents) so paper
# results aren't more optimistic than real execution would be.
PAPER_SLIPPAGE_CENTS_MAX = 1.0

# --- Order fill polling (live_prod only) ---
# Aggressive limit orders (priced through the market) should fill fast if
# they're going to fill at all. Poll frequently for a short window, then
# cancel whatever's left resting rather than waiting indefinitely.
ORDER_FILL_POLL_INTERVAL_SECONDS = 2
ORDER_FILL_TIMEOUT_SECONDS = 60

# --- Maker-first fallback ---
# Maker fees are ~1/4 the taker rate. Try resting AT the best bid first
# (doesn't cross the spread = maker); only fall back to the aggressive
# through-the-market taker order if unfilled after this window.
MAKER_ATTEMPT_TIMEOUT_SECONDS = 15
MAKER_FEE_RATE_MULTIPLIER = 0.25   # approximation of the real ~1.75%/7% ratio
# Paper mode can't observe real maker fills, so this simulates the
# fill-or-fallback tradeoff probabilistically -- a labeled approximation,
# not a measurement. Real maker fill rates depend on queue position, which
# paper mode has no way to know.
PAPER_MAKER_FILL_PROBABILITY = 0.5

# --- Hold-to-settlement ---
# Selling before settlement pays the taker fee a second time. A position
# already this close to certain (in cents, out of 100) skips its take-profit
# sell and rides to settlement instead, where no second fee applies. Never
# applies to stop-losses -- a loser doesn't get held hoping for a reversal.
SETTLEMENT_HOLD_THRESHOLD_CENTS = 95

# --- Orderbook-depth-aware sizing ---
# A capped-Kelly position can be sized without regard to how much liquidity
# is actually resting at that price. On a thin book this walks the price and
# adds real slippage beyond the 1-2c assumption elsewhere. This caps size to
# at most this fraction of the visible resting size at the entry price level.
MAX_BOOK_DEPTH_FRACTION = 0.5
