"""
Execution layer. Strategy/signal code never touches this directly except
through the Broker interface below -- swapping paper_prod for live_prod
is a one-line config change, not a rewrite.
"""

import os
import time
import json
from dataclasses import dataclass, field
from typing import Optional
import config
import kalshi_market_data
import risk


def kalshi_taker_fee_dollars(contracts: int, price_cents: float) -> float:
    """
    Kalshi's real per-order fee formula, verified against docs/fee schedule
    (Sep 2026): fee = round_up(0.07 * contracts * price * (1 - price)),
    price in dollars. Peaks at ~1.75c/contract at a 50c price, shrinks
    toward the extremes. Applies to TAKER fills -- our aggressive
    through-the-market limit orders cross the spread, so they're takers,
    not the ~1.75% maker rate. Charged on EVERY order, so a round-trip
    (open + close) pays it twice.
    """
    import math
    price_dollars = price_cents / 100.0
    raw_fee = 0.07 * contracts * price_dollars * (1 - price_dollars)
    return math.ceil(raw_fee * 100) / 100.0  # round up to the next cent


def kalshi_maker_fee_dollars(contracts: int, price_cents: float) -> float:
    """
    Approximation of Kalshi's maker fee: roughly MAKER_FEE_RATE_MULTIPLIER
    (~1/4) of the taker rate. Real maker pricing may not be an exact
    fraction of the taker formula -- treat this as directionally correct,
    not exact, and re-verify against the current fee schedule before relying
    on it for precise accounting.
    """
    return round(kalshi_taker_fee_dollars(contracts, price_cents) * config.MAKER_FEE_RATE_MULTIPLIER, 2)


@dataclass
class Position:
    ticker: str
    event_ticker: str
    direction: str  # "yes" or "no"
    entry_price_cents: float
    size_dollars: float
    opened_ts: float
    reason: str
    strategy: str = ""                       # "momentum" | "reversion" | "value_entry" -- picks the exit rule
    market_title: str = ""                   # human-readable match/market name, from Kalshi's own "title" field
    order_id: Optional[str] = None          # live mode only
    requested_contracts: int = 0             # live mode only: what we asked for
    filled_contracts: int = 0                # live mode only: what actually filled
    status: str = "open"                     # live mode only: "pending_fill" | "open" | "closing"
    # High-water mark for the SIDE actually held (not raw yes-price -- so
    # "higher is always more favorable" regardless of direction). Used by
    # favorite_entry_thin / favorite_entry_majority's trailing stop-loss:
    # those strategies ride to settlement for upside (no take-profit) but
    # need real downside protection, anchored to the best price seen since
    # entry rather than a fixed distance from entry. None until the first
    # exit-check updates it (initialized to entry price at that point).
    peak_side_price_cents: Optional[float] = None


class PaperBroker:
    """
    Logs intended trades and tracks a virtual balance/position book using
    REAL market prices (pulled from whatever data source you feed it), but
    never sends an actual order to Kalshi. This is the safe way to validate
    signal quality against live market behavior before risking money.
    """

    def __init__(self, starting_balance: float = config.PAPER_STARTING_BALANCE_DOLLARS, log_path: str = "paper_trades.jsonl"):
        import threading
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.daily_pnl = 0.0
        self.peak_daily_pnl = 0.0  # high-water mark for the session, tracked for
                                    # peak-drawdown protection -- separate from the
                                    # flat daily loss cap, which only looks at NET
                                    # loss from the start, not from a peak
        self.total_fees_paid = 0.0
        self.open_positions: dict[str, Position] = {}
        self.log_path = log_path
        # Needed once this broker is shared across more than one loop/thread
        # (the merged tennis+momentum process) -- unlike the original
        # single-loop design, open_position/close_position/the accessors can
        # now genuinely be called concurrently from two different threads.
        self._lock = threading.Lock()

    def _simulated_fill_price(self, quoted_price_cents: float, direction: str, is_buy: bool) -> float:
        """
        Paper mode otherwise assumes a perfect fill at the quoted price,
        which is optimistic. Adds random unfavorable slippage up to
        PAPER_SLIPPAGE_CENTS_MAX to better match real execution quality.
        Unfavorable = higher price when buying, lower price when selling.
        """
        import random
        slip = random.uniform(0, config.PAPER_SLIPPAGE_CENTS_MAX)
        return quoted_price_cents + slip if is_buy else quoted_price_cents - slip

    def _simulate_execution(self, quoted_price_cents: float, direction: str, is_buy: bool):
        """
        Simulates the maker-first-then-taker-fallback logic KalshiLiveBroker
        actually performs, since paper mode can't observe real queue
        position or fill probability. With PAPER_MAKER_FILL_PROBABILITY
        chance, fills as a maker at the quoted price (no adverse slippage,
        the ~1/4-rate maker fee); otherwise falls back to a taker fill (the
        existing unfavorable-slippage price, full taker fee). This is a
        labeled approximation, not a measurement -- real maker fill rates
        depend on queue position and how long the order rests.
        Returns (fill_price_cents, was_maker: bool).
        """
        import random
        if random.random() < config.PAPER_MAKER_FILL_PROBABILITY:
            return quoted_price_cents, True
        return self._simulated_fill_price(quoted_price_cents, direction, is_buy), False

    def open_position(self, ticker: str, event_ticker: str, direction: str, price_cents: float, size_dollars: float, reason: str, strategy: str = "", market_title: str = "") -> Position:
        """
        Unconditional open -- does NOT check MAX_CONCURRENT_POSITIONS or
        per-event duplication. Safe to call directly when only one loop is
        ever using this broker (the original single-bot design). Once a
        broker is shared across multiple loops/threads, prefer
        try_open_position() instead, which checks and opens atomically
        under one lock -- calling risk.can_open_new_position() separately
        beforehand leaves a race window between the check and this call.
        """
        with self._lock:
            return self._open_position_locked(ticker, event_ticker, direction, price_cents, size_dollars, reason, strategy, market_title)

    def try_open_position(self, ticker: str, event_ticker: str, direction: str, price_cents: float, size_dollars: float,
                           reason: str, strategy: str = "", market_title: str = "") -> Optional[Position]:
        """
        Atomically checks risk.can_open_new_position() and opens under the
        SAME lock, so two threads (e.g. tennis and momentum loops sharing
        one broker) can't both pass the concurrency-cap check before either
        one's position is actually recorded. Returns None if blocked by the
        cap or an existing position in the same event; the caller doesn't
        need to call can_open_new_position() separately beforehand.
        """
        with self._lock:
            open_count = len(self.open_positions)
            open_events = {p.event_ticker for p in self.open_positions.values()}
            allowed, _reason = risk.can_open_new_position(open_count, open_events, event_ticker)
            if not allowed:
                return None
            return self._open_position_locked(ticker, event_ticker, direction, price_cents, size_dollars, reason, strategy, market_title)

    def _open_position_locked(self, ticker, event_ticker, direction, price_cents, size_dollars, reason, strategy, market_title) -> Position:
        """Actual open logic -- callers must already hold self._lock."""
        fill_price, was_maker = self._simulate_execution(price_cents, direction, is_buy=True)
        contracts = max(1, int(size_dollars / (fill_price / 100.0)))
        fee = kalshi_maker_fee_dollars(contracts, fill_price) if was_maker else kalshi_taker_fee_dollars(contracts, fill_price)
        self.balance -= fee
        self.total_fees_paid += fee

        pos = Position(
            ticker=ticker,
            event_ticker=event_ticker,
            direction=direction,
            entry_price_cents=fill_price,
            size_dollars=size_dollars,
            opened_ts=time.time(),
            reason=reason,
            strategy=strategy,
            market_title=market_title,
            filled_contracts=contracts,
        )
        self.open_positions[ticker] = pos
        self._log({"action": "open", "ticker": ticker, "strategy": strategy, "market_title": market_title, "direction": direction,
                   "quoted_price_cents": price_cents, "fill_price_cents": fill_price,
                   "was_maker": was_maker, "size_dollars": size_dollars, "fee_dollars": fee, "reason": reason})
        return pos

    def close_position(self, ticker: str, exit_price_cents: float) -> float:
        with self._lock:
            pos = self.open_positions.pop(ticker, None)
            if pos is None:
                return 0.0

            fill_price, was_maker = self._simulate_execution(exit_price_cents, pos.direction, is_buy=False)
            contracts = pos.filled_contracts or max(1, int(pos.size_dollars / (pos.entry_price_cents / 100.0)))
            fee = kalshi_maker_fee_dollars(contracts, fill_price) if was_maker else kalshi_taker_fee_dollars(contracts, fill_price)

            # P&L: proceeds minus cost, per contract, linear in price -- NOT a
            # percentage return on size_dollars. Real Kalshi contracts pay $1
            # if correct, $0 if not; a price move of D cents on N contracts is
            # worth N*D/100 dollars, period, regardless of entry price. Found
            # Sep 24 while building the live order-placement migration: this
            # used pos.size_dollars * pnl_pct instead, which is wrong by a
            # factor of entry_price/100 -- silently understating P&L on every
            # trade this entire session, worst on the cheap low-cents entries
            # value_entry/momentum favor (e.g. a 10c entry understated real
            # P&L by 90%). `contracts` was already computed above for the fee
            # calculation; it just wasn't being used here too.
            price_delta = fill_price - pos.entry_price_cents
            direction_multiplier = 1 if pos.direction == "yes" else -1
            pnl_dollars = contracts * (price_delta * direction_multiplier) / 100.0 - fee

            self.balance += pnl_dollars
            self.daily_pnl += pnl_dollars
            self.peak_daily_pnl = max(self.peak_daily_pnl, self.daily_pnl)
            self.total_fees_paid += fee
            self._log({"action": "close", "ticker": ticker, "strategy": pos.strategy, "market_title": pos.market_title, "direction": pos.direction,
                       "quoted_exit_price_cents": exit_price_cents,
                       "fill_price_cents": fill_price, "was_maker": was_maker, "fee_dollars": fee,
                       "pnl_dollars": round(pnl_dollars, 2), "balance_after": round(self.balance, 2)})
            return pnl_dollars

    def _log(self, record: dict):
        record["ts"] = time.time()
        record["mode"] = "paper"
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        print(f"[PAPER] {record}")
        try:
            import db_logger
            db_logger.log_trade(record)
        except Exception as e:
            print(f"[warn] db_logger unavailable, file logging still worked: {e}")

    # --- accessors (match KalshiLiveBroker's interface so runner code doesn't
    # need to branch on which broker it's talking to) ---

    def get_open_position_count(self) -> int:
        with self._lock:
            return len(self.open_positions)

    def get_open_event_tickers(self) -> set[str]:
        with self._lock:
            return {p.event_ticker for p in self.open_positions.values()}

    def get_open_positions_snapshot(self) -> list:
        with self._lock:
            return list(self.open_positions.values())

    def shutdown(self, wait: bool = True):
        pass  # nothing to clean up in paper mode


class KalshiLiveBroker:
    """
    Real order execution against Kalshi's production API.

    Signing scheme and endpoints verified against docs.kalshi.com (Sep 2026):
      - Auth headers: KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP
      - Signature = RSA-PSS(SHA256, MGF1-SHA256, salt_length=DIGEST_LENGTH)
        over the string f"{timestamp_ms}{METHOD}{path}" (path only, no query string)
      - POST /portfolio/events/orders to place (V2 -- see _place_order for the
        Sep 24 migration off the deprecated /portfolio/orders), GET
        /portfolio/balance to check funds

    Orders are placed as aggressive LIMIT orders (priced through the current
    market price) rather than true market orders, since the create-order
    endpoint's documented schema is price-based. This trades a small amount
    of slippage for a guaranteed-schema-correct request. Re-verify against
    the live docs before relying on this for size or price precision.

    Method signatures are identical to PaperBroker (open_position /
    close_position / balance / daily_pnl / open_positions) so nothing above
    this class needs to change when you switch environments.
    """

    def __init__(self):
        import uuid  # local import, only needed in live mode
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self._uuid = uuid

        self.api_key_id = os.environ.get(config.KALSHI_API_KEY_ID_ENV)
        private_key_path = os.environ.get(config.KALSHI_PRIVATE_KEY_PATH_ENV)
        if not self.api_key_id or not private_key_path:
            raise RuntimeError(
                f"Set {config.KALSHI_API_KEY_ID_ENV} and {config.KALSHI_PRIVATE_KEY_PATH_ENV} "
                "env vars before using KalshiLiveBroker."
            )

        from cryptography.hazmat.primitives import serialization
        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

        self.base_url = config.KALSHI_API_BASE_URL
        self.open_positions: dict[str, Position] = {}
        self.daily_pnl = 0.0
        self.peak_daily_pnl = 0.0  # mirrors PaperBroker -- high-water mark for
                                    # _should_halt()'s peak-drawdown check, shared
                                    # by both loops regardless of broker type
        self.starting_balance = self.get_balance()
        self.balance = self.starting_balance
        self._sync_positions_from_kalshi()

        # Fill-waiting used to block the caller for up to ORDER_FILL_TIMEOUT_SECONDS
        # per order, which meant only one position could be opened/closed at a
        # time even though nothing about Kalshi itself requires that. Orders
        # are now placed synchronously (fast) and the wait-for-fill step runs
        # in a background thread, so N orders can be in flight at once, up to
        # MAX_CONCURRENT_POSITIONS workers. `_lock` protects open_positions/
        # balance/daily_pnl since multiple worker threads touch them.
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=config.MAX_CONCURRENT_POSITIONS, thread_name_prefix="kalshi-fill-wait"
        )

    # --- signing ---

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, path),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None) -> dict:
        import requests
        # `path` always includes the full "/trade-api/v2/..." prefix -- required
        # for the signature (confirmed against Kalshi's own docs: the signed
        # string uses the complete path, not a suffix). self.base_url ALSO
        # already includes "/trade-api/v2" (shared with kalshi_market_data.py's
        # own calls, which correctly rely on that and must not be touched).
        # Real production bug (Sep 24, first live connection attempt ever):
        # naively concatenating base_url + path doubled "/trade-api/v2",
        # producing a 404 on every single endpoint. Fix: derive the bare
        # domain from base_url for URL construction only, while the
        # signature below still signs the full, unmodified `path`.
        domain_only = self.base_url.split("/trade-api/v2")[0]
        url = domain_only + path
        headers = self._headers(method, path)  # signs the FULL path, unchanged -- see docstring
        resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=10)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Kalshi's actual error message (field, code, reason) lives in the
            # response body, which raise_for_status()'s default exception text
            # never includes -- without this, a 400 just says "Bad Request"
            # with no way to know WHICH field failed validation. Added Sep 24
            # while diagnosing the first real 400 from the new V2 order
            # endpoint, which gave zero detail without this.
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.exceptions.HTTPError(f"{e} -- response body: {detail}", response=resp) from None
        return resp.json()

    # --- account ---

    def get_balance(self) -> float:
        data = self._request("GET", "/trade-api/v2/portfolio/balance")
        print(f"[LIVE] raw balance response: {data}")
        # balance is returned in cents per Kalshi's convention
        return data.get("balance", 0) / 100.0

    def _sync_positions_from_kalshi(self):
        """
        Queries Kalshi's real portfolio on startup and reconstructs
        self.open_positions from whatever's ACTUALLY open on the real
        account -- instead of assuming a blank slate. Without this, a
        redeploy while real positions are open would make the bot
        completely blind to them: no exit-check would ever run on them
        again, and position-count-based risk limits (MAX_CONCURRENT_POSITIONS,
        the per-window cap) would be computed against an empty set that
        doesn't reflect real exposure.

        Real, honest limitations -- Kalshi's GET /portfolio/positions
        response (verified against docs.kalshi.com's documented schema,
        NOT against a real populated response) has no concept of "which of
        our bot's strategies opened this position" -- that's purely
        bot-internal metadata Kalshi has no way to know:

        - direction is INFERRED from the sign of the `position` field
          (positive assumed = net long YES, negative = net long NO) --
          the standard convention, but not something this codebase has
          confirmed against a real, populated response yet.
        - entry_price_cents is APPROXIMATED as total_traded_dollars /
          total_traded -- the average cost across ALL historical fills on
          this ticker, not necessarily one clean "entry price" if there
          were multiple partial fills or round trips on the same ticker.
        - strategy is inferred from series prefix: CRYPTO_SERIES tickers
          get "momentum" (safe -- momentum_loop only ever trades those
          series). Anything else gets "resynced_unknown", a new
          pseudo-strategy with its own generic, conservative exit rule
          (see combined_runner.py / risk.py) -- we genuinely cannot know
          which of the tennis-specific strategies really opened it, so
          re-syncing gives it SOME managed exit rather than leaving a
          real position completely unmanaged after a restart.

        This method itself has NOT been tested against Kalshi's real, live
        API -- only against the documented response schema. Treat the
        first live restart with real positions open as a genuine test of
        this path, not a guarantee it behaves correctly.
        """
        positions_synced = []
        cursor = None
        while True:
            params = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/trade-api/v2/portfolio/positions", params=params)

            for mp in data.get("market_positions", []):
                net_contracts = mp.get("position", 0)
                if net_contracts == 0:
                    continue  # flat -- nothing actually held on this ticker

                ticker = mp["ticker"]
                direction = "yes" if net_contracts > 0 else "no"
                total_traded = mp.get("total_traded", 0)
                total_traded_dollars = float(mp.get("total_traded_dollars", 0) or 0)
                entry_price_cents = (total_traded_dollars / total_traded * 100.0) if total_traded > 0 else 50.0
                size_dollars = abs(float(mp.get("market_exposure_dollars", 0) or 0))
                filled_contracts = abs(net_contracts)

                series = ticker.split("-")[0]
                if series in config.CRYPTO_SERIES:
                    strategy = "momentum"
                    event_ticker = ticker  # per-strike granularity, matches _momentum_event_key
                else:
                    strategy = "resynced_unknown"
                    event_ticker = ticker.rsplit("-", 1)[0] if "-" in ticker else ticker  # matches _derive_match_key

                pos = Position(
                    ticker=ticker,
                    event_ticker=event_ticker,
                    direction=direction,
                    entry_price_cents=entry_price_cents,
                    size_dollars=size_dollars,
                    opened_ts=time.time(),  # real open time not available from this endpoint -- treated as "now"
                    reason="resynced from Kalshi on startup",
                    strategy=strategy,
                    market_title="",  # not returned by this endpoint
                    filled_contracts=filled_contracts,
                    status="open",
                )
                self.open_positions[ticker] = pos
                positions_synced.append(pos)

            cursor = data.get("cursor")
            if not cursor:
                break

        if positions_synced:
            print(f"[LIVE] Re-synced {len(positions_synced)} real open position(s) from Kalshi on startup:")
            for pos in positions_synced:
                print(f"  {pos.ticker}: strategy={pos.strategy} direction={pos.direction} "
                      f"entry~{pos.entry_price_cents:.1f}c size=${pos.size_dollars:.2f} "
                      f"(strategy/entry_price INFERRED -- verify against the real account)")
        else:
            print("[LIVE] No open positions found on Kalshi -- starting with a clean slate.")

    # --- order fill polling ---

    def _get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/trade-api/v2/portfolio/orders/{order_id}").get("order", {})

    def _cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")

    def _poll_until_filled_or_timeout(self, order_id: str, timeout_seconds: float = None) -> dict:
        """
        Polls an order until it's fully filled or `timeout_seconds` elapses
        (defaults to ORDER_FILL_TIMEOUT_SECONDS). If anything is still
        resting at timeout, cancels the remainder so we don't leave a stale
        order sitting on the book. Returns the final order state.
        """
        if timeout_seconds is None:
            timeout_seconds = config.ORDER_FILL_TIMEOUT_SECONDS
        deadline = time.time() + timeout_seconds
        order = self._get_order(order_id)

        while time.time() < deadline:
            status = order.get("status")
            remaining = order.get("remaining_count", 0)
            if status in ("executed", "canceled") or remaining == 0:
                break
            time.sleep(config.ORDER_FILL_POLL_INTERVAL_SECONDS)
            order = self._get_order(order_id)

        if order.get("remaining_count", 0) > 0 and order.get("status") not in ("executed", "canceled"):
            fill_count_before_cancel = order.get("fill_count", 0)
            print(f"[LIVE] order {order_id} still has {order['remaining_count']} resting after "
                  f"{timeout_seconds}s, canceling remainder")
            try:
                self._cancel_order(order_id)
            except Exception as e:
                print(f"[warn] cancel failed for {order_id}: {e} -- check manually, it may still be resting")
            order = self._get_order(order_id)
            # some cancel responses lag on fill_count; trust the higher of the two reads
            order["fill_count"] = max(order.get("fill_count", 0), fill_count_before_cancel)

        return order

    def _place_order(self, ticker: str, side: str, action: str, count: int, price_cents: float, is_maker: bool) -> str:
        """
        Places a single order via Kalshi's V2 create-order endpoint
        (POST /portfolio/events/orders), returns its order_id. Shared by
        both the maker attempt and taker fallback.

        Migrated Sep 24 after the legacy POST /portfolio/orders endpoint
        started returning 410 Gone in the live shakedown -- confirmed via
        Kalshi's own docs: that endpoint was slated for deprecation "no
        earlier than May 6, 2026," which has since passed. The V2 endpoint
        is a genuinely different shape, not just a different path:
          - side is "bid"/"ask", always relative to the YES leg -- not "yes"/"no"
          - no separate action (buy/sell) field -- folded into bid
            (buy yes / sell no) vs ask (sell yes / buy no)
          - price is a fixed-point DOLLAR string (e.g. "0.56"), not integer cents
          - count is a fixed-point string too (e.g. "10.00"), not a plain int
          - self_trade_prevention_type is newly required
          - the response is FLAT (order_id at the top level), not nested
            under an "order" key like the legacy response was

        Translation from the old (side, action) pair to the V2 model,
        derived directly from Kalshi's own definition ("bid means buy YES,
        ask means sell YES; selling YES is economically equivalent to
        buying NO at 1-price") and verified against all four cases:
          yes+buy  -> bid, price unchanged
          yes+sell -> ask, price unchanged
          no+buy   -> ask, price = 100 - price   (buying NO = selling YES at 1-price)
          no+sell  -> bid, price = 100 - price   (selling NO = buying YES at 1-price)

        _fill_maker_then_taker's maker/taker distinction maps onto
        time_in_force: a resting maker attempt uses good_till_canceled (we
        poll and cancel it ourselves on timeout); the aggressive taker
        fallback uses immediate_or_cancel.
        """
        price_cents = min(99, max(1, int(round(price_cents))))

        if side == "yes":
            book_side = "bid" if action == "buy" else "ask"
            yes_price_cents = price_cents
        else:  # side == "no"
            book_side = "ask" if action == "buy" else "bid"
            yes_price_cents = 100 - price_cents

        body = {
            "ticker": ticker,
            "client_order_id": str(self._uuid.uuid4()),
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{yes_price_cents / 100.0:.2f}",
            "time_in_force": "good_till_canceled" if is_maker else "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            # Explicit, not omitted -- Kalshi's own docs show a COMPLETE working
            # example that always includes this field ("0 is the primary
            # subaccount"), even though the schema marks it optional. Added
            # Sep 24 after every order attempt failed with insufficient_balance
            # despite tiny orders ($4-5) against a genuinely funded $150.01
            # predictions balance -- confirmed via a real manual trade the
            # account itself works fine, and the API key is confirmed scoped
            # to predictions, not the separate ~$5.56 perpetuals pool. Testing
            # whether omitting this field doesn't actually default to the
            # primary subaccount the way the docs describe.
            "subaccount": 0,
            # Explicit, not omitted -- confirmed via the real balance breakdown
            # logged on this exact startup: total $150.01 is split across FOUR
            # exchange_index shards (0: $135.73, 1: $0.00, 2: $11.64, 3: $2.64).
            # The schema says this "auto-routes when ticker is provided" if
            # omitted -- but every order attempt so far has failed with
            # insufficient_balance despite tiny order sizes against a genuinely
            # funded account, which is exactly what you'd see if auto-routing
            # were sending tennis/BTC orders to the empty shard (index 1)
            # instead of index 0, where the bulk of the real funds actually sit.
            "exchange_index": 0,
        }
        implied_cost = count * (yes_price_cents / 100.0)
        print(f"[LIVE] order request: {ticker} side={book_side} count={body['count']} "
              f"price={body['price']} subaccount={body['subaccount']} exchange_index={body['exchange_index']} "
              f"implied_cost=${implied_cost:.2f} current_balance=${self.balance:.2f}")
        result = self._request("POST", "/trade-api/v2/portfolio/events/orders", json_body=body)
        return result.get("order_id")

    def _fill_maker_then_taker(self, ticker: str, side: str, action: str, requested_contracts: int,
                                maker_price_cents: float, taker_price_cents: float) -> tuple:
        """
        Tries a resting order AT maker_price_cents first (doesn't cross the
        spread -- pays the ~1/4-rate maker fee if it fills). Whatever's left
        unfilled after MAKER_ATTEMPT_TIMEOUT_SECONDS falls back to an
        aggressive taker order at taker_price_cents for the remainder.
        Returns (total_filled_contracts, size_weighted_avg_fill_price_cents).
        """
        maker_order_id = self._place_order(ticker, side, action, requested_contracts, maker_price_cents, is_maker=True)
        print(f"[LIVE] maker attempt: {ticker} {side} x{requested_contracts} @ {maker_price_cents:.0f}c, order_id={maker_order_id}")
        maker_final = self._poll_until_filled_or_timeout(maker_order_id, timeout_seconds=config.MAKER_ATTEMPT_TIMEOUT_SECONDS)
        maker_filled = maker_final.get("fill_count", 0)

        taker_filled = 0
        remainder = requested_contracts - maker_filled
        if remainder > 0:
            if maker_filled > 0:
                print(f"[LIVE] maker filled {maker_filled}/{requested_contracts}, falling back to taker for the remaining {remainder}")
            else:
                print(f"[LIVE] maker attempt filled 0, falling back to taker for all {remainder}")
            taker_order_id = self._place_order(ticker, side, action, remainder, taker_price_cents, is_maker=False)
            taker_final = self._poll_until_filled_or_timeout(taker_order_id)
            taker_filled = taker_final.get("fill_count", 0)

        total_filled = maker_filled + taker_filled
        if total_filled == 0:
            return 0, 0.0
        weighted_price = (maker_filled * maker_price_cents + taker_filled * taker_price_cents) / total_filled
        return total_filled, weighted_price

    # --- trading ---

    def try_open_position(self, ticker: str, event_ticker: str, direction: str, price_cents: float, size_dollars: float,
                           reason: str, strategy: str = "", market_title: str = "") -> Optional[Position]:
        """
        Atomically checks risk.can_open_new_position() and reserves the
        position placeholder under the SAME lock -- mirrors PaperBroker's
        method of the same name, so two threads sharing one broker can't
        both pass the concurrency-cap check before either one's position is
        actually recorded. Added Sep 24 after the live shakedown's first
        real signal fire crashed with AttributeError -- this method existed
        on PaperBroker (added during the tennis+momentum merge) but was
        never backported here, since every test since then ran against
        PaperBroker only.

        The orderbook fetch (for maker pricing) happens BEFORE the lock is
        acquired, same as open_position -- it's read-only market data with
        no bearing on whether a slot is available, so only the actual
        reservation needs to be atomic with the cap check, not the network
        call.
        """
        requested_contracts = max(1, int(size_dollars / (price_cents / 100.0)))
        taker_price = min(99, max(1, int(price_cents) + (2 if direction == "yes" else -2)))

        try:
            ob = kalshi_market_data.get_orderbook(ticker)
            maker_price = ob["best_yes_bid_cents"] if direction == "yes" else ob["best_no_bid_cents"]
            if maker_price <= 0:
                maker_price = taker_price
        except Exception as e:
            print(f"[warn] orderbook fetch failed for {ticker}, skipping maker attempt: {e}")
            maker_price = taker_price

        pos = Position(
            ticker=ticker, event_ticker=event_ticker, direction=direction,
            entry_price_cents=price_cents, size_dollars=0.0,
            opened_ts=time.time(), reason=reason, strategy=strategy, market_title=market_title,
            requested_contracts=requested_contracts, filled_contracts=0,
            status="pending_fill",
        )
        with self._lock:
            open_count = len(self.open_positions)
            open_events = {p.event_ticker for p in self.open_positions.values()}
            allowed, _reason = risk.can_open_new_position(open_count, open_events, event_ticker)
            if not allowed:
                return None
            self.open_positions[ticker] = pos

        self._executor.submit(self._resolve_open_fill, ticker, pos, direction, requested_contracts, maker_price, taker_price)
        return pos

    def open_position(self, ticker: str, event_ticker: str, direction: str, price_cents: float, size_dollars: float, reason: str, strategy: str = "", market_title: str = "") -> Optional[Position]:
        """
        Fetches the current best bid (fast, single GET) to price a maker
        attempt, reserves the position placeholder immediately, then hands
        the whole maker-then-taker-fallback fill sequence to a background
        thread. Returns almost instantly -- concurrency is unaffected by
        adding the maker attempt, since the wait happens off-thread either way.
        """
        requested_contracts = max(1, int(size_dollars / (price_cents / 100.0)))
        taker_price = min(99, max(1, int(price_cents) + (2 if direction == "yes" else -2)))

        try:
            ob = kalshi_market_data.get_orderbook(ticker)
            maker_price = ob["best_yes_bid_cents"] if direction == "yes" else ob["best_no_bid_cents"]
            if maker_price <= 0:
                maker_price = taker_price  # no resting bid to join -- skip straight to taker pricing
        except Exception as e:
            print(f"[warn] orderbook fetch failed for {ticker}, skipping maker attempt: {e}")
            maker_price = taker_price

        pos = Position(
            ticker=ticker, event_ticker=event_ticker, direction=direction,
            entry_price_cents=price_cents, size_dollars=0.0,
            opened_ts=time.time(), reason=reason, strategy=strategy, market_title=market_title,
            requested_contracts=requested_contracts, filled_contracts=0,
            status="pending_fill",
        )
        with self._lock:
            self.open_positions[ticker] = pos

        self._executor.submit(self._resolve_open_fill, ticker, pos, direction, requested_contracts, maker_price, taker_price)
        return pos

    def _resolve_open_fill(self, ticker: str, pos: Position, direction: str, requested_contracts: int,
                            maker_price: float, taker_price: float):
        """Runs in a background thread: maker attempt, taker fallback, then updates the placeholder in place."""
        try:
            filled, weighted_price = self._fill_maker_then_taker(ticker, direction, "buy", requested_contracts, maker_price, taker_price)
        except Exception as e:
            print(f"[warn] fill sequence failed for {ticker}: {e}")
            with self._lock:
                self.open_positions.pop(ticker, None)
            return

        with self._lock:
            if filled == 0:
                print(f"[LIVE] {ticker} filled 0 contracts -- no position opened")
                self.open_positions.pop(ticker, None)
                return
            if filled < requested_contracts:
                print(f"[LIVE] partial fill: {filled}/{requested_contracts} contracts on {ticker}")
            pos.filled_contracts = filled
            pos.entry_price_cents = weighted_price
            pos.size_dollars = filled * (weighted_price / 100.0)
            pos.status = "open"

    def close_position(self, ticker: str, exit_price_cents: float) -> bool:
        """
        Same maker-first-then-taker-fallback pattern as open_position, and
        the same non-blocking reservation approach: marks the position
        "closing" synchronously so it can't be double-closed, then resolves
        the actual fill sequence in a background thread.

        Returns True if a close attempt was started, False if there was
        nothing to close (no such position, or already closing/pending).
        P&L applies asynchronously once the fill resolves -- check
        broker.daily_pnl or broker.balance after giving it a moment.
        """
        with self._lock:
            pos = self.open_positions.get(ticker)
            if pos is None or pos.status == "closing":
                return False
            if pos.status == "pending_fill":
                print(f"[LIVE] {ticker} order still filling, can't close yet -- try again shortly")
                return False
            pos.status = "closing"
            contracts_to_sell = pos.filled_contracts

        taker_price = min(99, max(1, int(exit_price_cents) + (-2 if pos.direction == "yes" else 2)))
        try:
            ob = kalshi_market_data.get_orderbook(ticker)
            # selling YES joins the yes-ask queue, i.e. prices at/near the
            # existing best ask rather than crossing it
            maker_price = ob["best_yes_ask_cents"] if pos.direction == "yes" else ob["best_no_ask_cents"]
            if maker_price <= 0 or maker_price >= 100:
                maker_price = taker_price
        except Exception as e:
            print(f"[warn] orderbook fetch failed for {ticker} close, skipping maker attempt: {e}")
            maker_price = taker_price

        print(f"[LIVE] closing {ticker} x{contracts_to_sell}, maker attempt @ {maker_price:.0f}c first")
        self._executor.submit(self._resolve_close_fill, ticker, pos, contracts_to_sell, maker_price, taker_price)
        return True

    def _resolve_close_fill(self, ticker: str, pos: Position, contracts_to_sell: int, maker_price: float, taker_price: float):
        """Runs in a background thread. Maker attempt, taker fallback, applies P&L, updates/removes the position."""
        try:
            closed_contracts, weighted_exit_price = self._fill_maker_then_taker(
                ticker, pos.direction, "sell", contracts_to_sell, maker_price, taker_price
            )
        except Exception as e:
            print(f"[warn] close-fill sequence failed for {ticker}: {e}")
            with self._lock:
                pos.status = "open"  # revert so it's not stuck "closing" forever
            return

        if closed_contracts == 0:
            print(f"[LIVE] close for {ticker} filled 0 contracts -- still open, retry")
            with self._lock:
                pos.status = "open"
            return

        # P&L: proceeds minus cost, per contract, linear in price -- see
        # PaperBroker's close_position for the full explanation of the bug
        # this fixes (was scaling by entry-price-derived closed_size_dollars
        # instead of contracts directly, wrong by a factor of entry_price/100).
        price_delta = weighted_exit_price - pos.entry_price_cents
        direction_multiplier = 1 if pos.direction == "yes" else -1
        pnl_dollars = closed_contracts * (price_delta * direction_multiplier) / 100.0

        with self._lock:
            self.daily_pnl += pnl_dollars
            self.peak_daily_pnl = max(self.peak_daily_pnl, self.daily_pnl)
            if closed_contracts < contracts_to_sell:
                remaining = contracts_to_sell - closed_contracts
                print(f"[LIVE] partial close: {closed_contracts}/{contracts_to_sell} closed, "
                      f"{remaining} contracts still open on {ticker} -- may need a follow-up close")
                pos.filled_contracts = remaining
                pos.size_dollars = remaining * (pos.entry_price_cents / 100.0)
                pos.status = "open"
            else:
                self.open_positions.pop(ticker, None)

        try:
            self.balance = self.get_balance()  # re-sync with actual account state
        except Exception as e:
            print(f"[warn] balance re-sync failed after closing {ticker}: {e}")

    # --- thread-safe accessors (use these instead of touching open_positions directly) ---

    def get_open_position_count(self) -> int:
        with self._lock:
            return len(self.open_positions)

    def get_open_event_tickers(self) -> set[str]:
        with self._lock:
            return {p.event_ticker for p in self.open_positions.values()}

    def get_open_positions_snapshot(self) -> list:
        """Only returns positions with status 'open' -- pending_fill/closing aren't safe to act on yet."""
        with self._lock:
            return [p for p in self.open_positions.values() if p.status == "open"]

    def shutdown(self, wait: bool = True):
        """Call when stopping the bot so background fill-polling threads wind down cleanly."""
        self._executor.shutdown(wait=wait)


def get_broker():
    """Returns the correct broker for the configured environment."""
    if config.ENVIRONMENT in ("backtest", "paper_prod"):
        return PaperBroker()
    elif config.ENVIRONMENT == "live_prod":
        return KalshiLiveBroker()
    else:
        raise ValueError(f"Unknown ENVIRONMENT: {config.ENVIRONMENT}")
