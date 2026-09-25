"""
Combined runner: tennis (mean reversion + value entry) and momentum (BTC)
run as two independent loops in separate threads, sharing ONE broker --
one real risk ledger, one balance, one MAX_CONCURRENT_POSITIONS cap
enforced globally instead of independently per bot.

This reverses the earlier split into two separate Render services. That
split solved a real problem (a BTC-only redeploy no longer resets tennis's
running state, and vice versa) at the cost of a different one: each bot's
risk limits were enforced against its OWN balance with zero awareness of
the other, so in the worst case the two could combine to roughly double
the intended concurrent-position and per-trade exposure -- harmless in
paper_prod (fake money either way), but a real problem the moment either
bot trades real money on the same live account. This merge closes that
gap by construction: one PaperBroker (or KalshiLiveBroker), one lock,
try_open_position() checks and opens atomically so two threads can't both
slip past the same concurrency cap.

Tradeoff, accepted deliberately: a crash in one leg's loop no longer
isolates from the other (they're threads in one process again), and any
redeploy restarts both legs' in-memory state together. Kept as two
SEPARATE loops/threads rather than one shared loop specifically because
tennis and momentum have different natural cadences -- momentum reacts to
Coinbase price data that can move faster than tennis's Kalshi-trade-driven
signals need to poll.

Run: python combined_runner.py
Stop any time with Ctrl+C -- open paper positions just stay open in the
log, nothing bad happens.
"""

import time
import signal as os_signal
import sys
import threading
from datetime import datetime, timezone

import config
import signals
import risk
import execution
import kalshi_market_data as kmd
import coinbase_data

TENNIS_POLL_INTERVAL_SECONDS = 60
MOMENTUM_POLL_INTERVAL_SECONDS = 60
# Exit-checks now run on their own, much faster cadence than the full
# entry-scanning cycle -- see tennis_loop/momentum_loop for why. Real
# production evidence (Sep 24): a momentum position with a 6c stop-loss
# exited at a ~99c loss instead, because the only time exits were checked
# was once per full ~60-90s cycle (which also includes the slow,
# many-request entry scan) -- BTC moved fast enough within that window to
# blow straight through the stop before the next check ever happened.
TENNIS_EXIT_CHECK_INTERVAL_SECONDS = 15
MOMENTUM_EXIT_CHECK_INTERVAL_SECONDS = 15
# KXBTCD markets live far longer than KXBTC15M's strict 15-minute window
# (real examples observed 8 hours apart -- exact cadence not fully
# confirmed, but clearly hours not minutes). Candles are only used here to
# get a recent price reading for entry/exit bookkeeping, not for a
# Kalshi-side signal, so this doesn't need to be tight -- widened from 14.
MOMENTUM_CANDLE_LOOKBACK_MINUTES = 30
# How many strikes to pull per fetch when selecting from the ladder. Cheap
# to set high -- get_markets() results are cached for MARKETS_CACHE_TTL_SECONDS,
# so a bigger limit doesn't proportionally increase live request volume.
MOMENTUM_STRIKE_LADDER_FETCH_LIMIT = 20

# Series that list multiple simultaneous threshold/strike markets for the
# same close time (need _select_strike_candidates()'s nearest-to-spot
# selection). Any CRYPTO_SERIES entry NOT in this set is treated as a
# simple series -- exactly one open market at a time, no strike to choose
# between (e.g. KXBTC15M) -- and every open market becomes a direct
# candidate as-is, skipping strike-parsing entirely.
LADDER_SERIES = {"KXBTCD"}

_running = True

# Combined status snapshot -- both loops write their own sub-dict, so
# combined_server.py's /status can report both legs' activity from one
# shared broker without any locking gymnastics of its own (each loop only
# ever writes its own key).
latest_status = {
    "balance": None, "open_positions": None, "last_cycle_ts": None, "environment": None,
    "tennis": {"last_cycle_ts": None},
    "momentum": {"last_cycle_ts": None},
}


def _handle_shutdown(sig, frame):
    global _running
    print("\nShutting down after current cycle...")
    _running = False


def _derive_match_key(ticker: str) -> str:
    """
    Derives a shared key for "the same underlying match/window" directly
    from the ticker string, rather than trusting Kalshi's own event_ticker
    field (which doesn't reliably group paired per-player markets for the
    same tennis match). Strips the final "-XXX" segment.

    Tennis-specific: two tickers sharing this key ARE genuinely
    correlated/redundant bets (opposite sides of the same match). Do NOT
    use for momentum/KXBTCD -- every strike in the same window would
    collapse to the same key, blocking simultaneous holds of different
    strikes even though they're distinct bets. See _momentum_event_key().
    """
    parts = ticker.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else ticker


def _momentum_event_key(ticker: str) -> str:
    """
    Unlike _derive_match_key(), does NOT collapse to a shared window
    prefix -- each strike is its own independent event. Real data (Sep 21)
    showed the leg going idle for hours holding one strike with no
    fallback; the fallback added to fix that only works if different
    strikes of the same window can actually be held simultaneously,
    rather than blocked by tennis's pairing protection. Still fully
    prevents re-entering the EXACT SAME strike (the ticker itself is the
    key), just not other strikes of the same window.
    """
    return ticker


def _count_open_positions_in_window(broker, window_key: str) -> int:
    """
    Counts currently open momentum positions sharing the same underlying
    window (e.g. all KXBTCD-26SEP2217-T* strikes share window
    'KXBTCD-26SEP2217'), regardless of which specific strike is held.
    Reuses _derive_match_key()'s string transformation for a DIFFERENT
    purpose than its original tennis-pairing use -- here it's purely a
    correlated-exposure counter, not an event-dedup key (that's
    _momentum_event_key() above, deliberately NOT window-collapsed, since
    holding several different strikes at once is allowed -- just capped).
    """
    count = 0
    for pos in broker.get_open_positions_snapshot():
        if pos.strategy == "momentum" and _derive_match_key(pos.ticker) == window_key:
            count += 1
    return count


def _apply_depth_cap(ticker: str, direction: str, side_price: float, size: float) -> float:
    """
    Fetches the orderbook and caps `size` to MAX_BOOK_DEPTH_FRACTION of the
    liquidity actually resting at the entry price level. Fails open (returns
    size unchanged) if the orderbook fetch errors.
    """
    try:
        ob = kmd.get_orderbook(ticker)
        available = ob["best_yes_ask_size"] if direction == "yes" else ob["best_no_ask_size"]
        return risk.cap_size_by_depth(size, side_price, available)
    except Exception as e:
        print(f"[warn] orderbook depth check failed for {ticker}, sizing without a depth cap: {e}")
        return size


def _market_age_seconds(trades: list) -> float:
    """Age of the market based on its earliest observed trade in this sample."""
    if not trades:
        return float("inf")
    return time.time() - trades[0]["ts"]


def _closes_today(market: dict) -> bool:
    """
    True if a market's close_time falls on today's calendar date (UTC).
    Added Sep 25 specifically for tennis, after a real position was opened
    on a match that doesn't close until the next day -- meaning it would
    sit open overnight through the shakedown period, undesirable while the
    account is still being validated.

    Checks close_time only, not open_time -- the concern is specifically
    about not holding a position overnight, and close_time alone fully
    determines that regardless of when the market happened to open.

    Field confirmed against Kalshi's own documented Market schema
    (close_time, ISO 8601). A missing or unparseable timestamp is excluded
    rather than risked through.
    """
    close_time_str = market.get("close_time")
    if not close_time_str:
        return False
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    today_utc = datetime.now(timezone.utc).date()
    return close_dt.date() == today_utc


def _parse_strike_threshold(ticker: str):
    """
    Extracts the dollar threshold from a KXBTCD ticker suffix, e.g.
    'KXBTCD-26APR2117-T79999.99' -> 79999.99. Confirmed against real ticker
    examples (title: 'Bitcoin price on {date}?', resolving on whether
    BRTI's average is ABOVE this threshold at close). Returns None if the
    ticker doesn't match this pattern -- callers should skip, not guess.
    """
    try:
        suffix = ticker.rsplit("-", 1)[-1]
        if suffix.startswith("T"):
            return float(suffix[1:])
    except (ValueError, IndexError):
        pass
    return None


def _select_strike_candidates(markets: list, spot_price_dollars: float) -> list:
    """
    KXBTCD lists multiple simultaneous 'above $X' threshold markets for the
    same close time -- unlike KXBTC15M, which was a single market per
    window. Returns every parseable strike sorted nearest-to-spot first,
    the best available proxy for "will price be up or down from here," the
    same bet KXBTC15M represented directly.

    Returns a LIST (not just the single nearest) so the caller can fall
    through to the next-nearest strike when the top pick is already held --
    real data (Sep 21) showed the leg going fully idle for hours once its
    one nearest-strike position was open, since there was no fallback and
    the debounce check silently skipped every cycle after with no
    diagnostic at all.

    Known, deliberately unresolved tradeoff: the nearest-to-spot strike is
    also the one closest to 50c -- Kalshi's highest-fee price zone (fee
    peaks at a 50c price, per the verified fee formula). Nearest-strike is
    the most economically honest translation of the trend signal, but not
    necessarily the most fee-efficient choice. Flagged, not fixed here.
    """
    candidates = []
    for m in markets:
        threshold = _parse_strike_threshold(m["ticker"])
        if threshold is None:
            continue
        distance = abs(threshold - spot_price_dollars)
        candidates.append((distance, m))
    candidates.sort(key=lambda pair: pair[0])
    return [m for _, m in candidates]


def _check_market_settled(ticker: str):
    """
    Checks Kalshi's own market status directly, rather than guessing from a
    holding-time timer. Neither loop otherwise has any way to know a
    market closed if price never moved enough to hit take-profit or
    stop-loss on its own -- real evidence found Sep 20: three momentum
    positions sat "open" in our tracking for 18-25+ hours after their
    KXBTCD windows had almost certainly already settled, silently eating
    into MAX_CONCURRENT_POSITIONS with no way to ever naturally close.

    Returns (is_settled: bool, resolved_price_cents: float | None).
    resolved_price_cents is 100.0 if the market determined 'yes', 0.0 if
    'no', None if settled but the result isn't available yet (caller should
    still force-close using the last known price rather than wait
    indefinitely) or if the status check itself failed (fails open --
    treated as NOT settled, so a transient API error can't block a normal
    price-based exit).
    """
    try:
        market = kmd.get_market(ticker)
        status = market.get("status")
        if status in ("active", "initialized", None):
            return False, None
        result = market.get("result")
        if result == "yes":
            return True, 100.0
        elif result == "no":
            return True, 0.0
        else:
            return True, None  # closed/determined but no result yet -- still force-close
    except Exception as e:
        print(f"[warn] settlement check failed for {ticker}: {e}")
        return False, None  # fail open -- never block a normal price-based exit


def _should_halt(broker):
    """
    Checks all three account-level halt conditions together: net daily
    loss cap, absolute balance floor, and peak-drawdown. Returns
    (should_halt: bool, reason: str | None). Shared by both loops, checked
    both at the top of each cycle and immediately after every position
    close, so reaction time isn't limited to once per ~60-90s cycle.
    """
    if risk.daily_loss_breached(broker.daily_pnl, broker.starting_balance):
        return True, f"daily loss cap breached (pnl=${broker.daily_pnl:.2f}, cap={config.DAILY_LOSS_CAP_PCT*100:.0f}%)"
    trading_shard_balance = broker.get_trading_shard_balance()
    if risk.hard_balance_floor_breached(trading_shard_balance):
        return True, f"hard balance floor breached (trading shard balance=${trading_shard_balance:.2f}, floor=${config.MIN_BALANCE_DOLLARS:.2f})"
    if risk.drawdown_from_peak_breached(broker.daily_pnl, broker.peak_daily_pnl, broker.starting_balance):
        return True, f"peak drawdown breached (pnl=${broker.daily_pnl:.2f}, peak=${broker.peak_daily_pnl:.2f}, cap={config.PEAK_DRAWDOWN_CAP_PCT*100:.0f}%)"
    return False, None


def tennis_loop(broker):
    """Mean reversion + value entry. Runs in its own thread against the shared broker."""
    already_signaled_events: set[str] = set()
    cooldown_until: dict[str, float] = {}
    last_entry_scan_ts = 0.0  # entry-scanning is throttled separately from exit-checks -- see below

    while _running:
        halted = False
        for pos in broker.get_open_positions_snapshot():
            if pos.strategy not in ("reversion", "value_entry", "favorite_entry", "favorite_entry_thin", "favorite_entry_majority", "resynced_unknown"):
                continue  # not this loop's position -- momentum_loop owns it
            try:
                if pos.strategy == "reversion":
                    trades = kmd.get_recent_trades(pos.ticker, limit=5)
                    if not trades:
                        continue
                    current_price = trades[-1]["yes_price_cents"]
                    exit_reason = risk.check_cents_exit(
                        pos.direction, pos.entry_price_cents, current_price,
                        config.REVERSION_TAKE_PROFIT_CENTS, config.REVERSION_STOP_LOSS_CENTS,
                    )
                elif pos.strategy == "value_entry":
                    trades = kmd.get_recent_trades(pos.ticker, limit=5)
                    if not trades:
                        continue
                    current_price = trades[-1]["yes_price_cents"]
                    exit_reason = risk.check_value_entry_exit(pos.direction, pos.entry_price_cents, current_price)
                elif pos.strategy in ("favorite_entry", "resynced_unknown"):
                    # resynced_unknown (see execution.py's _sync_positions_from_kalshi):
                    # a real position found open on Kalshi at startup whose ORIGINAL
                    # strategy we have no way to know -- Kalshi's API has no concept
                    # of our internal strategy taxonomy. Reuses favorite_entry's
                    # exit rule as a safe, generic default rather than leaving a
                    # real position completely unmanaged after a restart.
                    trades = kmd.get_recent_trades(pos.ticker, limit=5)
                    if not trades:
                        continue
                    current_price = trades[-1]["yes_price_cents"]
                    exit_reason = risk.check_favorite_entry_exit(pos.direction, pos.entry_price_cents, current_price)
                else:  # favorite_entry_thin / favorite_entry_majority -- ride to
                    # settlement for upside (no take-profit at all), protected on
                    # the downside by a trailing stop anchored to the best price
                    # seen since entry, not a fixed distance from entry. A market
                    # thin or unclaimed at entry doesn't have to stay that way, so
                    # an uncapped hold would have real, unprotected downside risk.
                    trades = kmd.get_recent_trades(pos.ticker, limit=5)
                    if not trades:
                        continue
                    current_price = trades[-1]["yes_price_cents"]
                    side_price = current_price if pos.direction == "yes" else (100 - current_price)
                    if pos.peak_side_price_cents is None:
                        pos.peak_side_price_cents = side_price  # first check after open -- initialize the high-water mark
                    else:
                        pos.peak_side_price_cents = max(pos.peak_side_price_cents, side_price)
                    exit_reason = risk.check_trailing_stop_loss(pos.peak_side_price_cents, side_price)

                is_settled, resolved_price = _check_market_settled(pos.ticker)
                if is_settled:
                    close_price = resolved_price if resolved_price is not None else current_price
                    print(f"[exit] {pos.ticker} ({pos.strategy}): market_settled (closing at {close_price:.0f}c)")
                    broker.close_position(pos.ticker, close_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
                    should_halt, halt_reason = _should_halt(broker)
                    if should_halt:
                        print(f"[HALT] tennis_loop stopping: {halt_reason}")
                        halted = True
                        break
                    continue

                if exit_reason:
                    side_price = current_price if pos.direction == "yes" else (100 - current_price)
                    if exit_reason == "take_profit" and side_price >= config.SETTLEMENT_HOLD_THRESHOLD_CENTS:
                        print(f"[hold] {pos.ticker} ({pos.strategy}): at {side_price:.0f}c, holding to settlement")
                        continue
                    print(f"[exit] {pos.ticker} ({pos.strategy}): {exit_reason}")
                    broker.close_position(pos.ticker, current_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
                    should_halt, halt_reason = _should_halt(broker)
                    if should_halt:
                        print(f"[HALT] tennis_loop stopping: {halt_reason}")
                        halted = True
                        break
            except Exception as e:
                print(f"[warn] tennis exit check failed for {pos.ticker}: {e}")

        if halted:
            break

        # gate the entry phase specifically -- exits above always get a chance
        # to complete for this cycle even if a threshold was just breached
        should_halt, halt_reason = _should_halt(broker)
        if should_halt:
            print(f"[HALT] tennis_loop stopping: {halt_reason}")
            break

        # Entry-scanning (the slow, many-request pass below) is throttled to
        # roughly TENNIS_POLL_INTERVAL_SECONDS, independent of how often the
        # exit-check loop above actually runs (TENNIS_EXIT_CHECK_INTERVAL_SECONDS,
        # much faster). This is the fix for the gap-through issue: exits now
        # get checked far more often than new entries get scanned for.
        now = time.time()
        if now - last_entry_scan_ts >= TENNIS_POLL_INTERVAL_SECONDS:
            last_entry_scan_ts = now
            _tennis_entry_scan(broker, already_signaled_events, cooldown_until)

        open_count = broker.get_open_position_count()
        print(f"[tennis cycle done] balance=${broker.balance:.2f} open_positions={open_count}")
        latest_status["balance"] = round(broker.balance, 2)
        latest_status["open_positions"] = open_count
        latest_status["last_cycle_ts"] = time.time()
        latest_status["environment"] = config.ENVIRONMENT
        latest_status["tennis"]["last_cycle_ts"] = time.time()
        time.sleep(TENNIS_EXIT_CHECK_INTERVAL_SECONDS)


def _tennis_entry_scan(broker, already_signaled_events, cooldown_until):
    """
    The actual entry-scanning pass: fetches markets per series, checks
    reversion / favorite_entry / value_entry in priority order. Extracted
    into its own function so tennis_loop can throttle how often this
    (relatively slow, many-request) pass runs, independently of how often
    exit-checks run -- see tennis_loop's comments for why this split exists.
    """
    for series in config.TENNIS_SERIES:
            try:
                markets = kmd.get_markets(series, status="open", limit=config.MAX_MARKETS_PER_SERIES)
            except Exception as e:
                print(f"[warn] couldn't fetch markets for {series}: {e}")
                continue

            # Only trade matches that close today (UTC) -- added Sep 25 after
            # a real position was opened on a match that doesn't close until
            # the next day, leaving it open overnight. Applied before any
            # strategy gets a chance to evaluate the market at all.
            markets = [m for m in markets if _closes_today(m)]

            for m in markets:
                ticker = m["ticker"]
                event_ticker = _derive_match_key(ticker)
                if ticker in already_signaled_events:
                    continue
                if event_ticker in cooldown_until and time.time() < cooldown_until[event_ticker]:
                    continue

                try:
                    trades = kmd.get_recent_trades(ticker, limit=100)
                except Exception as e:
                    print(f"[warn] trades failed for {ticker}: {e}")
                    continue
                if not trades:
                    continue

                sig, diag = signals.detect_reversion_signal(trades, trades[-1]["ts"])
                if diag.get("reason") != "signal_fired":
                    if diag.get("spike_found"):
                        pullback_str = f", pullback={diag['pullback_cents']:.1f}c" if "pullback_cents" in diag else ""
                        print(f"[reversion diag] {ticker}: spike={diag['spike_direction']} "
                              f"magnitude={diag['spike_magnitude_cents']:.1f}c{pullback_str} reason={diag['reason']}")
                    elif "largest_move_cents" in diag:
                        print(f"[reversion diag] {ticker}: trades={diag.get('trade_count', 0)} "
                              f"largest_move={diag['largest_move_cents']:.1f}c "
                              f"(need {config.REVERSION_SPIKE_THRESHOLD_CENTS}c) reason={diag['reason']}")
                    else:
                        print(f"[reversion diag] {ticker}: trades={diag.get('trade_count', 0)} reason={diag['reason']}")
                if sig:
                    current_price = trades[-1]["yes_price_cents"]
                    side_price = current_price if sig.direction == "yes" else (100 - current_price)
                    win_prob = 0.5 + (sig.strength * 0.15)
                    size = risk.position_size_dollars(
                        broker.balance, win_prob,
                        config.REVERSION_TAKE_PROFIT_CENTS, config.REVERSION_STOP_LOSS_CENTS,
                        side_price_cents=side_price, market_title=m.get("title", ticker),
                    )
                    size = _apply_depth_cap(ticker, sig.direction, side_price, size)
                    if size > 0:
                        opened = broker.try_open_position(ticker, event_ticker, sig.direction, current_price, size,
                                                           sig.reason, strategy="reversion", market_title=m.get("title", ticker))
                        if opened:
                            already_signaled_events.add(ticker)
                    continue

                age = _market_age_seconds(trades)

                sig, trigger_type = signals.detect_favorite_entry_signal(trades, age, market_title=m.get("title"))
                if sig:
                    current_price = trades[-1]["yes_price_cents"]
                    side_price = current_price if sig.direction == "yes" else (100 - current_price)
                    win_prob = 0.55
                    win_cents = side_price * config.FAVORITE_ENTRY_TAKE_PROFIT_MIN_PCT
                    loss_cents = side_price * config.FAVORITE_ENTRY_STOP_LOSS_PCT
                    size = risk.position_size_dollars(broker.balance, win_prob, win_cents, loss_cents,
                                                       side_price_cents=side_price, market_title=m.get("title", ticker))
                    size = _apply_depth_cap(ticker, sig.direction, side_price, size)
                    if size > 0:
                        # thin_market positions hold to settlement instead of
                        # normal TP/SL -- tagged with a distinct strategy name
                        # so the exit-check dispatch treats them differently
                        # both thin_market and majority ride to settlement with
                        # trailing-stop protection instead of normal TP/SL --
                        # kept as distinct strategy names for diagnostic
                        # tracking of which trigger actually fired
                        if trigger_type == "thin_market":
                            strategy_name = "favorite_entry_thin"
                        elif trigger_type == "majority":
                            strategy_name = "favorite_entry_majority"
                        else:
                            strategy_name = "favorite_entry"
                        opened = broker.try_open_position(ticker, event_ticker, sig.direction, current_price, size,
                                                           sig.reason, strategy=strategy_name, market_title=m.get("title", ticker))
                        if opened:
                            already_signaled_events.add(ticker)
                    continue

                sig = signals.detect_value_entry_signal(trades, age, market_title=m.get("title"))
                if sig:
                    current_price = trades[-1]["yes_price_cents"]
                    side_price = current_price if sig.direction == "yes" else (100 - current_price)
                    win_prob = 0.55
                    win_cents = side_price * config.VALUE_ENTRY_TAKE_PROFIT_MIN_PCT
                    loss_cents = side_price * config.VALUE_ENTRY_STOP_LOSS_PCT
                    size = risk.position_size_dollars(broker.balance, win_prob, win_cents, loss_cents,
                                                       side_price_cents=side_price, market_title=m.get("title", ticker))
                    size = _apply_depth_cap(ticker, sig.direction, side_price, size)
                    if size > 0:
                        opened = broker.try_open_position(ticker, event_ticker, sig.direction, current_price, size,
                                                           sig.reason, strategy="value_entry", market_title=m.get("title", ticker))
                        if opened:
                            already_signaled_events.add(ticker)


def momentum_loop(broker):
    """BTC momentum. Runs in its own thread against the shared broker."""
    already_signaled_events: set[str] = set()
    cooldown_until: dict[str, float] = {}
    last_entry_scan_ts = 0.0  # entry-scanning is throttled separately from exit-checks -- see below

    while _running:
        halted = False
        for pos in broker.get_open_positions_snapshot():
            if pos.strategy != "momentum":
                continue  # not this loop's position -- tennis_loop owns it
            try:
                series = pos.ticker.split("-")[0]
                now = int(time.time())
                candles = kmd.get_candlesticks(series, pos.ticker, now - 120, now, period_interval=1)
                if not candles:
                    continue
                current_price = candles[-1]["price_cents"]
                exit_reason = risk.check_cents_exit(
                    pos.direction, pos.entry_price_cents, current_price,
                    config.MOMENTUM_TAKE_PROFIT_CENTS, config.MOMENTUM_STOP_LOSS_CENTS,
                )

                is_settled, resolved_price = _check_market_settled(pos.ticker)
                if is_settled:
                    close_price = resolved_price if resolved_price is not None else current_price
                    print(f"[exit] {pos.ticker} ({pos.strategy}): market_settled (closing at {close_price:.0f}c)")
                    broker.close_position(pos.ticker, close_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
                    should_halt, halt_reason = _should_halt(broker)
                    if should_halt:
                        print(f"[HALT] momentum_loop stopping: {halt_reason}")
                        halted = True
                        break
                    continue

                if exit_reason:
                    side_price = current_price if pos.direction == "yes" else (100 - current_price)
                    if exit_reason == "take_profit" and side_price >= config.SETTLEMENT_HOLD_THRESHOLD_CENTS:
                        print(f"[hold] {pos.ticker} ({pos.strategy}): at {side_price:.0f}c, holding to settlement")
                        continue
                    print(f"[exit] {pos.ticker} ({pos.strategy}): {exit_reason}")
                    broker.close_position(pos.ticker, current_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
                    should_halt, halt_reason = _should_halt(broker)
                    if should_halt:
                        print(f"[HALT] momentum_loop stopping: {halt_reason}")
                        halted = True
                        break
            except Exception as e:
                print(f"[warn] momentum exit check failed for {pos.ticker}: {e}")

        if halted:
            break

        should_halt, halt_reason = _should_halt(broker)
        if should_halt:
            print(f"[HALT] momentum_loop stopping: {halt_reason}")
            break

        # Entry-scanning is throttled to roughly MOMENTUM_POLL_INTERVAL_SECONDS,
        # independent of how often the exit-check loop above actually runs
        # (MOMENTUM_EXIT_CHECK_INTERVAL_SECONDS, much faster) -- see
        # TENNIS_EXIT_CHECK_INTERVAL_SECONDS's comment for the real
        # production incident this fixes.
        scan_now = time.time()
        if scan_now - last_entry_scan_ts >= MOMENTUM_POLL_INTERVAL_SECONDS:
            last_entry_scan_ts = scan_now
            _momentum_entry_scan(broker, already_signaled_events, cooldown_until)

        open_count = broker.get_open_position_count()
        print(f"[momentum cycle done] balance=${broker.balance:.2f} open_positions={open_count}")
        latest_status["balance"] = round(broker.balance, 2)
        latest_status["open_positions"] = open_count
        latest_status["last_cycle_ts"] = time.time()
        latest_status["environment"] = config.ENVIRONMENT
        latest_status["momentum"]["last_cycle_ts"] = time.time()
        time.sleep(MOMENTUM_EXIT_CHECK_INTERVAL_SECONDS)


def _momentum_entry_scan(broker, already_signaled_events, cooldown_until):
    """
    The actual entry-scanning pass: fetches the real Coinbase trend once,
    then checks each configured series' candidates. Extracted into its own
    function so momentum_loop can throttle how often this (relatively slow,
    many-request) pass runs, independently of how often exit-checks run.
    """
    try:
        btc_trend = coinbase_data.get_btc_trend(lookback_minutes=config.MOMENTUM_BTC_LOOKBACK_MINUTES)
    except Exception as e:
        print(f"[warn] Coinbase BTC trend fetch failed, skipping this cycle: {e}")
        btc_trend = None

    for series in config.CRYPTO_SERIES:
        try:
            markets = kmd.get_markets(series, status="open", limit=MOMENTUM_STRIKE_LADDER_FETCH_LIMIT)
        except Exception as e:
            print(f"[warn] couldn't fetch markets for {series}: {e}")
            continue
        if not markets or btc_trend is None:
            continue

        if series in LADDER_SERIES:
            candidates = _select_strike_candidates(markets, btc_trend["current_price"])
            if not candidates:
                print(f"[momentum diag] {series}: no parseable strike found in {len(markets)} markets, skipping")
                continue
        else:
            # simple series (e.g. KXBTC15M): exactly one market per window,
            # no strike to choose -- every open market is a direct candidate
            candidates = markets

        # the trend signal doesn't depend on which specific strike we're
        # looking at -- check it once, not once per candidate
        sig, diag = signals.detect_btc_trend_signal(btc_trend)
        if diag.get("reason") != "signal_fired":
            if diag.get("pct_change") is not None:
                print(f"[momentum diag] {series}: coinbase_pct_change={diag['pct_change']*100:+.3f}% "
                      f"(threshold {config.MOMENTUM_BTC_TREND_THRESHOLD_PCT*100:.2f}%) "
                      f"direction={diag.get('direction')} reason={diag['reason']}")
            else:
                print(f"[momentum diag] {series}: reason={diag['reason']}")
            continue

        # signal fired -- try each candidate strike nearest-to-farthest, falling
        # through to the next one on ANY failure (already held, cooling down, thin
        # liquidity, or blocked by try_open_position's own authoritative check --
        # not just the cheap local pre-filter, which could in principle desync
        # from the real broker state). Stops at the first successful open.
        opened_position = None
        attempted_any = False
        for candidate in candidates:
            cand_ticker = candidate["ticker"]
            cand_event = _momentum_event_key(cand_ticker)
            if cand_ticker in already_signaled_events:
                continue
            if cand_event in cooldown_until and time.time() < cooldown_until[cand_event]:
                continue
            attempted_any = True

            window_key = _derive_match_key(cand_ticker)
            in_window_count = _count_open_positions_in_window(broker, window_key)
            if in_window_count >= config.MOMENTUM_MAX_POSITIONS_PER_WINDOW:
                print(f"[momentum diag] {cand_ticker}: window {window_key} already has "
                      f"{in_window_count} position(s) (max {config.MOMENTUM_MAX_POSITIONS_PER_WINDOW}), "
                      f"trying next candidate")
                continue

            try:
                ob = kmd.get_orderbook(cand_ticker)
                available = ob["best_yes_ask_size"] if sig.direction == "yes" else ob["best_no_ask_size"]
            except Exception as e:
                print(f"[warn] orderbook check failed for {cand_ticker}, trying next candidate: {e}")
                continue
            if available < config.MOMENTUM_MIN_LIQUIDITY_CONTRACTS:
                print(f"[momentum diag] {cand_ticker}: signal fired but liquidity too thin "
                      f"({available:.0f} < {config.MOMENTUM_MIN_LIQUIDITY_CONTRACTS} contracts), trying next candidate")
                continue

            now = int(time.time())
            start_ts = now - MOMENTUM_CANDLE_LOOKBACK_MINUTES * 60
            try:
                candles = kmd.get_candlesticks(series, cand_ticker, start_ts, now, period_interval=1)
            except Exception as e:
                print(f"[warn] candlesticks failed for {cand_ticker}: {e}")
                continue

            current_price = candles[-1]["price_cents"] if candles else 50.0
            side_price = current_price if sig.direction == "yes" else (100 - current_price)
            win_prob = 0.5 + (sig.strength * 0.15)
            size = risk.position_size_dollars(
                broker.balance, win_prob,
                config.MOMENTUM_TAKE_PROFIT_CENTS, config.MOMENTUM_STOP_LOSS_CENTS,
                side_price_cents=side_price, market_title=candidate.get("title", cand_ticker),
            )
            size = _apply_depth_cap(cand_ticker, sig.direction, side_price, size)
            if size <= 0:
                continue
            if size < config.MOMENTUM_MIN_TRADE_SIZE_DOLLARS:
                # Skip, don't inflate -- a tiny Kelly-sized trade reflects
                # genuinely low confidence in this specific signal, and
                # forcing it up to some minimum would distort that on
                # purpose. Momentum-specific: tennis (value_entry/
                # favorite_entry/reversion) has shown real net gains
                # including its own small trades, so left completely
                # untouched rather than risk disrupting what's working there.
                print(f"[momentum diag] {cand_ticker}: signal fired but size (${size:.2f}) is below "
                      f"the ${config.MOMENTUM_MIN_TRADE_SIZE_DOLLARS:.2f} minimum, trying next candidate")
                continue

            opened = broker.try_open_position(cand_ticker, cand_event, sig.direction, current_price, size,
                                               sig.reason, strategy="momentum", market_title=candidate.get("title", cand_ticker))
            if opened:
                already_signaled_events.add(cand_ticker)
                opened_position = opened
                break  # successfully opened -- stop trying further candidates this cycle

        if sig and opened_position is None and not attempted_any:
            # every candidate was blocked by the cheap local pre-filter before
            # even being attempted -- explicit diagnostic instead of the silent
            # skip that hid this for hours on Sep 21
            print(f"[momentum diag] {series}: signal fired but all {len(candidates)} "
                  f"candidate strikes already held or cooling down, skipping this cycle")


def run():
    """Entry point: one broker, two loops in separate threads."""
    if config.ENVIRONMENT == "backtest":
        print("ENVIRONMENT is 'backtest' -- run backtest.py instead, this script needs live/paper data.")
        sys.exit(1)

    broker = execution.get_broker()
    print(f"[combined bot] Started in {config.ENVIRONMENT} mode. Balance: ${broker.balance:.2f}")

    if threading.current_thread() is threading.main_thread():
        os_signal.signal(os_signal.SIGINT, _handle_shutdown)

    tennis_thread = threading.Thread(target=tennis_loop, args=(broker,), daemon=True, name="tennis_loop")
    momentum_thread = threading.Thread(target=momentum_loop, args=(broker,), daemon=True, name="momentum_loop")
    tennis_thread.start()
    momentum_thread.start()

    # main thread just waits for shutdown -- the two loops do the real work
    while _running:
        time.sleep(1)

    print(f"Stopped. Final balance: ${broker.balance:.2f}, open positions: {broker.get_open_position_count()}")
    broker.shutdown()


if __name__ == "__main__":
    run()
