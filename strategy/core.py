"""Shared trading core — the decision pipeline used by both backtest and live.

All signal generation, risk checks, sizing, and trade evaluation run through
this module. Engines are thin adapters that feed candles and execute actions.

No I/O. No config imports except build_params_from_config().
"""

import logging
from dataclasses import dataclass, field

from strategy.signals import SignalResult, generate_signal, generate_signal_ema_cross
from costs.model import (
    calc_fill_price,
    calc_maker_rebate,
    calc_taker_fee,
    calc_funding_cost,
    calc_round_trip_cost,
)
from risk.liquidation import (
    calc_liquidation_price,
    calc_liquidation_buffer,
    is_stop_safe,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradingParams:
    """All tunable parameters in one frozen object."""
    vwap_window: int
    entry_sigma: float
    exit_sigma: float
    stop_sigma: float
    ema_period: int
    adx_period: int
    adx_threshold: float
    funding_block_threshold: float
    risk_per_trade: float
    target_leverage: int
    max_leverage: int
    min_liquidation_buffer: float
    margin_utilisation_cap: float
    maintenance_margin_rate: float
    maker_rebate_rate: float
    taker_fee_rate: float
    tick_size: float
    slippage_ticks_entry: int
    slippage_ticks_stop: int
    max_daily_loss_multiplier: int
    max_hold_candles: int
    hourly_funding_rate: float
    entry_expiry_candles: int = 2
    # Signal mode
    signal_mode: str = "vwap"  # "vwap" | "ema_cross"
    # EMA crossover params (used when signal_mode="ema_cross")
    fast_ema_period: int = 9
    slow_ema_period: int = 21
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_ratio: float = 2.0
    vol_ma_period: int = 20
    vol_filter_mult: float = 1.0


@dataclass
class TradeSetup:
    """Validated, ready-to-execute trade."""
    signal: str
    side: str
    entry_price: float
    stop_price: float
    target_price: float
    size_usd: float
    notional_usd: float
    leverage: float
    liq_price: float
    liq_buffer_ratio: float
    margin_required: float
    estimated_costs: dict
    signal_result: SignalResult


@dataclass
class ExitAction:
    """Exit decision."""
    exit_type: str  # "stop" | "target" | "signal" | "timeout" | "circuit_breaker"
    exit_price: float
    is_maker: bool
    signal_result: SignalResult | None = None


@dataclass
class CoreDecision:
    """What the core returns each candle."""
    action: str  # "enter" | "exit" | "hold" | "skip"
    trade_setup: TradeSetup | None = None
    exit_action: ExitAction | None = None
    block_reason: str | None = None
    signal_result: SignalResult | None = None


def build_params_from_config() -> TradingParams:
    """Build TradingParams from config.py — the ONE place that reads config."""
    from config import (
        VWAP_WINDOW, VWAP_ENTRY_SIGMA, VWAP_EXIT_SIGMA, VWAP_STOP_SIGMA,
        TREND_EMA_PERIOD, ADX_PERIOD, ADX_THRESHOLD, FUNDING_RATE_BLOCK,
        RISK_PER_TRADE, TARGET_LEVERAGE, MAX_LEVERAGE, MIN_LIQUIDATION_BUFFER,
        MARGIN_UTILISATION_CAP, HL_MAINTENANCE_MARGIN, MAKER_REBATE_RATE,
        TAKER_FEE_RATE, TICK_SIZE, SLIPPAGE_TICKS_ENTRY, SLIPPAGE_TICKS_STOP,
        MAX_DAILY_LOSS_MULTIPLIER, BACKTEST_HOURLY_FUNDING_RATE,
        ENTRY_EXPIRY_CANDLES,
    )

    max_hold_candles = 48  # 240 mins / 5 min candles

    return TradingParams(
        vwap_window=VWAP_WINDOW,
        entry_sigma=VWAP_ENTRY_SIGMA,
        exit_sigma=VWAP_EXIT_SIGMA,
        stop_sigma=VWAP_STOP_SIGMA,
        ema_period=TREND_EMA_PERIOD,
        adx_period=ADX_PERIOD,
        adx_threshold=ADX_THRESHOLD,
        funding_block_threshold=FUNDING_RATE_BLOCK,
        risk_per_trade=RISK_PER_TRADE,
        target_leverage=TARGET_LEVERAGE,
        max_leverage=MAX_LEVERAGE,
        min_liquidation_buffer=MIN_LIQUIDATION_BUFFER,
        margin_utilisation_cap=MARGIN_UTILISATION_CAP,
        maintenance_margin_rate=HL_MAINTENANCE_MARGIN,
        maker_rebate_rate=MAKER_REBATE_RATE,
        taker_fee_rate=TAKER_FEE_RATE,
        tick_size=TICK_SIZE,
        slippage_ticks_entry=SLIPPAGE_TICKS_ENTRY,
        slippage_ticks_stop=SLIPPAGE_TICKS_STOP,
        max_daily_loss_multiplier=MAX_DAILY_LOSS_MULTIPLIER,
        max_hold_candles=max_hold_candles,
        hourly_funding_rate=BACKTEST_HOURLY_FUNDING_RATE,
        entry_expiry_candles=ENTRY_EXPIRY_CANDLES,
    )


def calc_position_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    leverage: float,
    risk_per_trade: float,
    margin_utilisation_cap: float,
) -> tuple[float, float]:
    """Calculate position size based on risk and margin constraints.

    Returns (size_usd, notional_usd).
    size_usd is the risk-capped amount, notional_usd is the full leveraged exposure.
    """
    notional = equity * leverage
    risk_usd = equity * risk_per_trade
    stop_dist_pct = abs(entry_price - stop_price) / entry_price

    if stop_dist_pct == 0:
        return 0.0, 0.0

    risk_based_notional = risk_usd / stop_dist_pct
    size_usd = min(risk_based_notional, notional * margin_utilisation_cap)
    return size_usd, notional


def evaluate_entry(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    trend_closes: list[float],
    trend_highs: list[float],
    trend_lows: list[float],
    equity: float,
    current_position_side: str | None,
    funding_rate: float,
    params: TradingParams,
) -> CoreDecision:
    """Evaluate whether to enter a trade on the current candle.

    Calls generate_signal, then runs risk check + sizing if entry signal.
    """
    if params.signal_mode == "ema_cross":
        sig = generate_signal_ema_cross(
            closes=closes, highs=highs, lows=lows, volumes=volumes,
            current_position_side=current_position_side,
            fast_ema_period=params.fast_ema_period,
            slow_ema_period=params.slow_ema_period,
            atr_period=params.atr_period,
            atr_stop_mult=params.atr_stop_mult,
            rr_ratio=params.rr_ratio,
            vol_ma_period=params.vol_ma_period,
            vol_filter_mult=params.vol_filter_mult,
        )
    else:
        sig = generate_signal(
            closes=closes, highs=highs, lows=lows, volumes=volumes,
            trend_closes=trend_closes, trend_highs=trend_highs,
            trend_lows=trend_lows, current_position_side=current_position_side,
            vwap_window=params.vwap_window, entry_sigma=params.entry_sigma,
            exit_sigma=params.exit_sigma, stop_sigma=params.stop_sigma,
            ema_period=params.ema_period, adx_period=params.adx_period,
            adx_threshold=params.adx_threshold, funding_rate=funding_rate,
            funding_block_threshold=params.funding_block_threshold,
        )

    # Exit signals pass through
    if sig.signal in ("exit_long", "exit_short"):
        return CoreDecision(
            action="exit", signal_result=sig,
            exit_action=ExitAction(
                exit_type="signal", exit_price=sig.exit_price,
                is_maker=True, signal_result=sig,
            ),
        )

    # Not an entry signal
    if sig.signal not in ("long_entry", "short_entry"):
        return CoreDecision(
            action="skip", signal_result=sig, block_reason=sig.block_reason,
        )

    # --- Entry signal: run risk checks ---
    side = "long" if sig.signal == "long_entry" else "short"
    entry_price = sig.price
    stop_price = sig.stop_price

    # Liquidation check
    liq_price = calc_liquidation_price(
        side, entry_price, params.target_leverage,
        params.maintenance_margin_rate,
    )
    safe, buffer_ratio = is_stop_safe(
        side, entry_price, stop_price, params.target_leverage,
        params.maintenance_margin_rate, params.min_liquidation_buffer,
    )
    if not safe:
        return CoreDecision(
            action="skip", signal_result=sig,
            block_reason=f"liq_buffer_unsafe buffer={buffer_ratio:.3f}",
        )

    # Sizing
    size_usd, notional = calc_position_size(
        equity, entry_price, stop_price, params.target_leverage,
        params.risk_per_trade, params.margin_utilisation_cap,
    )
    if size_usd <= 0:
        return CoreDecision(
            action="skip", signal_result=sig,
            block_reason="zero_size",
        )

    margin_required = size_usd / params.target_leverage

    # Estimated costs
    estimated_costs = calc_round_trip_cost(
        side=side, notional_usd=size_usd, entry_price=entry_price,
        exit_price=sig.exit_price, maker_both_sides=True,
        hourly_funding_rate=params.hourly_funding_rate, hold_hours=1.0,
        tick_size=params.tick_size,
        slippage_ticks_entry=params.slippage_ticks_entry,
        slippage_ticks_exit=params.slippage_ticks_entry,
        maker_rebate_rate=params.maker_rebate_rate,
        taker_fee_rate=params.taker_fee_rate,
    )

    setup = TradeSetup(
        signal=sig.signal, side=side, entry_price=entry_price,
        stop_price=stop_price, target_price=sig.exit_price,
        size_usd=size_usd, notional_usd=notional,
        leverage=params.target_leverage, liq_price=liq_price,
        liq_buffer_ratio=buffer_ratio, margin_required=margin_required,
        estimated_costs=estimated_costs, signal_result=sig,
    )

    return CoreDecision(action="enter", trade_setup=setup, signal_result=sig)


def evaluate_exit(
    candle_high: float,
    candle_low: float,
    candle_close: float,
    position_side: str,
    position_entry_price: float,
    position_stop_price: float,
    position_target_price: float,
    hold_candles: int,
    params: TradingParams,
    signal_result: SignalResult | None = None,
) -> CoreDecision:
    """Evaluate exit conditions for current candle.

    Priority: stop > target > signal > timeout.
    """
    # 1. Stop hit
    if position_side == "long" and candle_low <= position_stop_price:
        fill = calc_fill_price("long", position_stop_price,
                               params.slippage_ticks_stop, params.tick_size)
        return CoreDecision(
            action="exit", signal_result=signal_result,
            exit_action=ExitAction("stop", fill, is_maker=False),
        )
    if position_side == "short" and candle_high >= position_stop_price:
        fill = calc_fill_price("short", position_stop_price,
                               params.slippage_ticks_stop, params.tick_size)
        return CoreDecision(
            action="exit", signal_result=signal_result,
            exit_action=ExitAction("stop", fill, is_maker=False),
        )

    # 2. Target hit
    if position_side == "long" and candle_high >= position_target_price:
        fill = calc_fill_price("long", position_target_price,
                               params.slippage_ticks_entry, params.tick_size)
        return CoreDecision(
            action="exit", signal_result=signal_result,
            exit_action=ExitAction("target", fill, is_maker=True),
        )
    if position_side == "short" and candle_low <= position_target_price:
        fill = calc_fill_price("short", position_target_price,
                               params.slippage_ticks_entry, params.tick_size)
        return CoreDecision(
            action="exit", signal_result=signal_result,
            exit_action=ExitAction("target", fill, is_maker=True),
        )

    # 3. Signal says exit
    if signal_result and signal_result.signal in ("exit_long", "exit_short"):
        return CoreDecision(
            action="exit", signal_result=signal_result,
            exit_action=ExitAction(
                "signal", signal_result.vwap_state.vwap if signal_result.vwap_state else candle_close,
                is_maker=True, signal_result=signal_result,
            ),
        )

    # 4. Timeout
    if hold_candles >= params.max_hold_candles:
        fill = calc_fill_price(position_side, candle_close,
                               params.slippage_ticks_stop, params.tick_size)
        return CoreDecision(
            action="exit", signal_result=signal_result,
            exit_action=ExitAction("timeout", fill, is_maker=False),
        )

    # Hold
    return CoreDecision(action="hold", signal_result=signal_result)


def calc_trade_pnl(
    side: str,
    entry_price: float,
    exit_price: float,
    size_usd: float,
    equity: float,
    leverage: float,
    is_maker_exit: bool,
    hold_hours: float,
    params: TradingParams,
) -> dict:
    """Calculate all PnL fields for a completed trade.

    Same calculation for backtest and live.
    """
    qty = size_usd / entry_price

    # Gross PnL
    if side == "long":
        gross_pnl = qty * (exit_price - entry_price)
    else:
        gross_pnl = qty * (entry_price - exit_price)

    # Slippage (already baked into fill prices, but we track the cost)
    entry_slip = params.slippage_ticks_entry * params.tick_size * qty
    if is_maker_exit:
        exit_slip = params.slippage_ticks_entry * params.tick_size * qty
    else:
        exit_slip = params.slippage_ticks_stop * params.tick_size * qty
    slippage_usd = -(entry_slip + exit_slip)

    # Fees
    entry_fee_usd = calc_maker_rebate(size_usd, params.maker_rebate_rate)
    if is_maker_exit:
        exit_fee_usd = calc_maker_rebate(size_usd, params.maker_rebate_rate)
    else:
        exit_fee_usd = -calc_taker_fee(size_usd, params.taker_fee_rate)

    # Funding
    funding_usd = calc_funding_cost(
        side, size_usd, params.hourly_funding_rate, hold_hours,
    )

    maker_rebate_usd = entry_fee_usd + (exit_fee_usd if exit_fee_usd > 0 else 0.0)
    net_pnl = gross_pnl + slippage_usd + entry_fee_usd + exit_fee_usd + funding_usd
    equity_return_pct = net_pnl / equity if equity > 0 else 0.0

    return {
        "pnl_usd": gross_pnl,
        "slippage_usd": slippage_usd,
        "entry_fee_usd": entry_fee_usd,
        "exit_fee_usd": exit_fee_usd,
        "funding_usd": funding_usd,
        "maker_rebate_usd": maker_rebate_usd,
        "net_pnl_usd": net_pnl,
        "equity_return_pct": equity_return_pct,
    }


def check_daily_halt(
    daily_loss_usd: float,
    equity: float,
    risk_per_trade: float,
    max_daily_loss_multiplier: int,
) -> tuple[bool, str | None]:
    """Check if daily drawdown threshold has been breached.

    Returns (should_halt, reason).
    """
    threshold = equity * risk_per_trade * max_daily_loss_multiplier
    if daily_loss_usd <= -threshold:
        return True, f"daily_dd_halt loss={daily_loss_usd:.2f} threshold={-threshold:.2f}"
    return False, None
