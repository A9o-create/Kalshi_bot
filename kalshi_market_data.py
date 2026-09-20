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
import random
import requests
import config

_last_request_ts = 0.0


def _get(url: str, params: dict = None, timeout: int = 10) -> requests.Response:
    """
    Every request in this module routes through here: enforces a minimum
    spacing between consecutive requests (KALSHI_MIN_REQUEST_INTERVAL_SECONDS)
    and retries 429s with exponential backoff + jitter (respecting a
    Retry-After header if Kalshi sends one) instead of failing on the first
    throttle. Still raises (and lets the caller's existing try/except handle
    it) if retries are exhausted or a non-429 error occurs.
    """
    global _last_request_ts

    for attempt in range(config.KALSHI_MAX_429_RETRIES + 1):
        elapsed = time.time() - _last_request_ts
        wait = config.KALSHI_MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)

        resp = requests.get(url, params=params, timeout=timeout)
        _last_request_ts = time.time()

        if resp.status_code == 429:
            if attempt == config.KALSHI_MAX_429_RETRIES:
                resp.raise_for_status()  # out of retries -- let the caller's warning/skip logic handle it
            retry_after = resp.headers.get("Retry-After")
            try:
                backoff = float(retry_after) if retry_after else config.KALSHI_BACKOFF_BASE_SECONDS * (2 ** attempt)
            except ValueError:
                backoff = config.KALSHI_BACKOFF_BASE_SECONDS * (2 ** attempt)
            backoff += random.uniform(0, config.KALSHI_BACKOFF_JITTER_SECONDS)
            time.sleep(backoff)
            continue

        resp.raise_for_status()
        return resp

    return resp  # unreachable in practice


def _base_url() -> str:
    return config.KALSHI_API_BASE_URL


_markets_cache = {}  # {(series_ticker, status, limit): (fetched_at, markets)}


def get_markets(series_ticker: str, status: str = "open", limit: int = 50) -> list[dict]:
    """
    Cached for MARKETS_CACHE_TTL_SECONDS -- a series' market listing doesn't
    meaningfully change minute to minute, and re-fetching it every single
    60s cycle across every series was pure wasted request volume. If the
    cache is stale but the fresh fetch fails (e.g. rate limited), falls
    back to the stale cached value rather than returning nothing -- a few
    minutes of staleness is far better than an empty scan.
    """
    key = (series_ticker, status, limit)
    now = time.time()
    cached = _markets_cache.get(key)
    if cached and (now - cached[0]) < config.MARKETS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        resp = _get(
            f"{_base_url()}/markets",
            params={"series_ticker": series_ticker, "status": status, "limit": limit},
        )
        markets = resp.json().get("markets", [])
        _markets_cache[key] = (now, markets)
        return markets
    except Exception:
        if cached:
            return cached[1]  # stale is better than nothing
        raise


def get_market(ticker: str) -> dict:
    """
    GET /markets/{ticker} -- a single market's current state, including
    `status` ('active' -> 'closed' -> 'determined' -> 'finalized', per
    Kalshi's own market lifecycle docs) and `result` ('yes'/'no'/None) once
    determined. Used to detect when a held position's market has actually
    settled on Kalshi's side, since neither loop otherwise has any way to
    know a market closed if price never moved enough to hit take-profit or
    stop-loss on its own. Cached for MARKETS_CACHE_TTL_SECONDS, same
    reasoning as get_markets() -- a single market's status doesn't need a
    fresh fetch every single cycle either.
    """
    key = ("single", ticker)
    now = time.time()
    cached = _markets_cache.get(key)
    if cached and (now - cached[0]) < config.MARKETS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        resp = _get(f"{_base_url()}/markets/{ticker}")
        market = resp.json().get("market", {})
        _markets_cache[key] = (now, market)
        return market
    except Exception:
        if cached:
            return cached[1]
        raise


def get_recent_trades(ticker: str, limit: int = 100, min_ts: int = None) -> list[dict]:
    params = {"ticker": ticker, "limit": limit}
    if min_ts is not None:
        params["min_ts"] = min_ts
    resp = _get(f"{_base_url()}/markets/trades", params=params)
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
    resp = _get(f"{_base_url()}/markets/{ticker}/orderbook")
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
    resp = _get(
        f"{_base_url()}/series/{series_ticker}/markets/{ticker}/candlesticks",
        params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
    )
    raw = resp.json().get("candlesticks", [])
    # normalize to the {"ts": int, "price_cents": float, "volume": float} shape signals.py expects
    normalized = []
    for c in raw:
        price = float(c.get("yes_ask", {}).get("close_dollars", 0)) * 100
        volume = float(c.get("volume", 0) or 0)
        normalized.append({"ts": c["end_period_ts"], "price_cents": price, "volume": volume})
    return normalized
