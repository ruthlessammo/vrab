"""Parity test — verify backtest and live data-slicing produce identical signals.

Both engines share the same core functions (generate_signal, evaluate_exit, etc.).
The risk of divergence is in how each engine slices candle data before calling those
functions. This test constructs the same data slices both engines would build for each
bar, feeds them to generate_signal, and asserts the outputs match.

Covers:
    - VWAP window alignment (primary candles)
    - Trend boundary alignment (15m candles)
    - Signal output parity (price, sigma, VWAP, ADX, regime)
"""

import bisect
import math
import pytest

from data.store import Candle
from strategy.signals import generate_signal, SignalResult


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------

INTERVAL_5M = 300_000   # ms
INTERVAL_15M = 900_000  # ms
BASE_TS = 1_700_000_000_000  # arbitrary start
VWAP_WINDOW = 36


def _make_5m_candles(n: int, base_price: float = 50_000.0) -> list[dict]:
    """Create n 5-minute candles with mild sine-wave variation.

    Returns list of dicts (backtest format) with aligned timestamps.
    """
    candles = []
    for i in range(n):
        # Sine wave creates non-trivial VWAP and sigma variation
        offset = 200 * math.sin(i * 0.15)
        price = base_price + offset
        candles.append({
            "ts": BASE_TS + i * INTERVAL_5M,
            "open": price - 5,
            "high": price + 30,
            "low": price - 30,
            "close": price,
            "volume": 1000.0 + 100 * abs(math.sin(i * 0.3)),
        })
    return candles


def _make_15m_candles(n: int, base_price: float = 50_000.0) -> list[dict]:
    """Create n 15-minute trend candles with mild trend."""
    candles = []
    for i in range(n):
        offset = 100 * math.sin(i * 0.05)
        price = base_price + offset
        candles.append({
            "ts": BASE_TS + i * INTERVAL_15M,
            "open": price - 3,
            "high": price + 20,
            "low": price - 20,
            "close": price,
            "volume": 3000.0,
        })
    return candles


def _to_candle_objects(candle_dicts: list[dict], symbol: str, tf: str) -> list[Candle]:
    """Convert backtest candle dicts to Candle dataclass objects (live format)."""
    return [
        Candle(
            symbol=symbol, tf=tf, ts=c["ts"],
            open=c["open"], high=c["high"], low=c["low"],
            close=c["close"], volume=c["volume"],
        )
        for c in candle_dicts
    ]


# ---------------------------------------------------------------------------
# Slice helpers — replicate each engine's data preparation exactly
# ---------------------------------------------------------------------------

def backtest_slice(
    primary_candles: list[dict],
    trend_candles: list[dict],
    bar_idx: int,
    vwap_win: int,
) -> dict:
    """Replicate backtest/engine.py:simulate_window data slicing at bar_idx.

    See backtest/engine.py lines 395-420.
    """
    all_closes = [c["close"] for c in primary_candles]
    all_highs = [c["high"] for c in primary_candles]
    all_lows = [c["low"] for c in primary_candles]
    all_volumes = [c["volume"] for c in primary_candles]

    win_start = max(0, bar_idx + 1 - vwap_win)
    closes = all_closes[win_start: bar_idx + 1]
    highs = all_highs[win_start: bar_idx + 1]
    lows = all_lows[win_start: bar_idx + 1]
    volumes = all_volumes[win_start: bar_idx + 1]

    candle_ts = primary_candles[bar_idx]["ts"]

    # Trend slicing — backtest uses candle_ts (bar T's ts)
    trend_ts_arr = [c["ts"] for c in trend_candles]
    trend_boundary = candle_ts - INTERVAL_15M
    trend_idx = bisect.bisect_right(trend_ts_arr, trend_boundary)
    trend_start = max(0, trend_idx - 100)

    t_closes = [c["close"] for c in trend_candles[trend_start:trend_idx]]
    t_highs = [c["high"] for c in trend_candles[trend_start:trend_idx]]
    t_lows = [c["low"] for c in trend_candles[trend_start:trend_idx]]

    return {
        "closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
        "t_closes": t_closes, "t_highs": t_highs, "t_lows": t_lows,
        "candle_ts": candle_ts,
    }


def live_slice(
    primary_candle_objs: list[Candle],
    trend_candle_objs: list[Candle],
    closed_bar_ts: int,
    vwap_win: int,
) -> dict:
    """Replicate live/engine.py:_on_candle_close data slicing (post-fix).

    The fix filters primary candles to ts <= closed_bar_ts and uses
    closed_bar_ts for the trend boundary.
    See live/engine.py lines 759-783.
    """
    # After fix: filter to candles at or before the closed bar
    primary_candles = [c for c in primary_candle_objs if c.ts <= closed_bar_ts]

    closes = [c.close for c in primary_candles[-vwap_win:]]
    highs = [c.high for c in primary_candles[-vwap_win:]]
    lows = [c.low for c in primary_candles[-vwap_win:]]
    volumes = [c.volume for c in primary_candles[-vwap_win:]]

    # Trend slicing — uses closed bar's ts (same as backtest)
    candle_ts = closed_bar_ts
    trend_ts_arr = [c.ts for c in trend_candle_objs]
    trend_boundary = candle_ts - INTERVAL_15M
    trend_idx = bisect.bisect_right(trend_ts_arr, trend_boundary)
    trend_start = max(0, trend_idx - 100)

    t_closes = [c.close for c in trend_candle_objs[trend_start:trend_idx]]
    t_highs = [c.high for c in trend_candle_objs[trend_start:trend_idx]]
    t_lows = [c.low for c in trend_candle_objs[trend_start:trend_idx]]

    return {
        "closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
        "t_closes": t_closes, "t_highs": t_highs, "t_lows": t_lows,
        "candle_ts": candle_ts,
    }


def _call_signal(sliced: dict) -> SignalResult:
    """Call generate_signal with sliced data."""
    return generate_signal(
        closes=sliced["closes"],
        highs=sliced["highs"],
        lows=sliced["lows"],
        volumes=sliced["volumes"],
        trend_closes=sliced["t_closes"],
        trend_highs=sliced["t_highs"],
        trend_lows=sliced["t_lows"],
        current_position_side=None,
        vwap_window=VWAP_WINDOW,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

TOL = 1e-9


class TestDataSliceParity:
    """Verify backtest and live (post-fix) produce identical data slices."""

    @pytest.fixture
    def candle_data(self):
        """Generate a shared set of test candles."""
        n_primary = VWAP_WINDOW + 100  # enough history
        n_trend = 200
        primary_dicts = _make_5m_candles(n_primary)
        trend_dicts = _make_15m_candles(n_trend)

        # Live format: Candle objects with bar T+1 always present in the list
        # (simulating the store having T+1 upserted before event fires)
        primary_objs = _to_candle_objects(primary_dicts, "BTC", "5m")
        trend_objs = _to_candle_objects(trend_dicts, "BTC", "15m")

        return {
            "primary_dicts": primary_dicts,
            "trend_dicts": trend_dicts,
            "primary_objs": primary_objs,
            "trend_objs": trend_objs,
        }

    def test_vwap_window_matches(self, candle_data):
        """VWAP window closes must be identical between backtest and live slices."""
        primary_dicts = candle_data["primary_dicts"]
        trend_dicts = candle_data["trend_dicts"]
        primary_objs = candle_data["primary_objs"]
        trend_objs = candle_data["trend_objs"]

        for bar_idx in range(VWAP_WINDOW, len(primary_dicts)):
            bt = backtest_slice(primary_dicts, trend_dicts, bar_idx, VWAP_WINDOW)

            # Live sees all candles including bar T+1 (bar_idx + 1), but
            # the fix filters to ts <= closed_bar_ts
            closed_bar_ts = primary_dicts[bar_idx]["ts"]
            # Include bar T+1 in the "store" if it exists
            store_limit = min(bar_idx + 2, len(primary_objs))
            live_primary = primary_objs[:store_limit]
            lv = live_slice(live_primary, trend_objs, closed_bar_ts, VWAP_WINDOW)

            assert bt["closes"] == lv["closes"], (
                f"VWAP closes differ at bar {bar_idx}: "
                f"bt[-3:]={bt['closes'][-3:]}, lv[-3:]={lv['closes'][-3:]}"
            )
            assert bt["highs"] == lv["highs"], f"Highs differ at bar {bar_idx}"
            assert bt["lows"] == lv["lows"], f"Lows differ at bar {bar_idx}"
            assert bt["volumes"] == lv["volumes"], f"Volumes differ at bar {bar_idx}"

    def test_trend_window_matches(self, candle_data):
        """Trend candle slice must be identical between backtest and live."""
        primary_dicts = candle_data["primary_dicts"]
        trend_dicts = candle_data["trend_dicts"]
        primary_objs = candle_data["primary_objs"]
        trend_objs = candle_data["trend_objs"]

        for bar_idx in range(VWAP_WINDOW, len(primary_dicts)):
            bt = backtest_slice(primary_dicts, trend_dicts, bar_idx, VWAP_WINDOW)

            closed_bar_ts = primary_dicts[bar_idx]["ts"]
            store_limit = min(bar_idx + 2, len(primary_objs))
            live_primary = primary_objs[:store_limit]
            lv = live_slice(live_primary, trend_objs, closed_bar_ts, VWAP_WINDOW)

            assert bt["t_closes"] == lv["t_closes"], (
                f"Trend closes differ at bar {bar_idx}"
            )
            assert bt["t_highs"] == lv["t_highs"], f"Trend highs differ at bar {bar_idx}"
            assert bt["t_lows"] == lv["t_lows"], f"Trend lows differ at bar {bar_idx}"

    def test_signal_output_parity(self, candle_data):
        """generate_signal must produce bit-identical output for both slices."""
        primary_dicts = candle_data["primary_dicts"]
        trend_dicts = candle_data["trend_dicts"]
        primary_objs = candle_data["primary_objs"]
        trend_objs = candle_data["trend_objs"]

        mismatches = []
        for bar_idx in range(VWAP_WINDOW, len(primary_dicts)):
            bt = backtest_slice(primary_dicts, trend_dicts, bar_idx, VWAP_WINDOW)

            closed_bar_ts = primary_dicts[bar_idx]["ts"]
            store_limit = min(bar_idx + 2, len(primary_objs))
            live_primary = primary_objs[:store_limit]
            lv = live_slice(live_primary, trend_objs, closed_bar_ts, VWAP_WINDOW)

            bt_sig = _call_signal(bt)
            lv_sig = _call_signal(lv)

            # Compare all numeric fields
            if bt_sig.signal != lv_sig.signal:
                mismatches.append(
                    f"bar {bar_idx}: signal bt={bt_sig.signal} lv={lv_sig.signal}"
                )
                continue

            if abs(bt_sig.price - lv_sig.price) > TOL:
                mismatches.append(
                    f"bar {bar_idx}: price bt={bt_sig.price} lv={lv_sig.price}"
                )
            if abs(bt_sig.sigma_dist - lv_sig.sigma_dist) > TOL:
                mismatches.append(
                    f"bar {bar_idx}: sigma bt={bt_sig.sigma_dist} lv={lv_sig.sigma_dist}"
                )
            if bt_sig.vwap_state and lv_sig.vwap_state:
                if abs(bt_sig.vwap_state.vwap - lv_sig.vwap_state.vwap) > TOL:
                    mismatches.append(
                        f"bar {bar_idx}: vwap bt={bt_sig.vwap_state.vwap} "
                        f"lv={lv_sig.vwap_state.vwap}"
                    )
                if abs(bt_sig.vwap_state.std_dev - lv_sig.vwap_state.std_dev) > TOL:
                    mismatches.append(
                        f"bar {bar_idx}: std_dev bt={bt_sig.vwap_state.std_dev} "
                        f"lv={lv_sig.vwap_state.std_dev}"
                    )
            if bt_sig.regime and lv_sig.regime:
                if abs(bt_sig.regime.adx - lv_sig.regime.adx) > TOL:
                    mismatches.append(
                        f"bar {bar_idx}: adx bt={bt_sig.regime.adx} lv={lv_sig.regime.adx}"
                    )
                if bt_sig.regime.trend_direction != lv_sig.regime.trend_direction:
                    mismatches.append(
                        f"bar {bar_idx}: trend bt={bt_sig.regime.trend_direction} "
                        f"lv={lv_sig.regime.trend_direction}"
                    )
                if bt_sig.regime.is_trending != lv_sig.regime.is_trending:
                    mismatches.append(
                        f"bar {bar_idx}: is_trending bt={bt_sig.regime.is_trending} "
                        f"lv={lv_sig.regime.is_trending}"
                    )

        assert not mismatches, (
            f"Signal parity failures ({len(mismatches)}):\n"
            + "\n".join(mismatches[:20])
        )


class TestPreFixDivergence:
    """Demonstrate that the OLD (pre-fix) slicing would have diverged.

    This test uses the pre-fix live slicing (includes bar T+1, uses T+1's ts
    for trend boundary) and verifies it does NOT match the backtest — proving
    the fix was necessary.
    """

    def _old_live_slice(
        self, primary_candle_objs, trend_candle_objs, bar_t1_ts, vwap_win,
    ):
        """Pre-fix live slicing: uses T+1's ts, doesn't filter out T+1."""
        closes = [c.close for c in primary_candle_objs[-vwap_win:]]
        highs = [c.high for c in primary_candle_objs[-vwap_win:]]
        lows = [c.low for c in primary_candle_objs[-vwap_win:]]
        volumes = [c.volume for c in primary_candle_objs[-vwap_win:]]

        trend_ts_arr = [c.ts for c in trend_candle_objs]
        trend_boundary = bar_t1_ts - INTERVAL_15M  # uses T+1's ts
        trend_idx = bisect.bisect_right(trend_ts_arr, trend_boundary)
        trend_start = max(0, trend_idx - 100)

        t_closes = [c.close for c in trend_candle_objs[trend_start:trend_idx]]
        t_highs = [c.high for c in trend_candle_objs[trend_start:trend_idx]]
        t_lows = [c.low for c in trend_candle_objs[trend_start:trend_idx]]

        return {
            "closes": closes, "highs": highs, "lows": lows, "volumes": volumes,
            "t_closes": t_closes, "t_highs": t_highs, "t_lows": t_lows,
            "candle_ts": bar_t1_ts,
        }

    def test_old_slicing_diverges(self):
        """Old live slicing must differ from backtest (proves fix was needed)."""
        primary_dicts = _make_5m_candles(VWAP_WINDOW + 50)
        trend_dicts = _make_15m_candles(200)
        primary_objs = _to_candle_objects(primary_dicts, "BTC", "5m")
        trend_objs = _to_candle_objects(trend_dicts, "BTC", "15m")

        found_vwap_divergence = False

        for bar_idx in range(VWAP_WINDOW, len(primary_dicts) - 1):
            bt = backtest_slice(primary_dicts, trend_dicts, bar_idx, VWAP_WINDOW)

            # Old live: store has up to bar T+1, uses T+1 timestamp
            bar_t1_idx = bar_idx + 1
            bar_t1_ts = primary_dicts[bar_t1_idx]["ts"]
            live_primary = primary_objs[:bar_t1_idx + 1]  # includes T+1

            old_lv = self._old_live_slice(
                live_primary, trend_objs, bar_t1_ts, VWAP_WINDOW,
            )

            if bt["closes"] != old_lv["closes"]:
                found_vwap_divergence = True
                break

        assert found_vwap_divergence, (
            "Expected old live slicing to diverge from backtest — "
            "if this fails, the pre-fix code was actually fine (unlikely)"
        )
