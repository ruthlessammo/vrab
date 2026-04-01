# VRAB Documentation

**VWAP Reversion Algo Bot — Complete Reference**

*Last updated: April 2026*

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [How the Strategy Works](#2-how-the-strategy-works)
3. [Risk Management](#3-risk-management)
4. [Transaction Costs](#4-transaction-costs)
5. [System Architecture](#5-system-architecture)
6. [How the Bot Runs](#6-how-the-bot-runs)
7. [Backtesting](#7-backtesting)
8. [Monitoring and Control](#8-monitoring-and-control)
9. [Configuration Reference](#9-configuration-reference)
10. [Operating Procedures](#10-operating-procedures)
11. [What Can Go Wrong](#11-what-can-go-wrong)
12. [Glossary](#12-glossary)

---

## 1. Executive Summary

VRAB is an automated trading bot that trades BTC (Bitcoin) perpetual futures on the Hyperliquid exchange. It makes money by betting that extreme price moves will reverse — a strategy called mean reversion.

The bot watches the price of Bitcoin every five minutes. When the price stretches far from its recent average, VRAB places a trade betting that the price will snap back. It manages its own risk by setting automatic stop-losses, limiting how much it can lose in a day, and shutting itself down if losses get too large.

VRAB runs 24/7, sends real-time alerts to your phone via Telegram, and provides a web dashboard for visual monitoring. It can run in "paper mode" (simulated trading with no real money) for testing, or "live mode" with real funds on the exchange.

**Who this document is for:** Anyone on the team, regardless of trading or programming background. Every technical term is explained the first time it appears.

---

## 2. How the Strategy Works

### 2.1 The Core Idea

VRAB trades on a simple observation: when a price moves far from its recent average, it tends to come back. Think of a rubber band stretched around the price — the further you pull it, the harder it snaps back.

The bot measures "how far is too far" using statistics, and only trades when the stretch is extreme enough to be worth the risk.

### 2.2 What is VWAP?

VWAP stands for Volume Weighted Average Price. It is the average price of Bitcoin over a period of time, but weighted by how much was traded at each price level. Prices where lots of trading happened count more than prices where little happened.

Think of it as the "fair price" that most traders actually paid. If the current price is far above VWAP, most recent buyers paid less — the price may be overextended. If the price is far below VWAP, most recent sellers got more — the price may be oversold.

VRAB calculates VWAP over the last 36 five-minute candles (three hours of trading data). A "candle" is a summary of price action over a time period — its open, high, low, and close prices, plus volume.

### 2.3 What are Sigma Bands?

Sigma (σ) is a measure of standard deviation — how spread out the data is. VRAB calculates how many standard deviations the current price is from VWAP. This number is called the "sigma distance."

- **0σ** means the price is exactly at VWAP (fair value)
- **+1σ** means the price is one standard deviation above VWAP (moderately high)
- **+2.5σ** means the price is 2.5 standard deviations above VWAP (very high — overbought)
- **-2.5σ** means the price is 2.5 standard deviations below VWAP (very low — oversold)

The rubber band analogy: at 0σ the band is relaxed. At ±2.5σ it is stretched to the point where a snap-back becomes likely.

### 2.4 Entry Rules

VRAB enters a trade when the price stretches to ±2.5σ from VWAP:

**Going Long (betting price will rise):**
The price has dropped to 2.5 standard deviations below VWAP. VRAB buys, expecting the price to revert back toward VWAP.

**Going Short (betting price will fall):**
The price has risen to 2.5 standard deviations above VWAP. VRAB sells, expecting the price to revert back down toward VWAP.

VRAB only enters one trade at a time. If it already has an open position, it does not open another.

### 2.5 Exit Rules

Once in a trade, VRAB exits in one of four ways (checked in this order):

1. **Stop-Loss Hit:** The price moves further against the trade, reaching 4.5σ from VWAP. This is the maximum acceptable loss. The bot closes the trade immediately with a market order. This is the worst-case exit.

2. **Target Hit:** The price reverts to VWAP (0σ). This is the intended outcome — the rubber band snapped back. The bot closes the trade with a limit order at VWAP.

3. **Signal Exit:** The strategy generates a new signal saying to exit (for example, VWAP itself has shifted). The bot exits at the current VWAP level.

4. **Timeout:** The trade has been open for 48 candles (four hours) without hitting either the stop or target. The bot closes at market price. This prevents capital from being tied up in stale trades.

### 2.6 The Trend Filter (ADX)

Mean reversion does not work when the market is trending strongly in one direction. If Bitcoin is in a clear uptrend, a dip below VWAP is not a reversal — it is just a temporary pullback before continuing higher. Trading against a strong trend is a losing strategy.

VRAB uses a metric called ADX (Average Directional Index) to measure trend strength. ADX is a number from 0 to 100:

- **ADX below 20:** Very weak trend (choppy, range-bound market)
- **ADX 20–35:** Mild trend (VRAB still trades here — mild trends often contain mean-reverting moves)
- **ADX above 35:** Strong trend (VRAB blocks all entries — too risky for mean reversion)

The threshold is set at 35. When ADX is above 35, VRAB sits on its hands and waits for the trend to weaken.

ADX is calculated from 15-minute candles (the "trend timeframe") rather than 5-minute candles. This gives a more stable read on the broader market direction and avoids reacting to short-term noise.

### 2.7 The Funding Rate Filter

Perpetual futures (the type of contract VRAB trades) have a mechanism called the "funding rate." Every hour, traders on one side of the market pay traders on the other side. The rate depends on market sentiment:

- When most traders are long (bullish), longs pay shorts (positive funding rate)
- When most traders are short (bearish), shorts pay longs (negative funding rate)

If the funding rate is extreme (above 0.03% per hour), VRAB blocks trades that would be on the paying side. For example, if longs are paying 0.05% per hour, VRAB will not open a new long position because the funding cost would eat into profits.

### 2.8 A Complete Trade Example

Suppose Bitcoin's VWAP over the last three hours is $68,000, with a standard deviation of $200.

**Entry:** The price drops to $67,500. That is $500 below VWAP, which is 2.5 × $200 = 2.5σ. VRAB places a buy (long) order at $67,500.

**Stop-Loss:** Placed at 4.5σ below VWAP = $68,000 - (4.5 × $200) = $67,100. If the price falls to $67,100, the trade is closed for a loss.

**Target:** Placed at VWAP = $68,000. If the price rises back to $68,000, the trade is closed for a profit.

**Outcome A (Win):** Price reverts to $68,000 in 30 minutes. Profit = the difference between entry ($67,500) and target ($68,000) = $500 per BTC, minus small costs. This is the intended scenario.

**Outcome B (Loss):** Price continues falling to $67,100. Loss = the difference between entry ($67,500) and stop ($67,100) = $400 per BTC, plus some slippage costs. The trade is automatically closed.

**Outcome C (Timeout):** Price sits at $67,700 for four hours, neither hitting the stop nor the target. VRAB closes at $67,700 for a $200 per BTC profit (partial win).

---

## 3. Risk Management

### 3.1 Position Sizing

VRAB risks exactly 1.5% of account equity on each trade. This means if the trade hits the stop-loss, the loss will be approximately 1.5% of the account.

Here is how the size is calculated step by step:

1. **Account equity:** $500 (starting balance)
2. **Risk amount:** $500 × 1.5% = $7.50 (maximum acceptable loss on this trade)
3. **Stop distance:** Suppose entry is $68,000 and stop is $67,100. The distance is $900, which is $900 / $68,000 = 1.32% of the entry price.
4. **Risk-based size:** $7.50 / 1.32% = $568 (this is the position size where a 1.32% move loses exactly $7.50)
5. **Margin cap:** With 10x leverage, the maximum position is $500 × 10 × 80% = $4,000. Since $568 is below $4,000, the risk-based size is used.
6. **Final size:** $568 notional, or approximately 0.00835 BTC at $68,000.

The 80% margin cap ensures VRAB never uses its entire available margin, leaving a buffer for adverse moves.

### 3.2 Leverage

Leverage means borrowing money from the exchange to take a larger position. With 10x leverage and $500 of your own money, you control $5,000 worth of Bitcoin. This amplifies both gains and losses by 10x.

VRAB uses 10x leverage as its default, with a hard cap at 20x. The position size is always determined by the risk rules above — leverage just determines how much margin (collateral) is required.

### 3.3 Liquidation

If a leveraged trade loses too much, the exchange forcibly closes the position to prevent the account from going negative. This is called liquidation. It is the worst possible outcome — you lose your entire margin for that trade.

With 10x leverage, liquidation happens at roughly 10% from entry (minus a small maintenance margin). For a long at $68,000, liquidation would occur around $61,762.

VRAB calculates the exact liquidation price using this formula:

- **Long:** Liquidation Price = Entry Price × (1 - 1/Leverage + Maintenance Margin Rate)
- **Short:** Liquidation Price = Entry Price × (1 + 1/Leverage - Maintenance Margin Rate)

The maintenance margin rate on Hyperliquid for BTC is 0.5%.

### 3.4 Liquidation Buffer

VRAB enforces a safety rule: the stop-loss must be placed no more than 30% of the way from entry to liquidation. This leaves a 70% cushion.

**Example (Long at $68,000):**
- Liquidation price: $61,762
- Distance from entry to liquidation: $68,000 - $61,762 = $6,238
- 30% of that distance: $6,238 × 30% = $1,871
- Stop must be within $1,871 of entry: $68,000 - $1,871 = $66,129 or closer
- Actual stop at $67,100 is $900 from entry, which is 14.4% of the liquidation distance. Safe.

If the stop would be more than 30% of the way to liquidation, VRAB rejects the trade entirely. This prevents situations where slippage or a flash crash could push the price past the stop and into liquidation territory.

### 3.5 Daily Loss Halt

If VRAB loses more than 3× the single-trade risk in one day, it stops trading for the rest of that day.

- Single-trade risk: 1.5% of equity = $7.50
- Daily halt threshold: 3 × $7.50 = $22.50
- If daily losses reach -$22.50, VRAB halts until midnight UTC

This prevents a bad day from spiraling into a catastrophic one. Trading resumes automatically the next day.

### 3.6 Circuit Breaker

The circuit breaker is a longer-term safety mechanism. It tracks the highest account balance ever reached ("peak equity"). If the account drops more than 10% from that peak, the circuit breaker activates and all trading stops.

**Example:**
- Peak equity reached: $600
- 10% drawdown threshold: $600 × 10% = $60
- If equity drops to $540 or below, the circuit breaker fires

Unlike the daily halt, the circuit breaker does not reset automatically. It requires manual intervention via the Telegram `/reset` command. This forces a human to review what went wrong before trading resumes.

The circuit breaker state is saved to the database, so it survives bot restarts.

### 3.7 Kill Switch

The kill switch is an emergency stop that immediately cancels all orders and closes any open position. It can be activated in two ways:

1. **Telegram:** Send `/kill` to the bot
2. **File:** Create a file at `/tmp/VRAB_KILL` on the server

The engine checks for the kill switch every candle (every five minutes). When activated, it cancels all orders, closes any position at market, and halts.

To deactivate the file-based kill switch, delete the file: `rm /tmp/VRAB_KILL`

---

## 4. Transaction Costs

### 4.1 Maker vs Taker

On an exchange, there are two types of orders:

- **Maker orders (limit orders):** You set a specific price and wait. Your order "makes" liquidity by sitting on the order book. Exchanges reward this with a rebate — Hyperliquid pays 0.02% of the order value back to you.

- **Taker orders (market orders):** You want to trade immediately at whatever price is available. Your order "takes" liquidity from the book. Exchanges charge a fee — Hyperliquid charges 0.035%.

VRAB places limit orders wherever possible (entries and take-profit exits) to earn the maker rebate. Only stop-loss exits use market orders (because speed matters more than cost when cutting a loss).

### 4.2 What the Rebate Means in Practice

On a $4,000 position:
- **Maker rebate:** $4,000 × 0.02% = $0.80 earned (you get paid to trade)
- **Taker fee:** $4,000 × 0.035% = $1.40 paid (you pay to trade)

Since VRAB enters with limit orders (maker) and often exits at the target with limit orders (maker), most trades earn rebates on both sides. Stop-loss exits pay the taker fee on the exit side only.

### 4.3 Slippage

Slippage is the difference between the price you intended to trade at and the price you actually got. It happens because the market moves between the time you decide to trade and the time your order fills.

VRAB models slippage conservatively:
- **Entry slippage:** 1 tick = $0.10 (limit orders get very close to the intended price)
- **Stop-loss slippage:** 3 ticks = $0.30 (market orders in a fast-moving market slip more)

A tick is the minimum price increment on the exchange. For BTC on Hyperliquid, one tick is $0.10.

### 4.4 Funding Costs

Perpetual futures have no expiry date (unlike traditional futures). To keep the futures price aligned with the spot price, the exchange uses a funding mechanism. Every hour, one side pays the other:

- **Positive funding rate + Long position = You pay** (cost)
- **Positive funding rate + Short position = You earn** (income)
- **Negative funding rate = Reversed**

VRAB uses 0.01% per hour as its conservative estimate in backtesting. In live trading, it reads the actual rate from the exchange.

**Example:** $4,000 position held for 1 hour with 0.01% funding rate:
- Long cost: $4,000 × 0.01% = $0.40 per hour
- Over a 4-hour hold: $1.60 total funding cost

### 4.5 Worked Example: Full Cost Breakdown

**Trade:** Long BTC at $68,000, exit at target $68,400 (VWAP), position size $3,940.

| Cost Item | Calculation | Amount |
|-----------|-------------|--------|
| Gross profit | 0.0579 BTC × ($68,400 - $68,000) | +$23.16 |
| Entry slippage | -1 tick × 0.0579 BTC | -$0.01 |
| Exit slippage | -1 tick × 0.0579 BTC | -$0.01 |
| Entry fee (maker rebate) | +$3,940 × 0.02% | +$0.79 |
| Exit fee (maker rebate) | +$3,940 × 0.02% | +$0.79 |
| Funding (1 hour) | -$3,940 × 0.01% × 1 | -$0.39 |
| **Net profit** | | **+$24.33** |

The maker rebates actually add to the profit. This is by design — VRAB's strategy of placing limit orders turns the exchange's fee structure into a small but consistent tailwind.

---

## 5. System Architecture

### 5.1 High-Level Data Flow

```
Hyperliquid Exchange
        |
        | WebSocket (real-time price data)
        | REST API (historical data, order placement)
        |
        v
   Candle Feed (live/feed.py)
        |
        | Candle close events every 5 minutes
        | Tick events every ~1 second (for paper fill checking)
        |
        v
   Trading Engine (live/engine.py)
        |
        |--- Strategy Core (strategy/core.py)
        |       |--- Signal Generation (strategy/signals.py)
        |       |--- Cost Model (costs/model.py)
        |       |--- Risk Checks (risk/liquidation.py)
        |
        |--- Order Execution
        |       |--- Real orders (live/hl_client.py) [live mode]
        |       |--- Simulated orders (live/paper.py) [paper mode]
        |
        |--- Data Store (data/store.py) --- SQLite Database
        |                                       |
        |                                       |--- candles (price history)
        |                                       |--- trades (completed trades)
        |                                       |--- signals (all signals generated)
        |                                       |--- daily_pnl (daily summaries)
        |                                       |--- meta (persistent state)
        |
        |--- Telegram (notifications/)
        |       |--- Alerts (trade fills, halts, errors)
        |       |--- Bot (remote commands: /status, /kill, etc.)
        |
        v
   Web Dashboard (dashboard/app.py)
        |
        | HTTP API (read-only queries to SQLite)
        |
        v
   Browser (dashboard/templates/index.html)
```

### 5.2 Module Map

| Module | Purpose |
|--------|---------|
| `config.py` | Every setting in one place: capital, leverage, strategy thresholds, fee rates, file paths |
| `strategy/signals.py` | Pure math: takes price arrays, returns entry/exit signals. No side effects. |
| `strategy/core.py` | The decision pipeline: signal → risk check → position sizing → cost estimate. Shared by backtest and live. |
| `costs/model.py` | Calculates fill prices with slippage, fees, funding costs, and break-even moves |
| `risk/liquidation.py` | Calculates liquidation prices and checks stop-loss safety margins |
| `live/engine.py` | The main loop: reads candles, calls the strategy, places orders, tracks positions |
| `live/feed.py` | Connects to the exchange WebSocket, streams candle data, detects candle closes |
| `live/hl_client.py` | Talks to Hyperliquid: place orders, check balances, cancel orders |
| `live/paper.py` | Simulates an exchange locally for testing — no real money involved |
| `data/store.py` | Reads and writes to the SQLite database, keeps a fast in-memory cache |
| `data/puller.py` | Downloads historical price data from Hyperliquid for backtesting |
| `backtest/engine.py` | Runs the strategy on historical data to see how it would have performed |
| `notifications/telegram.py` | Formats and sends messages to Telegram |
| `notifications/bot.py` | Listens for Telegram commands and responds |
| `dashboard/app.py` | Web server that provides API endpoints for the dashboard |

### 5.3 The Zero-Divergence Principle

The most important design decision in VRAB: the strategy code that runs in backtesting is the exact same code that runs in live trading. There is no separate "backtest version" and "live version" of the strategy.

Both the backtest engine and the live engine call the same functions in `strategy/core.py`:
- `evaluate_entry()` — decides whether to open a trade
- `evaluate_exit()` — decides whether to close a trade
- `calc_trade_pnl()` — calculates profit and loss

The engines are thin adapters that only handle the differences between simulated and real execution (where the data comes from, how orders are placed). All trading logic lives in the shared core.

This eliminates a common and dangerous problem: backtests that show great results but don't match what happens live because the code diverged.

### 5.4 The Database

VRAB stores everything in a single SQLite database file at `data/vrab.db`. SQLite is a lightweight database that lives in one file — no separate database server needed.

The database has five tables:

| Table | What it stores | Updated when |
|-------|---------------|--------------|
| `candles` | Price data (open, high, low, close, volume) for every 5-minute and 15-minute candle | Every candle close |
| `trades` | Every completed trade with 40+ fields: prices, sizes, costs, PnL, market context at entry | Every trade close |
| `signals` | Every signal the strategy generated, whether acted on or blocked, with full market state | Every candle |
| `daily_pnl` | One row per day: total PnL, trade count, max drawdown, start/end equity | Every trade close and at midnight |
| `meta` | Key-value pairs for persistent state: peak equity, circuit breaker status, open position | On specific state changes |

The database uses WAL (Write-Ahead Logging) mode, which allows the dashboard to read data while the engine writes to it without conflicts.

---

## 6. How the Bot Runs

### 6.1 Paper Mode vs Live Mode

VRAB has two operating modes controlled by the `PAPER_MODE` setting in `config.py`:

**Paper Mode (PAPER_MODE = True):**
- No real money is at risk
- Orders are simulated locally using the `PaperClient`
- Limit orders fill when the candle's price range touches the order price
- Uses the real exchange's price feed (WebSocket) but never places actual orders
- Starting balance is virtual ($500 by default)
- Use this for testing and validation before going live

**Live Mode (PAPER_MODE = False):**
- Real money on Hyperliquid
- Orders are placed on the actual exchange via the Hyperliquid SDK (Software Development Kit — a set of tools for communicating with the exchange)
- Requires a private key and wallet address in the `.env` file
- All safety mechanisms are active (dead-man switch, circuit breaker, kill switch)

### 6.2 The Five-Minute Loop

Every five minutes, when a candle closes, the engine runs through this sequence:

1. **Check kill switch** — If active, cancel everything and close any position immediately.

2. **Check for new day** — If it is past midnight UTC, finalize yesterday's PnL, send a daily summary, and reset daily counters.

3. **Refresh dead-man switch** (live mode only) — Tell the exchange "I am still alive, do not cancel my orders yet." If the bot dies without refreshing, the exchange auto-cancels all orders after 10 minutes.

4. **Check equity and circuit breaker** — Read the current account balance. If equity has dropped more than 10% from peak, activate the circuit breaker and stop trading.

5. **If in a position:** Run the exit evaluation. Check if the stop, target, signal exit, or timeout conditions are met. If yes, execute the exit.

6. **If halted:** Do nothing further (wait for reset or new day).

7. **If a pending entry order exists:** Check if it filled, expired, or was cancelled.

8. **If no position and not halted:** Run the entry evaluation. Generate a signal, run risk checks, calculate position size, and place an entry order if everything passes.

### 6.3 Order Types

VRAB uses three types of orders on the exchange:

- **Limit Order (post-only):** "I want to buy/sell at exactly this price. If the price is not there, wait." Used for entries and take-profit exits. Earns the maker rebate.

- **Trigger Order (stop-loss):** "If the price reaches this level, sell/buy immediately at market." This is a conditional order that lives on the exchange's servers. Even if VRAB's connection drops, the stop-loss still executes. Used for stop-loss protection.

- **Market Order:** "Buy/sell immediately at the best available price." Used for emergency closes, timeout exits, and stop-loss fills. Pays the taker fee.

### 6.4 Position Recovery

If VRAB is stopped (Ctrl+C, server restart, crash) while holding an open position, the position state is recovered on the next startup.

**How it works:**
- Every time a position is opened, the full position details (entry price, stop price, target price, size, timestamps, etc.) are saved to the database as a JSON (JavaScript Object Notation — a text format for storing structured data) string in the `meta` table.
- Every time a position is closed, that saved state is cleared.
- On startup, the engine checks the `meta` table for saved position data.

**Paper mode recovery:**
- Loads the saved position data
- Restores the paper client's internal state
- Re-places the stop-loss and take-profit orders
- Sends a Telegram alert confirming recovery

**Live mode recovery:**
- Loads the saved position data
- Queries the exchange to verify the position still exists
- If the exchange confirms the same position (same side): restores local state, cancels any stale orders, and re-places fresh stop and target orders
- If there is a mismatch (position was closed externally, or sides do not match): clears the stale data and sends a warning alert

### 6.5 Graceful Shutdown

When you press Ctrl+C (or the server sends a shutdown signal):

1. VRAB sets a shutdown flag to exit the main loop cleanly
2. All open orders are cancelled
3. A Telegram alert is sent ("VRAB Stopped" or "VRAB Shutting Down" with position details)
4. The WebSocket feed is disconnected
5. The Telegram bot is stopped

In live mode, open positions are not automatically closed on shutdown. The stop-loss trigger order remains on the exchange to protect the position. On restart, position recovery picks up where it left off.

Pressing Ctrl+C a second time forces an immediate exit without cleanup.

---

## 7. Backtesting

### 7.1 What is Backtesting?

Backtesting runs the trading strategy on historical price data to see how it would have performed. It answers the question: "If this bot had been running over the past 90 days, would it have made money?"

VRAB uses walk-forward backtesting, which is more rigorous than simple backtesting. Instead of testing on one continuous period, it divides history into multiple windows and tests each one separately. Think of it as testing your umbrella in three different rainstorms instead of one — if it works in all three, you can be more confident it will work in the next one.

### 7.2 How it Works

1. **Pull data:** Download historical 5-minute and 15-minute candles from Hyperliquid (typically 90–120 days).

2. **Divide into windows:** Split the data into three 30-day windows (most recent first).

3. **Simulate each window:**
   - Start with $500 virtual capital
   - Walk through every 5-minute candle in order
   - Apply the exact same entry and exit rules as live trading
   - Simulate order fills (70% of limit orders fill — conservative assumption)
   - Track every trade with full cost modeling (slippage, fees, funding)

4. **Evaluate results** per window and across all windows.

### 7.3 Gate 0: The Go/No-Go Criteria

Before VRAB goes live, the backtest must pass "Gate 0" — a set of minimum performance thresholds. Every window must pass individually.

| Metric | Threshold | What it Means |
|--------|-----------|---------------|
| Sharpe Ratio | >= 1.5 | Risk-adjusted returns must be strong (see glossary) |
| Max Drawdown | <= 8% | Worst peak-to-trough decline must stay under 8% |
| Trade Count | >= 30 per window | Enough trades for statistical significance |
| Win Rate | >= 35% | At least 35% of trades must be winners |
| Expectancy | > $0 | Average trade must be profitable after costs |
| Liquidation Blocks | <= 10% | Less than 10% of signals blocked by liquidation safety |
| Daily Halts | <= 2 per window | Strategy should not hit daily loss limit frequently |

The win rate threshold of 35% may seem low, but this is intentional. VRAB is a "fat-tailed" strategy — it wins less often but wins bigger when it does. The average winner is significantly larger than the average loser.

### 7.4 BTC Results

Walk-forward test: 3 windows × 30 days (December 2025 through March 2026):

| Window | Period | Trades | Net PnL | Win Rate | Sharpe | Max Drawdown |
|--------|--------|--------|---------|----------|--------|--------------|
| W1 | Feb–Mar 2026 | 54 | +$143.76 | 42.6% | 3.33 | 5.74% |
| W2 | Jan–Feb 2026 | 39 | +$127.78 | 41.0% | 3.57 | 6.01% |
| W3 | Dec–Jan 2026 | 36 | +$48.61 | 36.1% | 0.92 | 7.30% |
| **Total** | **90 days** | **129** | **+$320.16** | **40.3%** | **2.78** | **5.74%** |

Starting capital: $500. Final equity: $820.16 (+64.0% return over 90 days).

Gate 0 verdict: **PASS** across all windows.

### 7.5 Cost Breakdown from Backtests

Over 129 trades:

| Cost Item | Total | Per Trade Average |
|-----------|-------|-------------------|
| Slippage | -$2.01 | -$0.016 |
| Entry fees (rebates) | +$91.60 | +$0.71 |
| Exit fees | -$73.95 | -$0.57 |
| Funding | +$1.08 | +$0.008 |
| **Net cost impact** | **+$16.72** | **+$0.13** |

Costs were net positive — the maker rebates earned more than the fees and slippage paid. This is a structural advantage of the limit-order-first approach.

### 7.6 Why ETH and SOL Failed

The strategy was tested on other assets with the same parameters:

| Asset | Net PnL | Sharpe | Max Drawdown | Verdict |
|-------|---------|--------|--------------|---------|
| BTC | +$320 | 2.78 | 5.74% | Pass |
| ETH | +$54 | 0.03 | 13.98% | Fail — no meaningful edge |
| SOL | -$290 | -4.95 | 62.01% | Fail — catastrophic loss |

ETH showed marginal profitability but with a near-zero Sharpe ratio, meaning the returns were not worth the risk. SOL was destructive — the tick size (minimum price increment) does not match BTC's, and SOL's market structure is more trend-driven than mean-reverting.

**Conclusion:** VRAB is a BTC-specific strategy. Other assets would need their own parameter sets and possibly different strategy logic.

---

## 8. Monitoring and Control

### 8.1 Telegram Commands

Send these commands to the VRAB Telegram bot from your phone:

| Command | What it Does |
|---------|-------------|
| `/status` | Shows current position (if any), market state (price, VWAP, sigma, ADX, trend), equity, daily PnL, and uptime |
| `/pnl` | Shows today's PnL, this week's PnL, total PnL, trade count, and win rate |
| `/equity` | Shows current account balance, starting capital, and total return percentage |
| `/trades` | Shows the last 5 completed trades with entry, exit, PnL, and exit reason |
| `/kill` | Emergency stop. Creates the kill switch file. Engine halts on next candle. |
| `/reset` | Clears the circuit breaker and resets peak equity to the current balance. Resumes trading. |

Any unrecognised command shows the help menu listing all available commands.

The bot only responds to messages from the configured Telegram chat ID — it ignores messages from anyone else.

### 8.2 Automatic Alerts

VRAB sends Telegram alerts automatically for:

- **Trade fills:** Entry and exit details, PnL, return %, hold time
- **Daily halt:** When daily loss limit is hit
- **Circuit breaker:** When 10% drawdown threshold is breached
- **Errors:** Any exception during candle processing
- **Daily summary:** End-of-day report with PnL, trade count, equity
- **Heartbeat:** Periodic status update every hour (12 candles)
- **Position recovery:** When a position is restored after restart
- **Feed reconnect:** When the WebSocket connection is re-established after a drop

Alerts are rate-limited to one message every two seconds to avoid flooding. If Telegram is down, VRAB continues trading normally — alerts are best-effort, not blocking.

### 8.3 Web Dashboard

The dashboard runs at `http://localhost:5555` (or your server's IP on port 5555). It shows:

**Status Bar (updates every 30 seconds):**
- Mode: paper or live
- Status: running, halted, or circuit breaker
- Equity: current account balance (calculated as starting capital plus net PnL)
- Daily PnL: today's profit or loss
- Trades today: number of completed trades
- Uptime: how long the engine has been running

**Sigma Deviation Bar (updates every 5 seconds):**
- A horizontal gauge showing where the current price sits relative to VWAP
- Centered at 0σ (VWAP), with marks at ±2.5σ (entry thresholds)
- Colors shift from green (near VWAP, calm) to yellow (±1.5σ, warming up) to red (±2.5σ, entry zone)
- Displays: current price, VWAP, sigma distance, ADX value, trend direction

**Statistics Cards:**
- Total PnL, total trades, win rate, average winner, average loser

**Charts:**
- Equity curve: line chart showing end-of-day equity over time
- Daily PnL: bar chart showing each day's profit or loss (green for gains, red for losses)

**Trade Table:**
- Last 20 trades with: side (long/short), entry price, exit price, PnL, return %, hold time, exit reason
- Rows are colour-coded: green for winners, red for losers

### 8.4 Log Files

VRAB writes logs to `logs/vrab.log`. The log file rotates when it reaches 10 MB, keeping the 5 most recent files.

**What to look for:**
- `INFO` messages: Normal operation (candle processing, heartbeats, trade events)
- `WARNING` messages: Non-critical issues (WebSocket reconnects, cancelled orders, daily halts)
- `ERROR` messages: Problems that need attention (failed order placement, database errors)

Every candle logs the current market state: price, VWAP, sigma distance, ADX, trend direction, and the engine's decision (enter, exit, hold, or skip with reason).

---

## 9. Configuration Reference

All settings live in `config.py`. They are grouped by category below.

### Capital and Risk

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `CAPITAL_USDC` | 500.0 | Starting account balance in USDC | Yes — set to your actual capital |
| `RISK_PER_TRADE` | 0.015 (1.5%) | Maximum loss per trade as fraction of equity | Careful — higher = larger positions, larger drawdowns |
| `MAKER_ONLY` | True | Only use limit (maker) orders for entries | Leave as True |

### Leverage

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `TARGET_LEVERAGE` | 10 | Default leverage multiplier | Careful — higher leverage = closer liquidation |
| `MAX_LEVERAGE` | 20 | Absolute maximum leverage | Leave as safety cap |
| `MIN_LIQUIDATION_BUFFER` | 0.30 (30%) | Stop must be within 30% of entry-to-liquidation distance | Lower = riskier, higher = fewer trades |
| `MARGIN_UTILISATION_CAP` | 0.80 (80%) | Never use more than 80% of available margin | Leave as is |
| `HL_MAINTENANCE_MARGIN` | 0.005 (0.5%) | Hyperliquid's maintenance margin for BTC | Do not change — set by exchange |

### Strategy

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `VWAP_WINDOW` | 36 | Candles for VWAP calculation (36 × 5min = 3 hours) | Rerun backtest before changing |
| `VWAP_ENTRY_SIGMA` | 2.5 | Entry threshold in standard deviations from VWAP | Rerun backtest before changing |
| `VWAP_EXIT_SIGMA` | 0.0 | Exit target (0 = at VWAP) | Rerun backtest before changing |
| `VWAP_STOP_SIGMA` | 4.5 | Stop-loss in standard deviations from VWAP | Rerun backtest before changing |
| `ENTRY_EXPIRY_CANDLES` | 2 | Cancel unfilled entry after this many candles | Yes |
| `TREND_EMA_PERIOD` | 15 | Candles for trend EMA (Exponential Moving Average) | Rerun backtest before changing |
| `ADX_PERIOD` | 14 | Candles for ADX calculation | Rerun backtest before changing |
| `ADX_THRESHOLD` | 35.0 | Block entries when ADX exceeds this | Rerun backtest before changing |

### Risk Limits

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `MAX_DAILY_LOSS_MULTIPLIER` | 3 | Halt after losing 3× single-trade risk in a day | Yes — lower is more conservative |
| `MAX_DRAWDOWN_PCT` | 0.10 (10%) | Circuit breaker triggers at 10% drawdown from peak | Yes — lower is more conservative |
| `MAX_POSITION_HOLD_MINS` | 240 | Close position after 4 hours regardless | Yes |
| `MAX_OPEN_POSITIONS` | 1 | Only one trade at a time | Leave at 1 |
| `FUNDING_RATE_BLOCK` | 0.0003 (0.03%) | Block entries when hourly funding rate exceeds this | Yes |

### Transaction Costs

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `MAKER_REBATE_RATE` | 0.0002 (0.02%) | Rebate earned on limit orders | Only if Hyperliquid changes fees |
| `TAKER_FEE_RATE` | 0.00035 (0.035%) | Fee paid on market orders | Only if Hyperliquid changes fees |
| `TICK_SIZE` | 0.1 | Minimum price increment ($0.10) | Do not change — set by exchange |
| `SLIPPAGE_TICKS_ENTRY` | 1 | Assumed slippage on entry (1 × $0.10 = $0.10) | Yes — higher is more conservative |
| `SLIPPAGE_TICKS_STOP` | 3 | Assumed slippage on stop exit (3 × $0.10 = $0.30) | Yes — higher is more conservative |

### Backtest

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `BACKTEST_HOURLY_FUNDING_RATE` | 0.0001 (0.01%) | Assumed funding rate for backtesting | Yes — higher is more conservative |
| `BACKTEST_FILL_RATE` | 0.70 (70%) | Percentage of limit orders assumed to fill | Yes — lower is more conservative |

### Infrastructure

| Parameter | Value | Meaning | Safe to Change? |
|-----------|-------|---------|-----------------|
| `PAPER_MODE` | True | Run in simulated mode (no real money) | Set to False for live trading |
| `HEARTBEAT_INTERVAL_CANDLES` | 12 | Send status alert every 12 candles (1 hour) | Yes |
| `CANDLE_BACKFILL_COUNT` | 200 | Candles to fetch on startup per timeframe | Leave at 200 |
| `DB_PATH` | data/vrab.db | Location of the SQLite database | Yes — use absolute path if needed |
| `LOG_PATH` | logs/vrab.log | Location of the log file | Yes |
| `KILL_SWITCH_PATH` | /tmp/VRAB_KILL | Location of the kill switch file | Yes |

---

## 10. Operating Procedures

### 10.1 Pull Historical Data

Before running a backtest, you need historical price data. This command downloads 120 days of 5-minute BTC candles (and companion 15-minute candles automatically):

```bash
cd /home/will/vrab
.venv/bin/python -m data.puller --symbol BTC --tf 5m --days 120
```

The data is stored in `data/vrab.db`. If you run this again later, it only downloads new data — it picks up where it left off.

### 10.2 Run a Backtest

```bash
.venv/bin/python -m backtest.engine --symbol BTC --tf 5m --windows 3 --window-days 30
```

This runs the strategy on three 30-day windows and outputs per-window metrics plus the Gate 0 verdict.

### 10.3 Start Paper Trading

Make sure `PAPER_MODE = True` in `config.py` (this is the default), then:

```bash
.venv/bin/python -m live.engine
```

The engine starts, backfills candles, connects to the WebSocket, and begins processing candles. You will see log output every 5 minutes showing market state and decisions.

### 10.4 Start Live Trading

1. Set `PAPER_MODE = False` in `config.py`
2. Ensure your `.env` file contains:
   - `HL_PRIVATE_KEY=your_private_key`
   - `HL_WALLET_ADDRESS=your_wallet_address`
   - `TELEGRAM_TOKEN=your_bot_token`
   - `TELEGRAM_CHAT_ID=your_chat_id`
3. Set `CAPITAL_USDC` to your actual account balance
4. Run:

```bash
.venv/bin/python -m live.engine
```

### 10.5 Start the Dashboard

The dashboard is a separate process from the engine:

```bash
.venv/bin/python -m dashboard
```

Open `http://localhost:5555` in your browser (or `http://your-server-ip:5555` from another machine).

### 10.6 Stop the Bot

**Graceful (recommended):**
Press `Ctrl+C` once. The bot cancels orders, sends a shutdown alert, and exits cleanly.

**Emergency:**
Send `/kill` via Telegram. The engine halts on the next candle close (within 5 minutes).

**Forced:**
Press `Ctrl+C` twice for immediate exit (no cleanup).

### 10.7 Reset After Circuit Breaker

1. Review what caused the drawdown (check `/trades` and log files)
2. When satisfied, send `/reset` via Telegram
3. The circuit breaker clears, peak equity is set to the current balance, and trading resumes

### 10.8 Back Up the Database

```bash
cp data/vrab.db data/vrab.db.backup
```

The database is a single file. Copy it to create a backup. Safe to do while the engine is running (SQLite WAL mode supports concurrent reads).

---

## 11. What Can Go Wrong

### Exchange WebSocket Disconnects

**What happens:** The connection to Hyperliquid drops — no price data arrives.

**How VRAB handles it:** The engine detects silence (no events for 6 minutes) and automatically reconnects the WebSocket, re-backfills any missed candles, and re-subscribes to the feed. A Telegram alert is sent: "Feed Reconnected."

**Risk:** During the disconnection period (up to 6 minutes), the bot cannot process new candles. Existing stop-loss trigger orders on the exchange continue to protect open positions.

### Bot Crashes Mid-Position

**What happens:** The process dies unexpectedly while holding an open position.

**How VRAB handles it:** Position state is saved to the database on every entry. On restart, the engine loads the saved state, verifies it against the exchange (live mode), and re-places stop and target orders. A recovery alert is sent via Telegram.

**Risk:** Between crash and restart, only the exchange-side stop trigger order protects the position. The target limit order may have been cancelled during shutdown. The risk window is the time it takes to restart.

### Telegram Goes Down

**What happens:** The Telegram API is unreachable.

**How VRAB handles it:** All Telegram calls are non-blocking and wrapped in error handling. Trading continues normally. Alerts are lost but no trades are affected.

**Risk:** You will not receive alerts until Telegram recovers. Check the log file for events that happened during the outage.

### Database Issues

**What happens:** The SQLite file is corrupted or locked.

**How VRAB handles it:** SQLite in WAL mode is resilient to most crashes. However, if the file is corrupted, the engine will fail to start.

**Recovery:** Restore from backup: `cp data/vrab.db.backup data/vrab.db`. Some recent data may be lost. All trades recorded up to the backup point are preserved.

### Strategy Stops Working

**What happens:** Market conditions change and mean reversion no longer works. Trades consistently hit stop-losses. The account draws down.

**How VRAB handles it:** The circuit breaker fires at 10% drawdown from peak. The daily halt limits single-day losses. Both send alerts.

**What you should do:** If the circuit breaker fires repeatedly, do not just keep resetting. Review recent trades for patterns (are stops too tight? is ADX filtering correctly?). Consider pausing and re-running backtests on recent data before resuming.

### Funding Rate Spikes

**What happens:** An extreme market event causes funding rates to spike to 0.1% per hour or more. Holding a leveraged position becomes very expensive.

**How VRAB handles it:** The funding rate filter blocks new entries when the rate exceeds 0.03%. Existing positions have a 4-hour timeout, limiting funding exposure.

**Risk:** If a spike occurs while a position is already open and the funding direction is adverse, the 4-hour maximum hold limits the damage.

---

## 12. Glossary

**ADX (Average Directional Index):** A measure of trend strength on a scale from 0 to 100. Low ADX (under 20) means the market is range-bound. High ADX (over 35) means the market is trending strongly.

**Backtest:** Running a trading strategy on historical data to measure how it would have performed. Not a guarantee of future results, but a necessary validation step.

**Candle:** A summary of price action over a fixed time period. Contains four prices (open, high, low, close) and the volume traded. VRAB uses 5-minute candles for trading decisions and 15-minute candles for trend analysis.

**Circuit Breaker:** An automatic safety mechanism that stops all trading when the account drops more than 10% from its highest recorded balance. Requires manual reset via Telegram.

**Dead-Man Switch:** A safety mechanism on the exchange. VRAB tells Hyperliquid "cancel all my orders if I do not check in within 10 minutes." If the bot crashes, the exchange cancels pending orders automatically.

**Drawdown:** The decline from a peak balance to a subsequent low point. A 10% drawdown means the account has fallen 10% from its highest value.

**EMA (Exponential Moving Average):** A type of moving average that gives more weight to recent prices. VRAB uses a 15-candle EMA on the trend timeframe to gauge market direction.

**Equity:** The total value of the account, including unrealised gains or losses on open positions.

**Expectancy:** The average profit per trade, including losers. A positive expectancy means the strategy makes money on average over many trades.

**Funding Rate:** A periodic payment between long and short traders in perpetual futures markets. Keeps the futures price aligned with the spot price. Longs pay shorts when funding is positive; shorts pay longs when negative.

**Gate 0:** VRAB's minimum performance criteria that a backtest must pass before the strategy goes live. Includes thresholds for Sharpe ratio, drawdown, trade count, win rate, and expectancy.

**Kill Switch:** An emergency stop that halts all trading immediately. Can be triggered via Telegram (`/kill`) or by creating a file on the server.

**Leverage:** Borrowing from the exchange to control a larger position. 10x leverage means $500 of your own money controls $5,000 worth of Bitcoin. Amplifies both gains and losses.

**Limit Order:** An order to buy or sell at a specific price. It sits on the order book until the price reaches that level, or it is cancelled. Earns a maker rebate.

**Liquidation:** When the exchange forcibly closes your position because losses have consumed your margin. With 10x leverage, this happens at roughly 10% from entry. VRAB's liquidation buffer is designed to prevent this.

**Maker/Taker:** Makers add orders to the book (limit orders) and earn a rebate. Takers remove orders from the book (market orders) and pay a fee.

**Margin:** The collateral required to hold a leveraged position. With 10x leverage, the margin is 10% of the position size.

**Market Order:** An order to buy or sell immediately at the best available price. Used when speed matters more than price. Pays a taker fee.

**Mean Reversion:** The tendency of prices to return toward their average after an extreme move. VRAB's core strategy is based on this behaviour.

**PnL (Profit and Loss):** The money made or lost on a trade or over a period. "Net PnL" includes all costs (fees, slippage, funding). "Gross PnL" is the raw price difference only.

**Perpetual Futures:** A type of derivative contract that tracks the price of an asset (like Bitcoin) with no expiry date. Traders can go long (bet on price rising) or short (bet on price falling) with leverage.

**Position:** An active trade. "Long position" means you have bought and are waiting to sell higher. "Short position" means you have sold and are waiting to buy back lower.

**Sharpe Ratio:** A measure of risk-adjusted return. It divides the average return by the volatility (standard deviation) of returns. Higher is better. A Sharpe above 1.5 is considered good. Above 2.0 is very strong.

**Sigma (σ):** Standard deviation. In VRAB's context, it measures how far the current price is from the VWAP in units of standard deviation. ±2.5σ is the entry threshold.

**Slippage:** The difference between the intended trade price and the actual fill price. Caused by market movement between decision and execution.

**SQLite:** A lightweight database that stores all data in a single file. No separate server needed. VRAB uses it for all persistent storage.

**Stop-Loss:** An order that automatically closes a position if the price moves against you by a certain amount. Limits the maximum loss on a trade.

**Take-Profit:** An order that automatically closes a position when the price reaches your target. Locks in the intended gain.

**Tick:** The minimum price increment on the exchange. For BTC on Hyperliquid, one tick is $0.10.

**Trigger Order:** A conditional order on the exchange that activates when the price hits a specified level. VRAB uses trigger orders for stop-losses so they execute even if the bot is offline.

**VWAP (Volume Weighted Average Price):** The average price of an asset weighted by how much was traded at each price level. Prices with high volume count more. Think of it as the "true average" that reflects where most actual trading happened.

**WAL (Write-Ahead Logging):** A database mode that allows simultaneous reading and writing. The dashboard can read data while the engine writes to it without conflicts.

**Walk-Forward Testing:** A backtesting methodology that divides historical data into multiple sequential windows and tests each one independently. More rigorous than testing on a single period because it checks consistency across different market conditions.

**WebSocket:** A persistent, two-way communication channel between the bot and the exchange. Unlike regular web requests (ask, receive, disconnect), a WebSocket stays open and the exchange pushes new data as it arrives. VRAB uses this for real-time candle data.

---

*This document was generated from the VRAB codebase as of April 2026. For technical implementation details, see `docs/SYSTEM.md`. For development history, see `docs/DEVLOG.md`. For research notes and parameter rationale, see `docs/RESEARCH.md`.*
