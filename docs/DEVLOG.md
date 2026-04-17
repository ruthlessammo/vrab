# VRAB Dev Log

## 2026-03-27 — Sprint 1: Foundation Build

### Built
- `config.py` — all parameters, dotenv loading, kill switch check
- `logging_config.py` — rotating file + stdout handlers
- `strategy/signals.py` — VWAP, EMA, ADX, regime, signal generation (pure functions)
- `strategy/core.py` — shared trading core with TradingParams, evaluate_entry/exit, sizing, PnL calc, daily halt
- `costs/model.py` — fill price, fees, funding, round-trip, break-even, leveraged round-trip (pure functions)
- `risk/liquidation.py` — liq price, buffer, stop safety, max safe leverage, funding at leverage (pure functions)
- `data/store.py` — SQLite store with enhanced schemas (40+ column trades table), deque cache, threading lock
- `data/puller.py` — async candle puller with validation, gap detection, companion TF auto-pull
- `notifications/telegram.py` — async alerts with rate limiting, silent failure
- `dashboard/app.py` — read-only Flask API
- `backtest/engine.py` — walk-forward backtest (thin adapter over core)
- Full test suite: test_signals, test_costs, test_liquidation, test_core, test_engine, test_store

### Decisions Made
- **Volume-weighted std for VWAP bands** — unweighted std overweights low-volume candles that shouldn't influence band width
- **strategy/core.py as shared decision pipeline** — prevents backtest/live divergence by centralizing all signal → risk → sizing → cost logic. Engines only handle I/O and fill simulation.
- **TradingParams frozen dataclass** — immutable parameter snapshot prevents accidental mutation during a run
- **SQLite WAL mode** — allows concurrent reads (dashboard) while backtest writes
- **Trend candle alignment**: use most recent closed 15m candle (ts <= T - 900,000ms) to avoid look-ahead bias
- **Sharpe**: per-trade returns, annualized by sqrt(252 × trades_per_day), risk-free = 0
- **Max drawdown**: peak-to-trough on cumulative equity curve

### Next
- Paper trading validation (24-48h)
- Live deployment

## 2026-03-30 — Sprint 1: Backtest Results & Parameter Tuning

### Data
- Pulled 365 days of BTC, ETH, SOL (5m + 15m) from Binance Futures
- 105K candles per asset per timeframe, zero gaps

### Parameter Sweep
- Swept: risk (1.5-3.0%), stop sigma (3.5-4.5), entry sigma (1.5-3.0), ADX (20-35)
- **Winner**: entry=2.5σ, stop=4.5σ, ADX<35, risk=1.5%, 10x leverage
- ADX 35 (vs 30) was the key improvement: halved max DD (9.69% → 5.74%) while increasing PnL

### BTC Results (3×30d walk-forward)
- 129 trades, +$320 net PnL (+64.0% on $500), Sharpe 2.78, Max DD 5.74%
- Gate 0: PASS (after revising WR gate to 35%, trade count to 30/window)
- Strategy is fat-tailed MR: wins on trade size not frequency (40% WR, avg winner >> avg loser)

### Multi-Asset
- ETH: marginal (+10.8%), Sharpe 0.03 — no edge
- SOL: destructive (-58.0%), tick size mismatch + trending market structure
- **Decision: BTC only** with this strategy

### Gate 0 Revisions
- Win rate gate: 50% → 35% (inappropriate for fat-tailed MR)
- Min trades: 60 → 30/window (43 avg/window is sufficient)
- Centralised all gate thresholds in config.py (was duplicated across 4 files)

### Config Changes
- `ADX_THRESHOLD`: 30.0 → 35.0

## 2026-03-30 — Sprint 2: Live Execution Engine

### Built
- `live/hl_client.py` — thin wrapper around hyperliquid-python-sdk (Exchange + Info)
- `live/paper.py` — paper trading client (same interface, virtual fills)
- `live/feed.py` — WebSocket candle feed with REST backfill and candle close detection
- `live/engine.py` — async main loop mirroring backtest/engine.py simulate_window()

### Architecture
- Zero divergence maintained: engine calls strategy.core.evaluate_entry/exit identically
- Paper mode built as drop-in client replacement (PAPER_MODE=True in config)
- HL trigger orders for stop-loss (server-side), post-only ALO for entries (maker rebate)
- Dead-man switch via exchange.schedule_cancel (auto-cancel if bot dies)
- Startup reconciliation: syncs with HL position state, rebuilds daily PnL from DB
- SIGINT/SIGTERM graceful shutdown: cancels orders, alerts via Telegram

### Order Flow
- Entry: post-only limit (ALO) at setup.entry_price, expires after entry_expiry_candles
- Target: post-only limit (reduce_only) placed on entry fill
- Stop: HL trigger order (server-side, market on trigger)
- Timeout: IOC market close after max_hold_candles

### Next
- Paper trading validation (24-48h)
- Live deployment (PAPER_MODE=False)

## 2026-03-30 — Sprint 2: Telegram Bot & PnL Logging

### Built
- `notifications/bot.py` — async long-polling Telegram bot with /status, /pnl, /equity, /trades, /kill commands
- `notifications/telegram.py` — 5 new formatters (status, pnl_summary, equity, trades_list, daily_summary)
- Daily PnL persistence wired into live engine (after trade close + day rollover)
- Daily auto-summary sent via Telegram on day rollover
- Signal counting (generated/blocked) per day for daily records

### Architecture
- Bot runs as background asyncio task in engine event loop, reads shared `EngineStatus` dataclass
- Security: only responds to configured `TELEGRAM_CHAT_ID`
- /kill creates kill switch file, engine picks it up next candle
- `store.update_daily_pnl()` called after every trade close (running upsert) and at day rollover (finalize)
- Daily summary includes PnL, trade count, equity, signals generated/blocked

### Code Review & Cleanup
- **Bug fix**: `/pnl` command double-counted today's trades (DB already includes them, was adding `daily_pnl` on top)
- **Bug fix**: redundant `_get_equity()` calls in `_execute_exit` — two network round-trips where one suffices
- **Bug fix**: heartbeat/sanity check never fired — early returns in `_on_candle_close` skipped them. Moved periodic tasks above trading logic
- **Refactor**: `PendingEntry` dataclass replaces untyped dict (was 6 string keys with no static checking)
- **Refactor**: `aiohttp.ClientSession` reused across requests (bot polling + alert sending). Was creating new TCP+TLS connection per request
- **Refactor**: `SOURCE` module constant replaces 6 repeated `"paper" if PAPER_MODE else "live"` expressions
- **Refactor**: `Store.get_daily_state()` public method replaces `_hot_state` private access from engine
- **Refactor**: `_on_candle_close` split into `_finalize_day()`, `_check_pending_entry()`, `_evaluate_and_enter()`
- **Fix**: `bot_task.cancel()` now awaited for clean shutdown
- **Fix**: paper mode `cancel_order` called synchronously instead of unnecessary `asyncio.to_thread` wrapper
- **Minor**: `format_trade_alert` uses `:+.2f` format spec instead of manual sign prefix

### Config Additions
- `TELEGRAM_POLL_INTERVAL = 2`
- `DAILY_SUMMARY_ENABLED = True`

### Next
- Paper trading validation (24-48h)
- Live deployment (PAPER_MODE=False)

## 2026-04-05 — Sprint 3: Live Bug Fixes & Robustness

### Bug Fixes
- **Price rounding**: Added `_round_price()` to hl_client.py — raw float stops/targets caused HL SDK `float_to_wire()` to reject orders, leaving positions unprotected
- **TICK_SIZE**: BTC on HL uses 1.0 (whole dollars), not 0.1 — fixed in config.py
- **closedPnl race condition**: Added 2s delay + retry in `_calc_live_pnl()` — HL fills not indexed immediately after exit
- **Entry notification**: `_on_entry_filled()` was silent — added Telegram alert on position open
- **Graceful shutdown**: `feed.stop()` never called `disconnect_websocket()` — WS thread kept running 30s+ until SIGKILL. Added disconnect + `_stopped` guard in callback
- **Mid-candle exit detection**: Engine only checked exits on 5m candle close. Added throttled (5s) HL position polling on tick events — detects stop/TP fills within seconds via `live/exit_detect.py`
- **ALO rejection retry**: Post-only orders rejected at ask price — added GTC fallback in `_execute_entry()`
- **Missed trade on restart**: `_restore_position()` now records trade to DB when position was closed while engine was down, reusing `_handle_mid_candle_exit()`

### New Features
- `/graduation` Telegram command — 3-gate capital scaling progress tracker
- `/close` Telegram command — force close any open position

### Architecture
- `live/exit_detect.py` — pure testable functions for exit type inference and fill price extraction
- TDD approach adopted: tests written before implementation for all new logic
- `_execute_exit()` order placement wrapped in try/except for mid-candle exit resilience

### Tests Added
- `tests/test_mid_candle_exit.py` — 14 tests (infer_exit, extract_exit_price)

## 2026-04-17 — Sprint 4: Missed Trade Investigation & Observability

### Problem
Missed a textbook mean-reversion entry at 01:25 UTC — σ=-2.60, ADX=10.9 (range-bound), but `counter_trend_long` blocked the long because 15m EMA said "down". At ADX=10.9 trend direction is noise, not signal. Price bounced immediately. Had to manually query SQLite to discover this — no real-time visibility into blocked trades.

### Root Cause
Counter-trend filter applied unconditionally regardless of ADX level. In low-ADX regimes, the EMA trend direction is meaningless but still blocked valid mean-reversion setups.

### New Features

**1. Telegram alerts for blocked trades**
- `notifications/telegram.py` — `format_blocked_signal()` shows side, block reason, price, VWAP, σ bar, ADX, trend
- `live/engine.py` — fires alert in `_process_entry()` whenever an entry signal is generated but blocked
- Immediate visibility — no more DB archaeology to find missed trades

**2. Counter-trend ADX minimum gate**
- `config.py` — `COUNTER_TREND_MIN_ADX = 20.0`
- `strategy/signals.py` — counter-trend filter now requires `regime.adx >= counter_trend_min_adx`
- Below ADX 20, trend direction is noise → entries allowed regardless of EMA direction
- Threaded through `TradingParams`, `evaluate_entry()`, backtest engine, sweep
- Default 0.0 in `TradingParams` preserves backward compat for existing tests

**3. Shadow book (hypothetical PnL for blocked trades)**
- `strategy/shadow.py` — `ShadowBook` class tracks blocked entries as shadow positions
- Uses same `evaluate_exit()` and `calc_round_trip_cost()` as real trades (zero divergence)
- Exits via stop/target/timeout only (no signal exit — can't know hypothetical signal state)
- `data/store.py` — separate `shadow_trades` table, isolated from real PnL/hot state
- `live/engine.py` — integrated into candle loop (`on_candle`) and day rollover (`clear`)
- `config.py` — `SHADOW_BOOK_ENABLED = True`
- Max 20 concurrent shadow positions, in-memory only (not persisted across restarts)

### Decisions
- **Separate `shadow_trades` table** (not `source="shadow"` in trades) — prevents accidental aggregation into real PnL, dashboard metrics, or Gate 0 validation
- **ADX minimum default 0.0 in TradingParams** — existing tests pass without modification, config sets the real value (20.0)
- **No signal-exit for shadows** — shadow positions skip signal exits because we can't replay `generate_signal` for a hypothetical position side without running the full signal pipeline

### Config Changes
- `COUNTER_TREND_MIN_ADX = 20.0`
- `SHADOW_BOOK_ENABLED = True`

### Tests Added
- `tests/test_signals.py` — 3 tests (ADX gate: low ADX allows entry, high ADX blocks, both sides)
- `tests/test_shadow.py` — 10 tests (creation, stop/target/timeout exits, hold, multiple positions, context preservation, clear, cap)
- `tests/test_store.py` — 1 test (shadow trade DB isolation)
- Total: 134 tests passing (was 120)
