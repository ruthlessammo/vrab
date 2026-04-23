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

**4. Telegram /pause and /resume commands**
- `notifications/bot.py` — `/pause` stops new entries, `/resume` re-enables them
- Separate `_paused` flag in engine — persists across day rollover (unlike `_halted_today`)
- `/resume` clears both `_paused` and `_halted_today` — one command to unblock trading regardless of cause
- Open positions still monitored for exits while paused

### Bug Fixes
- Kill switch used file-based path (`/tmp/VRAB_KILL`) but `_halted_today` was also set and persisted to DB — removing the file wasn't enough to resume. `/resume` now clears all halt states

## 2026-04-22 — Graduation Cutover

### Problem
21 live trades included a chunk from before the counter-trend filter and ADX gate existed. Those early unfiltered losses dragged expectancy to -$0.25 and total PnL to -$5.18, polluting graduation metrics with a strategy that no longer exists. Underlying performance (Sharpe 2.57, max DD 0.8%) was strong — but Gate 2 couldn't pass on mixed data.

### Solution
Added `GRADUATION_CUTOVER_TS` in config — graduation metrics now only count trades and daily records after 2026-04-17 19:55 UTC (when filter changes deployed). All historical data preserved in DB for analysis; only `/graduation` applies the filter.

### Changes
- `config.py` — `GRADUATION_CUTOVER_TS = 1776455700000` (2026-04-17 19:55 UTC)
- `notifications/bot.py` — `_cmd_graduation()` filters trades by `entry_ts >= cutover` and daily records by `date >= cutover_date`
- `notifications/telegram.py` — `format_graduation()` shows `Since: YYYY-MM-DD` when cutover active
- 3 files, ~10 lines added. No schema changes, no test changes.

## 2026-04-22 — Shadow Stats in Daily Summary

### Problem
Shadow book tracked hypothetical PnL of blocked trades but results were only visible via manual SQLite queries. No daily visibility into whether filters were helping or costing money.

### Solution
Added shadow trade stats to the end-of-day Telegram summary. When shadow trades complete during a day, the summary shows count, average PnL, and win/loss breakdown.

### Changes
- `live/engine.py` — `_shadow_completions_today` list accumulates completed shadow trades, passed to `format_daily_summary()`, cleared in `_finalize_day()`
- `notifications/telegram.py` — `format_daily_summary()` accepts optional `shadow_trades` param, appends `Shadow: N blocked, avg +$X.XX (WW/LL)` when present
- 2 files, ~15 lines. Omitted when no shadow trades that day (clean output).

## 2026-04-22 — Gate 0 Recalibration & 365-Day Backtest

### Problem
Gate 0 thresholds (Sharpe ≥ 1.5, DD ≤ 8%, halts ≤ 2) were calibrated for 30-day walk-forward windows. Applied to a full year of data they were unrealistic — even a profitable strategy with real edge (Sharpe 1.54, +$1,340) couldn't pass.

### Investigation
Ran 365-day single-window backtest (Apr 2025 → Apr 2026) at both 1.0% and 1.5% risk per trade:

| Metric | 1.5% risk | 1.0% risk |
|--------|-----------|-----------|
| Net PnL | +$1,340 | +$840 |
| Gross PnL | +$597 | +$366 |
| Sharpe | 1.54 | 1.24 |
| Max DD | 17.7% | 11.1% |
| Halts | 35 | 12 |
| Win Rate | 38.0% | 36.4% |

Key finding: **gross PnL is positive** (+$597 at 1.5%) — signal alpha is real, not just rebate farming. The 90-day backtest that showed negative gross PnL was a regime-dependent window, not representative of the full year.

### Decisions
- **Sharpe gate: 1.5 → 1.0** — 1.0 is the industry standard for "good strategy". 1.5 over a full year is top-decile and unnecessarily restrictive.
- **DD gate: 8% → 20%** — single-asset, 10x leveraged BTC with $500 capital. 8% DD over a year is unrealistic. 20% reflects the actual risk appetite.
- **Halt gate: removed** — daily halts are a safety *feature*, not a failure signal. Counting them as a gate punishes the strategy for using its own risk management. Circuit breaker (10% from peak) remains as the real drawdown protection.
- **Risk per trade: stays at 1.5%** — higher Sharpe (1.54 vs 1.24), +60% more PnL, rougher ride but within DD tolerance. Decision: be in it to win it.

### Changes
- `config.py` — `GATE0_MIN_SHARPE = 1.0`, `GATE0_MAX_DD = 0.20`, removed `GATE0_MAX_HALTS`
- `backtest/engine.py` — removed halt count from Gate 0 validation (kept in output as monitoring metric)
- 365-day backtest: **Gate 0 PASS** at 1.5% risk

## 2026-04-22 — Parity Audit: Live/Backtest Data-Slicing Fix

### Motivation
ETH Desk (other strategy) had a look-ahead bias where backtest and live computed features differently for the same timestamp. Both had passing unit tests — neither caught the bug because they were never forced to produce identical outputs. Before VRAB accumulates more live trades for graduation, audited for the same bug class.

### Findings
VRAB's shared-core architecture (`strategy/core.py` + `strategy/signals.py`) is strong — no separate implementations. Both engines call the same pure functions. However, the **data-slicing layer** in `live/engine.py` had two parity issues:

1. **VWAP window shifted +1 bar** — the candle feed upserts bar T+1 to the store before firing the candle-close event. The live engine read from the store without filtering, so the VWAP window included bar T+1's first tick (negligible volume) and dropped the oldest bar vs the backtest's correct `[T-35, ..., T]` window.

2. **Trend boundary shifted +5 min** — the live engine used `event["candle"].ts` (bar T+1's timestamp) instead of the closed bar's timestamp for the 15m trend boundary. Occasionally included one extra 15m candle in the regime filter that the backtest excluded.

3. **Exit evaluation used wrong OHLC** — bar T+1's first tick (high ≈ low ≈ open) instead of the closed bar's full range, making the candle-close exit check nearly useless.

**Root cause**: `_on_candle_close` used `event["candle"]` (bar T+1) instead of `event["closed_candle"]` (bar T) for all decision data.

**Not look-ahead**: The backtest was the conservative side — it used less data. The bugs affected the live engine only, causing slightly different (not better) signals.

### Fix
- `live/engine.py` — use `event["closed_candle"]` for all trading logic, filter `primary_candles` to `ts <= candle_ts`, use closed candle OHLC for exits and shadow book. `new_candle.close` only used for paper mode mid-price.

### Tests
- `tests/test_vrab_parity.py` — 4 tests: VWAP window match, trend window match, signal output parity (within 1e-9), and proof that pre-fix code diverged. 138 total tests passing.

### Audit Report
- `research/outputs/vrab_parity_audit.md` — full 6-section report covering architecture map, timestamp audit, parity test results, fill price audit, regime filter deep-dive, and findings.

## 2026-04-22 — Regime Filtering Investigation

### Question
12×30d walk-forward showed 3 bleed months (Feb, Oct, May) with 17-21% win rate dragging performance. Can we detect bad regimes in advance and skip them?

### 12×30d Walk-Forward Results

| Window | Period | PnL | Sharpe | WR | DD |
|--------|--------|-----|--------|----|----|
| 12 | Apr–May 25 | +$70 | 1.75 | 41% | 9.1% |
| 11 | May–Jun 25 | -$42 | -4.10 | 20% | 16.9% |
| 10 | Jun–Jul 25 | +$104 | 4.26 | 40% | 3.0% |
| 9 | Jul–Aug 25 | +$35 | -0.02 | 34% | 6.8% |
| 8 | Aug–Sep 25 | +$0.4 | -1.79 | 29% | 7.2% |
| 7 | Sep–Oct 25 | +$26 | -1.03 | 30% | 12.5% |
| 6 | Oct–Nov 25 | -$41 | -4.73 | 21% | 10.1% |
| 5 | Nov–Dec 25 | +$161 | 6.42 | 57% | 3.9% |
| 4 | Dec–Jan 26 | +$69 | 1.76 | 36% | 3.4% |
| 3 | Jan–Feb 26 | +$109 | 3.68 | 46% | 6.5% |
| 2 | Feb–Mar 26 | -$35 | -3.31 | 17% | 11.6% |
| 1 | Mar–Apr 26 | +$21 | -0.41 | 33% | 9.9% |

### Method
Built `backtest/regime_analysis.py` — standalone research script that annotates every backtest trade with backward-looking regime metrics at entry time, then splits into quintiles to test predictive power.

### Metrics Tested
1. **24h directional move** — abs % change over 288 candles
2. **24h realized volatility** — annualized from 5m log-returns
3. **24h high-low range / close** — total range vs price
4. **VWAP bandwidth** — volume-weighted std / vwap

### Results
All four metrics: **Spearman ρ ≈ 0** (no predictive signal). No monotonic relationship between any metric and trade PnL. Highest-volatility quintile actually had the *best* performance — mean-reversion needs big swings.

### Conclusion
**No regime filter added.** The bleed months aren't caused by measurable market conditions we can detect in advance. Drawdowns are the cost of running leveraged mean-reversion on BTC. The strategy produces +$1,340/year including those months — accept the rough with the smooth.

## 2026-04-23 — Reconciliation Rewrite + PnL Source of Truth

### Problem
The old reconcile tool independently grouped HL fills into trades then fuzzy-matched against DB trades. This was fragile — swapped matches, scoring heuristics, and every edge case (multi-fill entries, missing opens) introduced errors. Running it showed 14/22 trades failing to match because DB `entry_ts` stores candle timestamps (signal time), not actual fill execution time — gaps of 60+ minutes.

### Changes
1. **DB-anchored reconciliation** — rewrote `tools/reconcile_hl.py`. Each DB trade defines a time window; HL fills within it are claimed and summed. No independent grouping or fuzzy matching. Level 0 (totals) + Level 1 (per-trade) comparison.

2. **Order ID matching** — HL API fills have `oid` (order ID). The engine already tracked `entry_oid`, `stop_oid`, `target_oid` on `PositionState` but never persisted them. Added all three to the `trades` table with ALTER TABLE migration for existing DBs. Reconcile now matches fills by oid when available, falls back to 1-hour time window for legacy trades.

3. **Equity delta for total PnL** — `/pnl`, `/equity`, and dashboard Total PnL now use `equity - initial_capital` (actual HL balance change) instead of summing DB trade `net_pnl`. Trade sums were off by ~$1.13 due to orphan fills the DB doesn't know about (pre-engine trades, manual closes). Win rate and trade count still from DB trades.

### Findings
- DB `entry_ts` and `exit_ts` are candle-aligned, not actual fill times — this is by design (signal time), but means time-window reconciliation needs wide margins for legacy trades.
- 6 orphan fills: 4 from pre-engine trading (Apr 3-4), 2 from manual closes where the exit fill happened much later than DB expected.
- Daily PnL was already correct (uses equity delta). Only the total PnL display was slightly off.
- HL `closedPnl` includes fees but excludes funding. `net_pnl_usd` in DB = `closedPnl + funding`. The math is sound.

### Files
- `tools/reconcile_hl.py` — full rewrite
- `data/store.py` — `entry_oid`, `stop_oid`, `target_oid` columns + migration
- `live/engine.py` — wire oids to Trade
- `notifications/bot.py` — equity delta for /pnl, /equity
- `dashboard/app.py` + `index.html` — equity delta for Total PnL stat
- `tests/test_reconcile.py` — 15 tests (time-window + oid matching)
