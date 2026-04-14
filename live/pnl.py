"""Pure PnL calculation from Hyperliquid fills.

No I/O, no SDK calls. Takes pre-fetched fill and funding data.

HL closedPnl semantics:
- Open fills: closedPnl = -fee (fee paid to enter)
- Close fills: closedPnl = gross price PnL - close fee
- sum(closedPnl across all fills) = net PnL before funding

Therefore: net_pnl = sum(closedPnl) + funding (NO additional fee subtraction).
Gross price PnL = sum(closedPnl) + sum(fees).
"""

_OPEN_DIRS = {"Open Long", "Open Short"}


def calc_pnl_from_fills(
    fills: list[dict],
    funding_usd: float = 0.0,
    equity: float = 1.0,
) -> dict:
    """Calculate PnL breakdown from HL fills.

    Args:
        fills: HL user_fills filtered to this trade's time window.
        funding_usd: Pre-summed funding payment (negative = cost).
        equity: Account equity at entry (for return % calc).

    Returns:
        Dict matching Trade dataclass PnL fields.
    """
    if not fills:
        return {
            "pnl_usd": 0.0,
            "entry_fee_usd": 0.0,
            "exit_fee_usd": 0.0,
            "funding_usd": 0.0,
            "net_pnl_usd": 0.0,
            "equity_return_pct": 0.0,
        }

    # Split fills into open vs close and accumulate in one pass
    entry_fees = 0.0
    exit_fees = 0.0
    closed_pnl = 0.0
    for f in fills:
        fee = float(f["fee"])
        closed_pnl += float(f["closedPnl"])
        if f["dir"] in _OPEN_DIRS:
            entry_fees += fee
        else:
            exit_fees += fee

    total_fees = entry_fees + exit_fees

    # Gross price PnL = closed_pnl + total_fees
    gross_pnl = closed_pnl + total_fees

    # Net = sum(closedPnl) + funding
    net_pnl = closed_pnl + funding_usd

    return {
        "pnl_usd": gross_pnl,
        "entry_fee_usd": -entry_fees,   # negative = cost
        "exit_fee_usd": -exit_fees,     # negative = cost
        "funding_usd": funding_usd,
        "net_pnl_usd": net_pnl,
        "equity_return_pct": net_pnl / equity if equity > 0 else 0.0,
    }
