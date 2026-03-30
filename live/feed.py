"""WebSocket candle feed with REST backfill and reconnection.

Subscribes to 5m + 15m BTC candles via the HL SDK's WebsocketManager.
Detects candle closes and pushes events to an asyncio.Queue for the engine.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from hyperliquid.info import Info

from data.store import Candle, Store

logger = logging.getLogger(__name__)

# Interval durations in milliseconds
INTERVAL_MS = {"5m": 300_000, "15m": 900_000}


class CandleFeed:
    """Live candle feed via WebSocket with REST backfill."""

    def __init__(
        self,
        info: Info,
        symbol: str,
        store: Store,
        candle_queue: asyncio.Queue,
        primary_tf: str = "5m",
        trend_tf: str = "15m",
        backfill_count: int = 200,
    ):
        self._info = info
        self._symbol = symbol
        self._store = store
        self._queue = candle_queue
        self._primary_tf = primary_tf
        self._trend_tf = trend_tf
        self._backfill_count = backfill_count

        # Track the latest candle open timestamp to detect closes
        self._last_ts: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sub_ids: list[int] = []

    def backfill(self) -> None:
        """Pull recent candles from REST to warm the cache."""
        now_ms = int(time.time() * 1000)

        for tf in (self._primary_tf, self._trend_tf):
            interval_ms = INTERVAL_MS[tf]
            start_ms = now_ms - (self._backfill_count * interval_ms)

            raw = self._info.candles_snapshot(
                name=self._symbol,
                interval=tf,
                startTime=start_ms,
                endTime=now_ms,
            )

            candles = []
            for c in raw:
                candles.append(Candle(
                    symbol=self._symbol,
                    tf=tf,
                    ts=int(c["t"]),
                    open=float(c["o"]),
                    high=float(c["h"]),
                    low=float(c["l"]),
                    close=float(c["c"]),
                    volume=float(c["v"]),
                ))

            count = self._store.upsert_candles(candles)
            logger.info(
                "Backfilled %d %s %s candles (%s → %s)",
                count, self._symbol, tf,
                datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            )

            # Track the latest candle ts
            if candles:
                self._last_ts[tf] = candles[-1].ts

    def _on_candle(self, msg: dict) -> None:
        """WebSocket callback — runs in the SDK's background thread."""
        logger.debug("WS raw msg keys=%s", list(msg.keys()))
        data = msg.get("data", {})
        if not data:
            logger.debug("WS msg has no 'data' key, full msg: %s", str(msg)[:500])
            return
        if isinstance(data, list):
            logger.debug("WS data is list (len=%d), first=%s", len(data), str(data[0])[:300] if data else "empty")
            # HL SDK sends candle updates as a list of candle dicts
            data = data[-1] if data else {}
        tf = data.get("i", "")
        ts = int(data.get("t", 0))

        if tf not in (self._primary_tf, self._trend_tf):
            return

        # Build candle
        candle = Candle(
            symbol=data.get("s", self._symbol),
            tf=tf,
            ts=ts,
            open=float(data["o"]),
            high=float(data["h"]),
            low=float(data["l"]),
            close=float(data["c"]),
            volume=float(data["v"]),
        )

        # Detect candle close: new open timestamp means previous candle closed
        prev_ts = self._last_ts.get(tf, 0)
        if ts != prev_ts and prev_ts != 0:
            # Previous candle closed — upsert and notify engine
            self._store.upsert_candles([candle])

            if tf == self._primary_tf and self._loop:
                # Push closed candle event to the engine
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    {"type": "candle_close", "tf": tf, "ts": prev_ts, "candle": candle},
                )
                logger.debug("Candle close: %s %s ts=%d", self._symbol, tf, prev_ts)

        # Always update the live candle in store (partial updates)
        self._store.upsert_candles([candle])
        self._last_ts[tf] = ts

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> None:
        """Subscribe to candle WebSocket channels."""
        self._loop = loop

        for tf in (self._primary_tf, self._trend_tf):
            sub = {"type": "candle", "coin": self._symbol, "interval": tf}
            sub_id = self._info.subscribe(sub, self._on_candle)
            self._sub_ids.append(sub_id)
            logger.info("Subscribed to %s %s candles (sub_id=%d)", self._symbol, tf, sub_id)

    def stop(self) -> None:
        """Unsubscribe and disconnect."""
        for sub_id in self._sub_ids:
            try:
                self._info.unsubscribe(
                    {"type": "candle", "coin": self._symbol, "interval": self._primary_tf},
                    sub_id,
                )
            except Exception:
                pass
        self._sub_ids.clear()
        logger.info("Candle feed stopped")
