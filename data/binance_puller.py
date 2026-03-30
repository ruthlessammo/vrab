"""Async historical candle puller from Binance Futures (public API, no key needed).

Pulls BTCUSDT perpetual candles and stores as "BTC" in the same candles table.
HL data takes priority in overlapping timestamps (INSERT OR IGNORE).

CLI: python -m data.binance_puller --symbol BTC --tf 5m --days 365
"""

import argparse
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone

import aiohttp

from config import DB_PATH

logger = logging.getLogger(__name__)

BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_MAX_CANDLES = 1500  # Binance limit per request

# Map our timeframe names to Binance interval strings
TF_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# Binance symbol mapping
SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

COMPANION_TF = {"5m": "15m"}


def _init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with candle schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            tf TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, tf, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_candles_lookup
            ON candles (symbol, tf, ts DESC);
    """)
    conn.commit()
    return conn


def _get_existing_ts_range(conn: sqlite3.Connection, symbol: str, tf: str) -> tuple[int | None, int | None]:
    """Get min and max timestamps of existing data (from HL or prior pulls)."""
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol = ? AND tf = ?",
        (symbol, tf),
    ).fetchone()
    if row and row[0] is not None:
        return row[0], row[1]
    return None, None


def _validate_binance_candle(kline: list) -> bool:
    """Validate Binance kline data."""
    try:
        o, h, l, c, v = float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4]), float(kline[5])
        if h < l or o < 0 or c < 0 or v < 0 or h < 0 or l < 0:
            return False
        return True
    except (IndexError, ValueError, TypeError):
        return False


def _upsert_binance_candles(conn: sqlite3.Connection, symbol: str, tf: str,
                            klines: list[list], use_ignore: bool = True) -> int:
    """Insert Binance candles. Uses INSERT OR IGNORE to preserve existing HL data."""
    valid = []
    invalid_count = 0

    for k in klines:
        if not _validate_binance_candle(k):
            invalid_count += 1
            continue
        valid.append((
            symbol, tf, int(k[0]),  # open time as ts
            float(k[1]),  # open
            float(k[2]),  # high
            float(k[3]),  # low
            float(k[4]),  # close
            float(k[5]),  # volume
        ))

    if invalid_count > 0:
        logger.warning("Skipped %d invalid Binance candles", invalid_count)

    if valid:
        # INSERT OR IGNORE: if HL data already exists at this ts, keep it
        sql = """INSERT OR IGNORE INTO candles
                 (symbol, tf, ts, open, high, low, close, volume)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        conn.executemany(sql, valid)
        conn.commit()

    return len(valid)


def _detect_gaps(conn: sqlite3.Connection, symbol: str, tf: str) -> int:
    """Detect gaps > 1.5x interval."""
    interval_ms = INTERVAL_MS.get(tf, 300_000)
    gap_threshold = int(interval_ms * 1.5)

    rows = conn.execute(
        "SELECT ts FROM candles WHERE symbol = ? AND tf = ? ORDER BY ts ASC",
        (symbol, tf),
    ).fetchall()

    gap_count = 0
    for i in range(1, len(rows)):
        diff = rows[i][0] - rows[i - 1][0]
        if diff > gap_threshold:
            gap_count += 1
            if gap_count <= 10:  # only log first 10
                gap_dt = datetime.fromtimestamp(rows[i - 1][0] / 1000, tz=timezone.utc)
                logger.warning(
                    "Gap at %s: %d ms (%.1f intervals)",
                    gap_dt.strftime("%Y-%m-%d %H:%M"), diff, diff / interval_ms,
                )
    return gap_count


async def pull_binance_candles(
    symbol: str,
    tf: str,
    days: int,
    db_path: str = DB_PATH,
) -> None:
    """Pull historical candles from Binance Futures public API.

    No API key needed. Chunks at 1500 candles per request, 0.1s between requests.
    Uses INSERT OR IGNORE to preserve existing HL data at overlapping timestamps.
    """
    conn = _init_db(db_path)
    binance_symbol = SYMBOL_MAP.get(symbol, f"{symbol}USDT")
    binance_tf = TF_MAP.get(tf, tf)
    interval_ms = INTERVAL_MS.get(tf, 300_000)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 86_400_000)

    chunk_duration_ms = BINANCE_MAX_CANDLES * interval_ms
    total_inserted = 0
    chunk_count = 0

    logger.info(
        "Pulling Binance %s %s from %s to %s (%d days)",
        binance_symbol, tf,
        datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        days,
    )

    async with aiohttp.ClientSession() as session:
        cursor = start_ms
        while cursor < now_ms:
            chunk_end = min(cursor + chunk_duration_ms, now_ms)

            params = {
                "symbol": binance_symbol,
                "interval": binance_tf,
                "startTime": cursor,
                "endTime": chunk_end,
                "limit": BINANCE_MAX_CANDLES,
            }

            for attempt in range(3):
                try:
                    async with session.get(
                        f"{BINANCE_FUTURES_URL}/fapi/v1/klines",
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                    if isinstance(data, list) and data:
                        inserted = _upsert_binance_candles(conn, symbol, tf, data)
                        total_inserted += inserted
                        chunk_count += 1

                        if chunk_count % 20 == 0 or chunk_count == 1:
                            logger.info(
                                "Chunk %d: %d candles (%s to %s)",
                                chunk_count, inserted,
                                datetime.fromtimestamp(cursor / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                                datetime.fromtimestamp(chunk_end / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            )

                        # Advance cursor past the last received candle
                        last_ts = int(data[-1][0])
                        cursor = last_ts + interval_ms
                    else:
                        cursor = chunk_end
                    break

                except Exception as e:
                    wait = 2 ** attempt
                    logger.warning(
                        "Binance chunk failed (attempt %d/3): %s — retrying in %ds",
                        attempt + 1, e, wait,
                    )
                    await asyncio.sleep(wait)
            else:
                logger.error("Binance chunk failed after 3 attempts, skipping to next chunk")
                cursor = chunk_end

            await asyncio.sleep(0.1)  # Binance rate limit is generous

    # Summary
    total = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE symbol = ? AND tf = ?",
        (symbol, tf),
    ).fetchone()[0]

    date_range = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol = ? AND tf = ?",
        (symbol, tf),
    ).fetchone()

    gap_count = _detect_gaps(conn, symbol, tf)

    min_dt = datetime.fromtimestamp(date_range[0] / 1000, tz=timezone.utc) if date_range[0] else None
    max_dt = datetime.fromtimestamp(date_range[1] / 1000, tz=timezone.utc) if date_range[1] else None

    # Check how many are from this pull vs already existed
    print(f"\n{'='*60}")
    print(f"Binance pull complete: {symbol} {tf}")
    print(f"  Binance source:    {binance_symbol}")
    print(f"  New candles:       {total_inserted}")
    print(f"  Total in DB:       {total}")
    print(f"  Date range:        {min_dt} → {max_dt}")
    print(f"  Gaps detected:     {gap_count}")
    print(f"{'='*60}\n")

    conn.close()


async def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Pull historical candles from Binance Futures (splices with HL data)"
    )
    parser.add_argument("--symbol", default="BTC", help="Trading symbol (BTC, ETH, SOL)")
    parser.add_argument("--tf", default="5m", help="Candle timeframe")
    parser.add_argument("--days", type=int, default=365, help="Days of history to pull")
    parser.add_argument("--db", default=DB_PATH, help="Database path")
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    # Pull primary timeframe
    await pull_binance_candles(args.symbol, args.tf, args.days, args.db)

    # Auto-pull companion timeframe
    companion = COMPANION_TF.get(args.tf)
    if companion:
        logger.info("Auto-pulling companion timeframe: %s", companion)
        await pull_binance_candles(args.symbol, companion, args.days, args.db)


if __name__ == "__main__":
    asyncio.run(main())
