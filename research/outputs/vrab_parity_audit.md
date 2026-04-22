# VRAB Parity Audit: Look-Ahead Bias Check

**Date**: 2026-04-22
**Auditor**: Claude (automated code audit)
**Scope**: Signal/feature computation paths in backtest vs live
**Status**: COMPLETE — issues found and fixed

---

## 1. Architecture Map

### 1.1 Single code path: the strong position

VRAB uses a **shared-core architecture**. All signal generation, risk checks, sizing, and
trade evaluation run through `strategy/core.py`, which imports pure functions from
`strategy/signals.py`. Neither module has I/O or config imports (config is injected via
`TradingParams`).

| Component | Backtest (`backtest/engine.py`) | Live (`live/engine.py`) |
|---|---|---|
| Entry decision | `evaluate_entry()` from `strategy.core` | Same function |
| Exit decision | `evaluate_exit()` from `strategy.core` | Same function |
| Signal generation | `generate_signal()` from `strategy.signals` | Same function |
| VWAP | `calc_vwap()` from `strategy.signals` | Same function |
| Regime (ADX/EMA) | `calc_regime()` from `strategy.signals` | Same function |
| Position sizing | `calc_position_size()` from `strategy.core` | Same function |
| PnL calculation | `calc_trade_pnl()` from `strategy.core` | Same function |

**There are no separate implementations.** Both engines are thin adapters that feed data
to the same pure functions. This eliminates the ETH Desk bug class (two implementations
drifting).

### 1.2 Findings: data-slicing divergence (now fixed)

The attack surface was in how each engine **slices data** before calling shared functions.
Two issues found, both traced to a single root cause:

**Root cause**: The live feed fires `candle_close` when bar T+1's first tick arrives, and
the engine was using `event["candle"]` (bar T+1) instead of `event["closed_candle"]`
(bar T) for decision data.

**Finding 1 — VWAP window shifted +1 bar**: The store contained bar T+1 (upserted by
feed before event fires), so the live VWAP window was `[T-vwap_win+2, ..., T, T+1]`
instead of the backtest's `[T-vwap_win+1, ..., T]`. Bar T+1 had negligible volume (one
tick), but the window was structurally wrong.

**Finding 2 — Trend boundary shifted +5 min**: Using T+1's timestamp shifted the 15m
trend boundary forward by 5 minutes, occasionally including one extra 15m candle that
the backtest excluded.

**Fix applied** (`live/engine.py:_on_candle_close`):
- Extract `closed_candle` from event; use its timestamp for all trading logic
- Filter `primary_candles` to `ts <= candle_ts` to exclude bar T+1
- Use `closed_candle` OHLC for exit evaluation and shadow book
- Keep `new_candle.close` only for paper mode mid-price (latest price, not decision data)

---

## 2. Timestamp and Data-Slicing Audit

For each feature, what data window does each engine use at decision time T?

### VWAP (`calc_vwap`)

| | Window | Includes bar T? | Includes bar T+1? |
|---|---|---|---|
| **Backtest** | `all_closes[T-vwap_win+1 : T+1]` | Yes (T is fully closed) | No |
| **Live (post-fix)** | `[c for c in store if c.ts <= T][-vwap_win:]` | Yes | No |
| **Live (pre-fix)** | `store[-vwap_win:]` (store has T+1) | Yes | **Yes (1 tick)** |

Post-fix: **MATCH**. Both use exactly `[T-vwap_win+1, ..., T]`.

### sigma_distance

Computed as `(closes[-1] - vwap) / std_dev`. Inherits VWAP window.

| | `price` (= `closes[-1]`) |
|---|---|
| **Backtest** | Bar T close |
| **Live (post-fix)** | Bar T close (last candle in filtered window) |
| **Live (pre-fix)** | Bar T+1 open ≈ T close (structurally wrong, numerically close) |

Post-fix: **MATCH**.

### Regime filter (`calc_regime` → `calc_adx`, `calc_ema`)

| | Trend boundary | Window |
|---|---|---|
| **Backtest** | `T_ts - 900_000` | 15m candles with `ts <= T_ts - 900_000` |
| **Live (post-fix)** | `T_ts - 900_000` (uses closed bar's ts) | Same |
| **Live (pre-fix)** | `(T_ts + 300_000) - 900_000` = `T_ts - 600_000` | Could include 1 extra 15m candle |

Post-fix: **MATCH**.

### Entry/stop/target prices

Derived from VWAP and sigma. Same functions, same inputs post-fix: **MATCH**.

### Exit evaluation

Takes scalar OHLC (no rolling window). Post-fix uses `closed_candle.high/low/close`:
**MATCH** with backtest's `candle["high"]/["low"]/["close"]` at bar i.

### Warmup / NaN handling

Both engines skip bars when `len(closes) < vwap_window`. The backtest loop starts at
`range(vwap_win, ...)`. The live engine checks
`len(primary_candles) < vwap_window + 5`. `generate_signal` returns `"none"` with
`block="insufficient_data"` if fewer than `vwap_window` candles: **MATCH**.

---

## 3. Parity Test

Test file: `tests/test_vrab_parity.py` — 4 tests, all passing.

### Test design

The test constructs 136+ bars of synthetic candle data with sine-wave price variation
(non-trivial VWAP and sigma) and 200 trend candles. For each bar from `vwap_window`
onward, it:

1. Builds the **backtest slice** (replicating `simulate_window`'s exact indexing)
2. Builds the **live slice** (replicating `_on_candle_close`'s post-fix filtering, with
   bar T+1 present in the store)
3. Asserts the two slices are identical (closes, highs, lows, volumes, trend data)
4. Feeds both slices to `generate_signal()` and compares every output field within 1e-9

### Results

| Test | Result |
|---|---|
| `test_vwap_window_matches` | **PASS** — all bars produce identical VWAP windows |
| `test_trend_window_matches` | **PASS** — all bars produce identical trend slices |
| `test_signal_output_parity` | **PASS** — every signal field matches within 1e-9 |
| `test_old_slicing_diverges` | **PASS** — confirms pre-fix code DID diverge |

---

## 4. Fill Price Audit

### Backtest entry fill

In `generate_signal`, `price = closes[-1]` (bar T close). This becomes
`TradeSetup.entry_price`. The backtest records entry at this price. The cost model then
applies slippage via `calc_fill_price` — for a long entry:
`fill = entry_price + slippage_ticks * tick_size` (SLIPPAGE_TICKS_ENTRY=1, TICK_SIZE=1.0,
so +$1 adverse slippage on a ~$87K price = ~0.001%).

### Live entry fill

The live engine places a **maker limit order** at `setup.entry_price` (same price the
backtest uses). A maker limit order fills at the limit price or better. The backtest's
+$1 slippage assumption is therefore **conservative** — live fills can match or beat the
backtest price.

### Backtest exit fill

- **Stop**: `calc_fill_price` with SLIPPAGE_TICKS_STOP=3 → +$3 adverse. Conservative.
- **Target**: `calc_fill_price` with SLIPPAGE_TICKS_ENTRY=1 → +$1 adverse. Then a
  `BACKTEST_FILL_RATE=0.70` probability filter — 30% of maker exits don't fill.
- **Timeout**: Uses close + taker slippage.

### Live exit fill

- **Stop**: HL trigger order at stop price → real market fill (usually close to stop).
- **Target**: Maker limit at target → fills at target or better.
- **Timeout**: Market close.

### Verdict

The backtest fill model is **conservative**:
- Entry: backtest adds +$1 slippage; live gets maker fills at or better
- Target: backtest applies 30% miss rate; live gets all maker fills
- Stop: backtest adds +$3 slippage; live gets market fills (variable but similar)

**No fill-price look-ahead detected.** Backtest fills are realistic or pessimistic.

---

## 5. Regime Filter Deep-Dive

### What it consumes

`calc_regime()` (`strategy/signals.py:192-219`) takes:
- `trend_closes`, `trend_highs`, `trend_lows` — 15-minute candles
- Parameters: `ema_period=15`, `adx_period=14`, `adx_threshold=35.0`

It computes:
- **ADX** via `calc_adx(highs, lows, closes, period)` — Wilder's ADX
- **EMA** via `calc_ema(closes, period)` — exponential moving average of trend closes
- **is_trending**: `adx >= adx_threshold`
- **trend_direction**: "up" if `close > ema * 1.001`, "down" if `< ema * 0.999`, else "flat"

### Window and current-bar inclusion

The trend candles passed to `calc_regime` are selected via:
```python
trend_boundary = candle_ts - 900_000
trend_idx = bisect.bisect_right(trend_ts_arr, trend_boundary)
```

This includes 15m candles with `ts <= candle_ts - 900_000`. Since a 15m candle with
timestamp `ts` covers `[ts, ts+900_000)`, any included candle has fully closed by time
`ts + 900_000 <= candle_ts`. **The window does NOT include the current (in-progress) 15m
candle.** Only completed candles feed the regime filter.

`calc_adx` and `calc_ema` are standard implementations that process the array in order,
using only past values. No current-bar leakage.

### Batch vs live parity

Post-fix: both engines use `closed_candle.ts` for `candle_ts`, producing identical trend
boundaries. The parity test at `tests/test_vrab_parity.py::test_trend_window_matches`
confirms all bars produce identical trend slices.

Pre-fix: the live used `T+1`'s timestamp, shifting the boundary +5 min. This occasionally
included one extra 15m candle. **Now fixed.**

---

## 6. Findings and Recommendations

### Verdict: PARTIAL → fixed to PASS

**Before fix**: Two data-slicing parity issues in `live/engine.py:_on_candle_close`:
1. VWAP window included bar T+1 (1 tick of next bar) — structural divergence
2. Trend boundary shifted +5 min — occasional extra 15m candle

**After fix**: All features produce identical outputs between backtest and live. Parity
test confirms matching within 1e-9 tolerance across 100 synthetic bars.

### No look-ahead bias in backtest

Neither finding inflated backtest returns. The backtest was actually *more conservative*
(used less data at each decision point). The bugs only affected the live engine, causing
it to use slightly different (not better) data than the backtest.

### Fill prices are realistic

The backtest cost model applies maker/taker fees, slippage, funding costs, and a 30%
fill miss rate for maker exits. Live fills via maker limit orders are typically at or
better than backtest assumptions. No fill-price look-ahead detected.

### Remaining notes

- **Exit evaluation timing**: Pre-fix, the live exit check used bar T+1's first tick
  OHLC (high ≈ low ≈ open) instead of the closed bar's full range. This made the
  candle-close exit check nearly useless. The fix corrects this to use `closed_candle`
  OHLC, matching the backtest. In practice, paper fills were already caught by the
  tick-based fill check and belt-and-suspenders section; live fills were handled by HL
  exchange orders. So this was a correctness issue, not a missed-exit bug.

- **`backtest/regime_analysis.py:42`** uses `candles[idx - LOOKBACK_5M : idx + 1]` which
  includes the current bar. This is a research tool (not the trading pipeline) and its
  regime metrics are backward-looking aggregates over 24h, so including the current bar
  is intentional and has negligible impact. Not a bug.

### Changes made

| File | Change |
|---|---|
| `live/engine.py` | Fixed `_on_candle_close` to use `closed_candle` for all decision data |
| `tests/test_vrab_parity.py` | New: 4 parity tests (regression guard) |
| `research/outputs/vrab_parity_audit.md` | This report |
