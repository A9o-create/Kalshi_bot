"""
Position sizing and risk limits. Pure functions, easy to unit-test.
"""

import config
from typing import Optional


def kelly_fraction(win_prob: float, win_cents: float, loss_cents: float) -> float:
    """
    Standard Kelly formula for a binary bet: f* = (p*b - q) / b
    where b = win_cents/loss_cents (odds), p = win_prob, q = 1 - p.
    Returns 0 if the edge is negative (Kelly says don't bet).
    """
    if loss_cents <= 0 or win_cents <= 0:
        return 0.0
    b = win_cents / loss_cents
    p = win_prob
    q = 1 - p
    f = (p * b - q) / b
    return max(0.0, f)


def cap_size_by_depth(size_dollars: float, price_cents: float, available_contracts: float, max_depth_fraction: float = None) -> float:
    """
    Caps a position to at most `max_depth_fraction` of the liquidity actually
    resting at the entry price level, so a capped-Kelly size doesn't walk a
    thin book. If available_contracts is 0/unknown (e.g. orderbook fetch
    failed), returns size_dollars unchanged rather than blocking the trade --
    fail open on missing depth data, not closed.
    """
    if max_depth_fraction is None:
        max_depth_fraction = config.MAX_BOOK_DEPTH_FRACTION
    if not available_contracts or available_contracts <= 0 or price_cents <= 0:
        return size_dollars
    depth_cap_dollars = available_contracts * max_depth_fraction * (price_cents / 100.0)
    return min(size_dollars, depth_cap_dollars)


def entry_favorability_multiplier(side_price_cents: float) -> float:
    """
    side_price_cents = the price actually paid for the side being bought
    (not necessarily the raw YES price -- for a NO position it's 100 minus
    that). Boosts sizing when the entry falls in the 35-50c "sweet spot":
    meaningfully cheaper fees than a 50c coin flip, while avoiding the
    illiquidity of deep longshots below 35c. Multiplier only, never a hard
    gate -- entries outside the band still size normally, just smaller.
    """
    if config.FAVORABLE_ENTRY_PRICE_MIN_CENTS <= side_price_cents <= config.FAVORABLE_ENTRY_PRICE_MAX_CENTS:
        return config.FAVORABLE_ENTRY_SIZE_MULTIPLIER
    return 1.0


def watchlist_multiplier(market_title: str) -> float:
    """
    Boosts sizing when the market's title matches one of WATCHLIST_PLAYERS
    (case-insensitive substring). Does NOT restrict which markets get
    scanned or traded -- every market is still evaluated normally, this
    only makes watchlist matches size larger, same "boost not gate" pattern
    as entry_favorability_multiplier above.
    """
    if not config.WATCHLIST_PLAYERS or not market_title:
        return 1.0
    title_lower = market_title.lower()
    if any(name.lower() in title_lower for name in config.WATCHLIST_PLAYERS):
        return config.WATCHLIST_SIZE_MULTIPLIER
    return 1.0


def position_size_dollars(balance: float, win_prob: float, win_cents: float, loss_cents: float,
                           side_price_cents: Optional[float] = None, market_title: Optional[str] = None) -> float:
    """
    Capped fractional Kelly: min(0.25 * Kelly, 3% of balance), with optional
    favorability boosts (price band + watchlist, stacked multiplicatively)
    applied before the hard cap -- so a favorable/watchlisted entry can size
    up to the cap sooner, but the cap itself (the true risk ceiling) never moves.
    """
    full_kelly = kelly_fraction(win_prob, win_cents, loss_cents)
    price_multiplier = entry_favorability_multiplier(side_price_cents) if side_price_cents is not None else 1.0
    watch_multiplier = watchlist_multiplier(market_title) if market_title is not None else 1.0
    fractional_kelly_dollars = balance * full_kelly * config.MAX_KELLY_FRACTION * price_multiplier * watch_multiplier
    hard_cap_dollars = balance * config.MAX_POSITION_PCT_OF_BALANCE
    return max(0.0, min(fractional_kelly_dollars, hard_cap_dollars))


def daily_loss_breached(daily_pnl: float, starting_balance: float) -> bool:
    """True if today's losses exceed the daily cap and the bot should halt."""
    if starting_balance <= 0:
        return True
    return daily_pnl <= -(starting_balance * config.DAILY_LOSS_CAP_PCT)


def hard_balance_floor_breached(current_balance: float) -> bool:
    """
    True if live balance has fallen to or below MIN_BALANCE_DOLLARS -- an
    absolute floor, independent of the percentage-based daily_loss_breached
    check above. Exists because that check has two real gaps: it's measured
    against a FIXED starting_balance (not the live, moving balance), and
    it's only evaluated once per full cycle, not immediately after each
    individual position close. This uses the live balance directly and is
    meant to be checked as close to every closing event as practical, as a
    genuine last-resort guarantee against ever reaching zero or negative,
    regardless of what the percentage math says.
    """
    return current_balance <= config.MIN_BALANCE_DOLLARS


def drawdown_from_peak_breached(daily_pnl: float, peak_daily_pnl: float, starting_balance: float) -> bool:
    """
    True if the session has given back too much from its own intraday peak
    -- distinct from daily_loss_breached, which only checks NET loss from
    the starting balance. A session that runs up +15% and gives back 24%
    of that (landing around -9% net) would NOT trip daily_loss_breached at
    an 18% cap, despite a 39-point peak-to-trough swing. This catches that
    case by tracking the high-water mark (peak_daily_pnl) and halting once
    the pullback from it exceeds PEAK_DRAWDOWN_CAP_PCT of starting balance.
    """
    if starting_balance <= 0:
        return True
    drawdown = peak_daily_pnl - daily_pnl
    return drawdown >= (starting_balance * config.PEAK_DRAWDOWN_CAP_PCT)


def can_open_new_position(open_position_count: int, open_events: set, event_ticker: str) -> tuple[bool, str]:
    """Checks concurrency and per-event exposure limits. Returns (allowed, reason_if_not)."""
    if open_position_count >= config.MAX_CONCURRENT_POSITIONS:
        return False, f"max concurrent positions ({config.MAX_CONCURRENT_POSITIONS}) reached"
    if event_ticker in open_events:
        return False, f"already have a position in event {event_ticker}"
    return True, ""


def check_cents_exit(direction: str, entry_yes_price_cents: float, current_yes_price_cents: float,
                      take_profit_cents: float, stop_loss_cents: float) -> Optional[str]:
    """
    For legs 1 (momentum) and 2 (reversion): exit thresholds are a flat cents
    move. `entry`/`current` are always the YES price regardless of which side
    is held (matches the convention execution.py's P&L calc already uses).
    Returns "take_profit", "stop_loss", or None.
    """
    price_delta = current_yes_price_cents - entry_yes_price_cents
    direction_multiplier = 1 if direction == "yes" else -1
    position_pnl_cents = price_delta * direction_multiplier

    if position_pnl_cents >= take_profit_cents:
        return "take_profit"
    if position_pnl_cents <= -stop_loss_cents:
        return "stop_loss"
    return None


def check_value_entry_exit(direction: str, entry_yes_price_cents: float, current_yes_price_cents: float) -> Optional[str]:
    """
    For leg 3 (value entry): exit is a PERCENT move on the side actually
    held, not a flat cents move -- a position bought at 10c needs a very
    different cents move than one bought at 45c to represent the same 15%
    gain. Converts YES-price terms to "the held side's own price" first.
    Returns "take_profit" or "stop_loss" or None.
    """
    if direction == "yes":
        entry_side_price = entry_yes_price_cents
        current_side_price = current_yes_price_cents
    else:
        entry_side_price = 100 - entry_yes_price_cents
        current_side_price = 100 - current_yes_price_cents

    if entry_side_price <= 0:
        return None  # avoid division by zero on a degenerate entry price

    pct_change = (current_side_price - entry_side_price) / entry_side_price

    # fires as soon as gain crosses the floor of the band; a gap straight
    # past the ceiling still counts as take-profit rather than being missed
    if pct_change >= config.VALUE_ENTRY_TAKE_PROFIT_MIN_PCT:
        return "take_profit"
    if pct_change <= -config.VALUE_ENTRY_STOP_LOSS_PCT:
        return "stop_loss"
    return None


def check_favorite_entry_exit(direction: str, entry_yes_price_cents: float, current_yes_price_cents: float) -> Optional[str]:
    """
    For leg 4 (favorite entry): same percent-of-held-side structure as
    check_value_entry_exit, but against FAVORITE_ENTRY_* thresholds --
    kept as a separate function (not a shared one with extra parameters)
    so this leg's thresholds can be tuned independently later, same
    pattern as every other leg having its own dedicated config.
    """
    if direction == "yes":
        entry_side_price = entry_yes_price_cents
        current_side_price = current_yes_price_cents
    else:
        entry_side_price = 100 - entry_yes_price_cents
        current_side_price = 100 - current_yes_price_cents

    if entry_side_price <= 0:
        return None

    pct_change = (current_side_price - entry_side_price) / entry_side_price

    if pct_change >= config.FAVORITE_ENTRY_TAKE_PROFIT_MIN_PCT:
        return "take_profit"
    if pct_change <= -config.FAVORITE_ENTRY_STOP_LOSS_PCT:
        return "stop_loss"
    return None


def check_trailing_stop_loss(peak_side_price_cents: float, current_side_price_cents: float) -> Optional[str]:
    """
    For favorite_entry_thin / favorite_entry_majority: no take-profit
    component at all by design -- those strategies ride to settlement for
    upside instead of selling early. This is the only exit besides actual
    settlement, protecting against the downside a genuinely uncapped hold
    would otherwise have. Anchored to the peak (best) side price observed
    since entry, not a fixed distance from entry -- so a position that
    spiked well past its entry price and then reverses still gets
    protected relative to that real high point, not given back to zero.

    Worked example this was built against: entry at 63c, price runs up to
    a 95c peak, then reverses -- triggers once price drops to
    95 - FAVORITE_ENTRY_TRAILING_STOP_CENTS, not 63 minus that amount.
    """
    if current_side_price_cents <= peak_side_price_cents - config.FAVORITE_ENTRY_TRAILING_STOP_CENTS:
        return "stop_loss"
    return None
