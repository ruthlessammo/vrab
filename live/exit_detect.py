"""Pure functions for mid-candle exit detection."""


def infer_exit(side: str, stop: float, target: float, fill_px: float) -> str:
    """Infer whether a fill was a stop or target hit."""
    if side == "long":
        return "target" if fill_px >= target else "stop"
    else:
        return "target" if fill_px <= target else "stop"


def extract_exit_price(fills: list, entry_ts: int, close_side: str) -> float | None:
    """Extract exit price from HL fills list.

    Returns the last matching fill price, or None if no fills match.
    """
    close_fills = [
        f for f in fills
        if f["time"] >= entry_ts - 60_000 and f["side"] == close_side
    ]
    return float(close_fills[-1]["px"]) if close_fills else None
