"""Tests for tools/reconcile_hl.py — DB-anchored reconciliation."""

import pytest
from tools.reconcile_hl import reconcile, format_reconcile_report, format_reconcile_telegram


def _fill(time_ms, coin="BTC", closed_pnl=0.0, direction="Close Long",
          oid=None):
    """Helper to create a fill dict."""
    f = {
        "time": time_ms,
        "coin": coin,
        "dir": direction,
        "px": 100000.0,
        "sz": 0.01,
        "ntl": 1000.0,
        "fee": 0.0,
        "closedPnl": closed_pnl,
    }
    if oid is not None:
        f["oid"] = oid
    return f


def _db_trade(entry_ts, exit_ts, side="long", symbol="BTC",
              net_pnl_usd=0.0, funding_usd=0.0,
              entry_oid=None, stop_oid=None, target_oid=None):
    """Helper to create a DB trade dict."""
    return {
        "id": 1,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "side": side,
        "symbol": symbol,
        "net_pnl_usd": net_pnl_usd,
        "funding_usd": funding_usd,
        "entry_oid": entry_oid,
        "stop_oid": stop_oid,
        "target_oid": target_oid,
    }


class TestReconcile:
    def test_empty_inputs(self):
        """No fills and no DB trades → clean result."""
        result = reconcile([], [])
        assert result["hl_total_pnl"] == 0.0
        assert result["db_total_pnl"] == 0.0
        assert result["db_count"] == 0
        assert result["orphan_fills"] == []
        assert result["unmatched_db"] == []

    def test_single_trade_exact_match(self):
        """One DB trade, one fill inside window → matched, no diff."""
        fills = [_fill(1000_000, closed_pnl=5.0)]
        db_trades = [_db_trade(999_000, 1001_000, net_pnl_usd=5.5, funding_usd=0.5)]
        result = reconcile(fills, db_trades)

        assert result["db_count"] == 1
        assert len(result["per_trade"]) == 1
        trade = result["per_trade"][0]
        # HL pnl = 5.0, DB pnl ex-funding = 5.5 - 0.5 = 5.0
        assert abs(trade["hl_pnl"] - 5.0) < 0.001
        assert abs(trade["db_pnl_ex_funding"] - 5.0) < 0.001
        assert abs(trade["diff"]) < 0.001
        assert result["orphan_fills"] == []
        assert result["unmatched_db"] == []

    def test_multi_fill_trade(self):
        """Multiple fills within one DB trade window get summed."""
        fills = [
            _fill(1000_000, closed_pnl=0.0, direction="Open Long"),
            _fill(1001_000, closed_pnl=0.0, direction="Open Long"),
            _fill(1002_000, closed_pnl=3.0),
        ]
        db_trades = [_db_trade(999_000, 1003_000, net_pnl_usd=3.2, funding_usd=0.2)]
        result = reconcile(fills, db_trades)

        assert len(result["per_trade"]) == 1
        assert abs(result["per_trade"][0]["hl_pnl"] - 3.0) < 0.001
        assert result["per_trade"][0]["fill_count"] == 3

    def test_orphan_fills(self):
        """Fills outside any DB trade window are reported as orphans."""
        fills = [
            _fill(1000_000, closed_pnl=5.0),
            _fill(9000_000, closed_pnl=1.0),  # way outside any trade
        ]
        db_trades = [_db_trade(999_000, 1001_000, net_pnl_usd=5.5, funding_usd=0.5)]
        result = reconcile(fills, db_trades)

        assert len(result["orphan_fills"]) == 1
        assert result["orphan_fills"][0]["time"] == 9000_000

    def test_unmatched_db_trade(self):
        """DB trade with no fills in window is reported."""
        fills = []
        db_trades = [_db_trade(1000_000, 2000_000, net_pnl_usd=1.0)]
        result = reconcile(fills, db_trades)

        assert len(result["unmatched_db"]) == 1
        assert result["unmatched_db"][0]["entry_ts"] == 1000_000

    def test_totals(self):
        """Level 0 totals are computed from all fills and all DB trades."""
        fills = [
            _fill(1000_000, closed_pnl=5.0),
            _fill(2000_000, closed_pnl=-2.0),
        ]
        db_trades = [
            _db_trade(999_000, 1001_000, net_pnl_usd=5.5, funding_usd=0.5),
            _db_trade(1999_000, 2001_000, net_pnl_usd=-1.5, funding_usd=0.5),
        ]
        result = reconcile(fills, db_trades)
        assert abs(result["hl_total_pnl"] - 3.0) < 0.001
        # DB total ex-funding = (5.5 - 0.5) + (-1.5 - 0.5) = 3.0
        assert abs(result["db_total_pnl"] - 3.0) < 0.001

    def test_fill_not_double_claimed(self):
        """A fill within two overlapping DB trade windows is claimed by first only."""
        fills = [_fill(1500_000, closed_pnl=5.0)]
        db_trades = [
            _db_trade(1000_000, 2000_000, net_pnl_usd=5.5, funding_usd=0.5),
            _db_trade(1400_000, 1600_000, net_pnl_usd=0.1, funding_usd=0.0),
        ]
        result = reconcile(fills, db_trades)
        # Fill claimed by first trade (ordered by entry_ts)
        assert result["per_trade"][0]["fill_count"] == 1
        assert result["per_trade"][1]["fill_count"] == 0

    def test_coin_filtering(self):
        """Fills for different coins don't match BTC DB trade."""
        fills = [
            _fill(1000_000, coin="ETH", closed_pnl=5.0),
        ]
        db_trades = [_db_trade(999_000, 1001_000, symbol="BTC", net_pnl_usd=5.5, funding_usd=0.5)]
        result = reconcile(fills, db_trades)
        assert len(result["orphan_fills"]) == 1
        assert len(result["unmatched_db"]) == 1


class TestOidMatching:
    def test_entry_and_exit_oid_match(self):
        """Entry fills matched by entry_oid, exit fills by stop_oid."""
        fills = [
            _fill(1000_000, closed_pnl=0.0, direction="Open Long", oid=42),
            _fill(8000_000, closed_pnl=5.0, direction="Close Long", oid=99),
        ]
        db_trades = [_db_trade(
            500_000, 600_000,  # narrow window, fills are far outside
            net_pnl_usd=5.5, funding_usd=0.5,
            entry_oid=42, stop_oid=99,
        )]
        result = reconcile(fills, db_trades)
        assert result["per_trade"][0]["fill_count"] == 2
        assert abs(result["per_trade"][0]["hl_pnl"] - 5.0) < 0.001
        assert result["orphan_fills"] == []

    def test_target_oid_match(self):
        """Exit fill matched by target_oid (take-profit hit)."""
        fills = [
            _fill(1000_000, closed_pnl=0.0, direction="Open Long", oid=42),
            _fill(8000_000, closed_pnl=5.0, direction="Close Long", oid=77),
        ]
        db_trades = [_db_trade(
            500_000, 600_000,
            net_pnl_usd=5.5, funding_usd=0.5,
            entry_oid=42, target_oid=77,
        )]
        result = reconcile(fills, db_trades)
        assert result["per_trade"][0]["fill_count"] == 2

    def test_fallback_to_time_window_when_no_oid(self):
        """DB trade without any oids falls back to time-window matching."""
        fills = [_fill(1000_000, closed_pnl=5.0)]
        db_trades = [_db_trade(
            999_000, 1001_000,
            net_pnl_usd=5.5, funding_usd=0.5,
        )]
        result = reconcile(fills, db_trades)
        assert result["per_trade"][0]["fill_count"] == 1

    def test_oid_no_false_match(self):
        """Fills with different oid are not matched even if in time window."""
        fills = [_fill(1000_000, closed_pnl=5.0, oid=99)]
        db_trades = [_db_trade(
            999_000, 1001_000,
            net_pnl_usd=5.5, funding_usd=0.5, entry_oid=42,
        )]
        result = reconcile(fills, db_trades)
        assert result["per_trade"][0]["fill_count"] == 0
        assert len(result["orphan_fills"]) == 1

    def test_mixed_oid_and_legacy(self):
        """Mix of OID-matched and time-window-matched trades."""
        fills = [
            _fill(1000_000, closed_pnl=0.0, direction="Open Long", oid=42),
            _fill(1500_000, closed_pnl=5.0, direction="Close Long", oid=99),
            _fill(2000_000, closed_pnl=-2.0),  # no oid, legacy
        ]
        db_trades = [
            _db_trade(500_000, 600_000, net_pnl_usd=5.5, funding_usd=0.5,
                      entry_oid=42, stop_oid=99),
            _db_trade(1999_000, 2001_000, net_pnl_usd=-1.5, funding_usd=0.5),
        ]
        result = reconcile(fills, db_trades)
        assert result["per_trade"][0]["fill_count"] == 2  # oid match
        assert result["per_trade"][1]["fill_count"] == 1  # time window
        assert result["orphan_fills"] == []


class TestFormatReport:
    def test_clean_report(self):
        result = reconcile(
            [_fill(1000_000, closed_pnl=5.0)],
            [_db_trade(999_000, 1001_000, net_pnl_usd=5.5, funding_usd=0.5)],
        )
        text = format_reconcile_report(result)
        assert "DB trades: 1" in text
        assert "Orphan" not in text or "0" in text

    def test_telegram_format(self):
        result = reconcile(
            [_fill(1000_000, closed_pnl=5.0)],
            [_db_trade(999_000, 1001_000, net_pnl_usd=5.5, funding_usd=0.5)],
        )
        text = format_reconcile_telegram(result)
        assert "*Reconciliation*" in text
