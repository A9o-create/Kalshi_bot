"""
Validates signal logic against synthetic data with a known planted event.
This is the step we substituted for a real backtest, since the demo Kalshi
instance had zero real price movement to test against.

Run: python backtest.py
"""

import synthetic_data
import signals


def backtest_momentum():
    print("=" * 60)
    print("LEG 1: Crypto Momentum")
    print("=" * 60)

    candles, truth = synthetic_data.generate_crypto_candles()
    fired_minutes = []

    # Slide a 35-minute window across the data, same as live would see it minute-by-minute
    for end in range(35, len(candles) + 1):
        window = candles[:end]
        sig = signals.detect_momentum_signal(window)
        if sig:
            fired_minutes.append((end - 1, sig))  # minute index the signal fired on

    event_start, event_end = truth["event_start_minute"], truth["event_end_minute"]
    hits = [(m, s) for m, s in fired_minutes if event_start <= m <= event_end + 5]
    false_positives = [(m, s) for m, s in fired_minutes if not (event_start <= m <= event_end + 5)]

    print(f"Planted event: minutes {event_start}-{event_end}, expected direction = {truth['expected_direction']}")
    print(f"Signal fired {len(fired_minutes)} time(s) total")
    print(f"  -> {len(hits)} within/near the planted event window (true positives)")
    print(f"  -> {len(false_positives)} outside it (false positives)")
    if hits:
        m, s = hits[0]
        correct_direction = s.direction == truth["expected_direction"]
        print(f"  First hit at minute {m}: direction={s.direction} ({'correct' if correct_direction else 'WRONG'}), reason: {s.reason}")
    print()


def backtest_reversion():
    print("=" * 60)
    print("LEG 2: Tennis Mean Reversion")
    print("=" * 60)

    trades, truth = synthetic_data.generate_tennis_trades()
    fired = []

    # Check the detector at each trade timestamp, as if running live tick-by-tick
    for i in range(1, len(trades)):
        current_ts = trades[i]["ts"]
        sig = signals.detect_reversion_signal(trades[: i + 1], current_ts)
        if sig:
            fired.append((current_ts, sig))

    spike_start = truth["spike_start_second"]
    reverted_by = truth["reverted_by_second"]
    hits = [(t, s) for t, s in fired if spike_start <= t <= reverted_by + 60]
    false_positives = [(t, s) for t, s in fired if not (spike_start <= t <= reverted_by + 60)]

    print(f"Planted event: spike at {spike_start}s, reverts by {reverted_by}s, expected direction = {truth['expected_direction']}")
    print(f"Signal fired {len(fired)} time(s) total")
    print(f"  -> {len(hits)} within/near the planted event window (true positives)")
    print(f"  -> {len(false_positives)} outside it (false positives)")
    if hits:
        t, s = hits[0]
        correct_direction = s.direction == truth["expected_direction"]
        print(f"  First hit at t={t}s: direction={s.direction} ({'correct' if correct_direction else 'WRONG'}), reason: {s.reason}")
    print()


if __name__ == "__main__":
    backtest_momentum()
    backtest_reversion()
    print("Note: this validates detector LOGIC against a known synthetic pattern.")
    print("It does not tell you whether these thresholds are well-tuned for real")
    print("market behavior -- that's what paper_prod mode is for.")
