"""
Standalone momentum (BTC/KXBTC15M) run loop -- split out from
live_paper_runner.py so this leg can be redeployed, tuned, and restarted
without touching the tennis legs' running state at all. Every redeploy
resets the broker's in-memory balance/positions; before this split, a
BTC-only change (like the trend-trigger redesign) meant losing whatever the
tennis legs had accumulated too, purely as a side effect of sharing one
process. This bot only ever trades KXBTC15M, so it needs its own PaperBroker
with its own independent starting balance -- not a slice of a shared pool.

Run: python momentum_runner.py
Stop any time with Ctrl+C -- an open paper position just stays open in the
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

POLL_INTERVAL_SECONDS = 60
CANDLE_LOOKBACK_MINUTES = 14  # KXBTC15M markets only live 15 min total; need 8 (6+2) for the detector, pad a bit

_running = True

# Live status snapshot, updated once per cycle -- momentum_server.py's HTTP
# handler reads this directly, same pattern as the tennis bot's server.py.
latest_status = {"balance": None, "open_positions": None, "last_cycle_ts": None, "environment": None}


def _handle_shutdown(sig, frame):
    global _running
    print("\nShutting down after current cycle...")
    _running = False


def _derive_match_key(ticker: str) -> str:
    """
    Same logic as the tennis bot's version -- kept here rather than shared,
    since these two files are now deliberately independent deployments.
    For KXBTC15M this mostly just strips the strike/window suffix; the
    paired-market scenario it was built for is a tennis-specific problem,
    but applying it here costs nothing and keeps the pattern consistent.
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
    print(f"[momentum bot] Started in {config.ENVIRONMENT} mode. Balance: ${broker.balance:.2f}")

    already_signaled_events: set[str] = set()
    cooldown_until: dict[str, float] = {}

    if threading.current_thread() is threading.main_thread():
        os_signal.signal(os_signal.SIGINT, _handle_shutdown)

    while _running:
        if risk.daily_loss_breached(broker.daily_pnl, broker.starting_balance):
            print(f"Daily loss cap breached (pnl={broker.daily_pnl:.2f}). Halting for today.")
            break

        # --- check exits first, so a closed position frees up its slot
        # before this cycle's entry logic runs. This broker only ever
        # holds momentum positions, so no strategy dispatch needed. ---
        for pos in broker.get_open_positions_snapshot():
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
                        print(f"[hold] {pos.ticker} ({pos.strategy}): at {side_price:.0f}c, "
                              f"holding to settlement instead of selling at take-profit")
                        continue
                    print(f"[exit] {pos.ticker} ({pos.strategy}): {exit_reason}")
                    broker.close_position(pos.ticker, current_price)
                    already_signaled_events.discard(pos.ticker)
                    cooldown_until[pos.event_ticker] = time.time() + config.REENTRY_COOLDOWN_SECONDS
            except Exception as e:
                print(f"[warn] exit check failed for {pos.ticker}: {e}")

        open_event_tickers = broker.get_open_event_tickers()

        try:
            btc_trend = coinbase_data.get_btc_trend(lookback_minutes=config.MOMENTUM_BTC_LOOKBACK_MINUTES)
        except Exception as e:
            print(f"[warn] Coinbase BTC trend fetch failed, skipping this cycle: {e}")
            btc_trend = None

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
                if event_ticker in cooldown_until and time.time() < cooldown_until[event_ticker]:
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
