"""Regime filtering research — analyze whether any backward-looking metric
separates winning from losing trades.

Usage:
    python -m backtest.regime_analysis
"""

import math
import sqlite3
import sys
from dataclasses import dataclass

from config import (
    DB_PATH, SYMBOL, CANDLE_TF, TREND_TF,
    CAPITAL_USDC, RISK_PER_TRADE, TARGET_LEVERAGE,
)
from backtest.engine import simulate_window

LOOKBACK_5M = 288  # 24h of 5m candles


@dataclass
class TradeWithRegime:
    """A backtest trade annotated with regime metrics at entry."""
    net_pnl: float
    entry_ts: int
    side: str
    # Regime metrics
    directional_move_24h: float  # abs % change over 24h
    realized_vol_24h: float      # annualized vol from 5m returns
    range_ratio_24h: float       # (high - low) / close over 24h
    vwap_bandwidth: float        # std_dev / vwap (relative band width)


def compute_regime_metrics(
    candles: list[dict], idx: int,
) -> dict | None:
    """Compute regime metrics at candle index using only backward-looking data."""
    if idx < LOOKBACK_5M:
        return None

    window = candles[idx - LOOKBACK_5M : idx + 1]
    closes = [c["close"] for c in window]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    volumes = [c["volume"] for c in window]

    current_close = closes[-1]
    past_close = closes[0]

    # 1. Directional move: abs % change over 24h
    directional_move = abs(current_close - past_close) / past_close

    # 2. Realized vol: std of log-returns, annualized
    log_returns = []
    for i in range(1, len(closes)):
        if closes[i] > 0 and closes[i - 1] > 0:
            log_returns.append(math.log(closes[i] / closes[i - 1]))
    if len(log_returns) < 2:
        return None
    mean_r = sum(log_returns) / len(log_returns)
    var_r = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
    realized_vol = math.sqrt(var_r) * math.sqrt(LOOKBACK_5M)

    # 3. Range ratio: (24h high - 24h low) / close
    range_ratio = (max(highs) - min(lows)) / current_close

    # 4. VWAP bandwidth: volume-weighted std / vwap
    total_vol = sum(volumes)
    if total_vol <= 0:
        return None
    vwap = sum(c * v for c, v in zip(closes, volumes)) / total_vol
    var_vwap = sum(v * (c - vwap) ** 2 for c, v in zip(closes, volumes)) / total_vol
    vwap_std = math.sqrt(var_vwap) if var_vwap > 0 else 0
    bandwidth = vwap_std / vwap if vwap > 0 else 0

    return {
        "directional_move_24h": directional_move,
        "realized_vol_24h": realized_vol,
        "range_ratio_24h": range_ratio,
        "vwap_bandwidth": bandwidth,
    }


def build_candle_index(candles: list[dict]) -> dict[int, int]:
    """Map timestamp → index for fast lookup."""
    return {c["ts"]: i for i, c in enumerate(candles)}


def print_quintile_analysis(name: str, trades: list[TradeWithRegime], key: str) -> None:
    """Print quintile breakdown for a given metric."""
    values = [(getattr(t, key), t) for t in trades]
    values.sort(key=lambda x: x[0])

    n = len(values)
    q_size = n // 5

    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")

    total_pnl_improvement = 0.0

    for q in range(5):
        start = q * q_size
        end = (q + 1) * q_size if q < 4 else n
        bucket = [t for _, t in values[start:end]]

        if not bucket:
            continue

        wins = sum(1 for t in bucket if t.net_pnl > 0)
        wr = wins / len(bucket)
        avg_pnl = sum(t.net_pnl for t in bucket) / len(bucket)
        total_pnl = sum(t.net_pnl for t in bucket)
        lo = values[start][0]
        hi = values[end - 1][0]

        print(
            f"  Q{q + 1} ({lo:.4f}–{hi:.4f}):  "
            f"{len(bucket):3d} trades, WR {wr:5.1%}, "
            f"avg {avg_pnl:+7.2f}, total {total_pnl:+8.2f}"
        )

        if q == 4:  # worst quintile
            total_pnl_improvement = -total_pnl

    # Correlation: Spearman rank correlation between metric and PnL
    from itertools import count
    ranked_metric = list(range(n))
    pnl_vals = [t.net_pnl for _, t in values]
    pnl_ranked = sorted(range(n), key=lambda i: pnl_vals[i])
    pnl_ranks = [0] * n
    for rank, idx in enumerate(pnl_ranked):
        pnl_ranks[idx] = rank

    mean_m = (n - 1) / 2
    mean_p = (n - 1) / 2
    cov = sum((i - mean_m) * (pnl_ranks[i] - mean_p) for i in range(n))
    var_m = sum((i - mean_m) ** 2 for i in range(n))
    var_p = sum((r - mean_p) ** 2 for r in pnl_ranks)
    denom = math.sqrt(var_m * var_p) if var_m > 0 and var_p > 0 else 1
    spearman = cov / denom

    signal = "NONE"
    if abs(spearman) >= 0.15:
        signal = "MODERATE"
    if abs(spearman) >= 0.25:
        signal = "STRONG"

    direction = "↑ higher metric = better PnL" if spearman > 0 else "↓ higher metric = worse PnL"
    print(f"\n  Spearman ρ: {spearman:+.3f} ({direction})")
    print(f"  Signal: {signal}")
    if total_pnl_improvement > 0:
        print(f"  Filtering Q5 would save: +${total_pnl_improvement:.2f}")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Load full data range
    row = conn.execute(
        "SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM candles WHERE symbol = ? AND tf = ?",
        (SYMBOL, CANDLE_TF),
    ).fetchone()

    if not row or not row["max_ts"]:
        print(f"No data for {SYMBOL} {CANDLE_TF}")
        return

    min_ts, max_ts = row["min_ts"], row["max_ts"]

    # Load all candles
    primary_rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol = ? AND tf = ? ORDER BY ts ASC""",
        (SYMBOL, CANDLE_TF),
    ).fetchall()
    primary_candles = [dict(r) for r in primary_rows]

    trend_rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol = ? AND tf = ? ORDER BY ts ASC""",
        (SYMBOL, TREND_TF),
    ).fetchall()
    trend_candles = [dict(r) for r in trend_rows]

    conn.close()

    print(f"Loaded {len(primary_candles)} primary ({CANDLE_TF}) candles")
    print(f"Loaded {len(trend_candles)} trend ({TREND_TF}) candles")

    # Run backtest to get trades
    print("\nRunning 365-day backtest...")
    trades, halts, liq_blocks = simulate_window(
        primary_candles, trend_candles,
        CAPITAL_USDC, RISK_PER_TRADE, TARGET_LEVERAGE,
    )
    print(f"Got {len(trades)} trades, {halts} halts")

    # Build candle timestamp index
    ts_index = build_candle_index(primary_candles)

    # Annotate each trade with regime metrics
    annotated: list[TradeWithRegime] = []
    skipped = 0
    for t in trades:
        idx = ts_index.get(t.entry_ts)
        if idx is None:
            # Find nearest candle
            diffs = [(abs(c["ts"] - t.entry_ts), i) for i, c in enumerate(primary_candles)]
            _, idx = min(diffs)

        metrics = compute_regime_metrics(primary_candles, idx)
        if metrics is None:
            skipped += 1
            continue

        annotated.append(TradeWithRegime(
            net_pnl=t.net_pnl,
            entry_ts=t.entry_ts,
            side=t.side,
            **metrics,
        ))

    print(f"Annotated {len(annotated)} trades ({skipped} skipped — insufficient lookback)")

    if len(annotated) < 25:
        print("Too few trades for meaningful analysis")
        return

    # Run quintile analysis for each metric
    print_quintile_analysis(
        "24h Directional Move (abs % change)",
        annotated, "directional_move_24h",
    )
    print_quintile_analysis(
        "24h Realized Volatility (annualized)",
        annotated, "realized_vol_24h",
    )
    print_quintile_analysis(
        "24h High-Low Range / Close",
        annotated, "range_ratio_24h",
    )
    print_quintile_analysis(
        "VWAP Bandwidth (std / vwap)",
        annotated, "vwap_bandwidth",
    )

    # Summary
    print(f"\n{'=' * 60}")
    print("  VERDICT")
    print(f"{'=' * 60}")
    total_pnl = sum(t.net_pnl for t in annotated)
    wins = sum(1 for t in annotated if t.net_pnl > 0)
    print(f"  Total trades: {len(annotated)}")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    print(f"  Win rate: {wins / len(annotated):.1%}")
    print(f"\n  Look for metrics with STRONG signal and monotonic quintile PnL.")
    print(f"  If found → implement as regime gate in strategy/signals.py")
    print(f"  If not → accept DD as cost of doing business\n")


if __name__ == "__main__":
    main()
