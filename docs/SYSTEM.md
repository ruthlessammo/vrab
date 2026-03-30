# VRAB System Documentation

## Architecture Overview

VRAB is a VWAP mean-reversion trading bot for Hyperliquid perpetuals. The system is designed around a **zero-divergence principle**: all trading logic runs through the same code for both backtest and live execution.

### Module Dependency Graph

```
config.py (parameters)
    │
    ▼
strategy/signals.py  ◄── Pure functions, no config imports
costs/model.py       ◄── Pure functions, no config imports
risk/liquidation.py  ◄── Pure functions, no config imports
    │
    ▼
strategy/core.py     ◄── Shared decision pipeline
    │                    (calls signals, costs, risk)
    │                    (no config imports except build_params_from_config)
    │
    ├──────────────────┐
    ▼                  ▼
backtest/engine.py   live/engine.py
(historical data)    (WebSocket feed via live/feed.py)
(simulated fills)    (real orders via live/hl_client.py)
    │                  │  (or paper fills via live/paper.py)
    ▼                  ▼
data/store.py        data/store.py
(SQLite + cache)     (SQLite + cache)
```

### Data Flow

1. **Data Pull**: `data/puller.py` → Hyperliquid REST API → SQLite
2. **Signal Generation**: Candle arrays → `strategy/signals.py` → `SignalResult`
3. **Decision Pipeline**: `SignalResult` → `strategy/core.py` → `CoreDecision`
4. **Execution**: `CoreDecision` → Engine (backtest/live) → Fill simulation/real order
5. **Recording**: Trade result → `data/store.py` → SQLite

### Design Principles

- **Pure functions**: `signals.py`, `costs/model.py`, `risk/liquidation.py` have zero side effects, no config imports, all parameters explicit
- **Single source of truth**: `config.py` holds all parameters; `strategy/core.py` holds all decision logic
- **SQLite over Redis**: Single-server deployment, WAL mode for concurrent reads, simple backup (`cp`)
- **Threading safety**: `threading.Lock` on all SQLite writes in `Store`

## Schema Reference

### `candles` table
| Column | Type | Description |
|--------|------|-------------|
| symbol | TEXT | Trading pair (e.g., "BTC") |
| tf     | TEXT | Timeframe (e.g., "5m", "15m") |
| ts     | INT  | Open timestamp in milliseconds |
| open   | REAL | Open price |
| high   | REAL | High price |
| low    | REAL | Low price |
| close  | REAL | Close price |
| volume | REAL | Volume |

Primary key: (symbol, tf, ts)

### `trades` table
40+ columns capturing full trade context. Key groups:
- **Identity**: symbol, side, source (backtest/live/paper)
- **Prices**: entry, exit, stop, target, VWAP at entry, sigma at entry
- **Sizing**: size_usd, notional_usd, leverage, margin_used, equity_at_entry
- **Risk**: liq_price, liq_buffer_ratio
- **Timing**: entry_ts, exit_ts, hold_candles, hold_minutes
- **PnL**: pnl_usd (gross), slippage, entry_fee, exit_fee, funding, maker_rebate, net_pnl, equity_return_pct
- **Regime context**: adx, ema, trend_direction, regime_trending, vwap_std_dev, volume (at entry)
- **Metadata**: window_idx, created_at

### `signals` table
Every signal with full VWAP state + regime context for offline analysis.

### `daily_pnl` table
Daily aggregates: PnL, trade count, max drawdown, start/end equity, halt events, signal counts.

## Configuration Reference

See `config.py` for all parameters. Key interactions:
- **Daily DD threshold** = `RISK_PER_TRADE × MAX_DAILY_LOSS_MULTIPLIER` = 0.015 × 3 = 4.5%
- **Max hold** = `MAX_POSITION_HOLD_MINS / 5` = 48 candles
- **VWAP window** = `VWAP_WINDOW × 5m` = 8 hours
- **Liq buffer** = stop must be ≤ 30% of distance from entry to liquidation

## API Contracts

### Hyperliquid REST
- **Candles**: POST `https://api.hyperliquid.xyz/info` with `{"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "5m", "startTime": ms, "endTime": ms}}`
- Max 500 candles per request

### Dashboard
- `GET /` — status JSON
- `GET /api/trades?days=30` — trade history
- `GET /api/daily?days=30` — daily PnL
- `GET /api/signals?limit=100` — recent signals
- `GET /api/health` — health check + kill switch status

## Operational Runbook

### Pull Data
```bash
python -m data.puller --symbol BTC --tf 5m --days 120
```

### Run Backtest
```bash
python -m backtest.engine --symbol BTC --tf 5m --windows 3 --window-days 30
```

### Kill Switch
```bash
touch /tmp/VRAB_KILL    # activate
rm /tmp/VRAB_KILL       # deactivate
```

### Backup SQLite
```bash
cp data/vrab.db data/vrab.db.backup
```

### Run Live Engine (Paper Mode)
```bash
python -m live
```

### Run Live Engine (Real)
Set `PAPER_MODE = False` in `config.py`, ensure `.env` has `HL_PRIVATE_KEY` and `HL_WALLET_ADDRESS`.
```bash
python -m live
```

### Log Files
- `logs/vrab.log` — main log (rotating, 10MB × 5)
- Check for `ERROR` and `WARNING` entries
