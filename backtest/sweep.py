"""Parameter sweep for VRAB strategy.

CLI: python -m backtest.sweep --db data/vrab.db
"""

import argparse
import itertools
import sqlite3
import time
from datetime import datetime, timezone

from backtest.engine import simulate_window, WindowResult
from strategy.core import TradingParams
from config import (
    CAPITAL_USDC, RISK_PER_TRADE, TARGET_LEVERAGE, DB_PATH,
    VWAP_EXIT_SIGMA, TREND_EMA_PERIOD, ADX_PERIOD, ADX_THRESHOLD,
    FUNDING_RATE_BLOCK,
    MAX_LEVERAGE, MIN_LIQUIDATION_BUFFER, MARGIN_UTILISATION_CAP,
    HL_MAINTENANCE_MARGIN, MAKER_REBATE_RATE, TAKER_FEE_RATE,
    TICK_SIZE, SLIPPAGE_TICKS_ENTRY, SLIPPAGE_TICKS_STOP,
    MAX_DAILY_LOSS_MULTIPLIER, BACKTEST_HOURLY_FUNDING_RATE,
    ENTRY_EXPIRY_CANDLES,
)


# Parameter grid — trimmed to viable space
PARAM_GRID = {
    "entry_sigma": [2.0, 2.5, 3.0],
    "stop_sigma": [3.5, 4.5, 6.0],
    "vwap_window": [36, 48, 72, 96],     # 3h, 4h, 6h, 8h
    "adx_threshold": [15.0, 20.0, 30.0],
}


def _make_params(entry_sigma, stop_sigma, vwap_window, adx_threshold) -> TradingParams:
    """Build TradingParams with sweep overrides."""
    return TradingParams(
        vwap_window=vwap_window,
        entry_sigma=entry_sigma,
        exit_sigma=VWAP_EXIT_SIGMA,
        stop_sigma=stop_sigma,
        ema_period=TREND_EMA_PERIOD,
        adx_period=ADX_PERIOD,
        adx_threshold=adx_threshold,
        funding_block_threshold=FUNDING_RATE_BLOCK,
        risk_per_trade=RISK_PER_TRADE,
        target_leverage=TARGET_LEVERAGE,
        max_leverage=MAX_LEVERAGE,
        min_liquidation_buffer=MIN_LIQUIDATION_BUFFER,
        margin_utilisation_cap=MARGIN_UTILISATION_CAP,
        maintenance_margin_rate=HL_MAINTENANCE_MARGIN,
        maker_rebate_rate=MAKER_REBATE_RATE,
        taker_fee_rate=TAKER_FEE_RATE,
        tick_size=TICK_SIZE,
        slippage_ticks_entry=SLIPPAGE_TICKS_ENTRY,
        slippage_ticks_stop=SLIPPAGE_TICKS_STOP,
        max_daily_loss_multiplier=MAX_DAILY_LOSS_MULTIPLIER,
        max_hold_candles=48,
        hourly_funding_rate=BACKTEST_HOURLY_FUNDING_RATE,
        entry_expiry_candles=ENTRY_EXPIRY_CANDLES,
    )


def run_sweep(db_path: str, window_days: int = 120, symbol: str = "BTC"):
    """Run parameter sweep over the most recent window_days of data."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT MAX(ts) as max_ts FROM candles WHERE symbol=? AND tf='5m'",
        (symbol,),
    ).fetchone()
    if not row or not row["max_ts"]:
        print("No data found")
        return

    end_ts = row["max_ts"]
    start_ts = end_ts - (window_days * 86_400_000)

    primary_rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol=? AND tf='5m' AND ts >= ? AND ts <= ?
           ORDER BY ts ASC""",
        (symbol, start_ts, end_ts),
    ).fetchall()

    trend_start = start_ts - (window_days * 86_400_000)
    trend_rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol=? AND tf='15m' AND ts >= ? AND ts <= ?
           ORDER BY ts ASC""",
        (symbol, trend_start, end_ts),
    ).fetchall()

    primary = [dict(r) for r in primary_rows]
    trend = [dict(r) for r in trend_rows]
    conn.close()

    combos = [(e, s, v, a) for e, s, v, a in itertools.product(*PARAM_GRID.values()) if s > e]

    print(f"[{symbol}] Data: {len(primary)} primary candles, {len(trend)} trend candles")
    print(f"Period: {datetime.fromtimestamp(start_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')} → "
          f"{datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"Sweeping {len(combos)} combinations...\n")

    results = []
    t_start = time.time()

    for idx, (entry_sig, stop_sig, vwap_win, adx_thresh) in enumerate(combos):
        params = _make_params(entry_sig, stop_sig, vwap_win, adx_thresh)

        trades, halts, liq_blocks = simulate_window(
            primary, trend, CAPITAL_USDC, RISK_PER_TRADE, TARGET_LEVERAGE,
            params_override=params,
        )

        wr = WindowResult(
            window_idx=0, start_ts=start_ts, end_ts=end_ts,
            trades=trades, halt_count=halts, liq_blocked_count=liq_blocks,
            _window_days=window_days,
        )

        results.append({
            "entry_sigma": entry_sig,
            "stop_sigma": stop_sig,
            "vwap_window": vwap_win,
            "adx_threshold": adx_thresh,
            "n_trades": wr.n_trades,
            "net_pnl": wr.net_pnl,
            "win_rate": wr.win_rate,
            "sharpe": wr.sharpe,
            "max_dd": wr.max_drawdown,
            "expectancy": wr.expectancy,
            "halts": halts,
        })

    elapsed = time.time() - t_start

    # Sort by net_pnl descending
    results.sort(key=lambda r: r["net_pnl"], reverse=True)

    print(f"{'Entry':>6} {'Stop':>5} {'VWAP':>5} {'ADX':>5} | {'Trades':>6} {'NetPnL':>9} {'WinR':>6} {'Sharpe':>7} {'MaxDD':>7} {'Expect':>8} {'Halts':>5}")
    print("-" * 100)

    for r in results:
        flag = "*" if (r["win_rate"] >= 0.45 and r["net_pnl"] > 0 and r["max_dd"] < 0.15) else " "
        print(f"{r['entry_sigma']:>5.1f}s {r['stop_sigma']:>4.1f}s {r['vwap_window']:>5} {r['adx_threshold']:>5.0f} | "
              f"{r['n_trades']:>6} {r['net_pnl']:>9.2f} {r['win_rate']:>5.1%} {r['sharpe']:>7.2f} {r['max_dd']:>6.2%} "
              f"{r['expectancy']:>8.4f} {r['halts']:>5} {flag}")

    profitable = [r for r in results if r["net_pnl"] > 0]
    viable = [r for r in results if r["win_rate"] >= 0.45 and r["net_pnl"] > 0 and r["max_dd"] < 0.15]

    print(f"\n{'='*60}")
    print(f"Completed in {elapsed:.1f}s")
    print(f"Total configs: {len(results)}")
    print(f"Profitable:    {len(profitable)}/{len(results)}")
    print(f"Viable (WR>45%, DD<15%): {len(viable)}/{len(results)}")

    if viable:
        best = viable[0]
        print(f"\nBest viable:")
        print(f"  Entry: {best['entry_sigma']}s | Stop: {best['stop_sigma']}s | VWAP: {best['vwap_window']} | ADX: {best['adx_threshold']}")
        print(f"  Trades: {best['n_trades']} | PnL: ${best['net_pnl']:.2f} | WR: {best['win_rate']:.1%} | Sharpe: {best['sharpe']:.2f} | DD: {best['max_dd']:.2%}")
    elif profitable:
        best = profitable[0]
        print(f"\nBest profitable (not viable):")
        print(f"  Entry: {best['entry_sigma']}s | Stop: {best['stop_sigma']}s | VWAP: {best['vwap_window']} | ADX: {best['adx_threshold']}")
        print(f"  Trades: {best['n_trades']} | PnL: ${best['net_pnl']:.2f} | WR: {best['win_rate']:.1%} | Sharpe: {best['sharpe']:.2f} | DD: {best['max_dd']:.2%}")
    else:
        print(f"\nNo profitable config. Strategy thesis needs rework.")

    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# EMA Cross Sweep
# ---------------------------------------------------------------------------

EMA_GRID = {
    "fast_ema": [9, 21],
    "slow_ema": [21, 50],
    "atr_stop_mult": [1.0, 1.5, 2.5],
    "rr_ratio": [1.5, 2.0, 3.0],
    "vol_filter_mult": [0.0, 1.2],          # 0 = no filter
}


def _make_ema_params(fast_ema, slow_ema, atr_stop_mult, rr_ratio, vol_filter_mult) -> TradingParams:
    """Build TradingParams for EMA cross strategy."""
    # vwap_window controls data slice in engine — need enough for EMA warm-up
    data_window = max(slow_ema * 3, 80)
    return TradingParams(
        vwap_window=data_window,
        entry_sigma=2.5,
        exit_sigma=0.0,
        stop_sigma=4.5,
        ema_period=TREND_EMA_PERIOD,
        adx_period=ADX_PERIOD,
        adx_threshold=ADX_THRESHOLD,
        funding_block_threshold=FUNDING_RATE_BLOCK,
        # Shared risk/cost params
        risk_per_trade=RISK_PER_TRADE,
        target_leverage=TARGET_LEVERAGE,
        max_leverage=MAX_LEVERAGE,
        min_liquidation_buffer=MIN_LIQUIDATION_BUFFER,
        margin_utilisation_cap=MARGIN_UTILISATION_CAP,
        maintenance_margin_rate=HL_MAINTENANCE_MARGIN,
        maker_rebate_rate=MAKER_REBATE_RATE,
        taker_fee_rate=TAKER_FEE_RATE,
        tick_size=TICK_SIZE,
        slippage_ticks_entry=SLIPPAGE_TICKS_ENTRY,
        slippage_ticks_stop=SLIPPAGE_TICKS_STOP,
        max_daily_loss_multiplier=MAX_DAILY_LOSS_MULTIPLIER,
        max_hold_candles=96,  # trend following holds longer
        hourly_funding_rate=BACKTEST_HOURLY_FUNDING_RATE,
        entry_expiry_candles=ENTRY_EXPIRY_CANDLES,
        # EMA cross params
        signal_mode="ema_cross",
        fast_ema_period=fast_ema,
        slow_ema_period=slow_ema,
        atr_period=14,
        atr_stop_mult=atr_stop_mult,
        rr_ratio=rr_ratio,
        vol_ma_period=20,
        vol_filter_mult=vol_filter_mult,
    )


def run_ema_sweep(db_path: str, window_days: int = 180, symbol: str = "BTC"):
    """Run parameter sweep for EMA cross strategy."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT MAX(ts) as max_ts FROM candles WHERE symbol=? AND tf='5m'",
        (symbol,),
    ).fetchone()
    if not row or not row["max_ts"]:
        print("No data found")
        return

    end_ts = row["max_ts"]
    start_ts = end_ts - (window_days * 86_400_000)

    primary_rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol=? AND tf='5m' AND ts >= ? AND ts <= ?
           ORDER BY ts ASC""",
        (symbol, start_ts, end_ts),
    ).fetchall()

    trend_start = start_ts - (window_days * 86_400_000)
    trend_rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol=? AND tf='15m' AND ts >= ? AND ts <= ?
           ORDER BY ts ASC""",
        (symbol, trend_start, end_ts),
    ).fetchall()

    primary = [dict(r) for r in primary_rows]
    trend = [dict(r) for r in trend_rows]
    conn.close()

    # Filter: slow must be > fast
    combos = [
        (f, s, a, rr, v)
        for f, s, a, rr, v in itertools.product(*EMA_GRID.values())
        if s > f
    ]

    print(f"[{symbol}] Data: {len(primary)} primary candles, {len(trend)} trend candles")
    print(f"Period: {datetime.fromtimestamp(start_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')} → "
          f"{datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"Sweeping {len(combos)} EMA cross combinations...\n")

    results = []
    t_start = time.time()

    for idx, (fast, slow, atr_mult, rr, vol_mult) in enumerate(combos):
        params = _make_ema_params(fast, slow, atr_mult, rr, vol_mult)

        trades, halts, liq_blocks = simulate_window(
            primary, trend, CAPITAL_USDC, RISK_PER_TRADE, TARGET_LEVERAGE,
            params_override=params,
        )

        wr = WindowResult(
            window_idx=0, start_ts=start_ts, end_ts=end_ts,
            trades=trades, halt_count=halts, liq_blocked_count=liq_blocks,
            _window_days=window_days,
        )

        results.append({
            "fast": fast, "slow": slow, "atr_mult": atr_mult,
            "rr": rr, "vol_mult": vol_mult,
            "n_trades": wr.n_trades, "net_pnl": wr.net_pnl,
            "win_rate": wr.win_rate, "sharpe": wr.sharpe,
            "max_dd": wr.max_drawdown, "expectancy": wr.expectancy,
            "halts": halts,
        })

    elapsed = time.time() - t_start
    results.sort(key=lambda r: r["net_pnl"], reverse=True)

    print(f"{'Fast':>5} {'Slow':>5} {'ATR×':>5} {'R:R':>5} {'VolF':>5} | "
          f"{'Trades':>6} {'NetPnL':>9} {'WinR':>6} {'Sharpe':>7} {'MaxDD':>7} {'Expect':>8} {'Halts':>5}")
    print("-" * 105)

    for r in results:
        flag = "*" if (r["win_rate"] >= 0.45 and r["net_pnl"] > 0 and r["max_dd"] < 0.15) else " "
        print(f"{r['fast']:>5} {r['slow']:>5} {r['atr_mult']:>5.1f} {r['rr']:>5.1f} {r['vol_mult']:>5.1f} | "
              f"{r['n_trades']:>6} {r['net_pnl']:>9.2f} {r['win_rate']:>5.1%} {r['sharpe']:>7.2f} {r['max_dd']:>6.2%} "
              f"{r['expectancy']:>8.4f} {r['halts']:>5} {flag}")

    profitable = [r for r in results if r["net_pnl"] > 0]
    viable = [r for r in results if r["win_rate"] >= 0.45 and r["net_pnl"] > 0 and r["max_dd"] < 0.15]

    print(f"\n{'='*60}")
    print(f"Completed in {elapsed:.1f}s")
    print(f"Total configs: {len(results)}")
    print(f"Profitable:    {len(profitable)}/{len(results)}")
    print(f"Viable (WR>45%, DD<15%): {len(viable)}/{len(results)}")

    if viable:
        best = viable[0]
        print(f"\nBest viable:")
        print(f"  Fast: {best['fast']} | Slow: {best['slow']} | ATR×: {best['atr_mult']} | R:R: {best['rr']} | VolF: {best['vol_mult']}")
        print(f"  Trades: {best['n_trades']} | PnL: ${best['net_pnl']:.2f} | WR: {best['win_rate']:.1%} | Sharpe: {best['sharpe']:.2f} | DD: {best['max_dd']:.2%}")
    elif profitable:
        best = profitable[0]
        print(f"\nBest profitable (not viable):")
        print(f"  Fast: {best['fast']} | Slow: {best['slow']} | ATR×: {best['atr_mult']} | R:R: {best['rr']} | VolF: {best['vol_mult']}")
        print(f"  Trades: {best['n_trades']} | PnL: ${best['net_pnl']:.2f} | WR: {best['win_rate']:.1%} | Sharpe: {best['sharpe']:.2f} | DD: {best['max_dd']:.2%}")
    else:
        print(f"\nNo profitable config.")

    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VRAB Parameter Sweep")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--mode", default="vwap", choices=["vwap", "ema"],
                        help="Strategy to sweep: vwap or ema")
    args = parser.parse_args()

    from logging_config import setup_logging
    import logging
    setup_logging(level=logging.WARNING)

    if args.mode == "ema":
        run_ema_sweep(args.db, args.days, args.symbol)
    else:
        run_sweep(args.db, args.days, args.symbol)
