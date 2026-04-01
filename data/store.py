"""Unified data store — SQLite for persistence, in-memory deque for hot cache."""

import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    """OHLCV candle."""
    symbol: str
    tf: str
    ts: int  # milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dt(self) -> datetime:
        """UTC datetime from timestamp."""
        return datetime.fromtimestamp(self.ts / 1000, tz=timezone.utc)


@dataclass
class Position:
    """Open position state."""
    symbol: str
    side: str
    entry_price: float
    size_usd: float
    notional_usd: float
    leverage: float
    liq_price: float
    entry_ts: int
    entry_order_id: str
    stop_price: float
    exit_price: float
    status: str = "open"


@dataclass
class Trade:
    """Completed trade with full cost breakdown."""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size_usd: float
    notional_usd: float
    leverage: float
    liq_price: float
    entry_ts: int
    exit_ts: int
    exit_reason: str
    pnl_usd: float = 0.0
    funding_usd: float = 0.0
    slippage_usd: float = 0.0
    entry_fee_usd: float = 0.0
    exit_fee_usd: float = 0.0
    maker_rebate_usd: float = 0.0
    equity_return_pct: float = 0.0
    # Enhanced fields
    stop_price: float = 0.0
    target_price: float = 0.0
    vwap_at_entry: float = 0.0
    sigma_at_entry: float = 0.0
    margin_used_usd: float = 0.0
    equity_at_entry: float = 0.0
    liq_buffer_ratio: float = 0.0
    hold_candles: int = 0
    hold_minutes: float = 0.0
    adx_at_entry: float = 0.0
    ema_at_entry: float = 0.0
    trend_direction_at_entry: str = ""
    regime_trending_at_entry: int = 0
    vwap_std_dev_at_entry: float = 0.0
    volume_at_entry: float = 0.0
    net_pnl_usd: float = 0.0
    source: str = "backtest"
    window_idx: int | None = None

    @property
    def net_pnl(self) -> float:
        """Net PnL including all costs.

        entry_fee_usd and exit_fee_usd already contain maker rebates (positive)
        or taker fees (negative). maker_rebate_usd is informational only.
        """
        return (self.pnl_usd + self.slippage_usd + self.entry_fee_usd
                + self.exit_fee_usd + self.funding_usd)


@dataclass
class HotState:
    """Live trading state."""
    position: Position | None = None
    open_orders: list = field(default_factory=list)
    daily_pnl_usd: float = 0.0
    daily_start_equity: float = 0.0
    trade_count_today: int = 0
    halted: bool = False
    halt_reason: str | None = None


class Store:
    """SQLite-backed data store with in-memory cache."""

    def __init__(self, db_path: str):
        """Connect to SQLite and initialize schema."""
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], deque] = {}
        self._hot_state = HotState()
        self._init_schema()

    def _init_schema(self) -> None:
        """Create all tables and set pragmas."""
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    tf TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    open REAL, high REAL, low REAL, close REAL, volume REAL,
                    PRIMARY KEY (symbol, tf, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_candles_lookup
                    ON candles (symbol, tf, ts DESC);

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'backtest',
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    stop_price REAL,
                    target_price REAL,
                    vwap_at_entry REAL,
                    sigma_at_entry REAL,
                    size_usd REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    leverage REAL NOT NULL,
                    margin_used_usd REAL,
                    equity_at_entry REAL,
                    liq_price REAL,
                    liq_buffer_ratio REAL,
                    entry_ts INTEGER NOT NULL,
                    exit_ts INTEGER NOT NULL,
                    hold_candles INTEGER,
                    hold_minutes REAL,
                    exit_reason TEXT NOT NULL,
                    pnl_usd REAL NOT NULL DEFAULT 0,
                    slippage_usd REAL NOT NULL DEFAULT 0,
                    entry_fee_usd REAL NOT NULL DEFAULT 0,
                    exit_fee_usd REAL NOT NULL DEFAULT 0,
                    funding_usd REAL NOT NULL DEFAULT 0,
                    maker_rebate_usd REAL NOT NULL DEFAULT 0,
                    net_pnl_usd REAL,
                    equity_return_pct REAL,
                    adx_at_entry REAL,
                    ema_at_entry REAL,
                    trend_direction_at_entry TEXT,
                    regime_trending_at_entry INTEGER,
                    vwap_std_dev_at_entry REAL,
                    volume_at_entry REAL,
                    window_idx INTEGER,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts
                    ON trades (symbol, entry_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_trades_source ON trades (source);
                CREATE INDEX IF NOT EXISTS idx_trades_exit_reason
                    ON trades (exit_reason);

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    tf TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    signal_type TEXT NOT NULL,
                    acted_on INTEGER NOT NULL DEFAULT 0,
                    block_reason TEXT,
                    price REAL,
                    vwap REAL,
                    sigma_dist REAL,
                    upper_band REAL,
                    lower_band REAL,
                    std_dev REAL,
                    adx REAL,
                    ema REAL,
                    trend_direction TEXT,
                    funding_rate REAL,
                    entry_price REAL,
                    stop_price REAL,
                    exit_price REAL,
                    source TEXT NOT NULL DEFAULT 'backtest',
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_signals_lookup
                    ON signals (symbol, tf, ts DESC);

                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'backtest',
                    pnl_usd REAL NOT NULL DEFAULT 0,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    max_dd_pct REAL NOT NULL DEFAULT 0,
                    start_equity REAL,
                    end_equity REAL,
                    halted INTEGER NOT NULL DEFAULT 0,
                    signals_generated INTEGER DEFAULT 0,
                    signals_blocked INTEGER DEFAULT 0,
                    PRIMARY KEY (date, symbol, source)
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            self._conn.commit()

    def upsert_candles(self, candles: list[Candle]) -> int:
        """Thread-safe bulk upsert of candles. Returns count inserted."""
        if not candles:
            return 0
        rows = [
            (c.symbol, c.tf, c.ts, c.open, c.high, c.low, c.close, c.volume)
            for c in candles
        ]
        with self._lock:
            self._conn.executemany(
                """INSERT OR REPLACE INTO candles
                   (symbol, tf, ts, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            self._conn.commit()

        # Update cache
        for c in candles:
            key = (c.symbol, c.tf)
            if key not in self._cache:
                self._cache[key] = deque(maxlen=200)
            cache = self._cache[key]
            # Append if newer than last cached
            if not cache or c.ts > cache[-1].ts:
                cache.append(c)
            else:
                # Find and replace by ts
                for i, existing in enumerate(cache):
                    if existing.ts == c.ts:
                        cache[i] = c
                        break

        return len(rows)

    def get_candles(
        self,
        symbol: str,
        tf: str,
        limit: int = 200,
        from_memory: bool = True,
    ) -> list[Candle]:
        """Get candles, cache-first with DB fallback."""
        key = (symbol, tf)
        if from_memory and key in self._cache and len(self._cache[key]) >= limit:
            return list(self._cache[key])[-limit:]

        # DB fallback
        rows = self._conn.execute(
            """SELECT symbol, tf, ts, open, high, low, close, volume
               FROM candles WHERE symbol = ? AND tf = ?
               ORDER BY ts DESC LIMIT ?""",
            (symbol, tf, limit),
        ).fetchall()

        candles = [
            Candle(r["symbol"], r["tf"], r["ts"], r["open"], r["high"],
                   r["low"], r["close"], r["volume"])
            for r in reversed(rows)
        ]
        return candles

    def warm_cache(self, symbol: str, tf: str, limit: int = 200) -> int:
        """Pre-load candles into memory cache. Returns count loaded."""
        candles = self.get_candles(symbol, tf, limit, from_memory=False)
        key = (symbol, tf)
        self._cache[key] = deque(candles, maxlen=200)
        return len(candles)

    def record_trade(self, trade: Trade) -> int:
        """Write trade to DB, update hot state. Returns trade ID."""
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO trades (
                    symbol, side, source, entry_price, exit_price,
                    stop_price, target_price, vwap_at_entry, sigma_at_entry,
                    size_usd, notional_usd, leverage, margin_used_usd,
                    equity_at_entry, liq_price, liq_buffer_ratio,
                    entry_ts, exit_ts, hold_candles, hold_minutes,
                    exit_reason, pnl_usd, slippage_usd, entry_fee_usd,
                    exit_fee_usd, funding_usd, maker_rebate_usd,
                    net_pnl_usd, equity_return_pct,
                    adx_at_entry, ema_at_entry, trend_direction_at_entry,
                    regime_trending_at_entry, vwap_std_dev_at_entry,
                    volume_at_entry, window_idx
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
                )""",
                (
                    trade.symbol, trade.side, trade.source,
                    trade.entry_price, trade.exit_price,
                    trade.stop_price, trade.target_price,
                    trade.vwap_at_entry, trade.sigma_at_entry,
                    trade.size_usd, trade.notional_usd, trade.leverage,
                    trade.margin_used_usd, trade.equity_at_entry,
                    trade.liq_price, trade.liq_buffer_ratio,
                    trade.entry_ts, trade.exit_ts,
                    trade.hold_candles, trade.hold_minutes,
                    trade.exit_reason, trade.pnl_usd, trade.slippage_usd,
                    trade.entry_fee_usd, trade.exit_fee_usd,
                    trade.funding_usd, trade.maker_rebate_usd,
                    trade.net_pnl_usd, trade.equity_return_pct,
                    trade.adx_at_entry, trade.ema_at_entry,
                    trade.trend_direction_at_entry,
                    trade.regime_trending_at_entry,
                    trade.vwap_std_dev_at_entry, trade.volume_at_entry,
                    trade.window_idx,
                ),
            )
            self._conn.commit()
            trade_id = cursor.lastrowid

        # Update hot state
        self._hot_state.daily_pnl_usd += trade.net_pnl
        self._hot_state.trade_count_today += 1
        return trade_id

    def get_trades(
        self,
        symbol: str | None = None,
        since_ts: int | None = None,
        limit: int = 100,
    ) -> list[Trade]:
        """Retrieve trades from DB."""
        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if since_ts:
            query += " AND entry_ts >= ?"
            params.append(since_ts)
        query += " ORDER BY entry_ts DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        trades = []
        for r in rows:
            trades.append(Trade(
                symbol=r["symbol"], side=r["side"],
                entry_price=r["entry_price"], exit_price=r["exit_price"],
                size_usd=r["size_usd"], notional_usd=r["notional_usd"],
                leverage=r["leverage"], liq_price=r["liq_price"] or 0.0,
                entry_ts=r["entry_ts"], exit_ts=r["exit_ts"],
                exit_reason=r["exit_reason"],
                pnl_usd=r["pnl_usd"], funding_usd=r["funding_usd"],
                slippage_usd=r["slippage_usd"],
                entry_fee_usd=r["entry_fee_usd"],
                exit_fee_usd=r["exit_fee_usd"],
                maker_rebate_usd=r["maker_rebate_usd"],
                equity_return_pct=r["equity_return_pct"] or 0.0,
                net_pnl_usd=r["net_pnl_usd"] or 0.0,
                source=r["source"],
            ))
        return trades

    def log_signal(
        self,
        symbol: str,
        tf: str,
        ts: int,
        signal_type: str,
        acted_on: bool,
        block_reason: str | None = None,
        source: str = "backtest",
        **kwargs: Any,
    ) -> None:
        """Log a signal to the signals table."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO signals (
                    symbol, tf, ts, signal_type, acted_on, block_reason,
                    price, vwap, sigma_dist, upper_band, lower_band,
                    std_dev, adx, ema, trend_direction, funding_rate,
                    entry_price, stop_price, exit_price, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?)""",
                (
                    symbol, tf, ts, signal_type, int(acted_on), block_reason,
                    kwargs.get("price"), kwargs.get("vwap"),
                    kwargs.get("sigma_dist"), kwargs.get("upper_band"),
                    kwargs.get("lower_band"), kwargs.get("std_dev"),
                    kwargs.get("adx"), kwargs.get("ema"),
                    kwargs.get("trend_direction"), kwargs.get("funding_rate"),
                    kwargs.get("entry_price"), kwargs.get("stop_price"),
                    kwargs.get("exit_price"), source,
                ),
            )
            self._conn.commit()

    def update_daily_pnl(
        self,
        date_str: str,
        symbol: str,
        pnl_usd: float,
        trade_count: int,
        max_dd_pct: float,
        source: str = "backtest",
        start_equity: float | None = None,
        end_equity: float | None = None,
        halted: bool = False,
        signals_generated: int = 0,
        signals_blocked: int = 0,
    ) -> None:
        """Upsert daily PnL record."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO daily_pnl (
                    date, symbol, source, pnl_usd, trade_count, max_dd_pct,
                    start_equity, end_equity, halted,
                    signals_generated, signals_blocked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date_str, symbol, source, pnl_usd, trade_count, max_dd_pct,
                 start_equity, end_equity, int(halted),
                 signals_generated, signals_blocked),
            )
            self._conn.commit()

    def get_daily_pnl(self, days: int = 30) -> list[dict]:
        """Get daily PnL records."""
        rows = self._conn.execute(
            """SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?""",
            (days,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_state(self) -> HotState:
        """Return a snapshot of the current hot state."""
        return self._hot_state

    def reconcile_daily_state(self, capital_usdc: float) -> None:
        """Rebuild hot state from today's DB trades on startup."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_of_day_ms = int(
            datetime.strptime(today, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )
        trades = self.get_trades(since_ts=start_of_day_ms, limit=1000)
        daily_pnl = sum(t.net_pnl for t in trades)
        self._hot_state.daily_pnl_usd = daily_pnl
        self._hot_state.daily_start_equity = capital_usdc
        self._hot_state.trade_count_today = len(trades)
        logger.info(
            "Reconciled daily state: pnl=%.2f trades=%d",
            daily_pnl, len(trades),
        )

    def get_meta(self, key: str) -> str | None:
        """Read a persistent key-value pair."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a persistent key-value pair."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
