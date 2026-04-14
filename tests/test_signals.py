"""Tests for strategy/signals.py — 22 tests."""

import pytest
from strategy.signals import (
    VWAPState, calc_vwap, sigma_distance, calc_ema, calc_adx,
    calc_regime, generate_signal,
)


def flat_candles(price, n, volume=1000.0):
    """Helper: constant-price candles."""
    return {
        "closes": [price] * n,
        "highs": [price + 1.0] * n,
        "lows": [price - 1.0] * n,
        "volumes": [volume] * n,
    }


def trending_up_candles(start, step, n, volume=1000.0):
    """Helper: linearly increasing candles."""
    closes = [start + i * step for i in range(n)]
    return {
        "closes": closes,
        "highs": [c + step for c in closes],
        "lows": [c - step * 0.5 for c in closes],
        "volumes": [volume] * n,
    }


# ── TestVWAP (5) ──


class TestVWAP:
    def test_flat_market_vwap_near_price(self):
        c = flat_candles(50000, 100)
        vs = calc_vwap(c["closes"], c["highs"], c["lows"], c["volumes"])
        assert abs(vs.vwap - 50000) < 1.0

    def test_flat_market_symmetric_bands(self):
        c = flat_candles(50000, 100)
        vs = calc_vwap(c["closes"], c["highs"], c["lows"], c["volumes"])
        upper_dist = vs.upper_band - vs.vwap
        lower_dist = vs.vwap - vs.lower_band
        assert abs(upper_dist - lower_dist) < 0.01

    def test_constant_prices_zero_std(self):
        n = 50
        price = 50000.0
        vs = calc_vwap([price] * n, [price] * n, [price] * n, [1000.0] * n)
        assert vs.std_dev == 0.0

    def test_fewer_than_2_raises(self):
        with pytest.raises(ValueError):
            calc_vwap([50000], [50000], [50000], [1000])

    def test_sigma_distance(self):
        vs = VWAPState(vwap=50000, upper_band=50500, lower_band=49500,
                       std_dev=500, candle_count=100)
        assert abs(sigma_distance(50500, vs) - 1.0) < 0.001
        assert abs(sigma_distance(49500, vs) - (-1.0)) < 0.001
        assert abs(sigma_distance(50000, vs) - 0.0) < 0.001


# ── TestEMA (4) ──


class TestEMA:
    def test_constant_returns_constant(self):
        result = calc_ema([100.0] * 50, 15)
        assert abs(result - 100.0) < 0.001

    def test_single_value(self):
        assert calc_ema([42.0], 15) == 42.0

    def test_empty_returns_zero(self):
        assert calc_ema([], 15) == 0.0

    def test_rising_tail_pushes_above(self):
        values = [100.0] * 30 + [110.0] * 10
        result = calc_ema(values, 15)
        assert result > 100.0


# ── TestADX (3) ──


class TestADX:
    def test_insufficient_data_returns_zero(self):
        assert calc_adx([100] * 10, [100] * 10, [100] * 10, period=14) == 0.0

    def test_flat_market_low_adx(self):
        n = 100
        c = flat_candles(50000, n)
        adx = calc_adx(c["highs"], c["lows"], c["closes"], period=14)
        assert adx < 25.0

    def test_trending_higher_than_flat(self):
        flat = flat_candles(50000, 100)
        trend = trending_up_candles(50000, 100, 100)
        adx_flat = calc_adx(flat["highs"], flat["lows"], flat["closes"])
        adx_trend = calc_adx(trend["highs"], trend["lows"], trend["closes"])
        assert adx_trend > adx_flat


# ── TestRegime (3) ──


class TestRegime:
    def test_flat_not_trending(self):
        c = flat_candles(50000, 100)
        r = calc_regime(c["closes"], c["highs"], c["lows"])
        assert r.is_trending is False

    def test_strong_uptrend_direction(self):
        c = trending_up_candles(50000, 200, 100)
        r = calc_regime(c["closes"], c["highs"], c["lows"])
        assert r.trend_direction == "up"

    def test_trending_higher_adx(self):
        flat = flat_candles(50000, 100)
        trend = trending_up_candles(50000, 200, 100)
        r_flat = calc_regime(flat["closes"], flat["highs"], flat["lows"])
        r_trend = calc_regime(trend["closes"], trend["highs"], trend["lows"])
        assert r_trend.adx > r_flat.adx


# ── TestGenerateSignal (7) ──


class TestGenerateSignal:
    def _flat_signal_args(self, n=100, price=50000):
        c = flat_candles(price, n)
        return {
            "closes": c["closes"], "highs": c["highs"],
            "lows": c["lows"], "volumes": c["volumes"],
            "trend_closes": c["closes"], "trend_highs": c["highs"],
            "trend_lows": c["lows"], "current_position_side": None,
        }

    def test_flat_at_vwap_none(self):
        sig = generate_signal(**self._flat_signal_args(n=100), vwap_window=96)
        assert sig.signal == "none"

    def test_insufficient_data(self):
        sig = generate_signal(**self._flat_signal_args(n=50), vwap_window=96)
        assert sig.signal == "none"
        assert sig.block_reason == "insufficient_data"

    def test_deep_below_band_long_entry(self):
        # Create data where price dips well below VWAP
        n = 100
        c = flat_candles(50000, n)
        # Last candle is deep below
        c["closes"][-1] = 48000
        c["highs"][-1] = 48100
        c["lows"][-1] = 47900
        trend = flat_candles(50000, n)  # flat regime
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side=None,
            vwap_window=96,
        )
        assert sig.signal == "long_entry"

    def test_trending_blocks_entry(self):
        n = 100
        c = flat_candles(50000, n)
        c["closes"][-1] = 48000
        c["highs"][-1] = 48100
        c["lows"][-1] = 47900
        # Use trending data for regime
        trend = trending_up_candles(50000, 200, n)
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side=None,
            vwap_window=96, adx_threshold=10.0,
        )
        assert sig.signal == "none"
        assert "trending_regime" in sig.block_reason

    def test_high_funding_blocks_long(self):
        n = 100
        c = flat_candles(50000, n)
        c["closes"][-1] = 48000
        c["highs"][-1] = 48100
        c["lows"][-1] = 47900
        trend = flat_candles(50000, n)
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side=None,
            vwap_window=96, funding_rate=0.001,
            funding_block_threshold=0.0003,
        )
        assert sig.signal == "none"
        assert "funding_block" in sig.block_reason

    def test_in_long_above_vwap_exit(self):
        n = 100
        c = flat_candles(50000, n)
        c["closes"][-1] = 50100  # above VWAP
        trend = flat_candles(50000, n)
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side="long",
            vwap_window=96,
        )
        assert sig.signal == "exit_long"

    def test_in_short_below_vwap_exit(self):
        n = 100
        c = flat_candles(50000, n)
        c["closes"][-1] = 49900  # below VWAP
        trend = flat_candles(50000, n)
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side="short",
            vwap_window=96,
        )
        assert sig.signal == "exit_short"

    def test_short_blocked_in_uptrend(self):
        """Short entry should be blocked when trend_direction is 'up'."""
        n = 100
        c = flat_candles(50000, n)
        # Price well above VWAP → would trigger short_entry
        c["closes"][-1] = 52000
        c["highs"][-1] = 52100
        c["lows"][-1] = 51900
        # Trend candles: mostly flat but last close above EMA → trend_direction="up"
        # Use flat data with last few candles nudged up to get EMA crossover
        trend = flat_candles(50000, n)
        for i in range(n - 5, n):
            trend["closes"][i] = 50200
            trend["highs"][i] = 50300
            trend["lows"][i] = 50100
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side=None,
            vwap_window=96, adx_threshold=99.0,  # disable regime filter to isolate counter-trend
        )
        assert sig.signal == "none"
        assert "counter_trend" in sig.block_reason

    def test_long_blocked_in_downtrend(self):
        """Long entry should be blocked when trend_direction is 'down'."""
        n = 100
        c = flat_candles(50000, n)
        # Price well below VWAP → would trigger long_entry
        c["closes"][-1] = 48000
        c["highs"][-1] = 48100
        c["lows"][-1] = 47900
        # Trend candles: mostly flat but last few candles nudged down → trend_direction="down"
        trend = flat_candles(50000, n)
        for i in range(n - 5, n):
            trend["closes"][i] = 49800
            trend["highs"][i] = 49900
            trend["lows"][i] = 49700
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side=None,
            vwap_window=96, adx_threshold=99.0,
        )
        assert sig.signal == "none"
        assert "counter_trend" in sig.block_reason

    def test_short_allowed_in_flat_regime(self):
        """Short entry should still work when trend is flat."""
        n = 100
        c = flat_candles(50000, n)
        c["closes"][-1] = 52000
        c["highs"][-1] = 52100
        c["lows"][-1] = 51900
        trend = flat_candles(50000, n)
        sig = generate_signal(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"], current_position_side=None,
            vwap_window=96,
        )
        assert sig.signal == "short_entry"
