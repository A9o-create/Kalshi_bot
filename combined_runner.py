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

import config
import signals
import risk
import execution
import kalshi_market_data as kmd
import coinbase_data

TENNIS_POLL_INTERVAL_SECONDS = 60
MOMENTUM_POLL_INTERVAL_SECONDS = 60
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
    """
    parts = ticker.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else ticker


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


def _select_nearest_strike_market(markets: list, spot_price_dollars: float):
    """
    KXBTCD lists multiple simultaneous 'above $X' threshold markets for the
    same close time -- unlike KXBTC15M, which was a single market per
    window. Picks whichever strike sits closest to current spot price as
    the best available proxy for "will price be up or down from here,"
    the same bet KXBTC15M represented directly.

    Known, deliberately unresolved tradeoff: the nearest-to-spot strike is
    also the one closest to 50c -- Kalshi's highest-fee price zone (fee
    peaks at a 50c price, per the verified fee formula). Nearest-strike is
    the most economically honest translation of the trend signal, but not
    necessarily the most fee-efficient choice. Flagged, not fixed here.
    """
    best_market = None
    best_distance = None
    for m in markets:
        threshold = _parse_strike_threshold(m["ticker"])
        if threshold is None:
            continue
        distance = abs(threshold - spot_price_dollars)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_market = m
    return best_market


def tennis_loop(broker):
    """Mean reversion + value entry. Runs in its own thread against the shared broker."""
    already_signaled_events: set[str] = set()
    cooldown_until: dict[str, float] = {}

    while _running:
        for pos in broker.get_open_positions_snapshot():
            if pos.strategy not in ("reversion", "value_entry"):
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
                else:  # value_entry
                    trades = kmd.get_recent_trades(pos.ticker, limit=5)
                    if not trades:
                        continue
                    current_price = trades[-1]["yes_price_cents"]
                    exit_reason = risk.check_value_entry_exit(pos.direction, pos.entry_price_cents, current_price)

                if exit_reason:
                    side_price = current_price if pos.direction == "yes" else (100 - current_price)
                    if exit_reason == "take_profit" and side_price >= config.SETTLEMENT_HOLD_THRESHOLD_CENTS:
                        print(f"[hold] {pos.ticker} ({pos.strategy}): at {side_price:.0f}c, holding to settlement")
                        continue
                    print(f"[exit] {pos.ticker} ({pos.strategy}): {exit_reason}")
                    broker.close_position(pos.ticker, current_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
            except Exception as e:
                print(f"[warn] tennis exit check failed for {pos.ticker}: {e}")

        for series in config.TENNIS_SERIES:
            try:
                markets = kmd.get_markets(series, status="open", limit=config.MAX_MARKETS_PER_SERIES)
            except Exception as e:
                print(f"[warn] couldn't fetch markets for {series}: {e}")
                continue

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

        open_count = broker.get_open_position_count()
        print(f"[tennis cycle done] balance=${broker.balance:.2f} open_positions={open_count}")
        latest_status["balance"] = round(broker.balance, 2)
        latest_status["open_positions"] = open_count
        latest_status["last_cycle_ts"] = time.time()
        latest_status["environment"] = config.ENVIRONMENT
        latest_status["tennis"]["last_cycle_ts"] = time.time()
        time.sleep(TENNIS_POLL_INTERVAL_SECONDS)


def momentum_loop(broker):
    """BTC momentum. Runs in its own thread against the shared broker."""
    already_signaled_events: set[str] = set()
    cooldown_until: dict[str, float] = {}

    while _running:
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
                if exit_reason:
                    side_price = current_price if pos.direction == "yes" else (100 - current_price)
                    if exit_reason == "take_profit" and side_price >= config.SETTLEMENT_HOLD_THRESHOLD_CENTS:
                        print(f"[hold] {pos.ticker} ({pos.strategy}): at {side_price:.0f}c, holding to settlement")
                        continue
                    print(f"[exit] {pos.ticker} ({pos.strategy}): {exit_reason}")
                    broker.close_position(pos.ticker, current_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
            except Exception as e:
                print(f"[warn] momentum exit check failed for {pos.ticker}: {e}")

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

            m = _select_nearest_strike_market(markets, btc_trend["current_price"])
            if m is None:
                print(f"[momentum diag] {series}: no parseable strike found in {len(markets)} markets, skipping")
                continue

            ticker = m["ticker"]
            event_ticker = _derive_match_key(ticker)
            if ticker in already_signaled_events:
                continue
            if event_ticker in cooldown_until and time.time() < cooldown_until[event_ticker]:
                continue

            now = int(time.time())
            start_ts = now - MOMENTUM_CANDLE_LOOKBACK_MINUTES * 60
            try:
                candles = kmd.get_candlesticks(series, ticker, start_ts, now, period_interval=1)
            except Exception as e:
                print(f"[warn] candlesticks failed for {ticker}: {e}")
                continue

            sig, diag = signals.detect_btc_trend_signal(btc_trend)
            if diag.get("reason") != "signal_fired":
                if diag.get("pct_change") is not None:
                    print(f"[momentum diag] {ticker}: coinbase_pct_change={diag['pct_change']*100:+.3f}% "
                          f"(threshold {config.MOMENTUM_BTC_TREND_THRESHOLD_PCT*100:.2f}%) "
                          f"direction={diag.get('direction')} reason={diag['reason']}")
                else:
                    print(f"[momentum diag] {ticker}: reason={diag['reason']}")
            if sig:
                try:
                    ob = kmd.get_orderbook(ticker)
                    available = ob["best_yes_ask_size"] if sig.direction == "yes" else ob["best_no_ask_size"]
                except Exception as e:
                    print(f"[warn] orderbook check failed for {ticker}, skipping momentum entry: {e}")
                    continue
                if available < config.MOMENTUM_MIN_LIQUIDITY_CONTRACTS:
                    print(f"[momentum diag] {ticker}: signal fired but liquidity too thin "
                          f"({available:.0f} < {config.MOMENTUM_MIN_LIQUIDITY_CONTRACTS} contracts), skipping")
                    continue

                current_price = candles[-1]["price_cents"] if candles else 50.0
                side_price = current_price if sig.direction == "yes" else (100 - current_price)
                win_prob = 0.5 + (sig.strength * 0.15)
                size = risk.position_size_dollars(
                    broker.balance, win_prob,
                    config.MOMENTUM_TAKE_PROFIT_CENTS, config.MOMENTUM_STOP_LOSS_CENTS,
                    side_price_cents=side_price, market_title=m.get("title", ticker),
                )
                size = _apply_depth_cap(ticker, sig.direction, side_price, size)
                if size > 0:
                    opened = broker.try_open_position(ticker, event_ticker, sig.direction, current_price, size,
                                                       sig.reason, strategy="momentum", market_title=m.get("title", ticker))
                    if opened:
                        already_signaled_events.add(ticker)

        open_count = broker.get_open_position_count()
        print(f"[momentum cycle done] balance=${broker.balance:.2f} open_positions={open_count}")
        latest_status["balance"] = round(broker.balance, 2)
        latest_status["open_positions"] = open_count
        latest_status["last_cycle_ts"] = time.time()
        latest_status["environment"] = config.ENVIRONMENT
        latest_status["momentum"]["last_cycle_ts"] = time.time()
        time.sleep(MOMENTUM_POLL_INTERVAL_SECONDS)


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
