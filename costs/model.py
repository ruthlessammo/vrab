"""Pure stateless cost model functions.

No side effects. No config imports. All parameters passed explicitly.
Runs identically in backtest and live.
"""


def calc_fill_price(
    side: str,
    intended_price: float,
    slippage_ticks: int,
    tick_size: float,
) -> float:
    """Calculate actual fill price after slippage.

    Long fills above intended, short fills below.
    Zero slippage returns intended exactly.
    """
    slip = slippage_ticks * tick_size
    if side == "long":
        return intended_price + slip
    return intended_price - slip


def calc_maker_rebate(notional_usd: float, rebate_rate: float) -> float:
    """Calculate maker rebate (positive value)."""
    return abs(notional_usd * rebate_rate)


def calc_taker_fee(notional_usd: float, fee_rate: float) -> float:
    """Calculate taker fee (positive value)."""
    return abs(notional_usd * fee_rate)


def calc_funding_cost(
    side: str,
    notional_usd: float,
    hourly_rate: float,
    hold_hours: float,
) -> float:
    """Calculate funding cost over hold period.

    Long pays when rate positive (returns negative = cost).
    Long receives when rate negative (returns positive = income).
    Short is inverse.
    Always calculated on notional, not equity.
    """
    if hourly_rate == 0.0:
        return 0.0
    raw = notional_usd * hourly_rate * hold_hours
    if side == "long":
        return -raw  # long pays positive funding
    return raw  # short receives positive funding


def calc_round_trip_cost(
    side: str,
    notional_usd: float,
    entry_price: float,
    exit_price: float,
    maker_both_sides: bool,
    hourly_funding_rate: float,
    hold_hours: float,
    tick_size: float,
    slippage_ticks_entry: int,
    slippage_ticks_exit: int,
    maker_rebate_rate: float = 0.0002,
    taker_fee_rate: float = 0.00035,
) -> dict:
    """Calculate full round-trip cost breakdown.

    Returns dict with keys: slippage_usd, entry_fee_usd, exit_fee_usd,
    funding_usd, gross_pnl_usd, total_cost_usd, net_pnl_usd.
    """
    # Gross PnL from price movement
    qty = notional_usd / entry_price
    if side == "long":
        gross_pnl = qty * (exit_price - entry_price)
    else:
        gross_pnl = qty * (entry_price - exit_price)

    # Slippage (always a cost = negative)
    entry_slip = slippage_ticks_entry * tick_size * qty
    exit_slip = slippage_ticks_exit * tick_size * qty
    slippage_usd = -(entry_slip + exit_slip)

    # Fees: positive = rebate received, negative = fee paid
    if maker_both_sides:
        entry_fee = notional_usd * maker_rebate_rate
        exit_fee = notional_usd * maker_rebate_rate
    else:
        entry_fee = notional_usd * maker_rebate_rate
        exit_fee = -(notional_usd * taker_fee_rate)

    # Funding
    funding = calc_funding_cost(side, notional_usd, hourly_funding_rate, hold_hours)

    total_cost = slippage_usd + entry_fee + exit_fee + funding
    net_pnl = gross_pnl + total_cost

    return {
        "slippage_usd": slippage_usd,
        "entry_fee_usd": entry_fee,
        "exit_fee_usd": exit_fee,
        "funding_usd": funding,
        "gross_pnl_usd": gross_pnl,
        "total_cost_usd": total_cost,
        "net_pnl_usd": net_pnl,
    }


def calc_break_even_move(
    side: str,
    notional_usd: float,
    entry_price: float,
    maker_both_sides: bool,
    hourly_funding_rate: float,
    hold_hours: float,
    tick_size: float,
    slippage_ticks: int,
    maker_rebate_rate: float = 0.0002,
    taker_fee_rate: float = 0.00035,
) -> float:
    """Minimum price move in USD for trade to break even after all costs.

    Always positive. Increases with fees, slippage, and funding costs.
    """
    qty = notional_usd / entry_price

    # Slippage cost (entry + exit with same slippage)
    slip_cost = 2 * slippage_ticks * tick_size * qty

    # Fee cost
    if maker_both_sides:
        fee_cost = -(2 * notional_usd * maker_rebate_rate)
    else:
        fee_cost = -(notional_usd * maker_rebate_rate) + notional_usd * taker_fee_rate

    # Funding cost magnitude
    funding = calc_funding_cost(side, notional_usd, hourly_funding_rate, hold_hours)
    funding_cost = -funding if funding < 0 else 0.0  # only count costs, not income

    total_cost = slip_cost + fee_cost + funding_cost
    if total_cost < 0:
        total_cost = 0.0  # rebates exceed costs

    # Price move needed = total_cost / qty
    return total_cost / qty if qty > 0 else 0.0


def calc_leveraged_round_trip(
    side: str,
    equity_usd: float,
    leverage: float,
    entry_price: float,
    exit_price: float,
    maker_both_sides: bool,
    hourly_funding_rate: float,
    hold_hours: float,
    tick_size: float,
    slippage_ticks_entry: int,
    slippage_ticks_exit: int,
    maintenance_margin_rate: float,
) -> dict:
    """Full leveraged round-trip with liquidation price.

    Returns all keys from calc_round_trip_cost plus:
    notional_usd, margin_used_usd, leverage, liq_price, equity_return_pct.
    """
    from risk.liquidation import calc_liquidation_price

    notional_usd = equity_usd * leverage
    margin_used_usd = equity_usd

    rt = calc_round_trip_cost(
        side=side,
        notional_usd=notional_usd,
        entry_price=entry_price,
        exit_price=exit_price,
        maker_both_sides=maker_both_sides,
        hourly_funding_rate=hourly_funding_rate,
        hold_hours=hold_hours,
        tick_size=tick_size,
        slippage_ticks_entry=slippage_ticks_entry,
        slippage_ticks_exit=slippage_ticks_exit,
    )

    liq_price = calc_liquidation_price(side, entry_price, leverage, maintenance_margin_rate)
    equity_return_pct = rt["net_pnl_usd"] / equity_usd if equity_usd > 0 else 0.0

    return {
        **rt,
        "notional_usd": notional_usd,
        "margin_used_usd": margin_used_usd,
        "leverage": leverage,
        "liq_price": liq_price,
        "equity_return_pct": equity_return_pct,
    }
