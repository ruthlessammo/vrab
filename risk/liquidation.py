"""Pure stateless risk and liquidation functions.

No side effects. No config imports. All parameters passed explicitly.
Runs identically in backtest and live.
"""


def calc_liquidation_price(
    side: str,
    entry_price: float,
    leverage: float,
    maintenance_margin_rate: float,
) -> float:
    """Calculate liquidation price.

    Long: entry × (1 - 1/leverage + maintenance_margin_rate)
    Short: entry × (1 + 1/leverage - maintenance_margin_rate)

    Raises ValueError if leverage < 1.
    """
    if leverage < 1:
        raise ValueError(f"Leverage must be >= 1, got {leverage}")

    if side == "long":
        return entry_price * (1.0 - 1.0 / leverage + maintenance_margin_rate)
    return entry_price * (1.0 + 1.0 / leverage - maintenance_margin_rate)


def calc_liquidation_buffer(
    side: str,
    entry_price: float,
    stop_price: float,
    liq_price: float,
) -> float:
    """Fraction (0.0–1.0) of distance from entry to liquidation the stop occupies.

    0.0 = stop at entry, 1.0 = stop at liquidation.

    Raises ValueError if liq_price is on the wrong side of entry.
    """
    if side == "long":
        entry_to_liq = entry_price - liq_price
        if entry_to_liq <= 0:
            raise ValueError(
                f"Long liq_price ({liq_price}) must be below entry ({entry_price})"
            )
        return (entry_price - stop_price) / entry_to_liq
    else:
        entry_to_liq = liq_price - entry_price
        if entry_to_liq <= 0:
            raise ValueError(
                f"Short liq_price ({liq_price}) must be above entry ({entry_price})"
            )
        return (stop_price - entry_price) / entry_to_liq


def is_stop_safe(
    side: str,
    entry_price: float,
    stop_price: float,
    leverage: float,
    maintenance_margin_rate: float,
    min_buffer: float,
) -> tuple[bool, float]:
    """Check if stop is safely inside the liquidation buffer.

    Returns (safe, buffer_ratio).
    safe = True if buffer_ratio <= min_buffer.
    """
    liq_price = calc_liquidation_price(side, entry_price, leverage, maintenance_margin_rate)
    buffer_ratio = calc_liquidation_buffer(side, entry_price, stop_price, liq_price)
    return buffer_ratio <= min_buffer, buffer_ratio


def calc_margin_required(notional_usd: float, leverage: float) -> float:
    """Calculate margin required: notional / leverage."""
    return notional_usd / leverage


def calc_notional(equity_usd: float, leverage: float) -> float:
    """Calculate notional: equity × leverage."""
    return equity_usd * leverage


def calc_max_safe_leverage(
    side: str,
    entry_price: float,
    stop_price: float,
    maintenance_margin_rate: float,
    min_buffer: float,
) -> float:
    """Maximum leverage at which the stop remains safe.

    Returns 1.0 if no safe leverage exists above 1.
    """
    # Binary search for max leverage where buffer_ratio <= min_buffer
    lo, hi = 1.0, 200.0
    best = 1.0

    for _ in range(50):  # sufficient precision
        mid = (lo + hi) / 2.0
        try:
            safe, _ = is_stop_safe(
                side, entry_price, stop_price, mid,
                maintenance_margin_rate, min_buffer,
            )
        except ValueError:
            hi = mid
            continue

        if safe:
            best = mid
            lo = mid
        else:
            hi = mid

    return round(best, 2)


def calc_funding_at_leverage(
    equity_usd: float,
    leverage: float,
    hourly_rate: float,
    hold_hours: float,
) -> float:
    """Funding on notional: equity × leverage × hourly_rate × hold_hours.

    Signed: negative = cost to long in positive funding environment.
    """
    return -(equity_usd * leverage * hourly_rate * hold_hours)
