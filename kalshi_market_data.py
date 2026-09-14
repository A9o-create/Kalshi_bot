"""
Read-only Kalshi market data client. These endpoints work unauthenticated
(no API key needed) -- confirmed against docs.kalshi.com, Sep 2026.

Endpoints used:
    GET /markets                                          list markets
    GET /markets/trades                                   recent trades
    GET /series/{series_ticker}/markets/{ticker}/candlesticks   OHLC candles

Requires: pip install requests
"""

import time
import requests
import config


def _base_url() -> str:
    return config.KALSHI_API_BASE_URL


def get_markets(series_ticker: str, status: str = "open", limit: int = 50) -> list[dict]:
    resp = requests.get(
        f"{_base_url()}/markets",
        params={"series_ticker": series_ticker, "status": status, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("markets", [])


def get_recent_trades(ticker: str, limit: int = 100, min_ts: int = None) -> list[dict]:
    params = {"ticker": ticker, "limit": limit}
    if min_ts is not None:
        params["min_ts"] = min_ts
    resp = requests.get(f"{_base_url()}/markets/trades", params=params, timeout=10)
    resp.raise_for_status()
    trades = resp.json().get("trades", [])
    # normalize to the {"ts": int, "yes_price_cents": float} shape signals.py expects
    normalized = []
    for t in trades:
        ts = int(time.mktime(time.strptime(t["created_time"][:19], "%Y-%m-%dT%H:%M:%S")))
        normalized.append({"ts": ts, "yes_price_cents": float(t["yes_price_dollars"]) * 100})
    return sorted(normalized, key=lambda x: x["ts"])


def get_orderbook(ticker: str) -> dict:
    """
    Kalshi's orderbook is bids-only (no authentication required): a resting
    NO bid at price Y is economically a YES ask at (100-Y), with that bid's
    size being how many YES contracts are available to buy at that price.
    Returns best prices/sizes for both sides, already converted to the
    "ask" view a taker buyer actually needs.
    """
    resp = requests.get(f"{_base_url()}/markets/{ticker}/orderbook", timeout=10)
    resp.raise_for_status()
    ob = resp.json().get("orderbook_fp", {})
    yes_bids = ob.get("yes_dollars") or []
    no_bids = ob.get("no_dollars") or []

    # best bid is the LAST element in each array (sorted worst to best)
    best_yes_bid_cents, best_yes_bid_size = (float(yes_bids[-1][0]) * 100, float(yes_bids[-1][1])) if yes_bids else (0.0, 0.0)
    best_no_bid_cents, best_no_bid_size = (float(no_bids[-1][0]) * 100, float(no_bids[-1][1])) if no_bids else (0.0, 0.0)

    return {
        "best_yes_bid_cents": best_yes_bid_cents,
        "best_yes_bid_size": best_yes_bid_size,
        "best_no_bid_cents": best_no_bid_cents,
        "best_no_bid_size": best_no_bid_size,
        # a taker buying YES is filled by resting NO bids, and vice versa
        "best_yes_ask_cents": 100.0 - best_no_bid_cents if no_bids else 100.0,
        "best_yes_ask_size": best_no_bid_size,
        "best_no_ask_cents": 100.0 - best_yes_bid_cents if yes_bids else 100.0,
        "best_no_ask_size": best_yes_bid_size,
    }


def get_candlesticks(series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1) -> list[dict]:
    resp = requests.get(
        f"{_base_url()}/series/{series_ticker}/markets/{ticker}/candlesticks",
        params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.json().get("candlesticks", [])
    # normalize to the {"ts": int, "price_cents": float, "volume": float} shape signals.py expects
    normalized = []
    for c in raw:
        price = float(c.get("yes_ask", {}).get("close_dollars", 0)) * 100
        volume = float(c.get("volume", 0) or 0)
        normalized.append({"ts": c["end_period_ts"], "price_cents": price, "volume": volume})
    return normalized
