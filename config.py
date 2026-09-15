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
CRYPTO_SERIES = ["KXBTCD", "KXBTC", "KXETHD", "KXETH"]
# KXITFWMATCH confirmed live/active against Kalshi's real market data.
# KXITFMMATCH (men's) follows the same naming convention as the confirmed
# tickers (KXATPMATCH/KXWTAMATCH -> KXITFWMATCH for women) but is INFERRED,
# not independently verified. If wrong, it fails soft: get_markets() for an
# invalid series just returns empty/errors, which the runner already logs
# as a [warn] and skips -- watch the first few poll cycles' logs to confirm.
TENNIS_SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXITFWMATCH", "KXITFMMATCH"]

# --- Leg 1: Crypto momentum ---
MOMENTUM_VOLUME_SPIKE_MULTIPLE = 3.0     # current 5-min volume vs trailing 30-min avg
MOMENTUM_PRICE_MOVE_CENTS = 2            # min price move (cents) in the 5-min window
MOMENTUM_MAX_SPREAD_CENTS = 4            # don't chase if spread wider than this
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
VALUE_ENTRY_TAKE_PROFIT_MIN_PCT = 0.20   # widened from 0.15 -- same fee-floor reasoning
VALUE_ENTRY_TAKE_PROFIT_MAX_PCT = 0.30   # widened from 0.25
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
MAX_CONCURRENT_POSITIONS = 5
MAX_POSITIONS_PER_EVENT = 1

# --- Kalshi request pacing / 429 handling ---
# Kalshi's public endpoints have been getting rate-limited heavily during
# heavy-scan cycles (multiple series x multiple markets x candlesticks/trades
# each, with no spacing between requests). This enforces a minimum interval
# between consecutive requests, and retries 429s with backoff instead of
# giving up on the first one.
KALSHI_MIN_REQUEST_INTERVAL_SECONDS = 0.15   # caps request rate to ~6-7/sec
KALSHI_MAX_429_RETRIES = 2                    # up to 3 total attempts per call
KALSHI_BACKOFF_BASE_SECONDS = 1.0             # doubles each retry (1s, 2s, ...)
KALSHI_BACKOFF_JITTER_SECONDS = 0.5           # randomized, avoids thundering-herd retries

# --- Optional: restrict trading to markets matching specific player(s) ---
# When set, every leg only considers markets whose "title" field contains
# ANY of these strings (case-insensitive) -- e.g. specific players' surnames.
# Also meaningfully reduces API call volume per cycle (fewer per-market
# candlestick/trade-history lookups), which helps with Kalshi's rate limits.
# Leave empty list/None to scan everything, as before.
TARGET_MARKET_TITLE_FILTER = ["Baptiste", "Townsend", "Gauff"]

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
