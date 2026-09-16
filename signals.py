"""
Pure signal-detection logic. No network calls, no state beyond what's passed in.
This is the part we validate with synthetic data before ever touching real markets.

Candle format expected (matches Kalshi's 1-min candlestick shape, simplified):
    {"ts": int, "price_cents": float, "volume": float}

Trade format expected (matches Kalshi's trade shape, simplified):
    {"ts": int, "yes_price_cents": float}
"""

from dataclasses import dataclass
import re
from typing import Optional, List
import config


@dataclass
class Signal:
    direction: str      # "yes" or "no"
    reason: str          # human-readable explanation, for logging/debugging
    strength: float = 1.0  # 0-1, how strong the signal is (used for Kelly edge estimate)


def detect_momentum_signal(candles: List[dict], btc_direction: Optional[str] = None):
    """
    Leg 1: volume spike + price move together, in the same direction.
    `candles` should be sorted oldest -> newest, at 1-min resolution, covering
    at least MOMENTUM_TRAILING_WINDOW_MINUTES + MOMENTUM_CURRENT_WINDOW_MINUTES.

    `btc_direction`: an independent read on real BTC price direction (from
    Coinbase, not Kalshi's own order book -- see coinbase_data.py). Kalshi's
    volume-spike + price-move condition still decides WHETHER to trade
    (it's a legitimate "something's happening" trigger); when btc_direction
    is available, it decides WHICH WAY, since Kalshi's own price move on a
    15-minute market's thin book is a noisier read than the real index this
    contract actually resolves against. Falls back to Kalshi's own
    price-move direction if btc_direction is None (Coinbase unavailable) --
    this upgrades the direction call when possible rather than hard-requiring it.

    Returns (Signal | None, diagnostics: dict). Diagnostics are populated at
    EVERY return point, not just when a signal fires -- this module is
    deliberately pure (no I/O, no printing), so a caller that wants
    visibility into near-misses (real gap found Sep 16: KXBTC15M went ~10
    hours with zero fires and zero visibility into why) logs `diagnostics`
    itself rather than this function doing it.
    """
    current_n = config.MOMENTUM_CURRENT_WINDOW_MINUTES
    trailing_n = config.MOMENTUM_TRAILING_WINDOW_MINUTES
    diagnostics = {"candle_count": len(candles), "required_candles": current_n + trailing_n}

    if len(candles) < current_n + trailing_n:
        diagnostics["reason"] = "insufficient_history"
        return None, diagnostics

    current_window = candles[-current_n:]
    trailing_window = candles[-(current_n + trailing_n):-current_n]

    current_volume = sum(c["volume"] for c in current_window)
    trailing_avg_volume = sum(c["volume"] for c in trailing_window) / len(trailing_window)
    diagnostics["current_volume"] = current_volume
    diagnostics["trailing_avg_volume"] = trailing_avg_volume

    if trailing_avg_volume <= 0:
        diagnostics["reason"] = "zero_trailing_volume"
        diagnostics["volume_ratio"] = None
        return None, diagnostics

    volume_ratio = current_volume / trailing_avg_volume
    price_move = current_window[-1]["price_cents"] - current_window[0]["price_cents"]
    diagnostics["volume_ratio"] = volume_ratio
    diagnostics["price_move_cents"] = price_move

    volume_spike = volume_ratio >= config.MOMENTUM_VOLUME_SPIKE_MULTIPLE
    price_moved_enough = abs(price_move) >= config.MOMENTUM_PRICE_MOVE_CENTS
    diagnostics["volume_spike"] = volume_spike
    diagnostics["price_moved_enough"] = price_moved_enough

    if volume_spike and price_moved_enough:
        kalshi_direction = "yes" if price_move > 0 else "no"
        direction = btc_direction if btc_direction is not None else kalshi_direction
        source = "Coinbase BTC trend" if btc_direction is not None else "Kalshi price move"
        strength = min(1.0, (volume_ratio / config.MOMENTUM_VOLUME_SPIKE_MULTIPLE) *
                       (abs(price_move) / config.MOMENTUM_PRICE_MOVE_CENTS) / 2)
        diagnostics["reason"] = "signal_fired"
        sig = Signal(
            direction=direction,
            reason=(f"volume {volume_ratio:.1f}x trailing avg triggered entry, "
                    f"direction from {source} ({direction})"),
            strength=strength,
        )
        return sig, diagnostics

    if not volume_spike and not price_moved_enough:
        diagnostics["reason"] = "no_volume_spike_and_no_price_move"
    elif not volume_spike:
        diagnostics["reason"] = "no_volume_spike"
    else:
        diagnostics["reason"] = "no_price_move"
    return None, diagnostics


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


def _watchlist_player_direction(market_title: str):
    """
    Checks whether a watchlisted player appears in the market title, and
    which side of the contract actually represents her winning. Kalshi
    titles this market as "Will {Full Name} win the {A} vs {B} match?" --
    the captured subject clause may include a first name ("Will Hailey
    Baptiste win...", not just "Will Baptiste win..."), so this extracts
    that clause via regex rather than naively checking a literal prefix.

    Two passes, not one: if BOTH players in a match are watchlisted (e.g.
    Alcaraz vs Zverev), the subject match must always win regardless of
    which name happens to come first in WATCHLIST_PLAYERS -- a single-pass
    loop would incorrectly return whichever name it hits first, even if
    that name is the non-subject opponent.

    Returns (player_name, direction) or None if no watchlist player appears.
    """
    if not market_title or not config.WATCHLIST_PLAYERS:
        return None
    title_lower = market_title.lower().strip()
    subject_match = re.match(r"^will\s+(.+?)\s+win\b", title_lower)
    subject_clause = subject_match.group(1) if subject_match else ""

    for name in config.WATCHLIST_PLAYERS:
        if name.lower() in subject_clause:
            return (name, "yes")
    for name in config.WATCHLIST_PLAYERS:
        if name.lower() in title_lower:
            return (name, "no")  # mentioned, but not the subject -- she's the opponent
    return None


def detect_value_entry_signal(trades: List[dict], market_age_seconds: float, market_title: Optional[str] = None) -> Optional[Signal]:
    """
    Leg 3: buy whichever side (yes/no) is cheaper, but only near the start of
    the market's life (proxy for "beginning of the match" -- Kalshi doesn't
    expose match clock/score). Exit is percentage-based, handled separately
    by risk.check_value_entry_exit since it depends on the open position,
    not just the trade tape.

    For a watchlisted player specifically, this overrides the generic
    "whichever side is cheaper" logic: take HER side once her own win
    probability clears WATCHLIST_MIN_ENTRY_PCT, buying as low as possible
    above that floor. Below the floor she's too much of a longshot -- skip
    this market entirely rather than falling back to the generic rule,
    since the whole point is a deliberate, bounded entry on her specifically.
    """
    if not trades:
        return None
    if market_age_seconds > config.VALUE_ENTRY_MAX_MARKET_AGE_MINUTES * 60:
        return None

    yes_price = trades[-1]["yes_price_cents"]
    if yes_price <= 0 or yes_price >= 100:
        return None  # degenerate price, nothing to trade

    watchlist_match = _watchlist_player_direction(market_title) if market_title else None
    if watchlist_match:
        name, direction = watchlist_match
        her_price = yes_price if direction == "yes" else (100 - yes_price)
        if config.WATCHLIST_MIN_ENTRY_PCT < her_price <= 50:
            return Signal(
                direction=direction,
                reason=f"watchlist entry: {name} at {her_price:.0f}c (above {config.WATCHLIST_MIN_ENTRY_PCT}c floor)",
            )
        return None  # identified watchlist player, but outside the target range -- skip, don't fall back

    if yes_price <= 50:
        return Signal(direction="yes", reason=f"early value entry: yes cheap at {yes_price:.0f}c")
    else:
        no_price = 100 - yes_price
        return Signal(direction="no", reason=f"early value entry: no cheap at {no_price:.0f}c")
