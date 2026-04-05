"""Tests for mid-candle exit detection — pure function tests."""

from live.exit_detect import infer_exit, extract_exit_price


class TestInferExit:
    """Test exit type inference from fill price vs stop/target."""

    def test_long_stopped(self):
        assert infer_exit("long", stop=65000, target=70000, fill_px=65000) == "stop"

    def test_long_target(self):
        assert infer_exit("long", stop=65000, target=70000, fill_px=70000) == "target"

    def test_short_stopped(self):
        assert infer_exit("short", stop=70000, target=65000, fill_px=70000) == "stop"

    def test_short_target(self):
        assert infer_exit("short", stop=70000, target=65000, fill_px=65000) == "target"

    def test_long_between_stop_and_target_defaults_stop(self):
        assert infer_exit("long", stop=65000, target=70000, fill_px=67000) == "stop"

    def test_short_between_stop_and_target_defaults_stop(self):
        assert infer_exit("short", stop=70000, target=65000, fill_px=67000) == "stop"

    def test_long_at_exact_target(self):
        assert infer_exit("long", stop=65000, target=70000, fill_px=70000) == "target"

    def test_short_at_exact_target(self):
        assert infer_exit("short", stop=70000, target=65000, fill_px=65000) == "target"


class TestExtractExitPrice:
    """Test exit price extraction from HL fills."""

    def test_finds_close_fill(self):
        fills = [
            {"time": 1000, "side": "B", "px": "65000"},
            {"time": 2000, "side": "A", "px": "70000"},
        ]
        assert extract_exit_price(fills, entry_ts=500, close_side="A") == 70000.0

    def test_no_matching_fills(self):
        assert extract_exit_price([], entry_ts=500, close_side="A") is None

    def test_filters_by_entry_ts(self):
        fills = [
            {"time": 100, "side": "A", "px": "60000"},   # before entry window
            {"time": 2000, "side": "A", "px": "70000"},
        ]
        assert extract_exit_price(fills, entry_ts=1000, close_side="A") == 70000.0

    def test_uses_last_fill(self):
        fills = [
            {"time": 2000, "side": "A", "px": "69000"},
            {"time": 3000, "side": "A", "px": "70000"},
        ]
        assert extract_exit_price(fills, entry_ts=1000, close_side="A") == 70000.0

    def test_ignores_wrong_side(self):
        fills = [
            {"time": 2000, "side": "B", "px": "65000"},
        ]
        assert extract_exit_price(fills, entry_ts=1000, close_side="A") is None

    def test_multiple_sides_picks_correct(self):
        fills = [
            {"time": 2000, "side": "B", "px": "65000"},
            {"time": 2500, "side": "A", "px": "70000"},
            {"time": 3000, "side": "B", "px": "66000"},
        ]
        assert extract_exit_price(fills, entry_ts=1000, close_side="A") == 70000.0
