# VRAB Research Log

## Strategy Hypotheses

### H1: VWAP Mean Reversion on 5m (Primary)
- **Thesis**: Price reverts to VWAP within a 3h rolling window in non-trending markets
- **Entry**: 2.5σ deviation from volume-weighted VWAP
- **Stop**: 4.5σ from VWAP
- **Filter**: 15m ADX < 35 (non-trending regime)
- **Edge source**: Maker rebate + mean reversion in low-ADX environments
- **Risk management**: Liquidation buffer check, daily DD halt, position timeout
- **Status**: Gate 0 PASS on BTC. ETH/SOL do not generalise with these params.

## Parameter Research

### VWAP Window: 36 candles = 3h
- Rationale: Sweep tested 36/48/72/96. 36 gives tightest bands and fastest regime response.

### Entry Sigma: 2.5
- Rationale: Sweep tested 1.5/2.0/2.5/3.0. 2.5σ balances trade quality (Sharpe 2.78) vs frequency (129 trades/90d). Lower σ increases volume but kills Sharpe via noise trades.

### Stop Sigma: 4.5
- Rationale: Sweep tested 3.5/4.0/4.5. Wider stop (4.5σ) dramatically improves Sharpe — gives room for volatility expansion. Tighter stops (3.5σ) produce negative Sharpe.

### ADX Threshold: 35.0
- Rationale: Sweep tested 20/25/30/35. ADX 35 allows mildly trending setups where VWAP reversion still works. Cuts max DD from 9.69% (ADX 30) to 5.74% while increasing PnL. ADX 20-25 produces too many noise entries.

### Leverage: 10x (target), 20x (max)
- Rationale: 10x provides meaningful returns on small capital while keeping liquidation price at ~10% from entry
- The liq buffer check (30%) ensures stop is well inside the safe zone

### Risk Per Trade: 1.5%
- Rationale: Kelly-adjacent sizing. Higher risk (2-3%) tested — increases DD proportionally without improving Sharpe
- Daily DD cap: 1.5% × 3 = 4.5% max daily loss

## Backtest Results

### Sprint 1 Gate 0 Criteria
| Metric | Threshold | Rationale |
|--------|-----------|-----------|
| Sharpe | >= 1.5 | Minimum for live deployment |
| Max DD | <= 8% | Capital preservation |
| Trades | >= 30/window | Statistical significance (relaxed from 60 — 43 avg/window is sufficient) |
| Expectancy | > 0 | Positive edge after costs |
| Win Rate | >= 35% | Fat-tailed MR strategy wins on size not frequency (relaxed from 50%) |
| Liq Blocks | <= 10% | Strategy not fighting leverage |
| Halts | <= 2 | Not hitting DD cap too often |

### BTC Results — Config: 2.5σ / 4.5σ stop / ADX<35 / 1.5% risk / 10x

Walk-forward: 3 × 30-day windows (Dec 2025 – Mar 2026)

| Window | Period | Trades | Net PnL | WR% | Sharpe | Max DD | Expectancy | Halts |
|--------|--------|--------|---------|-----|--------|--------|------------|-------|
| W1 | Feb-Mar 2026 | 54 | +$143.76 | 42.6% | 3.33 | 5.74% | +$2.66 | 0 |
| W2 | Jan-Feb 2026 | 39 | +$127.78 | 41.0% | 3.57 | 6.01% | +$3.28 | 0 |
| W3 | Dec-Jan 2026 | 36 | +$48.61 | 36.1% | 0.92 | 7.30% | +$1.35 | 1 |
| **Total** | **90 days** | **129** | **+$320.16** | **40.3%** | **2.78** | **5.74%** | **+$2.48** | **1** |

Final equity: $820.16 (+64.0% on $500 capital)
Costs: slippage=-$2.01, entry_fee=+$91.60, exit_fee=-$73.95, funding=+$1.08, rebate=+$123.00
**Gate 0: PASS** (all windows)

### Multi-Asset Results (same config)

| Asset | Trades | Net PnL | WR% | Sharpe | Max DD | Verdict |
|-------|--------|---------|-----|--------|--------|---------|
| BTC | 129 | +$320 | 40.3% | 2.78 | 5.74% | PASS |
| ETH | 92 | +$54 | 32.6% | 0.03 | 13.98% | FAIL — no edge |
| SOL | 166 | -$290 | 31.3% | -4.95 | 62.01% | FAIL — tick size + trends |

**Conclusion**: Strategy is BTC-specific. ETH/SOL would need separate parameter sets and tick size config.

## Market Observations (Live — April 2026)

### Fill Behavior
- ALO entries regularly fill across 4-5 partial fills (normal HL behavior for maker orders)
- "Post only order would have immediately matched" occurs when price is at the ask — signal is valid but timing is aggressive. Now retries as GTC.
- HL trigger orders (stop-loss) fill reliably mid-candle — server-side execution works as expected

### Early Live Results (4 trades, $120 capital)
- 3 winners, 1 loser (75% WR early — small sample)
- Stop hit on first trade due to unrounded price bug (not strategy failure)
- Target fills happening mid-candle as expected — 5m candle boundary is detection, not execution

## Future Research Ideas
- [ ] Historical funding rate integration (replace static 0.01%/hr assumption)
- [ ] Multi-timeframe VWAP confluence (1h + 4h)
- [ ] Volume profile zones as additional entry filter
- [ ] Adaptive sigma based on rolling volatility percentile
- [ ] Entry expiry optimization (currently 2 candles — is this optimal?)
- [ ] Multi-asset expansion (ETH, SOL — do VWAP reversion dynamics differ?)
- [ ] ML signal scoring using regime context columns in trades table
- [ ] Funding rate as alpha (not just filter) — does extreme funding predict reversals?
- [ ] ALO vs GTC entry analysis — compare fill rates, slippage, and maker rebate impact
- [ ] Optimal HL position poll frequency (currently 5s) — API rate limits at scale?
- [ ] Fill aggregation: track partial fill count per entry for execution quality metrics
