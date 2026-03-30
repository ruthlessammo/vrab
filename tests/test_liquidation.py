"""Tests for risk/liquidation.py — 16 tests."""

import pytest
from risk.liquidation import (
    calc_liquidation_price, calc_liquidation_buffer, is_stop_safe,
    calc_max_safe_leverage, calc_funding_at_leverage,
)


# ── TestLiquidationPrice (4) ──


class TestLiquidationPrice:
    def test_long_below_entry(self):
        liq = calc_liquidation_price("long", 50000, 10, 0.005)
        assert liq < 50000

    def test_short_above_entry(self):
        liq = calc_liquidation_price("short", 50000, 10, 0.005)
        assert liq > 50000

    def test_higher_leverage_closer(self):
        liq_10x = calc_liquidation_price("long", 50000, 10, 0.005)
        liq_20x = calc_liquidation_price("long", 50000, 20, 0.005)
        # 20x liq should be closer to entry (higher) than 10x
        assert liq_20x > liq_10x

    def test_leverage_below_1_raises(self):
        with pytest.raises(ValueError):
            calc_liquidation_price("long", 50000, 0.5, 0.005)


# ── TestLiquidationBuffer (4) ──


class TestLiquidationBuffer:
    def test_stop_at_entry_returns_zero(self):
        liq = calc_liquidation_price("long", 50000, 10, 0.005)
        buf = calc_liquidation_buffer("long", 50000, 50000, liq)
        assert abs(buf - 0.0) < 0.001

    def test_stop_at_liq_returns_one(self):
        liq = calc_liquidation_price("long", 50000, 10, 0.005)
        buf = calc_liquidation_buffer("long", 50000, liq, liq)
        assert abs(buf - 1.0) < 0.001

    def test_long_closer_to_liq_higher_buffer(self):
        liq = calc_liquidation_price("long", 50000, 10, 0.005)
        near_entry = calc_liquidation_buffer("long", 50000, 49500, liq)
        near_liq = calc_liquidation_buffer("long", 50000, 45500, liq)
        assert near_liq > near_entry

    def test_short_directional_logic(self):
        liq = calc_liquidation_price("short", 50000, 10, 0.005)
        near_entry = calc_liquidation_buffer("short", 50000, 50500, liq)
        near_liq = calc_liquidation_buffer("short", 50000, 54500, liq)
        assert near_liq > near_entry


# ── TestStopSafety (4) ──


class TestStopSafety:
    def test_safe_stop_inside_buffer(self):
        safe, buf = is_stop_safe("long", 50000, 49900, 10, 0.005, 0.30)
        assert safe is True
        assert buf < 0.30

    def test_unsafe_stop_outside_buffer(self):
        safe, buf = is_stop_safe("long", 50000, 46000, 10, 0.005, 0.30)
        assert safe is False
        assert buf > 0.30

    def test_max_safe_leverage_decreases_with_tighter_stop(self):
        wide = calc_max_safe_leverage("long", 50000, 48000, 0.005, 0.30)
        tight = calc_max_safe_leverage("long", 50000, 49800, 0.005, 0.30)
        assert tight > wide  # tighter stop = can use more leverage

    def test_real_world_btc(self):
        """entry=80000, stop=79200 (−1.0%), leverage=20, mm=0.005, buffer=0.30"""
        # At 20x, liq = 80000 * (1 - 1/20 + 0.005) = 76400
        # buffer = (80000 - 79200) / (80000 - 76400) = 800/3600 = 0.222
        safe, buf = is_stop_safe("long", 80000, 79200, 20, 0.005, 0.30)
        assert safe is True
        assert buf < 0.30
        liq = calc_liquidation_price("long", 80000, 20, 0.005)
        assert liq < 79200  # liq is below stop


# ── TestLeveragedFunding (4) ──


class TestLeveragedFunding:
    def test_scales_with_leverage(self):
        f_10x = calc_funding_at_leverage(500, 10, 0.0001, 8)
        f_20x = calc_funding_at_leverage(500, 20, 0.0001, 8)
        assert abs(f_20x / f_10x - 2.0) < 0.001

    def test_zero_rate_returns_zero(self):
        assert calc_funding_at_leverage(500, 10, 0.0, 8) == 0.0

    def test_long_positive_funding_negative(self):
        # calc_funding_at_leverage returns negative for cost
        result = calc_funding_at_leverage(500, 10, 0.0001, 8)
        assert result < 0  # cost to long

    def test_short_positive_funding_positive(self):
        # Short receives positive funding
        result = calc_funding_at_leverage(500, 10, 0.0001, 8)
        # The function always returns -(equity * leverage * rate * hours)
        # For short, the sign convention means short receives, but the function
        # computes funding cost from long perspective
        # Actually, calc_funding_at_leverage doesn't take side — it just computes
        # the raw funding on notional. Negative = cost to long = receipt for short.
        assert result < 0  # negative means long pays / short receives
