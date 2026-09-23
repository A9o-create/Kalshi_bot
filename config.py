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
# _select_strike_candidates() picks whichever strikes sit closest to real
# spot price each cycle as the best proxy for the trend signal.
# CONFIRMED: the ticker/threshold structure, via real examples. NOT fully
# confirmed: exact recurrence cadence -- only two real close times observed
# (9 AM and 5 PM EDT, 8 hours apart), not a verified full schedule.
#
# KXBTC15M: "BTC price up in next 15 mins?" -- re-added alongside KXBTCD
# (Sep 21) once the actual blocker was fixed as a side effect of the
# KXBTCD redesign. The original failure was structural: the OLD trigger
# needed Kalshi's own volume/price history to build up over a 30-then-6
# minute window, and a market living only 15 minutes could never
# accumulate enough (confirmed across 5 full windows, zero exceptions).
# The current trigger is 100% Coinbase-driven and needs no Kalshi-side
# history at all, so that constraint no longer applies. Re-added
# specifically for polling consistency -- KXBTCD windows can run for
# hours with nothing new to evaluate; KXBTC15M generates a fresh
# opportunity every 15 minutes, all day, filling that gap. Single market
# per window (no ladder, no strike to pick) -- combined_runner.py treats
# it as a simple series, completely separate code path from KXBTCD's
# strike-selection logic.
CRYPTO_SERIES = ["KXBTCD", "KXBTC15M"]
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

# Original 0.15% was a first guess, uncalibrated. Real session data
# (Sep 23) showed it was too conservative: 0.15% was never cleared once in
# hours of continuous observation, yet real moves regularly reached
# 0.10-0.14% (-0.140%, -0.109%, +0.125%, -0.102%, among others) without
# ever quite triggering. Lowered to 0.08% -- a moderate reduction (within
# the 0.05-0.08% range discussed as reasonable), landing on the more
# conservative end given this threshold is shared across BOTH KXBTCD and
# KXBTC15M, so any reduction effectively doubles the trade-attempt surface,
# not just one series' worth.
MOMENTUM_BTC_TREND_THRESHOLD_PCT = 0.0008

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
# Caps how many strikes of the SAME underlying window (e.g. all
# KXBTCD-26SEP2217-T* strikes share one window) can be held simultaneously.
# Multiple strikes of one window are NOT diversified risk -- they're the
# same directional bet at different thresholds, and a decisive move can
# make them all lose together. Real production data (Sep 22): the
# multi-strike fallback logic (built to keep this leg "consistently
# active" when the nearest strike is already held) let 4 simultaneous NO
# strikes accumulate on one KXBTCD window; when BTC moved decisively up,
# all 4 settled against the bot within 8 seconds of each other, triggering
# the peak-drawdown circuit breaker. This cap limits how much correlated
# exposure the fallback can build on one window while still allowing SOME
# fallback activity (unlike reverting to a hard 1-position-per-window
# limit, which would undo the "stay active" benefit entirely).
MOMENTUM_MAX_POSITIONS_PER_WINDOW = 2
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

# --- Leg 4: favorite entry (buy the favorite the moment a match goes active) ---
# Opposite thesis from value_entry: instead of betting on early mispricing
# favoring the underdog, bet the market's own initial read (often informed
# by seeding/ranking/recent form) is worth taking immediately, before the
# match's own action moves the price.
# "Just went active" is proxied by a TIGHT market-age window -- far
# tighter than value_entry's 15-min "early in match" window -- since
# Kalshi doesn't expose a direct activation event; this is the earliest
# reliably detectable point via trade history alone.
FAVORITE_ENTRY_MAX_MARKET_AGE_SECONDS = 120
# Second trigger path, added Sep 22: catches markets that drift PAST the
# age window above without ever building real trade history -- real
# production data showed multiple tickers stuck at exactly 1 trade for
# hours, invisible to the age-only trigger. Excludes watchlist-vs-watchlist
# matches (_is_watchlist_vs_watchlist) -- kept exclusively on the age
# trigger, since those matches carry real volatility potential.
FAVORITE_ENTRY_MIN_TRADES_THRESHOLD = 10
# Third trigger path, also added Sep 22: covers everything neither of the
# above catches -- a mature market (real trade history, past the age
# window) with no other leg's signal firing on it, OR a watchlist-vs-
# watchlist match excluded from the thin_market path above. Buys the
# majority side regardless, on the same logic as the other two paths: the
# market's own pricing already reflects whatever's happened so far.
# Real consequence worth knowing: this makes favorite_entry fire on nearly
# every market it examines (age OR thin_market OR majority covers almost
# the whole space), which means value_entry -- checked after favorite_entry
# in the per-ticker priority order -- will rarely get a chance to fire
# anymore.
#
# Both favorite_entry_thin (from the trigger above) and favorite_entry_majority
# (from this one) share the same exit design: no take-profit at all --
# they ride to settlement for upside -- protected on the downside by
# FAVORITE_ENTRY_TRAILING_STOP_CENTS below instead of a fixed percentage
# TP/SL. Anchored to the PEAK price seen since entry, not entry price
# itself -- a position that ran up well past entry and then reversed is
# protected relative to that real high point. Worked example this number
# came from: entry at 63c, price peaks at 95c, stop triggers on a 10c dip
# FROM THE PEAK (85c), not a 10c dip from the original 63c entry.
FAVORITE_ENTRY_TRAILING_STOP_CENTS = 10
# Starting from the same percentage framework as value_entry (already
# fee-floor calibrated) rather than untested numbers -- easy to tune
# independently later since this is its own dedicated config. Only used by
# the ORIGINAL "age" trigger path now -- thin_market/majority use the
# trailing stop above instead.
FAVORITE_ENTRY_TAKE_PROFIT_MIN_PCT = 0.25
FAVORITE_ENTRY_TAKE_PROFIT_MAX_PCT = 0.375
FAVORITE_ENTRY_STOP_LOSS_PCT = 0.15

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
DAILY_LOSS_CAP_PCT = 0.18                # was 0.10 -- halts on NET cumulative loss from session start,
                                          # not a trailing peak, so a large intraday give-back can still
                                          # avoid triggering this if net loss stays under 18%
# Absolute last-resort floor, checked against LIVE balance directly (not a
# fixed starting-balance percentage). $50 rather than literally $0 --
# leaves a small buffer so one more in-flight fill can't push balance
# negative before the check catches it.
MIN_BALANCE_DOLLARS = 50.0
# Halts if the session gives back this much (as a % of starting balance)
# from its own intraday peak, even if net loss from the start hasn't hit
# DAILY_LOSS_CAP_PCT yet -- catches a large peak-to-trough swing that a
# purely net-based check would miss. First guess, not yet calibrated
# against real data -- watch and adjust, same as every other threshold.
PEAK_DRAWDOWN_CAP_PCT = 0.12
MAX_CONCURRENT_POSITIONS = 20  # was 10 -- momentum now scans TWO series (KXBTCD + KXBTC15M) sharing
                                # one ledger with tennis, and was observed hitting the old cap (10/10)
                                # in production, blocking both legs from opening anything new regardless
                                # of signal quality. Up to 60% of balance deployed at once now (was 30%)
                                # if every slot happened to fill at the 3% max simultaneously -- a real
                                # ceiling, though actual sizes observed so far are mostly well under that.
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
