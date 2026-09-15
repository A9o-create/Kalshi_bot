"""
Real BTC price data from Coinbase's public Exchange API -- independent of
Kalshi's own KXBTC15M order book. Verified against Coinbase's own docs
(docs.cdp.coinbase.com): GET /products/{product_id}/candles, no auth
required, rows as [timestamp, low, high, open, close, volume].

This exists because KXBTC15M resolves against CF Benchmarks' BRTI (an
external index), not Kalshi's own order flow -- and a 15-minute market's
internal book is often thin. This gives the bot a read on the actual thing
the contract resolves against, rather than inferring direction from a
noisy, low-volume proxy.
"""

import time
import requests

COINBASE_BASE_URL = "https://api.exchange.coinbase.com"


def get_btc_trend(lookback_minutes: int = 2) -> dict:
    """
    Fetches 1-min BTC-USD candles over the last `lookback_minutes` and
    returns the real price trend. Returns None on any failure (network,
    malformed response, insufficient data) -- callers should treat that as
    "no independent read available" and act conservatively, not guess.
    """
    try:
        end = int(time.time())
        start = end - lookback_minutes * 60 - 60  # pad by one extra candle
        resp = requests.get(
            f"{COINBASE_BASE_URL}/products/BTC-USD/candles",
            params={"start": start, "end": end, "granularity": 60},
            timeout=10,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not isinstance(candles, list) or len(candles) < 2:
            return None

        # don't trust the API's default ordering -- sort explicitly by timestamp
        candles_sorted = sorted(candles, key=lambda c: c[0])
        start_price = float(candles_sorted[0][4])   # close of oldest candle in window
        current_price = float(candles_sorted[-1][4])  # close of most recent candle
        if start_price <= 0:
            return None

        pct_change = (current_price - start_price) / start_price
        if pct_change > 0:
            direction = "yes"
        elif pct_change < 0:
            direction = "no"
        else:
            direction = None  # perfectly flat -- no call to make

        return {
            "start_price": start_price,
            "current_price": current_price,
            "pct_change": pct_change,
            "direction": direction,
        }
    except Exception:
        return None
