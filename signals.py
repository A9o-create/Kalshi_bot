"""
Pure signal-detection logic. No network calls, no state beyond what's passed in.
This is the part we validate with synthetic data before ever touching real markets.

Candle format expected (matches Kalshi's 1-min candlestick shape, simplified):
    {"ts": int, "price_cents": float, "volume": float}

Trade format expected (matches Kalshi's trade shape, simplified):
    {"ts": int, "yes_price_cents": float}
"""

from dataclasses import dataclass
from typing import Optional, List
import config


@dataclass
class Signal:
    direction: str      # "yes" or "no"
    reason: str          # human-readable explanation, for logging/debugging
    strength: float = 1.0  # 0-1, how strong the signal is (used for Kelly edge estimate)


def detect_momentum_signal(candles: List[dict]) -> Optional[Signal]:
    """
    Leg 1: volume spike + price move together, in the same direction.
    `candles` should be sorted oldest -> newest, at 1-min resolution, covering
    at least the last 35 minutes (30 min trailing average + 5 min current window).
    """
    if len(candles) < 35:
        return None

    current_window = candles[-5:]
    trailing_window = candles[-35:-5]

    current_volume = sum(c["volume"] for c in current_window)
    trailing_avg_volume = sum(c["volume"] for c in trailing_window) / len(trailing_window)

    if trailing_avg_volume <= 0:
        return None

    volume_ratio = current_volume / trailing_avg_volume
    price_move = current_window[-1]["price_cents"] - current_window[0]["price_cents"]

    volume_spike = volume_ratio >= config.MOMENTUM_VOLUME_SPIKE_MULTIPLE
    price_moved_enough = abs(price_move) >= config.MOMENTUM_PRICE_MOVE_CENTS

    if volume_spike and price_moved_enough:
        direction = "yes" if price_move > 0 else "no"
        strength = min(1.0, (volume_ratio / config.MOMENTUM_VOLUME_SPIKE_MULTIPLE) *
                       (abs(price_move) / config.MOMENTUM_PRICE_MOVE_CENTS) / 2)
        return Signal(
            direction=direction,
            reason=f"volume {volume_ratio:.1f}x trailing avg, price moved {price_move:+.1f}c in 5min",
            strength=strength,
        )
    return None


def detect_reversion_signal(trades: List[dict], current_ts: int) -> Optional[Signal]:
    """
    Leg 2: fade a sharp price spike once the first sign of pullback appears.
    `trades` sorted oldest -> newest. `current_ts` is "now" for window math.

    This is a two-stage detector:
      1. Find a spike >= REVERSION_SPIKE_THRESHOLD_CENTS within REVERSION_SPIKE_WINDOW_SECONDS
      2. Confirm a pullback of >= REVERSION_CONFIRM_PULLBACK_CENTS within
         REVERSION_CONFIRM_WINDOW_SECONDS after the spike peak
    """
    if len(trades) < 2:
        return None

    spike_window_start = current_ts - config.REVERSION_SPIKE_WINDOW_SECONDS - config.REVERSION_CONFIRM_WINDOW_SECONDS
    relevant = [t for t in trades if t["ts"] >= spike_window_start]
    if len(relevant) < 2:
        return None

    # Find the largest spike: scan for (low, high) pairs within the spike window
    best_spike = None  # (start_price, peak_price, peak_ts, direction)
    for i, t in enumerate(relevant):
        window_end_ts = t["ts"] + config.REVERSION_SPIKE_WINDOW_SECONDS
        window = [x for x in relevant[i:] if x["ts"] <= window_end_ts]
        if len(window) < 2:
            continue
        prices = [x["yes_price_cents"] for x in window]
        move_up = max(prices) - t["yes_price_cents"]
        move_down = t["yes_price_cents"] - min(prices)
        if move_up >= config.REVERSION_SPIKE_THRESHOLD_CENTS:
            peak_idx = prices.index(max(prices))
            candidate = (t["yes_price_cents"], max(prices), window[peak_idx]["ts"], "up")
            if best_spike is None or move_up > (best_spike[1] - best_spike[0]):
                best_spike = candidate
        if move_down >= config.REVERSION_SPIKE_THRESHOLD_CENTS:
            trough_idx = prices.index(min(prices))
            candidate = (t["yes_price_cents"], min(prices), window[trough_idx]["ts"], "down")
            if best_spike is None or move_down > abs(best_spike[0] - best_spike[1]):
                best_spike = candidate

    if best_spike is None:
        return None

    start_price, peak_price, peak_ts, spike_dir = best_spike
    after_peak = [t for t in relevant if peak_ts < t["ts"] <= peak_ts + config.REVERSION_CONFIRM_WINDOW_SECONDS]
    if not after_peak:
        return None

    latest_price = after_peak[-1]["yes_price_cents"]

    if spike_dir == "up":
        pullback = peak_price - latest_price
        if pullback >= config.REVERSION_CONFIRM_PULLBACK_CENTS:
            # spike was up, we're fading it -> buy NO
            return Signal(
                direction="no",
                reason=f"spiked +{peak_price - start_price:.1f}c, pulled back {pullback:.1f}c, fading to NO",
                strength=min(1.0, pullback / config.REVERSION_CONFIRM_PULLBACK_CENTS / 2),
            )
    else:
        pullback = latest_price - peak_price
        if pullback >= config.REVERSION_CONFIRM_PULLBACK_CENTS:
            # spike was down, we're fading it -> buy YES
            return Signal(
                direction="yes",
                reason=f"dropped -{start_price - peak_price:.1f}c, bounced {pullback:.1f}c, fading to YES",
                strength=min(1.0, pullback / config.REVERSION_CONFIRM_PULLBACK_CENTS / 2),
            )
    return None


def detect_value_entry_signal(trades: List[dict], market_age_seconds: float) -> Optional[Signal]:
    """
    Leg 3: buy whichever side (yes/no) is cheaper, but only near the start of
    the market's life (proxy for "beginning of the match" -- Kalshi doesn't
    expose match clock/score). Exit is percentage-based, handled separately
    by risk.check_value_entry_exit since it depends on the open position,
    not just the trade tape.
    """
    if not trades:
        return None
    if market_age_seconds > config.VALUE_ENTRY_MAX_MARKET_AGE_MINUTES * 60:
        return None

    yes_price = trades[-1]["yes_price_cents"]
    if yes_price <= 0 or yes_price >= 100:
        return None  # degenerate price, nothing to trade

    if yes_price <= 50:
        return Signal(direction="yes", reason=f"early value entry: yes cheap at {yes_price:.0f}c")
    else:
        no_price = 100 - yes_price
        return Signal(direction="no", reason=f"early value entry: no cheap at {no_price:.0f}c")
