# Kalshi Momentum + Mean-Reversion Bot

Three strategies, one framework:
- **Leg 1 (crypto momentum):** volume spike + price move together → trade the breakout direction
- **Leg 2 (tennis mean reversion):** sharp price spike that starts pulling back → fade it
- **Leg 3 (tennis value entry):** near the start of a match, buy whichever side is cheaper, take profit on a 15–25% move

## Files

| File | Purpose |
|---|---|
| `config.py` | **Everything you tune lives here.** Environment switch, thresholds, risk limits. |
| `signals.py` | Pure signal-detection logic. No I/O. Fully unit-testable. |
| `synthetic_data.py` | Generates fake price paths with known planted events, for validating logic without needing real market movement. |
| `backtest.py` | Runs the detectors against synthetic data, reports hit/miss/false-positive rates. |
| `risk.py` | Position sizing (capped fractional Kelly) and risk limits (daily loss cap, concurrency). |
| `execution.py` | `PaperBroker` (logs trades, tracks virtual P&L) and `KalshiLiveBroker` (placeholder — NOT wired up yet). |
| `kalshi_market_data.py` | Real, unauthenticated Kalshi market data (markets, trades, candlesticks). |
| `live_paper_runner.py` | Main loop: polls real prod data, runs signals with per-event debounce, executes via whichever broker `config.ENVIRONMENT` selects. |

## The three environments

Set `ENVIRONMENT` in `config.py`. This is the only thing that should change as you move from testing to real money.

### 1. `backtest` — synthetic data, no network calls
```bash
pip install -r requirements.txt
python backtest.py
```
Validates that the detectors fire on the pattern they're designed for and stay quiet on noise. Already run once — momentum leg was clean (0 false positives), reversion leg over-fired on repeat ticks of the same event (now fixed via debounce in `live_paper_runner.py`, not in the detector itself — keep detection stateless, handle dedup in the orchestration layer).

### 2. `paper_prod` — real Kalshi market data, fake money
```bash
python live_paper_runner.py
```
This is the step that actually matters. It watches real, live Kalshi markets and paper-trades your signals against them. Run it for at least a few days across different market conditions before trusting the thresholds. Trades log to `paper_trades.jsonl` — review win rate, average P&L per trade, and how often signals fire before touching real money.

**No API key needed for this mode** — market data endpoints are public.

### 3. `live_prod` — real orders, real money
`KalshiLiveBroker` in `execution.py` is now fully implemented: RSA-PSS request signing (verified against a test key round-trip — signature checks out against the public key using Kalshi's exact padding/hash spec), balance lookup, and order placement/closing via `POST /trade-api/v2/portfolio/orders`.

**What it does:**
- Signs every request with `RSA-PSS(SHA256, MGF1-SHA256, salt=DIGEST_LENGTH)` over `timestamp_ms + METHOD + path` (path only, no query string, no host)
- Places aggressive limit orders (priced 2¢ through the current market price) rather than true market orders, since the documented create-order schema is price-based — this trades tiny slippage for a request shape that's guaranteed to match the schema
- Closes positions by **selling** the side you hold (not buying the opposite side) — Kalshi doesn't net YES/NO intraday, so buying the opposite side hedges rather than flattens
- **Polls for fills** every `ORDER_FILL_POLL_INTERVAL_SECONDS` (default 2s) up to `ORDER_FILL_TIMEOUT_SECONDS` (default **60s**) via `GET /portfolio/orders/{order_id}`, and cancels whatever's still resting at the deadline via `DELETE /portfolio/orders/{order_id}` so nothing sits on the book unmanaged
- **Handles partial fills correctly**: `open_position` sizes the resulting `Position` to what actually filled, not what was requested. `close_position` mirrors this: a partially-filled close shrinks the position to the unsold remainder and keeps it tracked rather than dropping it
- **Opens/closes multiple positions concurrently.** Placing an order is one fast HTTP call; waiting for it to fill is the slow part (up to 60s now). That wait runs in a background thread pool (sized to `MAX_CONCURRENT_POSITIONS`) instead of blocking the run loop, so signals on different markets don't queue up behind each other. `open_position`/`close_position` return almost immediately with a `"pending_fill"` / `"closing"` placeholder that updates in place once the fill resolves — call `broker.get_open_position_count()` / `broker.get_open_event_tickers()` rather than touching `open_positions` directly, since those are the thread-safe accessors that lock around the background threads

All of this — zero fill, partial fill (open and close), and the concurrency itself — is covered by offline simulations using mocked API responses (including one that proves 3 simultaneous orders resolve in ~0.35s instead of the ~0.9s a serial implementation would take). Still not tested against Kalshi's live servers.

**Before flipping `config.ENVIRONMENT = "live_prod"`:**
1. Get a production API key + RSA private key from Kalshi account settings → API Keys
2. Set env vars: `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (path to the `.key` file, never the key contents itself in code or env)
3. Run `paper_prod` long enough that you trust the signal quality and sizing — the first real run should be small size, watched closely
4. Double check current field names/endpoint paths against `docs.kalshi.com` right before going live — API surfaces do change

**Worth knowing:** 60s is still a fair while to leave an aggressive (through-the-market) limit order resting — if the market moves against you during that window before it fills, the "aggressive" price may no longer be aggressive enough, or may fill at a worse level than expected. Keep an eye on `paper_prod` logs for how long fills are actually taking; if most fill well under 60s, it's safe to tighten further.

## Running it on Render so it's not tied to your laptop

Deployed to Render as a **web service**, not a cron job — a cron job restarts as a fresh process every time it fires, which would wipe `PaperBroker`'s in-memory balance and open positions on every tick. A web service is one continuous process, same as running it in tmux; `server.py` just wraps `live_paper_runner.run()` (completely unchanged) in a background thread, with a trivial `/health` endpoint on the side so Render's port-binding requirement is satisfied.

**Already done:**
- Created a Render Postgres database: `kalshi-bot-trades` (id `dpg-dajnueh5efls739m5ko0-a`, free plan — **expires 2026-10-14**, upgrade or recreate before then if still running)
- `db_logger.py`: every trade also inserts into a `trades` table when `DATABASE_URL` is set, so results can be queried remotely instead of needing filesystem access to wherever the bot runs. Auto-creates the table on first use. Fails silently (file logging is unaffected) if the DB is unreachable or `DATABASE_URL` isn't set at all — verified locally with no `DATABASE_URL` set: no crash, file logging worked exactly as before.

**What's left, and why it needs you:** Render's service-creation tools (web service, cron job) all deploy from a **git repository** — there's no way to push local files to Render directly through this connector. To finish:

1. Push this code to a GitHub (or GitLab) repo
2. Get the Internal Database URL for `kalshi-bot-trades` from the Render dashboard (`https://dashboard.render.com/d/dpg-dajnueh5efls739m5ko0-a` → Connect)
3. Give me the repo URL, and I'll create the web service with `runtime=python`, `buildCommand=pip install -r requirements.txt`, `startCommand=python server.py`, and env vars `ENVIRONMENT=paper_prod` + `DATABASE_URL=<the internal URL from step 2>`

(Or set it up yourself via the Render dashboard directly, using the same start command and env vars — either way, once it's running against this same Postgres instance, I can query it.)

## How I query it later

Once it's deployed and has accumulated trades, ask me to check on it — I'll run something like:

```sql
SELECT strategy, action, COUNT(*), SUM(pnl_dollars), SUM(fee_dollars), AVG(was_maker::int)
FROM trades
GROUP BY strategy, action
```

via the Render connector's read-only Postgres query tool, directly against `dpg-dajnueh5efls739m5ko0-a` — no need to paste logs or export anything yourself.

## What to tune based on paper_prod results

- If momentum fires too rarely: lower `MOMENTUM_VOLUME_SPIKE_MULTIPLE` or `MOMENTUM_PRICE_MOVE_CENTS`
- If reversion has a low hit rate on true reversion vs. continued trend: raise `REVERSION_CONFIRM_PULLBACK_CENTS` (wait for stronger confirmation) or add the real tennis-score data source noted in `signals.py`'s docstring — score data isn't available from Kalshi's market feed itself
- If daily loss cap trips often: your Kelly win-probability estimate (`win_prob` in `live_paper_runner.py`) is probably too optimistic — it's currently a naive function of signal strength (leg 1/2) or a flat 0.55 assumption (leg 3, value entry) and should be replaced with an actual measured win rate once you have paper-trading history

## Leg 3: value entry, in more detail

Buys whichever side (YES or NO) is priced cheaper, but only within `VALUE_ENTRY_MAX_MARKET_AGE_MINUTES` (default 15) of the market's first observed trade — a proxy for "beginning of the match" since Kalshi's market feed doesn't expose live score or match clock. Exit is **percentage-based**, not a flat cents move, because a position bought at 10¢ needs a very different cents move than one bought at 45¢ to represent the same 20% gain:

- Take profit as soon as the position's own value (not necessarily the YES price — if you're holding NO, a falling YES price is your gain) rises `VALUE_ENTRY_TAKE_PROFIT_MIN_PCT` (20%) or more. If price gaps straight past `VALUE_ENTRY_TAKE_PROFIT_MAX_PCT` (30%) before a check catches the 20% crossing, it still takes the profit rather than chasing further upside or missing the exit
- Stop loss at `VALUE_ENTRY_STOP_LOSS_PCT` (15% down)

This is a value/mean-reversion-flavored bet, not a directional edge — there's no signal-derived confidence the way legs 1 and 2 have (`sig.strength`), so the win-probability used for Kelly sizing is a flat placeholder (0.55) until paper-trading history gives you a real number to calibrate against.

## The exit-monitoring gap that got fixed alongside leg 3

Earlier versions of `live_paper_runner.py` opened positions for legs 1 and 2 but never checked them against their own configured take-profit/stop-loss thresholds — nothing ever called `close_position`. Adding leg 3's percentage-based exit surfaced this, so it's fixed for all three legs now: every cycle, before looking for new entries, the runner walks every open position (via `broker.get_open_positions_snapshot()`), fetches its current price, and checks it against the exit rule for whichever `strategy` opened it (`"momentum"` / `"reversion"` / `"value_entry"`, tracked on the `Position` object itself).

## What the growth simulation caught: fees were eating the edge

A Monte Carlo of the original thresholds (8¢/4¢ momentum, 10¢/6¢ reversion) against real Kalshi fees showed **every win-rate scenario losing money**, even a 60%-optimistic one. The cause: Kalshi's taker fee (`fee = round_up(0.07 × contracts × price × (1-price))`, verified against Kalshi's own published example) peaks at ~1.75¢/contract at a 50¢ price and is charged on **both** the open and the close — up to ~3.5¢/contract round trip. Against an 8¢ take-profit target, fees alone consumed 35-44% of it before counting losing trades, slippage, or anything else.

**Fixed by widening the take-profit/stop-loss thresholds** (`config.py`) with the fee floor built in:

| | Before | After |
|---|---|---|
| Momentum TP / SL | 8¢ / 4¢ | 15¢ / 6¢ |
| Reversion TP / SL | 10¢ / 6¢ | 18¢ / 8¢ |
| Value entry TP / SL | 15-25% / 15% | 20-30% / 15% |

These are still judgment calls, not a formal re-optimization — the honest validation is paper_prod, not this simulation. But rerunning the same Monte Carlo with the new thresholds moved every scenario meaningfully in the right direction: base case went from clearly losing (~$814 median after 150 trades) to essentially breakeven (~$996), and optimistic flipped from losing (~$895) to genuinely growing (~$1,134). Pessimistic still loses money (~$926, down from ~$812) — correctly so. No threshold change fixes a real losing win rate, and it shouldn't pretend to.

Also added to the simulation, closing the "not modeled" gaps from before: Kalshi's real fee formula (`execution.kalshi_taker_fee_dollars`), random unfavorable slippage on both legs of each trade, and the daily loss cap actually halting further trades once breached (assuming, as a labeled placeholder, 4 trades/day — real frequency is unknown until paper_prod runs).

## Entry price favorability: the 35-50¢ band

`risk.entry_favorability_multiplier` gives `FAVORABLE_ENTRY_SIZE_MULTIPLIER` (1.25×) extra position size when the price actually paid for the held side falls between `FAVORABLE_ENTRY_PRICE_MIN_CENTS` (35¢) and `FAVORABLE_ENTRY_PRICE_MAX_CENTS` (50¢) — meaningfully cheaper fees than a 50¢ coin flip, while avoiding the thin liquidity typical of deep longshots below 35¢ on Kalshi's sports and crypto markets.

This is a **sizing bias, not a hard gate** — entries outside the band still fire and size normally, they just don't get the boost. It's also always subordinate to the hard 3% risk cap: `position_size_dollars` now takes an optional `side_price_cents` argument, applies the multiplier before checking the cap, so a favorable entry can reach the cap sooner but the cap itself never moves. Verified with a test showing an exact 1.25× size difference between a 42¢ entry and a 65¢ entry when below the cap, and no difference at all when the edge is large enough that both hit the cap regardless.

One nuance worth flagging: the multiplier is applied to the **side price actually being bought**, not the raw YES price. Buying NO when YES trades at 65¢ means you're actually paying 35¢ for the NO side — right at the edge of the favorable band — so every call site in `live_paper_runner.py` computes `side_price = price if direction == "yes" else (100 - price)` before sizing.

## Three more profitability levers, now implemented

### 1. Maker-first, taker-fallback order routing

Every order previously crossed the spread immediately (a taker fill, full 7% fee rate). Now `KalshiLiveBroker._fill_maker_then_taker` tries resting AT the best bid first (doesn't cross = maker fill, ~1.75% rate, roughly 1/4 the cost) for up to `MAKER_ATTEMPT_TIMEOUT_SECONDS` (15s). Whatever's unfilled after that falls back to the old aggressive taker order for the remainder. The final `Position.entry_price_cents` is a size-weighted average across whatever mix of maker and taker fills actually happened.

`PaperBroker` can't observe real queue position or fill probability, so it simulates the tradeoff probabilistically via `PAPER_MAKER_FILL_PROBABILITY` (50%, a labeled approximation — not a measurement) — verified with a 20-trade test showing both the ~$0.88 maker fee and the ~$3.43-3.49 taker fee actually appearing across paper trades, confirming the simulation is live rather than silently always falling back to one path.

### 2. Hold-to-settlement instead of always selling at take-profit

When a position hits its take-profit trigger but the side actually held is at or above `SETTLEMENT_HOLD_THRESHOLD_CENTS` (95¢ — i.e. already close to certain), `live_paper_runner.py` now skips the sell and lets it ride to settlement instead, avoiding the second taker fee entirely. This **only applies to take-profit exits, never stop-losses** — a losing position never gets held on the hope of a reversal, regardless of price.

Known limitation, stated plainly: `PaperBroker` has no concept of an actual settlement event or payoff. In paper mode, a held position just stays open — it doesn't get credited the eventual $1 (or $0) at settlement. This means paper_prod's reported balance will understate the benefit of this feature until you manually reconcile held positions, or until a future version adds settlement simulation. The logic itself (when to hold vs. sell) is correct and real for live_prod; the paper-mode accounting around it is the known gap.

### 3. Orderbook-depth-aware sizing

`risk.cap_size_by_depth` caps a position to at most `MAX_BOOK_DEPTH_FRACTION` (50%) of the liquidity actually resting at the entry price level, using the real orderbook (`kalshi_market_data.get_orderbook`, confirmed against Kalshi's documented bids-only shape — a taker buying YES is filled by resting NO bids, and vice versa). Applied at all three entry points in `live_paper_runner.py` via `_apply_depth_cap`, right before `open_position` is called. Fails open (leaves size unchanged) if the orderbook fetch errors, rather than blocking a trade over a data hiccup.

Verified: a 100−dollar target sized against a thin 50-contract book at 40¢ correctly caps to $10 (50 × 0.5 × 0.40), while the same target against a deep 10,000-contract book passes through unchanged.
