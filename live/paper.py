"""Paper trading client — same interface as HLClient, no real orders.

Simulates fills locally for safe testing of the full engine pipeline.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_next_oid = 1


def _gen_oid() -> int:
    global _next_oid
    oid = _next_oid
    _next_oid += 1
    return oid


class PaperClient:
    """Drop-in replacement for HLClient that simulates fills locally."""

    def __init__(self, capital: float):
        self._balance = capital
        self._position: dict | None = None  # {side, size_btc, entry_price, ...}
        self._open_orders: list[dict] = []
        self._mid_price = 0.0
        self._funding_rate = 0.0001
        self._sz_decimals = 5

    def connect(self, symbol: str = "BTC", leverage: int = 10) -> None:
        """No-op for paper mode."""
        logger.info("Paper client connected (symbol=%s, leverage=%dx)", symbol, leverage)

    @property
    def address(self) -> str:
        return "0xPAPER"

    def set_mid_price(self, price: float) -> None:
        """Update the simulated mid price (called by engine on each candle)."""
        self._mid_price = price

    def set_funding_rate(self, rate: float) -> None:
        """Update simulated funding rate."""
        self._funding_rate = rate

    def get_balance(self) -> float:
        """Return virtual balance."""
        unrealized = 0.0
        if self._position:
            size_usd = self._position["size_btc"] * self._position["entry_price"]
            if self._position["side"] == "long":
                unrealized = self._position["size_btc"] * (self._mid_price - self._position["entry_price"])
            else:
                unrealized = self._position["size_btc"] * (self._position["entry_price"] - self._mid_price)
        return self._balance + unrealized

    def get_position(self, symbol: str = "BTC") -> dict | None:
        """Return virtual position."""
        return self._position

    def get_open_orders(self, symbol: str = "BTC") -> list[dict]:
        """Return virtual open orders."""
        return list(self._open_orders)

    def get_mid_price(self, symbol: str = "BTC") -> float:
        return self._mid_price

    def get_funding_rate(self, symbol: str = "BTC") -> float:
        return self._funding_rate

    def place_limit_order(
        self,
        symbol: str,
        is_buy: bool,
        size_btc: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = True,
    ) -> dict:
        """Simulate limit order placement. Fills are checked by check_fills()."""
        oid = _gen_oid()
        order = {
            "oid": oid,
            "coin": symbol,
            "side": "B" if is_buy else "A",
            "sz": str(size_btc),
            "limitPx": str(price),
            "timestamp": int(time.time() * 1000),
            "reduce_only": reduce_only,
            "post_only": post_only,
            "_is_buy": is_buy,
            "_price": price,
            "_size_btc": size_btc,
            "_reduce_only": reduce_only,
        }
        self._open_orders.append(order)
        logger.info(
            "PAPER limit: %s %.5f @ %.1f (oid=%d, reduce_only=%s)",
            "BUY" if is_buy else "SELL", size_btc, price, oid, reduce_only,
        )
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def place_market_order(
        self,
        symbol: str,
        is_buy: bool,
        size_btc: float,
        reduce_only: bool = False,
        slippage: float = 0.01,
    ) -> dict:
        """Simulate immediate market fill at mid price."""
        oid = _gen_oid()
        fill_price = self._mid_price

        if reduce_only and self._position:
            # Close position
            side = self._position["side"]
            pnl_per_btc = (fill_price - self._position["entry_price"]) if side == "long" else (self._position["entry_price"] - fill_price)
            pnl = pnl_per_btc * min(size_btc, self._position["size_btc"])
            self._balance += pnl
            remaining = self._position["size_btc"] - size_btc
            if remaining <= 0:
                self._position = None
            else:
                self._position["size_btc"] = remaining
        elif not reduce_only:
            # Open position
            self._position = {
                "side": "long" if is_buy else "short",
                "size_btc": size_btc,
                "entry_price": fill_price,
                "liq_price": 0.0,
                "unrealized_pnl": 0.0,
            }

        logger.info(
            "PAPER market: %s %.5f @ %.1f (oid=%d, reduce_only=%s)",
            "BUY" if is_buy else "SELL", size_btc, fill_price, oid, reduce_only,
        )
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": oid, "avgPx": str(fill_price)}}]}}}

    def place_trigger_order(
        self,
        symbol: str,
        is_buy: bool,
        size_btc: float,
        trigger_price: float,
        tpsl: str = "sl",
    ) -> dict:
        """Simulate trigger order — stored, checked on each candle by engine."""
        oid = _gen_oid()
        order = {
            "oid": oid,
            "coin": symbol,
            "side": "B" if is_buy else "A",
            "sz": str(size_btc),
            "limitPx": str(trigger_price),
            "timestamp": int(time.time() * 1000),
            "_is_buy": is_buy,
            "_price": trigger_price,
            "_size_btc": size_btc,
            "_reduce_only": True,
            "_is_trigger": True,
            "_tpsl": tpsl,
        }
        self._open_orders.append(order)
        logger.info(
            "PAPER trigger: %s %.5f trigger=%.1f (%s, oid=%d)",
            "BUY" if is_buy else "SELL", size_btc, trigger_price, tpsl, oid,
        )
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def cancel_order(self, symbol: str, oid: int) -> dict:
        """Remove order from virtual order book."""
        self._open_orders = [o for o in self._open_orders if o["oid"] != oid]
        logger.info("PAPER cancel oid=%d", oid)
        return {"status": "ok"}

    def cancel_all_orders(self, symbol: str = "BTC") -> None:
        """Clear all virtual orders."""
        count = len(self._open_orders)
        self._open_orders.clear()
        logger.info("PAPER cancel all (%d orders)", count)

    def query_order_status(self, oid: int) -> dict:
        """Check if order is still open."""
        for o in self._open_orders:
            if o["oid"] == oid:
                return {"status": "open", "order": o}
        return {"status": "filled"}

    def schedule_cancel(self, cancel_time_ms: int) -> Any:
        """No-op for paper mode."""
        return {"status": "ok"}

    def unschedule_cancel(self) -> Any:
        """No-op for paper mode."""
        return {"status": "ok"}

    def check_fills(self, candle_high: float, candle_low: float) -> list[dict]:
        """Check if any resting limit orders would have filled on this candle.

        Returns list of filled orders. Called by the engine after each candle.
        """
        filled = []
        remaining = []

        for order in self._open_orders:
            if order.get("_is_trigger"):
                # Trigger orders checked by engine via evaluate_exit
                remaining.append(order)
                continue

            price = order["_price"]
            is_buy = order["_is_buy"]
            would_fill = (is_buy and candle_low <= price) or (not is_buy and candle_high >= price)

            if would_fill:
                filled.append(order)
                size_btc = order["_size_btc"]

                if order["_reduce_only"] and self._position:
                    side = self._position["side"]
                    pnl_per_btc = (price - self._position["entry_price"]) if side == "long" else (self._position["entry_price"] - price)
                    self._balance += pnl_per_btc * min(size_btc, self._position["size_btc"])
                    remaining_size = self._position["size_btc"] - size_btc
                    if remaining_size <= 0:
                        self._position = None
                    else:
                        self._position["size_btc"] = remaining_size
                elif not order["_reduce_only"]:
                    self._position = {
                        "side": "long" if is_buy else "short",
                        "size_btc": size_btc,
                        "entry_price": price,
                        "liq_price": 0.0,
                        "unrealized_pnl": 0.0,
                    }
                logger.info("PAPER fill: oid=%d %s %.5f @ %.1f", order["oid"], "BUY" if is_buy else "SELL", size_btc, price)
            else:
                remaining.append(order)

        self._open_orders = remaining
        return filled
