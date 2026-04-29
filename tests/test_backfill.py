"""Tests for tools/backfill_pnl.py — direction-aware fill matching and PnL correction."""

import pytest

from tools.backfill_pnl import match_fills_to_trade, extract_prices_from_fills


def _fill(time_ms, direction, px=77000.0, sz=0.01, fee=0.15, closed_pnl=0.0):
    """Helper to create an HL fill dict."""
    return {
        "time": time_ms,
        "coin": "BTC",
        "dir": direction,
        "px": px,
        "sz": sz,
        "ntl": px * sz,
        "fee": fee,
        "closedPnl": closed_pnl,
    }


def _db_trade(entry_ts, exit_ts, side="long", net_pnl_usd=0.0):
    """Helper to create a DB trade dict."""
    return {
        "id": 1,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "side": side,
        "symbol": "BTC",
        "net_pnl_usd": net_pnl_usd,
        "funding_usd": 0.0,
        "entry_price": 77000.0,
        "exit_price": 77100.0,
        "entry_oid": None,
        "stop_oid": None,
        "target_oid": None,
    }


class TestMatchFillsByDirection:
    def test_long_trade_grabs_open_and_close_long(self):
        """Long trade should only match Open Long + Close Long fills."""
        fills = [
            _fill(1000, "Open Long", px=77000, closed_pnl=-0.15),
            _fill(1500, "Open Short", px=77000, closed_pnl=-0.10),  # wrong dir
            _fill(2000, "Close Long", px=77100, closed_pnl=2.5),
        ]
        trade = _db_trade(900, 2100, side="long")
        matched = match_fills_to_trade(trade, fills)
        assert len(matched) == 2
        assert all(f["dir"] in ("Open Long", "Close Long") for f in matched)

    def test_short_trade_grabs_open_and_close_short(self):
        """Short trade should only match Open Short + Close Short fills."""
        fills = [
            _fill(1000, "Open Short", px=77000, closed_pnl=-0.15),
            _fill(1500, "Open Long", px=77000, closed_pnl=-0.10),  # wrong dir
            _fill(2000, "Close Short", px=76900, closed_pnl=2.5),
        ]
        trade = _db_trade(900, 2100, side="short")
        matched = match_fills_to_trade(trade, fills)
        assert len(matched) == 2
        assert all(f["dir"] in ("Open Short", "Close Short") for f in matched)

    def test_fills_outside_window_excluded(self):
        """Fills outside the time window should not be matched."""
        # Use realistic timestamps — margins are 5s before entry, 30s after exit
        base = 1_776_000_000_000
        fills = [
            _fill(base - 60_000, "Open Long", closed_pnl=-0.15),   # 60s too early
            _fill(base, "Open Long", closed_pnl=-0.15),             # at entry
            _fill(base + 300_000, "Close Long", closed_pnl=2.5),   # at exit
            _fill(base + 400_000, "Close Long", closed_pnl=1.0),   # 100s after exit
        ]
        trade = _db_trade(base, base + 300_000, side="long")
        matched = match_fills_to_trade(trade, fills)
        assert len(matched) == 2


class TestNoCrossTradeSteal:
    def test_two_close_trades_no_stealing(self):
        """Two trades close together should each get their own fills."""
        base = 1_776_000_000_000
        fills = [
            _fill(base, "Open Long", px=77000, closed_pnl=-0.15),
            _fill(base + 300_000, "Close Long", px=77100, closed_pnl=2.5),
            _fill(base + 600_000, "Open Long", px=77200, closed_pnl=-0.15),
            _fill(base + 900_000, "Close Long", px=77050, closed_pnl=-1.0),
        ]
        trade1 = _db_trade(base, base + 300_000, side="long")
        trade2 = _db_trade(base + 600_000, base + 900_000, side="long")

        # Match first trade
        matched1 = match_fills_to_trade(trade1, fills)
        # Remove claimed fills
        remaining = [f for f in fills if f not in matched1]
        matched2 = match_fills_to_trade(trade2, remaining)

        assert len(matched1) == 2
        assert len(matched2) == 2
        assert matched1[0]["time"] == base
        assert matched2[0]["time"] == base + 600_000


class TestPnlMatchesHL:
    def test_pnl_from_fills_matches_closed_pnl_sum(self):
        """Computed net PnL should match sum of HL closedPnl."""
        from live.pnl import calc_pnl_from_fills

        fills = [
            _fill(1000, "Open Long", fee=0.13, closed_pnl=-0.13),
            _fill(2000, "Close Long", fee=0.38, closed_pnl=2.50),
        ]
        result = calc_pnl_from_fills(fills, funding_usd=0.0)
        expected_net = sum(float(f["closedPnl"]) for f in fills)
        assert abs(result["net_pnl_usd"] - expected_net) < 0.0001


class TestExtractPrices:
    def test_entry_price_weighted_avg(self):
        """Entry price should be size-weighted average of open fills."""
        fills = [
            _fill(1000, "Open Long", px=77000, sz=0.005),
            _fill(1001, "Open Long", px=77100, sz=0.005),
            _fill(2000, "Close Long", px=77200, sz=0.01),
        ]
        entry_px, exit_px = extract_prices_from_fills(fills, "long")
        expected_entry = (77000 * 0.005 + 77100 * 0.005) / 0.01
        assert abs(entry_px - expected_entry) < 0.01
        assert abs(exit_px - 77200.0) < 0.01

    def test_exit_price_weighted_avg(self):
        """Exit price should be size-weighted average of close fills."""
        fills = [
            _fill(1000, "Open Short", px=77000, sz=0.01),
            _fill(2000, "Close Short", px=76800, sz=0.007),
            _fill(2001, "Close Short", px=76900, sz=0.003),
        ]
        entry_px, exit_px = extract_prices_from_fills(fills, "short")
        expected_exit = (76800 * 0.007 + 76900 * 0.003) / 0.01
        assert abs(entry_px - 77000.0) < 0.01
        assert abs(exit_px - expected_exit) < 0.01

    def test_no_open_fills_returns_none(self):
        """If no open fills, entry price should be None."""
        fills = [
            _fill(2000, "Close Long", px=77200, sz=0.01),
        ]
        entry_px, exit_px = extract_prices_from_fills(fills, "long")
        assert entry_px is None
        assert abs(exit_px - 77200.0) < 0.01
