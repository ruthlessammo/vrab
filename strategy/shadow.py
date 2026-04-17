"""Shadow book — tracks hypothetical PnL of blocked trades.

Pure helper class with no I/O. Both live and backtest engines can use it.
Shadow positions use the same evaluate_exit() and cost functions as real trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from costs.model import calc_round_trip_cost
from strategy.core import (
    CoreDecision, TradingParams, ExitAction,
    calc_position_size, evaluate_exit,
)


@dataclass
class ShadowPosition:
    """Hypothetical position from a blocked entry signal."""
    side: str
    entry_price: float
    stop_price: float
    target_price: float
    size_usd: float
    entry_ts: int
    hold_candles: int = 0
    block_reason: str = ""
    sigma_at_entry: float = 0.0
    adx_at_entry: float = 0.0
    trend_direction_at_entry: str = ""


@dataclass
class ShadowTrade:
    """Completed shadow trade with hypothetical PnL."""
    symbol: str
    side: str
    block_reason: str
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    size_usd: float
    entry_ts: int
    exit_ts: int
    hold_candles: int
    exit_reason: str
    pnl_usd: float
    net_pnl_usd: float
    slippage_usd: float
    entry_fee_usd: float
    exit_fee_usd: float
    funding_usd: float
    sigma_at_entry: float = 0.0
    adx_at_entry: float = 0.0
    trend_direction_at_entry: str = ""


class ShadowBook:
    """Track hypothetical positions for blocked entry signals."""

    def __init__(self, params: TradingParams, symbol: str = "BTC",
                 max_positions: int = 20):
        self._params = params
        self._symbol = symbol
        self._max_positions = max_positions
        self._positions: list[ShadowPosition] = []

    def on_blocked_entry(
        self, decision: CoreDecision, candle_ts: int, equity: float,
    ) -> None:
        """Create shadow position from a blocked entry signal."""
        sig = decision.signal_result
        if not sig or sig.signal not in ("long_entry", "short_entry"):
            return
        if not decision.block_reason:
            return

        side = "long" if sig.signal == "long_entry" else "short"
        entry_price = sig.price
        stop_price = sig.stop_price if sig.stop_price else 0.0
        target_price = sig.exit_price if sig.exit_price else 0.0

        if not stop_price or not entry_price:
            return

        size_usd, _ = calc_position_size(
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            leverage=self._params.target_leverage,
            risk_per_trade=self._params.risk_per_trade,
            margin_utilisation_cap=self._params.margin_utilisation_cap,
        )
        if size_usd <= 0:
            return

        pos = ShadowPosition(
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            size_usd=size_usd,
            entry_ts=candle_ts,
            block_reason=decision.block_reason,
            sigma_at_entry=sig.sigma_dist,
            adx_at_entry=sig.regime.adx if sig.regime else 0.0,
            trend_direction_at_entry=sig.regime.trend_direction if sig.regime else "",
        )
        self._positions.append(pos)

        # Cap positions — drop oldest
        if len(self._positions) > self._max_positions:
            self._positions = self._positions[-self._max_positions:]

    def on_candle(
        self, candle_high: float, candle_low: float,
        candle_close: float, candle_ts: int,
    ) -> list[ShadowTrade]:
        """Evaluate all shadow positions against new candle.

        Returns list of completed shadow trades.
        """
        completed: list[ShadowTrade] = []
        remaining: list[ShadowPosition] = []

        for pos in self._positions:
            pos.hold_candles += 1
            exit_decision = evaluate_exit(
                candle_high=candle_high,
                candle_low=candle_low,
                candle_close=candle_close,
                position_side=pos.side,
                position_entry_price=pos.entry_price,
                position_stop_price=pos.stop_price,
                position_target_price=pos.target_price,
                hold_candles=pos.hold_candles,
                params=self._params,
                signal_result=None,  # no signal exit for shadows
            )

            if exit_decision.action == "exit":
                ea: ExitAction = exit_decision.exit_action
                hold_hours = (pos.hold_candles * 5) / 60  # 5m candles
                slippage_ticks_exit = (
                    self._params.slippage_ticks_entry if ea.is_maker
                    else self._params.slippage_ticks_stop
                )
                costs = calc_round_trip_cost(
                    side=pos.side,
                    notional_usd=pos.size_usd,
                    entry_price=pos.entry_price,
                    exit_price=ea.exit_price,
                    maker_both_sides=ea.is_maker,
                    hourly_funding_rate=self._params.hourly_funding_rate,
                    hold_hours=hold_hours,
                    tick_size=self._params.tick_size,
                    slippage_ticks_entry=self._params.slippage_ticks_entry,
                    slippage_ticks_exit=slippage_ticks_exit,
                    maker_rebate_rate=self._params.maker_rebate_rate,
                    taker_fee_rate=self._params.taker_fee_rate,
                )
                completed.append(ShadowTrade(
                    symbol=self._symbol,
                    side=pos.side,
                    block_reason=pos.block_reason,
                    entry_price=pos.entry_price,
                    exit_price=ea.exit_price,
                    stop_price=pos.stop_price,
                    target_price=pos.target_price,
                    size_usd=pos.size_usd,
                    entry_ts=pos.entry_ts,
                    exit_ts=candle_ts,
                    hold_candles=pos.hold_candles,
                    exit_reason=ea.exit_type,
                    pnl_usd=costs["gross_pnl_usd"],
                    net_pnl_usd=costs["net_pnl_usd"],
                    slippage_usd=costs["slippage_usd"],
                    entry_fee_usd=costs["entry_fee_usd"],
                    exit_fee_usd=costs["exit_fee_usd"],
                    funding_usd=costs["funding_usd"],
                    sigma_at_entry=pos.sigma_at_entry,
                    adx_at_entry=pos.adx_at_entry,
                    trend_direction_at_entry=pos.trend_direction_at_entry,
                ))
            else:
                remaining.append(pos)

        self._positions = remaining
        return completed

    def clear(self) -> None:
        """Clear all shadow positions (day boundary, restart)."""
        self._positions.clear()

    @property
    def count(self) -> int:
        return len(self._positions)
