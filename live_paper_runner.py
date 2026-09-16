"""
Main run loop. Polls real Kalshi production market data, runs three signal
detectors with per-event debounce, sizes positions via risk.py, executes
through whatever broker config.ENVIRONMENT resolves to, and monitors open
positions for take-profit/stop-loss exits every cycle.

Run: python live_paper_runner.py
Stop any time with Ctrl+C -- open paper positions just stay open in the log,
nothing bad happens.
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

POLL_INTERVAL_SECONDS = 60
CANDLE_LOOKBACK_MINUTES = 14  # KXBTC15M markets only live 15 min total; need 8 (6+2) for the detector, pad a bit

_running = True

# Live status snapshot, updated once per cycle. server.py's HTTP handler reads
# this directly -- exposing real-time bot state over HTTP without depending
# on the database at all, which sidesteps Render's broken query tool entirely
# for anything that doesn't need trade history specifically.
latest_status = {"balance": None, "open_positions": None, "last_cycle_ts": None, "environment": None}


def _handle_shutdown(sig, frame):
    global _running
    print("\nShutting down after current cycle...")
    _running = False


def _market_age_seconds(trades: list) -> float:
    """Age of the market based on its earliest observed trade in this sample."""
    if not trades:
        return float("inf")
    return time.time() - trades[0]["ts"]


def _derive_match_key(ticker: str) -> str:
    """
    Derives a shared key for "the same underlying match/window" directly
    from the ticker string, rather than trusting Kalshi's own event_ticker
    field (which we found doesn't reliably group paired per-player markets
    for the same match -- e.g. KXWTAMATCH-26SEP15SEMKIN-SEM and
    KXWTAMATCH-26SEP15SEMKIN-KIN are TWO SEPARATE Kalshi markets for the
    same match, one per player, and their event_ticker values didn't
    correctly group them, letting the bot hold correlated positions on
    both "sides" simultaneously -- economically close to the same bet,
    paying fees twice for overlapping exposure).

    Strips the final "-XXX" segment (the per-player/per-strike suffix),
    keeping everything before it as the shared match/window identifier.
    """
    parts = ticker.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else ticker


def _apply_depth_cap(ticker: str, direction: str, side_price: float, size: float) -> float:
    """
    Fetches the orderbook and caps `size` to MAX_BOOK_DEPTH_FRACTION of the
    liquidity actually resting at the entry price level, so a capped-Kelly
    size doesn't walk a thin book. Fails open (returns size unchanged) if the
    orderbook fetch errors -- missing depth data shouldn't block a trade.
    """
    try:
        ob = kmd.get_orderbook(ticker)
        available = ob["best_yes_ask_size"] if direction == "yes" else ob["best_no_ask_size"]
        return risk.cap_size_by_depth(size, side_price, available)
    except Exception as e:
        print(f"[warn] orderbook depth check failed for {ticker}, sizing without a depth cap: {e}")
        return size


def run():
    if config.ENVIRONMENT == "backtest":
        print("ENVIRONMENT is 'backtest' -- run backtest.py instead, this script needs live/paper data.")
        sys.exit(1)

    broker = execution.get_broker()
    print(f"Started in {config.ENVIRONMENT} mode. Balance: ${broker.balance:.2f}")

    # debounce state: remember which events we've already signaled on, so we
    # act once per event instead of once per poll cycle. Cleared for a ticker
    # once its position closes, so the same market can be re-entered later.
    already_signaled_events: set[str] = set()

    # signal.signal() only works in the main thread -- when server.py runs
    # this inside a background thread (the Render deployment path), skip it
    # entirely. Ctrl+C handling only matters for direct/tmux use anyway.
    if threading.current_thread() is threading.main_thread():
        os_signal.signal(os_signal.SIGINT, _handle_shutdown)

    while _running:
        if risk.daily_loss_breached(broker.daily_pnl, broker.starting_balance):
            print(f"Daily loss cap breached (pnl={broker.daily_pnl:.2f}). Halting for today.")
            break

        # --- check exits first, so a closed position frees up its slot
        # before this cycle's entry logic runs ---
        for pos in broker.get_open_positions_snapshot():
            try:
                if pos.strategy == "momentum":
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
                elif pos.strategy == "reversion":
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
                else:
                    continue  # unknown/untagged position, leave it alone

                if exit_reason:
                    side_price = current_price if pos.direction == "yes" else (100 - current_price)
                    if exit_reason == "take_profit" and side_price >= config.SETTLEMENT_HOLD_THRESHOLD_CENTS:
                        # this close to certain, ride to settlement instead of paying
                        # the taker fee a second time to sell early
                        print(f"[hold] {pos.ticker} ({pos.strategy}): at {side_price:.0f}c, "
                              f"holding to settlement instead of selling at take-profit")
                        continue
                    print(f"[exit] {pos.ticker} ({pos.strategy}): {exit_reason}")
                    broker.close_position(pos.ticker, current_price)
                    already_signaled_events.discard(pos.ticker)
            except Exception as e:
                print(f"[warn] exit check failed for {pos.ticker}: {e}")

        open_event_tickers = broker.get_open_event_tickers()

        # --- Leg 1: crypto momentum ---
        # Fetched once per cycle, not per-market -- it's the same underlying
        # BTC price regardless of which KXBTC15M ticker is currently live.
        try:
            btc_trend = coinbase_data.get_btc_trend(lookback_minutes=config.MOMENTUM_CURRENT_WINDOW_MINUTES)
        except Exception as e:
            print(f"[warn] Coinbase BTC trend fetch failed, falling back to Kalshi-only direction: {e}")
            btc_trend = None
        btc_direction = btc_trend["direction"] if btc_trend else None

        for series in config.CRYPTO_SERIES:
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
                allowed, reason = risk.can_open_new_position(broker.get_open_position_count(), open_event_tickers, event_ticker)
                if not allowed:
                    continue

                now = int(time.time())
                start_ts = now - CANDLE_LOOKBACK_MINUTES * 60
                try:
                    candles = kmd.get_candlesticks(series, ticker, start_ts, now, period_interval=1)
                except Exception as e:
                    print(f"[warn] candlesticks failed for {ticker}: {e}")
                    continue

                sig = signals.detect_momentum_signal(candles, btc_direction=btc_direction)
                if sig:
                    already_signaled_events.add(ticker)
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
                        broker.open_position(ticker, event_ticker, sig.direction, current_price, size, sig.reason, strategy="momentum", market_title=m.get("title", ticker))

        # --- Leg 2: tennis mean reversion, Leg 3: tennis value entry ---
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
                allowed, reason = risk.can_open_new_position(broker.get_open_position_count(), open_event_tickers, event_ticker)
                if not allowed:
                    continue

                try:
                    trades = kmd.get_recent_trades(ticker, limit=100)
                except Exception as e:
                    print(f"[warn] trades failed for {ticker}: {e}")
                    continue
                if not trades:
                    continue

                # Leg 2: reversion
                sig = signals.detect_reversion_signal(trades, trades[-1]["ts"])
                if sig:
                    already_signaled_events.add(ticker)
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
                        broker.open_position(ticker, event_ticker, sig.direction, current_price, size, sig.reason, strategy="reversion", market_title=m.get("title", ticker))
                    continue  # don't also try leg 3 on a market we just entered via leg 2

                # Leg 3: value entry (early match, buy the cheap side)
                age = _market_age_seconds(trades)
                sig = signals.detect_value_entry_signal(trades, age, market_title=m.get("title"))
                if sig:
                    already_signaled_events.add(ticker)
                    current_price = trades[-1]["yes_price_cents"]
                    side_price = current_price if sig.direction == "yes" else (100 - current_price)
                    # flat assumed edge for a value bet -- calibrate from paper_prod win rate
                    win_prob = 0.55
                    win_cents = side_price * config.VALUE_ENTRY_TAKE_PROFIT_MIN_PCT
                    loss_cents = side_price * config.VALUE_ENTRY_STOP_LOSS_PCT
                    size = risk.position_size_dollars(broker.balance, win_prob, win_cents, loss_cents, side_price_cents=side_price, market_title=m.get("title", ticker))
                    size = _apply_depth_cap(ticker, sig.direction, side_price, size)
                    if size > 0:
                        broker.open_position(ticker, event_ticker, sig.direction, current_price, size, sig.reason, strategy="value_entry", market_title=m.get("title", ticker))

        open_count = broker.get_open_position_count()
        print(f"[cycle done] balance=${broker.balance:.2f} open_positions={open_count}")
        latest_status["balance"] = round(broker.balance, 2)
        latest_status["open_positions"] = open_count
        latest_status["last_cycle_ts"] = time.time()
        latest_status["environment"] = config.ENVIRONMENT
        time.sleep(POLL_INTERVAL_SECONDS)

    print(f"Stopped. Final balance: ${broker.balance:.2f}, open positions: {broker.get_open_position_count()}")
    broker.shutdown()


if __name__ == "__main__":
    run()
