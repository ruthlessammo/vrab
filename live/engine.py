"""Live trading engine — thin async adapter over strategy.core.

Mirrors backtest/engine.py:simulate_window() but with real candles and real orders.
All trading decisions go through the shared core — this file only handles I/O.

CLI: python -m live.engine
"""

import asyncio
import bisect
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import (
    CAPITAL_USDC, DB_PATH, SYMBOL, CANDLE_TF, TREND_TF,
    TARGET_LEVERAGE, RISK_PER_TRADE,
    MAX_DAILY_LOSS_MULTIPLIER, MAX_DRAWDOWN_PCT,
    PAPER_MODE,
    HL_PRIVATE_KEY, HL_BASE_URL, HL_WALLET_ADDRESS,
    CANDLE_BACKFILL_COUNT, HEARTBEAT_INTERVAL_CANDLES,
    DAILY_SUMMARY_ENABLED,
    is_kill_switch_active,
)
from strategy.core import (
    TradingParams, CoreDecision, TradeSetup,
    build_params_from_config, evaluate_entry, evaluate_exit,
    calc_trade_pnl, check_daily_halt,
)
from data.store import Store, Trade, Candle
from notifications.telegram import (
    send_alert, format_trade_alert, format_halt_alert, format_error_alert,
    format_daily_summary,
)
from notifications.bot import TelegramBot
from live.feed import CandleFeed

logger = logging.getLogger(__name__)

# Dead-man switch interval: 10 minutes
DEADMAN_INTERVAL_MS = 600_000
# Position sanity check interval
SANITY_CHECK_CANDLES = 12  # every hour

SOURCE = "paper" if PAPER_MODE else "live"


@dataclass
class PositionState:
    """Local position tracking — mirrors backtest state."""
    side: str
    entry_price: float
    stop_price: float
    target_price: float
    size_usd: float
    size_btc: float
    liq_price: float
    liq_buffer_ratio: float
    equity_at_entry: float
    entry_ts: int
    hold_candles: int = 0
    entry_oid: int | None = None
    stop_oid: int | None = None
    target_oid: int | None = None
    signal_context: dict = field(default_factory=dict)


@dataclass
class PendingEntry:
    """Pending entry order awaiting fill."""
    oid: int
    setup: TradeSetup
    candles_waiting: int
    equity: float
    signal_context: dict
    entry_ts: int


@dataclass
class EngineStatus:
    """Shared status object — read by the Telegram bot."""
    position: PositionState | None = None
    equity: float = 0.0
    daily_pnl: float = 0.0
    halted: bool = False
    uptime_seconds: float = 0.0
    candle_count: int = 0
    mode: str = "paper"
    trade_count_today: int = 0
    # Market state
    price: float = 0.0
    vwap: float = 0.0
    sigma_dist: float = 0.0
    adx: float = 0.0
    trend: str = ""


class LiveEngine:
    """Main live trading loop."""

    def __init__(self, client, store: Store, params: TradingParams):
        self._client = client
        self._store = store
        self._params = params

        # Position state
        self._position: PositionState | None = None

        # Daily tracking
        self._current_day: str | None = None
        self._daily_pnl: float = 0.0
        self._halted_today: bool = False
        self._trade_count_today: int = 0
        self._signals_today: int = 0
        self._signals_blocked_today: int = 0
        self._daily_max_dd: float = 0.0
        self._daily_start_equity: float = CAPITAL_USDC

        # Entry expiry
        self._pending_signal_dir: str | None = None
        self._pending_signal_count: int = 0

        # Pending entry order
        self._pending_entry: PendingEntry | None = None

        # Circuit breaker (persists across days and restarts)
        self._peak_equity: float = CAPITAL_USDC
        self._circuit_breaker: bool = False

        # Candle counter for periodic tasks
        self._candle_count: int = 0
        self._start_time: float = time.time()

        # Shared status for Telegram bot
        self.status = EngineStatus(mode=SOURCE)

        # Shutdown flag
        self._shutdown = False

    async def run(self) -> None:
        """Main event loop."""
        loop = asyncio.get_event_loop()
        candle_queue: asyncio.Queue = asyncio.Queue()

        # Setup signal handlers for graceful shutdown
        # Second signal forces immediate exit
        def _handle_signal():
            if self._shutdown:
                logger.info("Forced exit")
                raise SystemExit(1)
            asyncio.ensure_future(self._graceful_shutdown())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

        # Step 1: Connect and reconcile
        logger.info("Starting live engine (paper=%s)", PAPER_MODE)
        await asyncio.to_thread(self._client.connect, SYMBOL, TARGET_LEVERAGE)
        await self._reconcile()

        # Step 2: Backfill and subscribe to candle feed
        info = self._client.info if not PAPER_MODE else self._create_info_for_paper()
        feed = CandleFeed(
            info=info, symbol=SYMBOL, store=self._store,
            candle_queue=candle_queue,
            backfill_count=CANDLE_BACKFILL_COUNT,
        )
        await asyncio.to_thread(feed.backfill)
        feed.subscribe(loop)

        # Step 3: Start dead-man switch
        if not PAPER_MODE:
            await self._refresh_deadman()

        # Step 4: Start Telegram bot
        bot = TelegramBot(self._store, self.status, engine=self)
        bot_task = asyncio.create_task(bot.run())

        await send_alert(f"*VRAB Started*\nMode: `{'paper' if PAPER_MODE else 'LIVE'}`\nSymbol: `{SYMBOL}`")

        # Step 5: Main loop — process candle close events
        logger.info("Engine running — waiting for candle events...")
        no_event_count = 0
        while not self._shutdown:
            try:
                event = await asyncio.wait_for(candle_queue.get(), timeout=5)
                no_event_count = 0
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                no_event_count += 1
                # After 6 minutes of silence, reconnect the WS feed
                if no_event_count >= 72:  # 72 × 5s = 6 minutes
                    logger.warning("No candle event for 6 minutes — reconnecting feed")
                    try:
                        await asyncio.to_thread(feed.reconnect)
                        await send_alert("*Feed Reconnected* — WS was stale")
                    except Exception as e:
                        logger.error("Feed reconnect failed: %s", e)
                    no_event_count = 0
                continue

            # Real-time paper fill checking on every WS tick (~1s)
            if event.get("type") == "tick":
                if PAPER_MODE and self._pending_entry:
                    self._client.set_mid_price(event["price"])
                    filled = self._client.check_fills(event["high"], event["low"])
                    for fill in filled:
                        await self._on_paper_fill(fill, candle_ts=0)
                continue

            if event.get("type") != "candle_close":
                continue

            try:
                await self._on_candle_close(event)
            except Exception as e:
                logger.error("Error processing candle: %s", e, exc_info=True)
                await send_alert(format_error_alert(str(e)))

        # Cleanup
        bot.stop()
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        feed.stop()
        if not PAPER_MODE:
            await asyncio.to_thread(self._client.unschedule_cancel)
        logger.info("Engine stopped")

    def _create_info_for_paper(self):
        """Create a standalone Info object for paper mode (WS candle feed)."""
        from hyperliquid.info import Info
        return Info(base_url=HL_BASE_URL, skip_ws=False)

    async def _reconcile(self) -> None:
        """Startup reconciliation — sync local state with exchange."""
        self._store.reconcile_daily_state(CAPITAL_USDC)
        hot = self._store.get_daily_state()
        self._daily_pnl = hot.daily_pnl_usd
        self._halted_today = hot.halted
        self._current_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Load circuit breaker state
        peak = self._store.get_meta("peak_equity")
        if peak:
            self._peak_equity = float(peak)
        self._circuit_breaker = self._store.get_meta("circuit_breaker") == "1"
        if self._circuit_breaker:
            logger.warning("Circuit breaker ACTIVE from previous session (peak=%.2f)", self._peak_equity)
            await send_alert(f"*CIRCUIT BREAKER ACTIVE*\nPeak equity: `${self._peak_equity:.2f}`\nSend /reset to resume trading")

        if PAPER_MODE:
            logger.info("Paper mode — no position reconciliation needed")
            return

        # Check HL for existing position
        hl_pos = await asyncio.to_thread(self._client.get_position, SYMBOL)
        if hl_pos:
            logger.warning(
                "HL has open position: %s %.5f BTC @ %.1f — NOT auto-managing. "
                "Close manually or set up position state.",
                hl_pos["side"], hl_pos["size_btc"], hl_pos["entry_price"],
            )
            await send_alert(
                f"*WARNING*: Existing {hl_pos['side']} position found on startup. "
                f"Size: {hl_pos['size_btc']:.5f} BTC @ {hl_pos['entry_price']:.1f}. "
                f"Not auto-managing — close manually if unintended."
            )

        # Cancel any stale orders
        await asyncio.to_thread(self._client.cancel_all_orders, SYMBOL)
        logger.info("Reconciliation complete")

    async def _refresh_deadman(self) -> None:
        """Refresh the dead-man switch timer."""
        cancel_at = int(time.time() * 1000) + DEADMAN_INTERVAL_MS
        await asyncio.to_thread(self._client.schedule_cancel, cancel_at)

    async def _on_candle_close(self, event: dict) -> None:
        """Process a closed 5m candle — the core trading loop."""
        candle: Candle = event["candle"]
        candle_ts = candle.ts
        candle_day = datetime.fromtimestamp(candle_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        self._candle_count += 1

        # --- Kill switch ---
        if is_kill_switch_active():
            logger.warning("Kill switch active — cancelling all orders")
            await asyncio.to_thread(self._client.cancel_all_orders, SYMBOL)
            if self._position:
                await self._emergency_close("kill_switch")
            self._halted_today = True
            await send_alert("*KILL SWITCH ACTIVE* — all orders cancelled, position closed")
            return

        # --- New day reset ---
        if candle_day != self._current_day:
            await self._finalize_day(candle_day)

        # --- Dead-man switch refresh ---
        if not PAPER_MODE and self._candle_count % 2 == 0:
            await self._refresh_deadman()

        # --- Cache equity for this candle (single call) ---
        equity = await self._get_equity()

        # --- Update shared status for Telegram bot ---
        self.status.position = self._position
        self.status.equity = equity
        self.status.daily_pnl = self._daily_pnl
        self.status.halted = self._halted_today or self._circuit_breaker
        self.status.uptime_seconds = time.time() - self._start_time
        self.status.candle_count = self._candle_count
        self.status.trade_count_today = self._trade_count_today

        # --- Circuit breaker: track peak equity, halt on excessive drawdown ---
        if equity > self._peak_equity:
            self._peak_equity = equity
            self._store.set_meta("peak_equity", str(equity))

        if self._circuit_breaker:
            return

        if self._peak_equity > 0:
            dd_from_peak = (self._peak_equity - equity) / self._peak_equity
            if dd_from_peak >= MAX_DRAWDOWN_PCT:
                self._circuit_breaker = True
                self._store.set_meta("circuit_breaker", "1")
                logger.warning(
                    "CIRCUIT BREAKER: dd=%.2f%% peak=%.2f equity=%.2f",
                    dd_from_peak * 100, self._peak_equity, equity,
                )
                await send_alert(
                    f"*CIRCUIT BREAKER TRIGGERED*\n"
                    f"Drawdown: `{dd_from_peak:.2%}` from peak `${self._peak_equity:.2f}`\n"
                    f"Current equity: `${equity:.2f}`\n"
                    f"Send /reset to resume trading"
                )
                return

        # --- Periodic tasks (run regardless of position state) ---
        if self._candle_count % HEARTBEAT_INTERVAL_CANDLES == 0:
            await self._heartbeat(equity)

        if not PAPER_MODE and self._candle_count % SANITY_CHECK_CANDLES == 0:
            await self._sanity_check()

        # --- Prepare candle data ---
        primary_candles = self._store.get_candles(SYMBOL, CANDLE_TF, limit=200)
        trend_candles = self._store.get_candles(SYMBOL, TREND_TF, limit=200)

        if len(primary_candles) < self._params.vwap_window + 5:
            logger.warning("Insufficient candles (%d), skipping", len(primary_candles))
            return

        vwap_win = self._params.vwap_window
        closes = [c.close for c in primary_candles[-vwap_win:]]
        highs = [c.high for c in primary_candles[-vwap_win:]]
        lows = [c.low for c in primary_candles[-vwap_win:]]
        volumes = [c.volume for c in primary_candles[-vwap_win:]]

        # 15m trend alignment — same as backtest
        trend_boundary = candle_ts - 900_000
        trend_ts_arr = [c.ts for c in trend_candles]
        trend_idx = bisect.bisect_right(trend_ts_arr, trend_boundary)
        trend_start = max(0, trend_idx - 100)
        t_closes = [c.close for c in trend_candles[trend_start:trend_idx]]
        t_highs = [c.high for c in trend_candles[trend_start:trend_idx]]
        t_lows = [c.low for c in trend_candles[trend_start:trend_idx]]

        # --- Belt-and-suspenders: check paper fills at candle close using closed candle's full range ---
        if PAPER_MODE:
            closed = event.get("closed_candle")
            check_high = closed.high if closed else candle.high
            check_low = closed.low if closed else candle.low
            self._client.set_mid_price(candle.close)
            filled = self._client.check_fills(check_high, check_low)
            for fill in filled:
                await self._on_paper_fill(fill, candle_ts)

        # --- Get real funding rate ---
        if not PAPER_MODE:
            try:
                funding_rate = await asyncio.to_thread(self._client.get_funding_rate, SYMBOL)
            except Exception:
                funding_rate = self._params.hourly_funding_rate
        else:
            funding_rate = self._params.hourly_funding_rate

        # --- If in position: evaluate exit ---
        if self._position is not None:
            self._position.hold_candles += 1

            exit_decision = evaluate_exit(
                candle_high=candle.high,
                candle_low=candle.low,
                candle_close=candle.close,
                position_side=self._position.side,
                position_entry_price=self._position.entry_price,
                position_stop_price=self._position.stop_price,
                position_target_price=self._position.target_price,
                hold_candles=self._position.hold_candles,
                params=self._params,
            )

            if exit_decision.action == "exit":
                await self._execute_exit(exit_decision, candle_ts)
            return

        # --- If halted, skip entries ---
        if self._halted_today:
            return

        # --- Check pending entry order ---
        if self._pending_entry:
            await self._check_pending_entry(candle_ts)
            return

        # --- No position, no pending: evaluate entry ---
        await self._evaluate_and_enter(
            candle, candle_ts, equity, funding_rate,
            closes, highs, lows, volumes,
            t_closes, t_highs, t_lows,
        )

    async def _finalize_day(self, new_day: str) -> None:
        """Finalize previous day's PnL, send summary, reset counters."""
        if self._current_day is not None:
            prev_equity = await self._get_equity()
            self._store.update_daily_pnl(
                date_str=self._current_day,
                symbol=SYMBOL,
                pnl_usd=self._daily_pnl,
                trade_count=self._trade_count_today,
                max_dd_pct=self._daily_max_dd,
                source=SOURCE,
                start_equity=self._daily_start_equity,
                end_equity=prev_equity,
                halted=self._halted_today,
                signals_generated=self._signals_today,
                signals_blocked=self._signals_blocked_today,
            )
            if DAILY_SUMMARY_ENABLED:
                summary = format_daily_summary(
                    date=self._current_day,
                    pnl=self._daily_pnl,
                    trade_count=self._trade_count_today,
                    equity=prev_equity,
                    signals_generated=self._signals_today,
                    signals_blocked=self._signals_blocked_today,
                )
                await send_alert(summary)

        self._current_day = new_day
        self._daily_pnl = 0.0
        self._halted_today = False
        self._trade_count_today = 0
        self._signals_today = 0
        self._signals_blocked_today = 0
        self._daily_max_dd = 0.0
        self._daily_start_equity = await self._get_equity()
        logger.info("New trading day: %s", new_day)

    async def _check_pending_entry(self, candle_ts: int) -> None:
        """Check pending entry order for fill or expiry."""
        self._pending_entry.candles_waiting += 1
        oid = self._pending_entry.oid

        if not PAPER_MODE:
            status = await asyncio.to_thread(self._client.query_order_status, oid)
            is_filled = status.get("status") == "filled" or status.get("order", {}).get("status") == "filled"
        else:
            status = self._client.query_order_status(oid)
            is_filled = status.get("status") == "filled"

        if status.get("status") == "cancelled":
            logger.info("Entry order cancelled (oid=%d)", oid)
            self._pending_entry = None
            self._pending_signal_dir = None
            self._pending_signal_count = 0
            return

        if is_filled:
            await self._on_entry_filled(self._pending_entry, candle_ts)
            self._pending_entry = None
            return

        if self._pending_entry.candles_waiting > self._params.entry_expiry_candles:
            logger.info("Entry order expired (oid=%d, waited %d candles)", oid, self._pending_entry.candles_waiting)
            if PAPER_MODE:
                self._client.cancel_order(SYMBOL, oid)
            else:
                await asyncio.to_thread(self._client.cancel_order, SYMBOL, oid)
            self._pending_entry = None
            self._pending_signal_dir = None
            self._pending_signal_count = 0

    async def _evaluate_and_enter(
        self, candle: Candle, candle_ts: int, equity: float, funding_rate: float,
        closes: list, highs: list, lows: list, volumes: list,
        t_closes: list, t_highs: list, t_lows: list,
    ) -> None:
        """Evaluate entry signal and place order if appropriate."""
        entry_decision = evaluate_entry(
            closes=closes, highs=highs, lows=lows, volumes=volumes,
            trend_closes=t_closes, trend_highs=t_highs, trend_lows=t_lows,
            equity=equity, current_position_side=None,
            funding_rate=funding_rate, params=self._params,
        )

        # Log market state every candle and update shared status
        sig = entry_decision.signal_result
        if sig and sig.vwap_state and sig.regime:
            self.status.price = sig.price
            self.status.vwap = sig.vwap_state.vwap
            self.status.sigma_dist = sig.sigma_dist
            self.status.adx = sig.regime.adx
            self.status.trend = sig.regime.trend_direction
            logger.info(
                "Market: price=%.1f vwap=%.1f σ=%.2f adx=%.1f trend=%s | %s%s",
                sig.price, sig.vwap_state.vwap, sig.sigma_dist,
                sig.regime.adx, sig.regime.trend_direction,
                entry_decision.action,
                f" ({entry_decision.block_reason})" if entry_decision.block_reason else "",
            )

        # Track signal counts
        if sig and sig.signal in ("long_entry", "short_entry"):
            self._signals_today += 1
            if entry_decision.action == "skip" and entry_decision.block_reason:
                self._signals_blocked_today += 1

        # Log signal
        if sig:
            self._store.log_signal(
                symbol=SYMBOL, tf=CANDLE_TF, ts=candle_ts,
                signal_type=sig.signal or "none",
                acted_on=entry_decision.action == "enter",
                block_reason=entry_decision.block_reason,
                source=SOURCE,
                price=sig.price,
                vwap=sig.vwap_state.vwap if sig.vwap_state else None,
                sigma_dist=sig.sigma_dist,
                adx=sig.regime.adx if sig.regime else None,
                ema=sig.regime.ema if sig.regime else None,
                trend_direction=sig.regime.trend_direction if sig.regime else None,
                funding_rate=funding_rate,
            )

        if entry_decision.action == "skip":
            if sig and sig.signal in ("long_entry", "short_entry"):
                if sig.signal == self._pending_signal_dir:
                    self._pending_signal_count += 1
                else:
                    self._pending_signal_dir = sig.signal
                    self._pending_signal_count = 1
            else:
                self._pending_signal_dir = None
                self._pending_signal_count = 0
            return

        if entry_decision.action == "enter":
            setup = entry_decision.trade_setup

            sig_dir = setup.signal
            if sig_dir == self._pending_signal_dir:
                self._pending_signal_count += 1
                if self._pending_signal_count > self._params.entry_expiry_candles:
                    self._pending_signal_dir = None
                    self._pending_signal_count = 0
                    logger.info("Entry signal expired (stale %d candles)", self._pending_signal_count)
                    return
            else:
                self._pending_signal_dir = sig_dir
                self._pending_signal_count = 1

            await self._execute_entry(setup, candle_ts, equity)

    async def _execute_entry(self, setup: TradeSetup, candle_ts: int, equity: float) -> None:
        """Place entry order."""
        is_buy = setup.side == "long"
        size_btc = setup.size_usd / setup.entry_price

        result = await asyncio.to_thread(
            self._client.place_limit_order,
            SYMBOL, is_buy, size_btc, setup.entry_price,
            reduce_only=False, post_only=True,
        )

        oid = self._extract_oid(result)
        if oid is None:
            logger.error("Failed to place entry order: %s", result)
            return

        # Capture signal context
        sig = setup.signal_result
        signal_context = {}
        if sig and sig.vwap_state:
            signal_context["vwap_at_entry"] = sig.vwap_state.vwap
            signal_context["sigma_at_entry"] = sig.sigma_dist
            signal_context["vwap_std_dev_at_entry"] = sig.vwap_state.std_dev
        if sig and sig.regime:
            signal_context["adx_at_entry"] = sig.regime.adx
            signal_context["ema_at_entry"] = sig.regime.ema
            signal_context["trend_direction_at_entry"] = sig.regime.trend_direction
            signal_context["regime_trending_at_entry"] = int(sig.regime.is_trending)

        self._pending_entry = PendingEntry(
            oid=oid,
            setup=setup,
            candles_waiting=0,
            equity=equity,
            signal_context=signal_context,
            entry_ts=candle_ts,
        )

        logger.info(
            "Entry order placed: %s %s %.5f BTC @ %.1f (oid=%d)",
            setup.side, SYMBOL, size_btc, setup.entry_price, oid,
        )

    async def _on_entry_filled(self, pending: PendingEntry, candle_ts: int) -> None:
        """Handle entry order fill — set up position state and exit orders."""
        setup = pending.setup
        equity = pending.equity
        size_btc = setup.size_usd / setup.entry_price

        self._position = PositionState(
            side=setup.side,
            entry_price=setup.entry_price,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            size_usd=setup.size_usd,
            size_btc=size_btc,
            liq_price=setup.liq_price,
            liq_buffer_ratio=setup.liq_buffer_ratio,
            equity_at_entry=equity,
            entry_ts=pending.entry_ts,
            entry_oid=pending.oid,
            signal_context=pending.signal_context,
        )

        # Place stop-loss trigger order on HL
        is_stop_buy = setup.side == "short"  # buy to close short, sell to close long
        result = await asyncio.to_thread(
            self._client.place_trigger_order,
            SYMBOL, is_stop_buy, size_btc, setup.stop_price, tpsl="sl",
        )
        self._position.stop_oid = self._extract_oid(result)

        # Place take-profit limit order
        is_tp_buy = setup.side == "short"
        result = await asyncio.to_thread(
            self._client.place_limit_order,
            SYMBOL, is_tp_buy, size_btc, setup.target_price,
            reduce_only=True, post_only=True,
        )
        self._position.target_oid = self._extract_oid(result)

        logger.info(
            "Position opened: %s %.5f BTC @ %.1f | stop=%.1f target=%.1f",
            setup.side, size_btc, setup.entry_price, setup.stop_price, setup.target_price,
        )

    async def _execute_exit(self, decision: CoreDecision, candle_ts: int) -> None:
        """Execute an exit — cancel existing orders, close position, record trade."""
        if self._position is None:
            return

        ea = decision.exit_action
        pos = self._position

        # Cancel existing exit orders
        for oid in (pos.stop_oid, pos.target_oid):
            if oid:
                try:
                    await asyncio.to_thread(self._client.cancel_order, SYMBOL, oid)
                except Exception as e:
                    logger.warning("Failed to cancel oid=%s: %s", oid, e)

        # Place exit order
        is_close_buy = pos.side == "short"
        if ea.is_maker:
            result = await asyncio.to_thread(
                self._client.place_limit_order,
                SYMBOL, is_close_buy, pos.size_btc, ea.exit_price,
                reduce_only=True, post_only=False,
            )
        else:
            result = await asyncio.to_thread(
                self._client.place_market_order,
                SYMBOL, is_close_buy, pos.size_btc,
                reduce_only=True,
            )

        # Use actual fill price from HL when available (market orders have slippage)
        exit_price = ea.exit_price
        if not PAPER_MODE:
            actual_price = self._extract_fill_price(result)
            if actual_price:
                exit_price = actual_price
                logger.info("Actual fill price: %.1f (theoretical: %.1f)", actual_price, ea.exit_price)

        # Belt-and-suspenders: cancel any remaining orders after exit
        try:
            await asyncio.to_thread(self._client.cancel_all_orders, SYMBOL)
        except Exception as e:
            logger.warning("Post-exit cancel_all failed: %s", e)

        # Calculate PnL
        hold_hours = (candle_ts - pos.entry_ts) / 3_600_000
        pnl_result = calc_trade_pnl(
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usd=pos.size_usd,
            equity=pos.equity_at_entry,
            leverage=self._params.target_leverage,
            is_maker_exit=ea.is_maker,
            hold_hours=hold_hours,
            params=self._params,
        )

        # Record trade
        trade = Trade(
            symbol=SYMBOL,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size_usd=pos.size_usd,
            notional_usd=pos.size_usd,
            leverage=self._params.target_leverage,
            liq_price=pos.liq_price,
            entry_ts=pos.entry_ts,
            exit_ts=candle_ts,
            exit_reason=ea.exit_type,
            hold_candles=pos.hold_candles,
            hold_minutes=hold_hours * 60,
            equity_at_entry=pos.equity_at_entry,
            liq_buffer_ratio=pos.liq_buffer_ratio,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            source=SOURCE,
            **pnl_result,
            **pos.signal_context,
        )
        trade_id = self._store.record_trade(trade)

        # Update daily tracking
        self._daily_pnl += trade.net_pnl
        self._trade_count_today += 1

        # Track max drawdown from daily start equity
        current_equity = await self._get_equity()
        if self._daily_start_equity > 0:
            dd_pct = max(0.0, (self._daily_start_equity - current_equity) / self._daily_start_equity)
            self._daily_max_dd = max(self._daily_max_dd, dd_pct)

        # Persist daily PnL to DB
        self._store.update_daily_pnl(
            date_str=self._current_day,
            symbol=SYMBOL,
            pnl_usd=self._daily_pnl,
            trade_count=self._trade_count_today,
            max_dd_pct=self._daily_max_dd,
            source=SOURCE,
            start_equity=self._daily_start_equity,
            end_equity=current_equity,
            halted=self._halted_today,
            signals_generated=self._signals_today,
            signals_blocked=self._signals_blocked_today,
        )

        # Check daily halt
        should_halt, halt_reason = check_daily_halt(
            self._daily_pnl, CAPITAL_USDC, RISK_PER_TRADE,
            MAX_DAILY_LOSS_MULTIPLIER,
        )
        if should_halt and not self._halted_today:
            self._halted_today = True
            logger.warning("Daily halt: %s", halt_reason)
            await send_alert(format_halt_alert(halt_reason, self._daily_pnl))

        logger.info(
            "Trade closed: %s %s net_pnl=%+.2f (%s) trade_id=%d",
            pos.side, ea.exit_type, trade.net_pnl, f"{trade.equity_return_pct:+.2%}", trade_id,
        )
        await send_alert(format_trade_alert(trade))

        # Reset position
        self._position = None

    async def _emergency_close(self, reason: str) -> None:
        """Emergency close — market order, no PnL calc."""
        if self._position is None:
            return

        pos = self._position
        is_close_buy = pos.side == "short"

        await asyncio.to_thread(self._client.cancel_all_orders, SYMBOL)
        await asyncio.to_thread(
            self._client.place_market_order,
            SYMBOL, is_close_buy, pos.size_btc, reduce_only=True,
        )

        logger.warning("Emergency close: %s (reason=%s)", pos.side, reason)
        await send_alert(f"*EMERGENCY CLOSE*\nSide: `{pos.side}`\nReason: `{reason}`")
        self._position = None

    async def _on_paper_fill(self, fill: dict, candle_ts: int) -> None:
        """Handle a paper mode limit order fill."""
        if self._pending_entry and fill["oid"] == self._pending_entry.oid:
            await self._on_entry_filled(self._pending_entry, candle_ts)
            self._pending_entry = None

    async def _get_equity(self) -> float:
        """Get current equity."""
        if PAPER_MODE:
            return self._client.get_balance()
        return await asyncio.to_thread(self._client.get_balance)

    async def _heartbeat(self, equity: float) -> None:
        """Periodic status log + Telegram heartbeat."""
        pos_str = "none"
        if self._position:
            pos_str = f"{self._position.side} {self._position.size_btc:.5f} BTC @ {self._position.entry_price:.1f} (hold={self._position.hold_candles})"

        logger.info(
            "Heartbeat: equity=%.2f daily_pnl=%+.2f halted=%s position=%s candles=%d σ=%.2f adx=%.1f",
            equity, self._daily_pnl, self._halted_today, pos_str, self._candle_count,
            self.status.sigma_dist, self.status.adx,
        )

        from notifications.telegram import format_status
        await send_alert(format_status(self.status, self._position))

    async def _sanity_check(self) -> None:
        """Compare local position state with HL."""
        hl_pos = await asyncio.to_thread(self._client.get_position, SYMBOL)
        local_has_pos = self._position is not None
        hl_has_pos = hl_pos is not None

        if local_has_pos != hl_has_pos:
            msg = f"Position mismatch! Local: {self._position}, HL: {hl_pos}"
            logger.error(msg)
            await send_alert(format_error_alert(msg))

    async def _graceful_shutdown(self) -> None:
        """Handle SIGINT/SIGTERM — cancel orders, set shutdown flag."""
        logger.info("Graceful shutdown initiated")
        self._shutdown = True
        try:
            await asyncio.to_thread(self._client.cancel_all_orders, SYMBOL)
        except Exception as e:
            logger.warning("Cancel orders on shutdown failed: %s", e)

        # Best-effort alert with 3s timeout — don't block shutdown
        try:
            if self._position and not PAPER_MODE:
                logger.info("Open position left on exchange — NOT auto-closing on shutdown")
                await asyncio.wait_for(send_alert(
                    f"*VRAB Shutting Down*\n"
                    f"Open position: `{self._position.side}` {self._position.size_btc:.5f} BTC\n"
                    f"Stop order should remain on HL"
                ), timeout=3)
            else:
                await asyncio.wait_for(send_alert("*VRAB Stopped*"), timeout=3)
        except (asyncio.TimeoutError, Exception):
            pass

    @staticmethod
    def _extract_oid(result: dict) -> int | None:
        """Extract order ID from SDK response."""
        try:
            resp = result.get("response", {})
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            statuses = data.get("statuses", [])
            if statuses:
                first = statuses[0]
                if "resting" in first:
                    return first["resting"]["oid"]
                if "filled" in first:
                    return first["filled"]["oid"]
        except (KeyError, IndexError, TypeError):
            pass
        logger.warning("Could not extract oid from: %s", result)
        return None

    @staticmethod
    def _extract_fill_price(result: dict) -> float | None:
        """Extract average fill price from SDK response."""
        try:
            resp = result.get("response", {})
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            statuses = data.get("statuses", [])
            if statuses:
                first = statuses[0]
                if "filled" in first:
                    return float(first["filled"]["avgPx"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        return None


async def main():
    """CLI entry point."""
    from logging_config import setup_logging
    setup_logging()

    params = build_params_from_config()
    store = Store(DB_PATH)

    if PAPER_MODE:
        from live.paper import PaperClient
        client = PaperClient(CAPITAL_USDC)
    else:
        from live.hl_client import HLClient
        client = HLClient(HL_PRIVATE_KEY, HL_BASE_URL, HL_WALLET_ADDRESS)

    engine = LiveEngine(client, store, params)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
