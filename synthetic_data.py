"""
Generates synthetic candle/trade data with KNOWN embedded events (a momentum
breakout, a tennis overreaction-then-reversion) so we can check the signal
detectors actually fire on the pattern they're designed for, and stay quiet
on plain noise. This replaces the real backtest we couldn't run against the
flat demo data.

Every generator returns (data, ground_truth) where ground_truth tells you
exactly where the planted event is, so you can check the detector against it.
"""

import random
from typing import List, Tuple


def generate_crypto_candles(
    minutes: int = 60,
    base_price_cents: float = 50.0,
    base_volume: float = 100.0,
    plant_momentum_event_at: int = 40,
    seed: int = 42,
) -> Tuple[List[dict], dict]:
    """
    60 minutes of 1-min candles. Mostly flat/noisy, with one planted
    volume-spike + price-move event at `plant_momentum_event_at` (minutes in).
    """
    rng = random.Random(seed)
    candles = []
    price = base_price_cents

    for m in range(minutes):
        if plant_momentum_event_at <= m < plant_momentum_event_at + 5:
            # planted event: volume spikes ~5x, price walks up ~3c over 5 minutes
            volume = base_volume * rng.uniform(4.5, 6.0)
            price += rng.uniform(0.4, 0.9)
        else:
            volume = base_volume * rng.uniform(0.3, 1.3)
            price += rng.uniform(-0.15, 0.15)

        candles.append({"ts": m * 60, "price_cents": round(price, 2), "volume": round(volume, 2)})

    ground_truth = {
        "event_type": "momentum_breakout",
        "event_start_minute": plant_momentum_event_at,
        "event_end_minute": plant_momentum_event_at + 5,
        "expected_direction": "yes",  # price walked up
    }
    return candles, ground_truth


def generate_tennis_trades(
    duration_seconds: int = 1800,
    base_price_cents: float = 57.0,
    trade_interval_seconds: int = 20,
    plant_spike_at_second: int = 900,
    seed: int = 7,
) -> Tuple[List[dict], dict]:
    """
    30 minutes of trades every ~20s. Mostly flat around base_price_cents,
    with one planted spike (up 18c) that partially reverts over the following
    ~4 minutes -- the exact pattern the reversion detector should catch.
    """
    rng = random.Random(seed)
    trades = []
    price = base_price_cents
    t = 0

    spike_peak_price = base_price_cents + 18
    reverted_price = base_price_cents + 6  # reverts most of the way, not all

    while t < duration_seconds:
        if plant_spike_at_second <= t < plant_spike_at_second + 60:
            # spike up sharply over ~1 minute
            progress = (t - plant_spike_at_second) / 60
            price = base_price_cents + 18 * progress
        elif plant_spike_at_second + 60 <= t < plant_spike_at_second + 300:
            # partial reversion over the next 4 minutes
            progress = (t - (plant_spike_at_second + 60)) / 240
            price = spike_peak_price - (spike_peak_price - reverted_price) * progress
        elif t >= plant_spike_at_second + 300:
            price = reverted_price + rng.uniform(-0.5, 0.5)
        else:
            price = base_price_cents + rng.uniform(-0.5, 0.5)

        trades.append({"ts": t, "yes_price_cents": round(price, 2)})
        t += trade_interval_seconds + rng.randint(-5, 5)

    ground_truth = {
        "event_type": "mean_reversion",
        "spike_start_second": plant_spike_at_second,
        "spike_peak_second": plant_spike_at_second + 60,
        "reverted_by_second": plant_spike_at_second + 300,
        "expected_direction": "no",  # spike was up, we fade to NO
    }
    return trades, ground_truth
