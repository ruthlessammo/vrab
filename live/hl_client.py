"""Thin wrapper around the Hyperliquid Python SDK.

Isolates all SDK interaction so the engine never touches the SDK directly.
All methods are synchronous — the engine calls them via asyncio.to_thread().
"""

import logging
from typing import Any

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

logger = logging.getLogger(__name__)


class HLClient:
    """Hyperliquid API client wrapping the SDK."""

    def __init__(self, private_key: str, base_url: str, wallet_address: str):
        self._private_key = private_key
        self._base_url = base_url
        self._wallet_address = wallet_address
        self._exchange: Exchange | None = None
        self._info: Info | None = None
        self._sz_decimals: int = 5  # BTC default, updated on connect

    def connect(self, symbol: str = "BTC", leverage: int = 10) -> None:
        """Initialize SDK objects and set leverage."""
        wallet = eth_account.Account.from_key(self._private_key)
        self._exchange = Exchange(wallet=wallet, base_url=self._base_url)
        self._info = Info(base_url=self._base_url, skip_ws=True)

        # Cache szDecimals for the symbol
        meta = self._info.meta()
        for asset in meta["universe"]:
            if asset["name"] == symbol:
                self._sz_decimals = asset["szDecimals"]
                break

        # Set cross-margin leverage
        result = self._exchange.update_leverage(leverage, symbol, is_cross=True)
        logger.info("Set leverage %dx for %s: %s", leverage, symbol, result)

    @property
    def info(self) -> Info:
        """Access the Info object (for WebSocket subscriptions in feed.py)."""
        if self._info is None:
            raise RuntimeError("Client not connected — call connect() first")
        return self._info

    @property
    def address(self) -> str:
        return self._wallet_address

    def _round_size(self, size_btc: float) -> float:
        """Round size to the correct number of decimal places."""
        return round(size_btc, self._sz_decimals)

    def get_balance(self) -> float:
        """Get account equity. Works for both classic and unified HL accounts.

        Classic: perps accountValue has everything.
        Unified: perps shows only margin, spot has full balance.
        Using max() handles both correctly.
        """
        state = self._info.user_state(self._wallet_address)
        perps = float(state["crossMarginSummary"]["accountValue"])

        spot_usdc = 0.0
        try:
            spot_state = self._info.spot_user_state(self._wallet_address)
            for bal in spot_state.get("balances", []):
                if bal["coin"] == "USDC":
                    spot_usdc = float(bal["total"])
                    break
        except Exception as e:
            logger.warning("Failed to read spot balance: %s", e)

        unrealized_pnl = 0.0
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            unrealized_pnl += float(pos.get("unrealizedPnl", 0))

        return max(perps, spot_usdc + unrealized_pnl)

    def get_position(self, symbol: str = "BTC") -> dict | None:
        """Get current position for symbol.

        Returns dict with side, size_btc, entry_price, liq_price, unrealized_pnl
        or None if no position.
        """
        state = self._info.user_state(self._wallet_address)
        for ap in state["assetPositions"]:
            pos = ap["position"]
            if pos["coin"] == symbol:
                szi = float(pos["szi"])
                if szi == 0:
                    continue
                return {
                    "side": "long" if szi > 0 else "short",
                    "size_btc": abs(szi),
                    "entry_price": float(pos["entryPx"]) if pos["entryPx"] else 0.0,
                    "liq_price": float(pos["liquidationPx"]) if pos["liquidationPx"] else 0.0,
                    "unrealized_pnl": float(pos["unrealizedPnl"]),
                }
        return None

    def get_open_orders(self, symbol: str = "BTC") -> list[dict]:
        """Get open orders for symbol."""
        orders = self._info.open_orders(self._wallet_address)
        return [o for o in orders if o["coin"] == symbol]

    def get_mid_price(self, symbol: str = "BTC") -> float:
        """Get current mid price."""
        mids = self._info.all_mids()
        return float(mids[symbol])

    def get_funding_rate(self, symbol: str = "BTC") -> float:
        """Get current funding rate for symbol."""
        meta, ctxs = self._info.meta_and_asset_ctxs()
        for asset_meta, ctx in zip(meta["universe"], ctxs):
            if asset_meta["name"] == symbol:
                return float(ctx["funding"])
        return 0.0

    def place_limit_order(
        self,
        symbol: str,
        is_buy: bool,
        size_btc: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = True,
    ) -> dict:
        """Place a limit order. Post-only (ALO) by default for maker rebate."""
        sz = self._round_size(size_btc)
        tif = "Alo" if post_only else "Gtc"
        order_type = {"limit": {"tif": tif}}

        result = self._exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=sz,
            limit_px=price,
            order_type=order_type,
            reduce_only=reduce_only,
        )
        logger.info(
            "Limit order: %s %s %.5f @ %.1f (post_only=%s reduce_only=%s) -> %s",
            "BUY" if is_buy else "SELL", symbol, sz, price, post_only, reduce_only, result,
        )
        return result

    def place_market_order(
        self,
        symbol: str,
        is_buy: bool,
        size_btc: float,
        reduce_only: bool = False,
        slippage: float = 0.01,
    ) -> dict:
        """Place a market order (IOC). Used for stops and timeouts."""
        sz = self._round_size(size_btc)

        if reduce_only:
            result = self._exchange.market_close(
                coin=symbol, sz=sz, slippage=slippage,
            )
        else:
            result = self._exchange.market_open(
                name=symbol, is_buy=is_buy, sz=sz, slippage=slippage,
            )
        logger.info(
            "Market order: %s %s %.5f (reduce_only=%s) -> %s",
            "BUY" if is_buy else "SELL", symbol, sz, reduce_only, result,
        )
        return result

    def place_trigger_order(
        self,
        symbol: str,
        is_buy: bool,
        size_btc: float,
        trigger_price: float,
        tpsl: str = "sl",
    ) -> dict:
        """Place a trigger (stop-loss or take-profit) order on HL."""
        sz = self._round_size(size_btc)
        order_type = {
            "trigger": {
                "triggerPx": trigger_price,
                "isMarket": True,
                "tpsl": tpsl,
            }
        }
        result = self._exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=sz,
            limit_px=trigger_price,
            order_type=order_type,
            reduce_only=True,
        )
        logger.info(
            "Trigger order: %s %s %.5f trigger=%.1f (%s) -> %s",
            "BUY" if is_buy else "SELL", symbol, sz, trigger_price, tpsl, result,
        )
        return result

    def cancel_order(self, symbol: str, oid: int) -> dict:
        """Cancel a single order."""
        result = self._exchange.cancel(symbol, oid)
        logger.info("Cancel order %d: %s", oid, result)
        return result

    def cancel_all_orders(self, symbol: str = "BTC") -> None:
        """Cancel all open orders for symbol."""
        orders = self.get_open_orders(symbol)
        if not orders:
            return
        cancels = [{"coin": symbol, "oid": o["oid"]} for o in orders]
        result = self._exchange.bulk_cancel(cancels)
        logger.info("Bulk cancel %d orders: %s", len(cancels), result)

    def query_order_status(self, oid: int) -> dict:
        """Query order status by oid."""
        return self._info.query_order_by_oid(self._wallet_address, oid)

    def schedule_cancel(self, cancel_time_ms: int) -> Any:
        """Schedule auto-cancel of all orders at future time (dead-man switch)."""
        result = self._exchange.schedule_cancel(cancel_time_ms)
        logger.debug("Schedule cancel at %d: %s", cancel_time_ms, result)
        return result

    def unschedule_cancel(self) -> Any:
        """Remove scheduled cancel."""
        return self._exchange.schedule_cancel(None)
