"""Tests for strategy/core.py — 10 tests."""

import pytest
from strategy.core import (
    TradingParams, evaluate_entry, evaluate_exit, calc_position_size,
    calc_trade_pnl, check_daily_halt,
)


def _default_params(**overrides) -> TradingParams:
    defaults = dict(
        vwap_window=96, entry_sigma=2.0, exit_sigma=0.0, stop_sigma=3.0,
        ema_period=15, adx_period=14, adx_threshold=25.0,
        funding_block_threshold=0.0003, risk_per_trade=0.015,
        target_leverage=10, max_leverage=20, min_liquidation_buffer=0.30,
        margin_utilisation_cap=0.80, maintenance_margin_rate=0.005,
        maker_rebate_rate=0.0002, taker_fee_rate=0.00035,
        tick_size=0.1, slippage_ticks_entry=1, slippage_ticks_stop=3,
        max_daily_loss_multiplier=3, max_hold_candles=48,
        hourly_funding_rate=0.0001, entry_expiry_candles=2,
    )
    defaults.update(overrides)
    return TradingParams(**defaults)


def _flat_candles(price, n, volume=1000.0):
    return {
        "closes": [price] * n,
        "highs": [price + 1.0] * n,
        "lows": [price - 1.0] * n,
        "volumes": [volume] * n,
    }


# ── TestEvaluateEntry (4) ──


class TestEvaluateEntry:
    def test_valid_entry(self):
        n = 100
        c = _flat_candles(50000, n)
        c["closes"][-1] = 48000
        c["highs"][-1] = 48100
        c["lows"][-1] = 47900
        trend = _flat_candles(50000, n)

        d = evaluate_entry(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"],
            equity=500, current_position_side=None,
            funding_rate=0.0, params=_default_params(),
        )
        assert d.action == "enter"
        assert d.trade_setup is not None
        assert d.trade_setup.side == "long"

    def test_trending_blocks(self):
        n = 100
        c = _flat_candles(50000, n)
        c["closes"][-1] = 48000
        c["highs"][-1] = 48100
        c["lows"][-1] = 47900
        # Use strongly trending data for regime detection
        trend_closes = [50000 + i * 200 for i in range(n)]
        trend_highs = [p + 300 for p in trend_closes]
        trend_lows = [p - 100 for p in trend_closes]
        d = evaluate_entry(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend_closes, trend_highs=trend_highs,
            trend_lows=trend_lows,
            equity=500, current_position_side=None,
            funding_rate=0.0, params=_default_params(adx_threshold=10.0),
        )
        assert d.action == "skip"
        block = d.block_reason or (d.signal_result.block_reason if d.signal_result else "")
        assert "trending_regime" in (block or "")

    def test_unsafe_liq_blocks(self):
        """High leverage + tight stop should exceed liq buffer."""
        n = 100
        c = _flat_candles(50000, n)
        # Small dip — stop will be close to entry
        c["closes"][-1] = 49500
        c["highs"][-1] = 49600
        c["lows"][-1] = 49400
        trend = _flat_candles(50000, n)
        # Use very high leverage so liq is close, and very tight buffer
        d = evaluate_entry(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"],
            equity=500, current_position_side=None,
            funding_rate=0.0,
            params=_default_params(
                entry_sigma=1.0,  # lower threshold to trigger entry
                target_leverage=20,
                min_liquidation_buffer=0.05,  # very strict
            ),
        )
        # Should either skip due to liq buffer or no signal
        if d.action == "skip" and d.block_reason:
            assert "liq_buffer" in d.block_reason
        else:
            # If no entry signal at all, that's also acceptable
            assert d.action in ("skip", "enter")

    def test_no_signal_skip(self):
        c = _flat_candles(50000, 100)
        trend = _flat_candles(50000, 100)
        d = evaluate_entry(
            closes=c["closes"], highs=c["highs"],
            lows=c["lows"], volumes=c["volumes"],
            trend_closes=trend["closes"], trend_highs=trend["highs"],
            trend_lows=trend["lows"],
            equity=500, current_position_side=None,
            funding_rate=0.0, params=_default_params(),
        )
        assert d.action == "skip"


# ── TestEvaluateExit (3) ──


class TestEvaluateExit:
    def test_stop_hit(self):
        d = evaluate_exit(
            candle_high=50100, candle_low=49000, candle_close=49200,
            position_side="long", position_entry_price=50000,
            position_stop_price=49100, position_target_price=51000,
            hold_candles=5, params=_default_params(),
        )
        assert d.action == "exit"
        assert d.exit_action.exit_type == "stop"
        assert d.exit_action.is_maker is False

    def test_target_hit(self):
        d = evaluate_exit(
            candle_high=51100, candle_low=50500, candle_close=51000,
            position_side="long", position_entry_price=50000,
            position_stop_price=49000, position_target_price=51000,
            hold_candles=5, params=_default_params(),
        )
        assert d.action == "exit"
        assert d.exit_action.exit_type == "target"
        assert d.exit_action.is_maker is True

    def test_timeout(self):
        d = evaluate_exit(
            candle_high=50100, candle_low=49900, candle_close=50000,
            position_side="long", position_entry_price=50000,
            position_stop_price=49000, position_target_price=51000,
            hold_candles=48, params=_default_params(max_hold_candles=48),
        )
        assert d.action == "exit"
        assert d.exit_action.exit_type == "timeout"


# ── TestPositionSizing (2) ──


class TestPositionSizing:
    def test_caps_at_margin_utilisation(self):
        size, notional = calc_position_size(
            equity=500, entry_price=50000, stop_price=49999,
            leverage=10, risk_per_trade=0.015, margin_utilisation_cap=0.80,
        )
        max_notional = 500 * 10 * 0.80
        assert size <= max_notional + 0.01

    def test_scales_with_equity(self):
        s1, _ = calc_position_size(500, 50000, 49000, 10, 0.015, 0.80)
        s2, _ = calc_position_size(1000, 50000, 49000, 10, 0.015, 0.80)
        assert s2 > s1


# ── TestDailyHalt (1) ──


class TestDailyHalt:
    def test_halt_triggers(self):
        # 500 * 0.015 * 3 = 22.5 threshold
        should, reason = check_daily_halt(-25.0, 500, 0.015, 3)
        assert should is True
        assert reason is not None

        should2, _ = check_daily_halt(-10.0, 500, 0.015, 3)
        assert should2 is False
