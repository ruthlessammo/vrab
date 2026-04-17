"""Tests for shadow book — blocked trade tracking."""

import pytest

from strategy.core import CoreDecision, TradingParams
from strategy.shadow import ShadowBook, ShadowPosition, ShadowTrade
from strategy.signals import SignalResult, VWAPState, RegimeState


def _make_params(**overrides) -> TradingParams:
    """Build TradingParams with test defaults."""
    defaults = dict(
        vwap_window=36, entry_sigma=2.5, exit_sigma=0.0, stop_sigma=4.5,
        ema_period=15, adx_period=14, adx_threshold=35.0,
        funding_block_threshold=0.0003, risk_per_trade=0.015,
        target_leverage=10, max_leverage=20, min_liquidation_buffer=0.30,
        margin_utilisation_cap=0.80, maintenance_margin_rate=0.006,
        maker_rebate_rate=0.0002, taker_fee_rate=0.00035, tick_size=1.0,
        slippage_ticks_entry=1, slippage_ticks_stop=3,
        max_daily_loss_multiplier=3, max_hold_candles=48,
        hourly_funding_rate=0.0001, counter_trend_min_adx=20.0,
    )
    defaults.update(overrides)
    return TradingParams(**defaults)


def _blocked_long_decision(price=75000.0, stop=74000.0, target=76000.0,
                           block_reason="counter_trend_long") -> CoreDecision:
    """Create a blocked long entry decision."""
    sig = SignalResult(
        signal="long_entry", price=price, stop_price=stop, exit_price=target,
        sigma_dist=-2.6,
        vwap_state=VWAPState(vwap=76000.0, upper_band=78000.0,
                             lower_band=74000.0, std_dev=500.0, candle_count=36),
        regime=RegimeState(is_trending=False, adx=10.9, ema=75500.0,
                           trend_direction="down"),
        block_reason=block_reason,
    )
    return CoreDecision(action="skip", block_reason=block_reason,
                        signal_result=sig)


def _blocked_short_decision(price=78000.0, stop=79000.0, target=76000.0,
                            block_reason="counter_trend_short") -> CoreDecision:
    """Create a blocked short entry decision."""
    sig = SignalResult(
        signal="short_entry", price=price, stop_price=stop, exit_price=target,
        sigma_dist=2.8,
        vwap_state=VWAPState(vwap=76000.0, upper_band=78000.0,
                             lower_band=74000.0, std_dev=500.0, candle_count=36),
        regime=RegimeState(is_trending=False, adx=10.9, ema=75500.0,
                           trend_direction="up"),
        block_reason=block_reason,
    )
    return CoreDecision(action="skip", block_reason=block_reason,
                        signal_result=sig)


class TestShadowBook:

    def test_shadow_created_on_block(self):
        params = _make_params()
        book = ShadowBook(params)
        decision = _blocked_long_decision()
        book.on_blocked_entry(decision, candle_ts=1000, equity=100.0)
        assert book.count == 1

    def test_no_shadow_without_block_reason(self):
        params = _make_params()
        book = ShadowBook(params)
        sig = SignalResult(
            signal="long_entry", price=75000.0, stop_price=74000.0,
            exit_price=76000.0, sigma_dist=-2.6, vwap_state=None,
            regime=None, block_reason=None,
        )
        decision = CoreDecision(action="skip", signal_result=sig)
        book.on_blocked_entry(decision, candle_ts=1000, equity=100.0)
        assert book.count == 0

    def test_shadow_exits_on_stop(self):
        params = _make_params()
        book = ShadowBook(params)
        book.on_blocked_entry(
            _blocked_long_decision(price=75000.0, stop=74000.0, target=76000.0),
            candle_ts=1000, equity=100.0,
        )
        # Candle that hits stop
        completed = book.on_candle(
            candle_high=75100.0, candle_low=73900.0,
            candle_close=74000.0, candle_ts=2000,
        )
        assert len(completed) == 1
        assert completed[0].exit_reason == "stop"
        assert completed[0].net_pnl_usd < 0  # stop = loss
        assert book.count == 0

    def test_shadow_exits_on_target(self):
        params = _make_params()
        book = ShadowBook(params)
        book.on_blocked_entry(
            _blocked_long_decision(price=75000.0, stop=74000.0, target=76000.0),
            candle_ts=1000, equity=100.0,
        )
        # Candle that hits target
        completed = book.on_candle(
            candle_high=76100.0, candle_low=75000.0,
            candle_close=76000.0, candle_ts=2000,
        )
        assert len(completed) == 1
        assert completed[0].exit_reason == "target"
        assert completed[0].net_pnl_usd > 0  # target = profit

    def test_shadow_exits_on_timeout(self):
        params = _make_params(max_hold_candles=3)
        book = ShadowBook(params)
        book.on_blocked_entry(
            _blocked_long_decision(price=75000.0, stop=74000.0, target=76000.0),
            candle_ts=1000, equity=100.0,
        )
        # 3 candles that don't hit stop or target
        for ts in [2000, 3000, 4000]:
            completed = book.on_candle(
                candle_high=75200.0, candle_low=74500.0,
                candle_close=75100.0, candle_ts=ts,
            )
        assert len(completed) == 1
        assert completed[0].exit_reason == "timeout"

    def test_shadow_hold_continues(self):
        params = _make_params()
        book = ShadowBook(params)
        book.on_blocked_entry(
            _blocked_long_decision(price=75000.0, stop=74000.0, target=76000.0),
            candle_ts=1000, equity=100.0,
        )
        # Candle that doesn't trigger any exit
        completed = book.on_candle(
            candle_high=75500.0, candle_low=74500.0,
            candle_close=75200.0, candle_ts=2000,
        )
        assert len(completed) == 0
        assert book.count == 1

    def test_multiple_shadows_independent(self):
        params = _make_params()
        book = ShadowBook(params)
        # Two blocked entries at different prices
        book.on_blocked_entry(
            _blocked_long_decision(price=75000.0, stop=74000.0, target=76000.0),
            candle_ts=1000, equity=100.0,
        )
        book.on_blocked_entry(
            _blocked_short_decision(price=78000.0, stop=79000.0, target=76000.0),
            candle_ts=2000, equity=100.0,
        )
        assert book.count == 2

        # Candle hits long target (high >= 76000) but not short target/stop
        # Short: target=76000 (exit when low <= target), stop=79000 (exit when high >= stop)
        # Use low=76100 so short target not hit, high=76100 hits long target
        completed = book.on_candle(
            candle_high=76100.0, candle_low=76050.0,
            candle_close=76080.0, candle_ts=3000,
        )
        assert len(completed) == 1
        assert completed[0].side == "long"
        assert completed[0].exit_reason == "target"
        assert book.count == 1  # short still open

    def test_shadow_preserves_context(self):
        params = _make_params()
        book = ShadowBook(params)
        book.on_blocked_entry(
            _blocked_long_decision(), candle_ts=1000, equity=100.0,
        )
        # Hit stop
        completed = book.on_candle(
            candle_high=75100.0, candle_low=73900.0,
            candle_close=74000.0, candle_ts=2000,
        )
        trade = completed[0]
        assert trade.block_reason == "counter_trend_long"
        assert trade.sigma_at_entry == pytest.approx(-2.6)
        assert trade.adx_at_entry == pytest.approx(10.9)
        assert trade.trend_direction_at_entry == "down"
        assert trade.symbol == "BTC"

    def test_clear_removes_all(self):
        params = _make_params()
        book = ShadowBook(params)
        book.on_blocked_entry(
            _blocked_long_decision(), candle_ts=1000, equity=100.0,
        )
        assert book.count == 1
        book.clear()
        assert book.count == 0

    def test_max_positions_cap(self):
        params = _make_params()
        book = ShadowBook(params, max_positions=2)
        for i in range(5):
            book.on_blocked_entry(
                _blocked_long_decision(), candle_ts=1000 + i, equity=100.0,
            )
        assert book.count == 2
