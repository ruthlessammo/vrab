"""Pure stateless signal functions.

No side effects. No config imports. All parameters passed explicitly.
Runs identically in backtest and live.
"""

import math
from dataclasses import dataclass


@dataclass
class VWAPState:
    """VWAP calculation result with Bollinger-style bands."""
    vwap: float
    upper_band: float
    lower_band: float
    std_dev: float
    candle_count: int


@dataclass
class RegimeState:
    """Market regime classification."""
    is_trending: bool
    adx: float
    ema: float
    trend_direction: str  # "up" | "down" | "flat"


@dataclass
class SignalResult:
    """Output of signal generation."""
    signal: str  # "long_entry" | "short_entry" | "exit_long" | "exit_short" | "none"
    price: float
    stop_price: float
    exit_price: float
    sigma_dist: float
    vwap_state: VWAPState | None
    regime: RegimeState | None
    block_reason: str | None


def calc_vwap(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
) -> VWAPState:
    """Calculate VWAP with volume-weighted standard deviation bands.

    VWAP = sum(TP_i * V_i) / sum(V_i)  where TP = (H + L + C) / 3
    std_dev = sqrt(sum(V_i * (TP_i - VWAP)^2) / sum(V_i))

    Raises ValueError if fewer than 2 candles provided.
    """
    n = len(closes)
    if n < 2:
        raise ValueError(f"calc_vwap requires at least 2 candles, got {n}")

    tp_vol_sum = 0.0
    vol_sum = 0.0
    typical_prices = []

    for i in range(n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        typical_prices.append(tp)
        tp_vol_sum += tp * volumes[i]
        vol_sum += volumes[i]

    if vol_sum == 0.0:
        vwap = typical_prices[-1]
        return VWAPState(
            vwap=vwap, upper_band=vwap, lower_band=vwap,
            std_dev=0.0, candle_count=n,
        )

    vwap = tp_vol_sum / vol_sum

    # Volume-weighted standard deviation
    var_sum = 0.0
    for i in range(n):
        diff = typical_prices[i] - vwap
        var_sum += volumes[i] * diff * diff
    std_dev = math.sqrt(var_sum / vol_sum)

    upper_band = vwap + 2.0 * std_dev
    lower_band = vwap - 2.0 * std_dev

    return VWAPState(
        vwap=vwap,
        upper_band=upper_band,
        lower_band=lower_band,
        std_dev=std_dev,
        candle_count=n,
    )


def sigma_distance(price: float, vwap_state: VWAPState) -> float:
    """Signed sigma distance from VWAP. Positive = above."""
    if vwap_state.std_dev == 0.0:
        return 0.0
    return (price - vwap_state.vwap) / vwap_state.std_dev


def calc_ema(values: list[float], period: int) -> float:
    """Calculate current EMA value.

    Returns 0.0 for empty list.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]

    multiplier = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * multiplier + ema * (1.0 - multiplier)
    return ema


def calc_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    """Calculate Wilder's ADX.

    Returns 0.0 if fewer than period + 1 candles.
    """
    n = len(closes)
    if n < period + 1:
        return 0.0

    # True Range, +DM, -DM
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, n):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0.0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    # Wilder smoothing (first value = sum of first `period` values)
    def wilder_smooth(values: list[float], p: int) -> list[float]:
        smoothed = [sum(values[:p])]
        for v in values[p:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / p + v)
        return smoothed

    atr = wilder_smooth(tr_list, period)
    smooth_plus = wilder_smooth(plus_dm_list, period)
    smooth_minus = wilder_smooth(minus_dm_list, period)

    # DI values
    dx_list = []
    for i in range(len(atr)):
        if atr[i] == 0.0:
            dx_list.append(0.0)
            continue
        plus_di = 100.0 * smooth_plus[i] / atr[i]
        minus_di = 100.0 * smooth_minus[i] / atr[i]
        di_sum = plus_di + minus_di
        if di_sum == 0.0:
            dx_list.append(0.0)
        else:
            dx_list.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_list) < period:
        return sum(dx_list) / len(dx_list) if dx_list else 0.0

    # ADX = Wilder smooth of DX
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def calc_regime(
    trend_closes: list[float],
    trend_highs: list[float],
    trend_lows: list[float],
    ema_period: int = 15,
    adx_period: int = 14,
    adx_threshold: float = 25.0,
) -> RegimeState:
    """Classify market regime from trend-timeframe candles."""
    adx = calc_adx(trend_highs, trend_lows, trend_closes, adx_period)
    ema = calc_ema(trend_closes, ema_period)
    is_trending = adx >= adx_threshold

    if not trend_closes:
        trend_direction = "flat"
    elif trend_closes[-1] > ema * 1.001:
        trend_direction = "up"
    elif trend_closes[-1] < ema * 0.999:
        trend_direction = "down"
    else:
        trend_direction = "flat"

    return RegimeState(
        is_trending=is_trending,
        adx=adx,
        ema=ema,
        trend_direction=trend_direction,
    )


def generate_signal(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    trend_closes: list[float],
    trend_highs: list[float],
    trend_lows: list[float],
    current_position_side: str | None,
    vwap_window: int = 96,
    entry_sigma: float = 2.0,
    exit_sigma: float = 0.0,
    stop_sigma: float = 3.0,
    ema_period: int = 15,
    adx_period: int = 14,
    adx_threshold: float = 25.0,
    funding_rate: float = 0.0,
    funding_block_threshold: float = 0.0003,
) -> SignalResult:
    """Generate trading signal from candle data.

    Executes decision tree in exact priority order.
    All parameters explicit — no config imports.
    """
    price = closes[-1] if closes else 0.0

    def _result(signal, stop=0.0, exit_p=0.0, sigma=0.0,
                vwap_st=None, regime=None, block=None):
        return SignalResult(
            signal=signal, price=price, stop_price=stop, exit_price=exit_p,
            sigma_dist=sigma, vwap_state=vwap_st, regime=regime,
            block_reason=block,
        )

    # 1. Insufficient data
    if len(closes) < vwap_window:
        return _result("none", block="insufficient_data")

    # 2. Calc VWAP on last vwap_window candles
    window_closes = closes[-vwap_window:]
    window_highs = highs[-vwap_window:]
    window_lows = lows[-vwap_window:]
    window_volumes = volumes[-vwap_window:]
    vwap_state = calc_vwap(window_closes, window_highs, window_lows, window_volumes)

    # 3. Calc regime
    regime = calc_regime(
        trend_closes, trend_highs, trend_lows,
        ema_period, adx_period, adx_threshold,
    )

    sigma = sigma_distance(price, vwap_state)

    # 4. Exit long: price >= VWAP
    if current_position_side == "long" and price >= vwap_state.vwap:
        return _result("exit_long", sigma=sigma, vwap_st=vwap_state, regime=regime)

    # 5. Exit short: price <= VWAP
    if current_position_side == "short" and price <= vwap_state.vwap:
        return _result("exit_short", sigma=sigma, vwap_st=vwap_state, regime=regime)

    # 6. Already in position
    if current_position_side is not None:
        return _result("none", sigma=sigma, vwap_st=vwap_state, regime=regime)

    # 7. Trending regime blocks entry
    if regime.is_trending:
        return _result(
            "none", sigma=sigma, vwap_st=vwap_state, regime=regime,
            block=f"trending_regime adx={regime.adx:.1f}",
        )

    std = vwap_state.std_dev

    # 8-9. Long entry
    if sigma <= -entry_sigma:
        if regime.trend_direction == "down":
            return _result(
                "none", sigma=sigma, vwap_st=vwap_state, regime=regime,
                block="counter_trend_long",
            )
        if funding_rate > funding_block_threshold:
            return _result(
                "none", sigma=sigma, vwap_st=vwap_state, regime=regime,
                block=f"funding_block rate={funding_rate:.5f}",
            )
        stop = vwap_state.lower_band - (stop_sigma - entry_sigma) * std
        return _result(
            "long_entry", stop=stop, exit_p=vwap_state.vwap,
            sigma=sigma, vwap_st=vwap_state, regime=regime,
        )

    # 10-11. Short entry
    if sigma >= entry_sigma:
        if regime.trend_direction == "up":
            return _result(
                "none", sigma=sigma, vwap_st=vwap_state, regime=regime,
                block="counter_trend_short",
            )
        if funding_rate < -funding_block_threshold:
            return _result(
                "none", sigma=sigma, vwap_st=vwap_state, regime=regime,
                block=f"funding_block rate={funding_rate:.5f}",
            )
        stop = vwap_state.upper_band + (stop_sigma - entry_sigma) * std
        return _result(
            "short_entry", stop=stop, exit_p=vwap_state.vwap,
            sigma=sigma, vwap_st=vwap_state, regime=regime,
        )

    # 12. No signal
    return _result("none", sigma=sigma, vwap_st=vwap_state, regime=regime)


# ---------------------------------------------------------------------------
# EMA Crossover + Volume Filter
# ---------------------------------------------------------------------------


def calc_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    """Calculate Average True Range (Wilder smoothing).

    Returns 0.0 if fewer than period + 1 candles.
    """
    n = len(closes)
    if n < 2:
        return 0.0

    tr_list = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list) if tr_list else 0.0

    # Wilder smoothing
    atr = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_ema_pair(values: list[float], period: int) -> tuple[float, float]:
    """Calculate EMA for current and previous value in one pass.

    Returns (ema_prev, ema_current). More efficient than calling calc_ema twice.
    """
    if len(values) < 2:
        v = values[0] if values else 0.0
        return v, v

    multiplier = 2.0 / (period + 1)
    ema = values[0]
    prev_ema = ema
    for v in values[1:]:
        prev_ema = ema
        ema = v * multiplier + ema * (1.0 - multiplier)
    return prev_ema, ema


def generate_signal_ema_cross(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    current_position_side: str | None,
    fast_ema_period: int = 9,
    slow_ema_period: int = 21,
    atr_period: int = 14,
    atr_stop_mult: float = 1.5,
    rr_ratio: float = 2.0,
    vol_ma_period: int = 20,
    vol_filter_mult: float = 1.0,
) -> SignalResult:
    """Generate signal from EMA crossover with volume confirmation.

    Entry: fast EMA crosses slow EMA + volume above average.
    Stop: ATR-based (entry ± atr_mult × ATR).
    Target: fixed R:R from stop distance.
    Exit: reverse crossover.
    """
    price = closes[-1] if closes else 0.0
    min_data = max(slow_ema_period + 2, atr_period + 1, vol_ma_period + 1)

    def _result(signal, stop=0.0, exit_p=0.0, block=None):
        return SignalResult(
            signal=signal, price=price, stop_price=stop, exit_price=exit_p,
            sigma_dist=0.0, vwap_state=None, regime=None,
            block_reason=block,
        )

    if len(closes) < min_data:
        return _result("none", block="insufficient_data")

    # Current and previous EMA values in one pass each
    fast_prev, fast_now = calc_ema_pair(closes, fast_ema_period)
    slow_prev, slow_now = calc_ema_pair(closes, slow_ema_period)

    # ATR for stop placement
    atr = calc_atr(highs, lows, closes, atr_period)
    if atr == 0.0:
        return _result("none", block="zero_atr")

    # Exit signals first (reverse cross while in position)
    if current_position_side == "long" and fast_now < slow_now and fast_prev >= slow_prev:
        return _result("exit_long")

    if current_position_side == "short" and fast_now > slow_now and fast_prev <= slow_prev:
        return _result("exit_short")

    if current_position_side is not None:
        return _result("none")

    # Detect crossover
    cross_up = fast_prev <= slow_prev and fast_now > slow_now
    cross_down = fast_prev >= slow_prev and fast_now < slow_now

    if not cross_up and not cross_down:
        return _result("none")

    # Volume filter (0 = disabled)
    if vol_filter_mult > 0 and len(volumes) >= vol_ma_period:
        vol_avg = sum(volumes[-vol_ma_period:]) / vol_ma_period
        if vol_avg > 0 and volumes[-1] < vol_filter_mult * vol_avg:
            return _result("none", block="low_volume")

    stop_dist = atr * atr_stop_mult
    target_dist = stop_dist * rr_ratio

    if cross_up:
        stop = price - stop_dist
        target = price + target_dist
        return _result("long_entry", stop=stop, exit_p=target)

    if cross_down:
        stop = price + stop_dist
        target = price - target_dist
        return _result("short_entry", stop=stop, exit_p=target)

    return _result("none")
