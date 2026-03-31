"""WebSocket candle feed with REST backfill and auto-reconnection.

Subscribes to 5m + 15m BTC candles via the HL SDK's WebsocketManager.
Detects candle closes and pushes events to an asyncio.Queue for the engine.
Auto-reconnects on WebSocket disconnect.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import HL_BASE_URL, WS_RECONNECT_MAX_BACKOFF
from data.store import Candle, Store

logger = logging.getLogger(__name__)

# Interval durations in milliseconds
INTERVAL_MS = {"5m": 300_000, "15m": 900_000}


class CandleFeed:
    """Live candle feed via WebSocket with REST backfill."""

    def __init__(
        self,
        info,
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
        self._last_msg_time: float = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sub_ids: list[int] = []
        self._stopped = False

    @property
    def seconds_since_last_msg(self) -> float:
        """Seconds since last WS message. 0 if no messages received yet."""
        if self._last_msg_time == 0.0:
            return 0.0
        return time.time() - self._last_msg_time

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

            if candles:
                self._last_ts[tf] = candles[-1].ts

    def _on_candle(self, msg: dict) -> None:
        """WebSocket callback — runs in the SDK's background thread."""
        try:
            self._last_msg_time = time.time()
            self._process_candle_msg(msg)
        except Exception as e:
            logger.error("WS candle callback error: %s (msg=%s)", e, str(msg)[:500])

    def _process_candle_msg(self, msg: dict) -> None:
        """Parse and handle a candle WS message."""
        data = msg.get("data", msg)

        if isinstance(data, list):
            if not data:
                return
            data = data[-1]

        if not isinstance(data, dict):
            return

        tf = data.get("i", "")
        ts = int(data.get("t", 0))

        if tf not in (self._primary_tf, self._trend_tf):
            return

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
            self._store.upsert_candles([candle])

            if tf == self._primary_tf and self._loop:
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    {"type": "candle_close", "tf": tf, "ts": prev_ts, "candle": candle},
                )
                logger.info("Candle close event: %s %s ts=%d", self._symbol, tf, prev_ts)

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

    def reconnect(self) -> None:
        """Tear down old WS and create a fresh connection + subscriptions.

        Called from engine via asyncio.to_thread when stale connection detected.
        """
        logger.warning("Reconnecting WebSocket feed...")

        # Tear down old subscriptions
        for sub_id in self._sub_ids:
            try:
                self._info.unsubscribe(
                    {"type": "candle", "coin": self._symbol, "interval": self._primary_tf},
                    sub_id,
                )
            except Exception:
                pass
        self._sub_ids.clear()

        # Create fresh Info object with new WS connection
        from hyperliquid.info import Info
        try:
            self._info = Info(base_url=HL_BASE_URL, skip_ws=False)
        except Exception as e:
            logger.error("Failed to create new Info object: %s", e)
            return

        # Backfill any missed candles
        self.backfill()

        # Re-subscribe
        for tf in (self._primary_tf, self._trend_tf):
            sub = {"type": "candle", "coin": self._symbol, "interval": tf}
            sub_id = self._info.subscribe(sub, self._on_candle)
            self._sub_ids.append(sub_id)
            logger.info("Reconnected: subscribed to %s %s candles (sub_id=%d)", self._symbol, tf, sub_id)

        self._last_msg_time = 0.0

    def stop(self) -> None:
        """Unsubscribe and disconnect."""
        self._stopped = True
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
