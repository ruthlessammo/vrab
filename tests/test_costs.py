"""Tests for costs/model.py — 24 tests."""

import pytest
from costs.model import (
    calc_fill_price, calc_maker_rebate, calc_taker_fee,
    calc_funding_cost, calc_round_trip_cost, calc_break_even_move,
    calc_leveraged_round_trip,
)


# ── TestFillPrice (3) ──


class TestFillPrice:
    def test_long_fills_above(self):
        result = calc_fill_price("long", 50000.0, 2, 0.1)
        assert result == 50000.2

    def test_short_fills_below(self):
        result = calc_fill_price("short", 50000.0, 2, 0.1)
        assert result == 49999.8

    def test_zero_slippage_exact(self):
        result = calc_fill_price("long", 50000.0, 0, 0.1)
        assert result == 50000.0


# ── TestFees (4) ──


class TestFees:
    def test_maker_rebate_positive(self):
        assert calc_maker_rebate(10000, 0.0002) > 0

    def test_taker_fee_positive(self):
        assert calc_taker_fee(10000, 0.00035) > 0

    def test_taker_greater_than_maker(self):
        notional = 10000
        assert calc_taker_fee(notional, 0.00035) > calc_maker_rebate(notional, 0.0002)

    def test_linear_scaling(self):
        r1 = calc_maker_rebate(10000, 0.0002)
        r2 = calc_maker_rebate(20000, 0.0002)
        assert abs(r2 - 2 * r1) < 0.001
        f1 = calc_taker_fee(10000, 0.00035)
        f2 = calc_taker_fee(20000, 0.00035)
        assert abs(f2 - 2 * f1) < 0.001


# ── TestFunding (5) ──


class TestFunding:
    def test_long_pays_positive_rate(self):
        result = calc_funding_cost("long", 10000, 0.0001, 8)
        assert result < 0  # cost

    def test_long_receives_negative_rate(self):
        result = calc_funding_cost("long", 10000, -0.0001, 8)
        assert result > 0  # income

    def test_short_receives_positive_rate(self):
        result = calc_funding_cost("short", 10000, 0.0001, 8)
        assert result > 0  # income

    def test_short_pays_negative_rate(self):
        result = calc_funding_cost("short", 10000, -0.0001, 8)
        assert result < 0  # cost

    def test_zero_rate_returns_zero(self):
        assert calc_funding_cost("long", 10000, 0.0, 8) == 0.0


# ── TestRoundTrip (6) ──


class TestRoundTrip:
    def _base_args(self, **overrides):
        defaults = dict(
            side="long", notional_usd=5000, entry_price=50000,
            exit_price=50500, maker_both_sides=True,
            hourly_funding_rate=0.0001, hold_hours=4,
            tick_size=0.1, slippage_ticks_entry=1, slippage_ticks_exit=1,
        )
        defaults.update(overrides)
        return defaults

    def test_net_equals_gross_plus_cost(self):
        rt = calc_round_trip_cost(**self._base_args())
        assert abs(rt["net_pnl_usd"] - (rt["gross_pnl_usd"] + rt["total_cost_usd"])) < 0.001

    def test_maker_cheaper_than_taker_exit(self):
        maker = calc_round_trip_cost(**self._base_args(maker_both_sides=True))
        taker = calc_round_trip_cost(**self._base_args(maker_both_sides=False))
        assert maker["net_pnl_usd"] > taker["net_pnl_usd"]

    def test_stop_costs_more_than_target(self):
        target = calc_round_trip_cost(**self._base_args(slippage_ticks_exit=1))
        stop = calc_round_trip_cost(**self._base_args(slippage_ticks_exit=3))
        assert target["total_cost_usd"] > stop["total_cost_usd"]  # less negative = more costly... wait
        # Actually: more slippage = more negative total_cost
        assert stop["total_cost_usd"] < target["total_cost_usd"]

    def test_longer_hold_increases_funding(self):
        short_hold = calc_round_trip_cost(**self._base_args(hold_hours=1))
        long_hold = calc_round_trip_cost(**self._base_args(hold_hours=8))
        # Long pays positive funding, so longer hold = more negative funding
        assert long_hold["funding_usd"] < short_hold["funding_usd"]

    def test_profitable_trade_positive_net(self):
        rt = calc_round_trip_cost(**self._base_args(exit_price=51000))
        assert rt["net_pnl_usd"] > 0

    def test_losing_trade_amplified_by_costs(self):
        rt = calc_round_trip_cost(**self._base_args(exit_price=49800))
        assert rt["net_pnl_usd"] < rt["gross_pnl_usd"]


# ── TestBreakEven (4) ──


class TestBreakEven:
    def _base_args(self, **overrides):
        defaults = dict(
            side="long", notional_usd=5000, entry_price=50000,
            maker_both_sides=True, hourly_funding_rate=0.0001,
            hold_hours=4, tick_size=0.1, slippage_ticks=1,
        )
        defaults.update(overrides)
        return defaults

    def test_returns_positive(self):
        be = calc_break_even_move(**self._base_args())
        assert be > 0

    def test_taker_higher_than_maker(self):
        maker_be = calc_break_even_move(**self._base_args(maker_both_sides=True))
        taker_be = calc_break_even_move(**self._base_args(maker_both_sides=False))
        assert taker_be > maker_be

    def test_more_slippage_higher_be(self):
        low = calc_break_even_move(**self._base_args(slippage_ticks=1))
        high = calc_break_even_move(**self._base_args(slippage_ticks=3))
        assert high > low

    def test_longer_hold_higher_be_for_long(self):
        short_hold = calc_break_even_move(**self._base_args(hold_hours=1))
        long_hold = calc_break_even_move(**self._base_args(hold_hours=8))
        assert long_hold > short_hold


# ── TestLeveragedRoundTrip (2) ──


class TestLeveragedRoundTrip:
    def test_equity_return_pct(self):
        result = calc_leveraged_round_trip(
            side="long", equity_usd=500, leverage=10,
            entry_price=50000, exit_price=50500,
            maker_both_sides=True, hourly_funding_rate=0.0001,
            hold_hours=4, tick_size=0.1,
            slippage_ticks_entry=1, slippage_ticks_exit=1,
            maintenance_margin_rate=0.005,
        )
        expected_return = result["net_pnl_usd"] / 500
        assert abs(result["equity_return_pct"] - expected_return) < 0.001

    def test_liq_prices(self):
        long_result = calc_leveraged_round_trip(
            side="long", equity_usd=500, leverage=10,
            entry_price=50000, exit_price=50500,
            maker_both_sides=True, hourly_funding_rate=0.0001,
            hold_hours=4, tick_size=0.1,
            slippage_ticks_entry=1, slippage_ticks_exit=1,
            maintenance_margin_rate=0.005,
        )
        assert long_result["liq_price"] < 50000

        short_result = calc_leveraged_round_trip(
            side="short", equity_usd=500, leverage=10,
            entry_price=50000, exit_price=49500,
            maker_both_sides=True, hourly_funding_rate=0.0001,
            hold_hours=4, tick_size=0.1,
            slippage_ticks_entry=1, slippage_ticks_exit=1,
            maintenance_margin_rate=0.005,
        )
        assert short_result["liq_price"] > 50000
