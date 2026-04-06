"""Tests for data/store.py — 6 tests."""

import threading
import pytest
from data.store import Store, Candle, Trade


class TestStore:
    def test_schema_creation(self):
        """Store(':memory:') creates all tables without error."""
        s = Store(":memory:")
        # Verify tables exist
        tables = s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "candles" in table_names
        assert "trades" in table_names
        assert "signals" in table_names
        assert "daily_pnl" in table_names
        s.close()

    def test_candle_upsert_and_retrieval(self):
        s = Store(":memory:")
        candles = [
            Candle("BTC", "5m", 1000, 50000, 50100, 49900, 50050, 100),
            Candle("BTC", "5m", 2000, 50050, 50150, 49950, 50100, 110),
            Candle("BTC", "5m", 3000, 50100, 50200, 50000, 50150, 120),
        ]
        count = s.upsert_candles(candles)
        assert count == 3

        result = s.get_candles("BTC", "5m", limit=10, from_memory=False)
        assert len(result) == 3
        assert result[0].ts == 1000  # ordered ascending
        assert result[-1].ts == 3000
        s.close()

    def test_cache_warm(self):
        s = Store(":memory:")
        candles = [
            Candle("BTC", "5m", i * 1000, 50000, 50100, 49900, 50050, 100)
            for i in range(10)
        ]
        s.upsert_candles(candles)
        loaded = s.warm_cache("BTC", "5m", limit=10)
        assert loaded == 10

        # from_memory should hit cache
        cached = s.get_candles("BTC", "5m", limit=5, from_memory=True)
        assert len(cached) == 5
        s.close()

    def test_trade_recording(self):
        s = Store(":memory:")
        trade = Trade(
            symbol="BTC", side="long",
            entry_price=50000, exit_price=50500,
            size_usd=500, notional_usd=5000,
            leverage=10, liq_price=45000,
            entry_ts=1000000, exit_ts=2000000,
            exit_reason="target", pnl_usd=50.0,
            net_pnl_usd=48.5,
        )
        trade_id = s.record_trade(trade)
        assert trade_id is not None and trade_id > 0

        trades = s.get_trades(symbol="BTC", limit=10)
        assert len(trades) == 1
        assert trades[0].entry_price == 50000
        s.close()

    def test_signal_logging(self):
        s = Store(":memory:")
        s.log_signal(
            symbol="BTC", tf="5m", ts=1000000,
            signal_type="long_entry", acted_on=True,
            price=48000, vwap=50000, sigma_dist=-2.5,
        )
        rows = s._conn.execute("SELECT * FROM signals").fetchall()
        assert len(rows) == 1
        assert rows[0]["signal_type"] == "long_entry"
        s.close()

    def test_ensure_today_row_creates_new(self):
        """ensure_today_row inserts a row if none exists and returns the seeded value."""
        s = Store(":memory:")
        result = s.ensure_today_row(symbol="BTC", source="live", start_equity=125.0)
        assert result == 125.0
        rows = s._conn.execute("SELECT * FROM daily_pnl").fetchall()
        assert len(rows) == 1
        assert rows[0]["start_equity"] == 125.0
        assert rows[0]["pnl_usd"] == 0.0
        assert rows[0]["trade_count"] == 0
        s.close()

    def test_ensure_today_row_idempotent(self):
        """Calling ensure_today_row twice keeps the FIRST start_equity."""
        s = Store(":memory:")
        first = s.ensure_today_row(symbol="BTC", source="live", start_equity=100.0)
        second = s.ensure_today_row(symbol="BTC", source="live", start_equity=999.0)
        assert first == 100.0
        assert second == 100.0
        rows = s._conn.execute("SELECT * FROM daily_pnl").fetchall()
        assert len(rows) == 1
        s.close()

    def test_ensure_today_row_preserves_counters(self):
        """ensure_today_row must not clobber existing pnl/trades/signals."""
        from datetime import datetime, timezone
        s = Store(":memory:")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.update_daily_pnl(
            date_str=today, symbol="BTC", source="live",
            pnl_usd=10.0, trade_count=2, max_dd_pct=1.0,
            start_equity=100.0, signals_generated=5, signals_blocked=1,
        )
        result = s.ensure_today_row(symbol="BTC", source="live", start_equity=999.0)
        assert result == 100.0
        rows = s._conn.execute("SELECT * FROM daily_pnl").fetchall()
        assert rows[0]["pnl_usd"] == 10.0
        assert rows[0]["trade_count"] == 2
        assert rows[0]["signals_generated"] == 5
        s.close()

    def test_reconcile_seeds_first_day(self):
        """Fresh DB → reconcile creates today's row with start_equity = current_equity."""
        s = Store(":memory:")
        s.reconcile_daily_state(current_equity=125.0, symbol="BTC", source="live")
        hot = s.get_daily_state()
        assert hot.daily_start_equity == 125.0
        assert hot.daily_pnl_usd == 0.0
        assert hot.signals_generated_today == 0
        assert hot.signals_blocked_today == 0
        assert hot.trade_count_today == 0
        assert hot.halted is False
        s.close()

    def test_reconcile_uses_persisted_start_equity(self):
        """If today's row exists, reconcile uses its start_equity (not current)."""
        from datetime import datetime, timezone
        s = Store(":memory:")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.update_daily_pnl(
            date_str=today, symbol="BTC", source="live",
            pnl_usd=0.0, trade_count=0, max_dd_pct=0.0,
            start_equity=100.0,
        )
        s.reconcile_daily_state(current_equity=110.0, symbol="BTC", source="live")
        hot = s.get_daily_state()
        assert hot.daily_start_equity == 100.0
        assert hot.daily_pnl_usd == 10.0  # 110 - 100
        s.close()

    def test_reconcile_restores_signal_counts(self):
        """signals_generated/blocked persist in daily_pnl row and reload on reconcile."""
        from datetime import datetime, timezone
        s = Store(":memory:")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.update_daily_pnl(
            date_str=today, symbol="BTC", source="live",
            pnl_usd=12.5, trade_count=3, max_dd_pct=0.0,
            start_equity=120.0, signals_generated=5, signals_blocked=2,
        )
        s.reconcile_daily_state(current_equity=132.5, symbol="BTC", source="live")
        hot = s.get_daily_state()
        assert hot.signals_generated_today == 5
        assert hot.signals_blocked_today == 2
        assert hot.trade_count_today == 3
        s.close()

    def test_reconcile_restores_halted_flag(self):
        """halted flag persists across reconcile (latent bug regression guard)."""
        from datetime import datetime, timezone
        s = Store(":memory:")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.update_daily_pnl(
            date_str=today, symbol="BTC", source="live",
            pnl_usd=-50.0, trade_count=2, max_dd_pct=5.0,
            start_equity=120.0, halted=True,
        )
        s.reconcile_daily_state(current_equity=70.0, symbol="BTC", source="live")
        hot = s.get_daily_state()
        assert hot.halted is True
        s.close()

    def test_reconcile_filters_by_symbol_source(self):
        """A row for a different (symbol, source) doesn't bleed into reconcile."""
        from datetime import datetime, timezone
        s = Store(":memory:")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.update_daily_pnl(
            date_str=today, symbol="BTC", source="backtest",
            pnl_usd=1.0, trade_count=1, max_dd_pct=0.0,
            start_equity=500.0, signals_generated=9, signals_blocked=9,
        )
        s.reconcile_daily_state(current_equity=125.0, symbol="BTC", source="live")
        hot = s.get_daily_state()
        # Live row was seeded fresh with current_equity, not the backtest row
        assert hot.daily_start_equity == 125.0
        assert hot.signals_generated_today == 0
        assert hot.signals_blocked_today == 0
        s.close()

    def test_thread_safety(self):
        """Concurrent writes from 4 threads don't raise."""
        s = Store(":memory:")
        errors = []

        def writer(thread_id):
            try:
                candles = [
                    Candle("BTC", "5m", thread_id * 1000 + i,
                           50000, 50100, 49900, 50050, 100)
                    for i in range(10)
                ]
                s.upsert_candles(candles)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        s.close()
