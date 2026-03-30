"""Async historical candle puller for Hyperliquid.

CLI: python -m data.puller --symbol BTC --tf 5m --days 120
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

import aiohttp

from config import DB_PATH, HL_BASE_URL, HL_MAX_CANDLES_PER_REQUEST

logger = logging.getLogger(__name__)

# Interval durations in milliseconds
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# When pulling 5m, also pull 15m
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


def _get_max_ts(conn: sqlite3.Connection, symbol: str, tf: str) -> int | None:
    """Get the latest timestamp in DB for this symbol/tf."""
    row = conn.execute(
        "SELECT MAX(ts) FROM candles WHERE symbol = ? AND tf = ?",
        (symbol, tf),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _validate_candle(c: dict) -> bool:
    """Validate candle data integrity."""
    try:
        o, h, l, cl, v = (
            float(c["o"]), float(c["h"]), float(c["l"]),
            float(c["c"]), float(c["v"]),
        )
        if h < l or o < 0 or cl < 0 or v < 0:
            return False
        if h < 0 or l < 0:
            return False
        return True
    except (KeyError, ValueError, TypeError):
        return False


def _upsert_candles(conn: sqlite3.Connection, symbol: str, tf: str,
                    candles: list[dict]) -> int:
    """Insert candles into DB, returns count inserted."""
    valid = []
    invalid_count = 0
    for c in candles:
        if not _validate_candle(c):
            invalid_count += 1
            continue
        valid.append((
            symbol, tf, int(c["t"]),
            float(c["o"]), float(c["h"]), float(c["l"]),
            float(c["c"]), float(c["v"]),
        ))

    if invalid_count > 0:
        logger.warning("Skipped %d invalid candles", invalid_count)

    if valid:
        conn.executemany(
            """INSERT OR REPLACE INTO candles
               (symbol, tf, ts, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            valid,
        )
        conn.commit()
    return len(valid)


def _detect_gaps(conn: sqlite3.Connection, symbol: str, tf: str) -> int:
    """Detect gaps > 1.5× interval. Returns gap count."""
    interval_ms = INTERVAL_MS.get(tf, 300_000)
    gap_threshold = int(interval_ms * 1.5)

    rows = conn.execute(
        """SELECT ts FROM candles WHERE symbol = ? AND tf = ?
           ORDER BY ts ASC""",
        (symbol, tf),
    ).fetchall()

    gap_count = 0
    for i in range(1, len(rows)):
        diff = rows[i][0] - rows[i - 1][0]
        if diff > gap_threshold:
            gap_count += 1
            gap_dt = datetime.fromtimestamp(rows[i - 1][0] / 1000, tz=timezone.utc)
            logger.warning(
                "Gap detected at %s: %d ms (%.1f intervals)",
                gap_dt.isoformat(), diff, diff / interval_ms,
            )
    return gap_count


async def pull_candles(
    symbol: str,
    tf: str,
    days: int,
    db_path: str = DB_PATH,
) -> None:
    """Pull historical candles from Hyperliquid API.

    Chunks at HL_MAX_CANDLES_PER_REQUEST, 0.25s between requests.
    Idempotent: resumes from max ts in DB.
    """
    conn = _init_db(db_path)
    interval_ms = INTERVAL_MS.get(tf, 300_000)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 86_400_000)

    # Resume from existing data
    max_ts = _get_max_ts(conn, symbol, tf)
    if max_ts is not None and max_ts > start_ms:
        start_ms = max_ts + interval_ms
        logger.info("Resuming from %s",
                     datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat())

    chunk_duration_ms = HL_MAX_CANDLES_PER_REQUEST * interval_ms
    total_inserted = 0
    chunk_count = 0

    async with aiohttp.ClientSession() as session:
        cursor = start_ms
        while cursor < now_ms:
            chunk_end = min(cursor + chunk_duration_ms, now_ms)

            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": tf,
                    "startTime": cursor,
                    "endTime": chunk_end,
                },
            }

            for attempt in range(3):
                try:
                    async with session.post(
                        f"{HL_BASE_URL}/info",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                    if isinstance(data, list):
                        inserted = _upsert_candles(conn, symbol, tf, data)
                        total_inserted += inserted
                        chunk_count += 1
                        logger.info(
                            "Chunk %d: %d candles (%s to %s)",
                            chunk_count, inserted,
                            datetime.fromtimestamp(cursor / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            datetime.fromtimestamp(chunk_end / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        )
                    break

                except Exception as e:
                    wait = 2 ** attempt
                    logger.warning(
                        "Chunk failed (attempt %d/3): %s — retrying in %ds",
                        attempt + 1, e, wait,
                    )
                    await asyncio.sleep(wait)
            else:
                logger.error("Chunk failed after 3 attempts at %d, skipping", cursor)

            cursor = chunk_end
            await asyncio.sleep(0.25)

    # Summary
    total = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE symbol = ? AND tf = ?",
        (symbol, tf),
    ).fetchone()[0]

    date_range = conn.execute(
        """SELECT MIN(ts), MAX(ts) FROM candles
           WHERE symbol = ? AND tf = ?""",
        (symbol, tf),
    ).fetchone()

    gap_count = _detect_gaps(conn, symbol, tf)

    min_dt = datetime.fromtimestamp(date_range[0] / 1000, tz=timezone.utc) if date_range[0] else None
    max_dt = datetime.fromtimestamp(date_range[1] / 1000, tz=timezone.utc) if date_range[1] else None

    print(f"\n{'='*60}")
    print(f"Pull complete: {symbol} {tf}")
    print(f"  Inserted this run: {total_inserted}")
    print(f"  Total in DB:       {total}")
    print(f"  Date range:        {min_dt} → {max_dt}")
    print(f"  Gaps detected:     {gap_count}")
    print(f"{'='*60}\n")

    conn.close()


async def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Pull historical candles from Hyperliquid")
    parser.add_argument("--symbol", default="BTC", help="Trading symbol")
    parser.add_argument("--tf", default="5m", help="Candle timeframe")
    parser.add_argument("--days", type=int, default=120, help="Days of history")
    parser.add_argument("--db", default=DB_PATH, help="Database path")
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    # Pull primary timeframe
    await pull_candles(args.symbol, args.tf, args.days, args.db)

    # Auto-pull companion timeframe
    companion = COMPANION_TF.get(args.tf)
    if companion:
        logger.info("Auto-pulling companion timeframe: %s", companion)
        await pull_candles(args.symbol, companion, args.days, args.db)


if __name__ == "__main__":
    asyncio.run(main())
