"""Telegram bot command handler — async long-polling listener.

Runs as a background task in the engine's event loop.
Only responds to messages from the configured TELEGRAM_CHAT_ID.

Commands: /status, /pnl, /equity, /trades, /kill, /reset
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, KILL_SWITCH_PATH,
    TELEGRAM_POLL_INTERVAL,
)
from data.store import Store
from notifications.telegram import (
    send_alert,
    format_status, format_pnl_summary, format_equity, format_trades_list,
)

logger = logging.getLogger(__name__)


class TelegramBot:
    """Long-polling Telegram bot for remote status queries."""

    def __init__(self, store: Store, engine_status, engine=None):
        """
        Args:
            store: Data store for trade/PnL queries.
            engine_status: Shared EngineStatus dataclass updated by the engine.
            engine: LiveEngine instance (for circuit breaker reset).
        """
        self._store = store
        self._status = engine_status
        self._engine = engine
        self._offset = 0  # Telegram update offset for polling
        self._running = False
        self._session: aiohttp.ClientSession | None = None

    async def run(self) -> None:
        """Poll for updates and dispatch commands. Runs until stopped."""
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.info("Telegram not configured — bot disabled")
            return

        self._running = True
        self._session = aiohttp.ClientSession()
        logger.info("Telegram bot started (polling every %ds)", TELEGRAM_POLL_INTERVAL)

        try:
            while self._running:
                try:
                    await self._poll()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("Bot poll error: %s", e)
                    await asyncio.sleep(5)
        finally:
            await self._session.close()
            self._session = None

    def stop(self) -> None:
        """Signal the bot to stop."""
        self._running = False

    async def _poll(self) -> None:
        """Fetch new messages from Telegram."""
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {
            "offset": self._offset,
            "timeout": TELEGRAM_POLL_INTERVAL,
            "allowed_updates": '["message"]',
        }

        try:
            async with self._session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=TELEGRAM_POLL_INTERVAL + 10),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1)
            return

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            # Security: only respond to configured chat
            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text.startswith("/"):
                await self._handle_command(text)

    async def _handle_command(self, text: str) -> None:
        """Route command to handler."""
        cmd = text.split()[0].lower().split("@")[0]  # handle /status@botname

        handlers = {
            "/status": self._cmd_status,
            "/pnl": self._cmd_pnl,
            "/equity": self._cmd_equity,
            "/trades": self._cmd_trades,
            "/close": self._cmd_close,
            "/kill": self._cmd_kill,
            "/reset": self._cmd_reset,
        }

        handler = handlers.get(cmd)
        if handler:
            try:
                response = await handler()
                await send_alert(response)
            except Exception as e:
                logger.error("Command %s failed: %s", cmd, e)
                await send_alert(f"*Error*: `{str(e)[:200]}`")
        else:
            await send_alert(
                "*Commands*\n"
                "/status — Current state\n"
                "/pnl — PnL summary\n"
                "/equity — Total equity\n"
                "/trades — Recent trades\n"
                "/close — Force close position\n"
                "/kill — Emergency stop\n"
                "/reset — Clear circuit breaker"
            )

    async def _cmd_status(self) -> str:
        """Handle /status command."""
        return format_status(self._status, self._status.position)

    async def _cmd_pnl(self) -> str:
        """Handle /pnl command."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = self._status.daily_pnl

        # Weekly: past days from DB (exclude today) + today's live total
        daily_records = self._store.get_daily_pnl(days=7)
        weekly_pnl = sum(
            r["pnl_usd"] for r in daily_records if r["date"] != today
        ) + daily_pnl

        # Total: all trades from DB (already includes today's closed trades)
        all_trades = self._store.get_trades(limit=10000)
        source = self._status.mode
        source_trades = [t for t in all_trades if t.source == source]
        total_pnl = sum(t.net_pnl for t in source_trades)
        total_count = len(source_trades)
        wins = sum(1 for t in source_trades if t.net_pnl > 0)
        win_rate = wins / total_count if total_count else 0.0

        return format_pnl_summary(daily_pnl, weekly_pnl, total_pnl, total_count, win_rate)

    async def _cmd_equity(self) -> str:
        """Handle /equity command."""
        equity = self._status.equity

        all_trades = self._store.get_trades(limit=10000)
        source = self._status.mode
        source_trades = [t for t in all_trades if t.source == source]
        total_pnl = sum(t.net_pnl for t in source_trades)

        initial_capital = float(self._store.get_meta("initial_capital") or "0")
        if initial_capital <= 0:
            initial_capital = equity  # fallback: treat current equity as capital
        return format_equity(equity, initial_capital, total_pnl)

    async def _cmd_trades(self) -> str:
        """Handle /trades command."""
        trades = self._store.get_trades(limit=5)
        return format_trades_list(trades)

    async def _cmd_close(self) -> str:
        """Handle /close command — force close any open position."""
        if not self._engine:
            return "*Error*: Engine reference not available"
        if not self._engine._position:
            # Check HL directly for orphaned positions
            from config import PAPER_MODE, SYMBOL
            if PAPER_MODE:
                return "*No position open*"
            import asyncio
            hl_pos = await asyncio.to_thread(self._engine._client.get_position, SYMBOL)
            if not hl_pos:
                return "*No position open*"
            # Force close orphaned position on HL
            is_buy = hl_pos["side"] == "short"
            await asyncio.to_thread(
                self._engine._client.cancel_all_orders, SYMBOL,
            )
            await asyncio.to_thread(
                self._engine._client.place_market_order,
                SYMBOL, is_buy, hl_pos["size_btc"], reduce_only=True,
            )
            return f"*Orphaned position closed*\n{hl_pos['side']} {hl_pos['size_btc']:.5f} BTC"
        await self._engine._emergency_close("telegram_close")
        return "*Position closed via /close*"

    async def _cmd_kill(self) -> str:
        """Handle /kill command — activate kill switch."""
        Path(KILL_SWITCH_PATH).touch()
        logger.warning("Kill switch activated via Telegram")
        return "*KILL SWITCH ACTIVATED*\nEngine will halt on next candle."

    async def _cmd_reset(self) -> str:
        """Handle /reset command — clear circuit breaker, reset peak equity."""
        if not self._engine:
            return "*Error*: Engine reference not available"
        equity = self._status.equity
        self._engine._circuit_breaker = False
        self._engine._peak_equity = equity
        self._store.set_meta("circuit_breaker", "0")
        self._store.set_meta("peak_equity", str(equity))
        logger.warning("Circuit breaker reset via Telegram (peak=%.2f)", equity)
        return f"*Circuit Breaker Reset*\nPeak equity set to `${equity:.2f}`\nTrading resumed."
