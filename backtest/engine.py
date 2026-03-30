"""Walk-forward backtest engine — thin adapter over strategy.core.

CLI: python -m backtest.engine --symbol BTC --tf 5m --windows 3 --window-days 30
"""

import argparse
import logging
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import (
    VWAP_WINDOW, VWAP_ENTRY_SIGMA, VWAP_EXIT_SIGMA, VWAP_STOP_SIGMA,
    TREND_EMA_PERIOD, ADX_PERIOD, ADX_THRESHOLD, FUNDING_RATE_BLOCK,
    BACKTEST_HOURLY_FUNDING_RATE, BACKTEST_FILL_RATE,
    CAPITAL_USDC, RISK_PER_TRADE,
    MAKER_REBATE_RATE, TAKER_FEE_RATE, TICK_SIZE,
    SLIPPAGE_TICKS_ENTRY, SLIPPAGE_TICKS_STOP,
    TARGET_LEVERAGE, MAX_LEVERAGE, MIN_LIQUIDATION_BUFFER,
    HL_MAINTENANCE_MARGIN, MARGIN_UTILISATION_CAP,
    MAX_DAILY_LOSS_MULTIPLIER, ENTRY_EXPIRY_CANDLES,
    DB_PATH,
    GATE0_MIN_SHARPE, GATE0_MAX_DD, GATE0_MIN_TRADES,
    GATE0_MIN_WIN_RATE, GATE0_MIN_EXPECTANCY,
    GATE0_MAX_LIQ_BLOCK_RATIO, GATE0_MAX_HALTS,
)
from strategy.core import (
    TradingParams, CoreDecision, TradeSetup, ExitAction,
    evaluate_entry, evaluate_exit, calc_position_size,
    calc_trade_pnl, check_daily_halt,
)
from strategy.signals import generate_signal
from costs.model import calc_fill_price, calc_maker_rebate, calc_taker_fee, calc_funding_cost
from risk.liquidation import (
    calc_liquidation_price, is_stop_safe, calc_funding_at_leverage,
)

logger = logging.getLogger(__name__)


@dataclass
class BTTrade:
    """Backtest trade with full context."""
    side: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    size_usd: float
    notional_usd: float
    leverage: float
    liq_price: float
    exit_reason: str
    pnl_usd: float = 0.0
    slippage_usd: float = 0.0
    entry_fee_usd: float = 0.0
    exit_fee_usd: float = 0.0
    funding_usd: float = 0.0
    maker_rebate_usd: float = 0.0
    equity_return_pct: float = 0.0
    # Enhanced context
    stop_price: float = 0.0
    target_price: float = 0.0
    vwap_at_entry: float = 0.0
    sigma_at_entry: float = 0.0
    liq_buffer_ratio: float = 0.0
    equity_at_entry: float = 0.0
    adx_at_entry: float = 0.0
    ema_at_entry: float = 0.0
    trend_direction_at_entry: str = ""
    regime_trending_at_entry: int = 0
    vwap_std_dev_at_entry: float = 0.0
    volume_at_entry: float = 0.0
    hold_candles: int = 0

    @property
    def net_pnl(self) -> float:
        """Net PnL including all costs."""
        return (self.pnl_usd + self.slippage_usd + self.entry_fee_usd
                + self.exit_fee_usd + self.funding_usd + self.maker_rebate_usd)


@dataclass
class WindowResult:
    """Results from one walk-forward window."""
    window_idx: int
    start_ts: int
    end_ts: int
    trades: list[BTTrade]
    halt_count: int
    liq_blocked_count: int
    _window_days: int = 30

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.net_pnl > 0)
        return wins / len(self.trades)

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough drawdown as positive fraction."""
        if not self.trades:
            return 0.0
        equity = CAPITAL_USDC
        peak = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t.net_pnl
            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe(self) -> float:
        """Annualized Sharpe ratio from per-trade returns."""
        if len(self.trades) < 2:
            return 0.0
        returns = [t.equity_return_pct for t in self.trades]
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.0
        if std_r == 0:
            return 0.0
        trades_per_day = len(self.trades) / max(self._window_days, 1)
        annualization = math.sqrt(252 * trades_per_day)
        return (mean_r / std_r) * annualization

    @property
    def expectancy(self) -> float:
        """Average net PnL per trade."""
        if not self.trades:
            return 0.0
        return self.net_pnl / len(self.trades)

    def cost_breakdown(self) -> dict:
        """Aggregate cost breakdown across all trades."""
        total_slippage = sum(t.slippage_usd for t in self.trades)
        total_entry_fees = sum(t.entry_fee_usd for t in self.trades)
        total_exit_fees = sum(t.exit_fee_usd for t in self.trades)
        total_funding = sum(t.funding_usd for t in self.trades)
        total_rebate = sum(t.maker_rebate_usd for t in self.trades)
        net_cost = total_slippage + total_entry_fees + total_exit_fees + total_funding
        return {
            "total_slippage_usd": total_slippage,
            "total_entry_fees_usd": total_entry_fees,
            "total_exit_fees_usd": total_exit_fees,
            "total_funding_usd": total_funding,
            "total_maker_rebate_usd": total_rebate,
            "net_cost_usd": net_cost,
            "cost_as_pct_of_gross_pnl": (
                abs(net_cost / self.gross_pnl) if self.gross_pnl != 0 else 0.0
            ),
            "avg_cost_per_trade_usd": (
                net_cost / len(self.trades) if self.trades else 0.0
            ),
        }

    def passed_gate_0(self) -> tuple[bool, list[str]]:
        """Gate 0 validation. Returns (passed, failure_reasons)."""
        failures = []
        if self.sharpe < GATE0_MIN_SHARPE:
            failures.append(f"sharpe={self.sharpe:.2f} < {GATE0_MIN_SHARPE}")
        if self.max_drawdown > GATE0_MAX_DD:
            failures.append(f"max_dd={self.max_drawdown:.2%} > {GATE0_MAX_DD:.0%}")
        if self.n_trades < GATE0_MIN_TRADES:
            failures.append(f"insufficient_trades n={self.n_trades} < {GATE0_MIN_TRADES}")
        if self.expectancy <= GATE0_MIN_EXPECTANCY:
            failures.append(f"expectancy={self.expectancy:.4f} <= {GATE0_MIN_EXPECTANCY}")
        if self.win_rate < GATE0_MIN_WIN_RATE:
            failures.append(f"win_rate={self.win_rate:.2%} < {GATE0_MIN_WIN_RATE:.0%}")

        total_signals = self.n_trades + self.liq_blocked_count
        if total_signals > 0 and self.liq_blocked_count / total_signals > GATE0_MAX_LIQ_BLOCK_RATIO:
            failures.append(
                f"liq_blocked={self.liq_blocked_count}/{total_signals} > {GATE0_MAX_LIQ_BLOCK_RATIO:.0%}"
            )
        if self.halt_count > GATE0_MAX_HALTS:
            failures.append(f"halt_count={self.halt_count} > {GATE0_MAX_HALTS}")

        return len(failures) == 0, failures

    def summary(self) -> str:
        """Human-readable summary."""
        passed, reasons = self.passed_gate_0()
        costs = self.cost_breakdown()

        best = max(self.trades, key=lambda t: t.equity_return_pct) if self.trades else None
        worst = min(self.trades, key=lambda t: t.equity_return_pct) if self.trades else None

        lines = [
            f"\n{'='*60}",
            f"Window {self.window_idx}",
            f"  Period: {datetime.fromtimestamp(self.start_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')} → "
            f"{datetime.fromtimestamp(self.end_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}",
            f"  Trades: {self.n_trades}",
            f"  Net PnL: ${self.net_pnl:.2f} | Gross: ${self.gross_pnl:.2f}",
            f"  Win Rate: {self.win_rate:.1%}",
            f"  Sharpe: {self.sharpe:.2f}",
            f"  Max DD: {self.max_drawdown:.2%}",
            f"  Expectancy: ${self.expectancy:.4f}",
            f"  Halts: {self.halt_count} | Liq Blocks: {self.liq_blocked_count}",
            f"\n  Cost Breakdown:",
            f"    Slippage:     ${costs['total_slippage_usd']:.2f}",
            f"    Entry Fees:   ${costs['total_entry_fees_usd']:.2f}",
            f"    Exit Fees:    ${costs['total_exit_fees_usd']:.2f}",
            f"    Funding:      ${costs['total_funding_usd']:.2f}",
            f"    Maker Rebate: ${costs['total_maker_rebate_usd']:.2f}",
            f"    Net Cost:     ${costs['net_cost_usd']:.2f}",
            f"    Avg/Trade:    ${costs['avg_cost_per_trade_usd']:.4f}",
        ]

        if best:
            lines.append(f"\n  Best Trade:  {best.equity_return_pct:+.2%} ({best.side})")
        if worst:
            lines.append(f"  Worst Trade: {worst.equity_return_pct:+.2%} ({worst.side})")

        gate_str = "PASS" if passed else f"FAIL: {', '.join(reasons)}"
        lines.append(f"\n  Gate 0: {gate_str}")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


def aggregate_gate_0(
    results: list["WindowResult"],
    total_days: int,
) -> tuple[bool, list[str]]:
    """Gate 0 on aggregate metrics across all windows.

    Uses the same thresholds as per-window but applied to the
    combined equity curve and trade list.
    """
    all_trades = [t for r in results for t in r.trades]
    n_trades = len(all_trades)
    total_halts = sum(r.halt_count for r in results)
    total_liq_blocks = sum(r.liq_blocked_count for r in results)

    if not all_trades:
        return False, ["no_trades"]

    net_pnl = sum(t.net_pnl for t in all_trades)
    wins = sum(1 for t in all_trades if t.net_pnl > 0)
    win_rate = wins / n_trades
    expectancy = net_pnl / n_trades

    # Max drawdown on combined equity curve
    equity = CAPITAL_USDC
    peak = equity
    max_dd = 0.0
    for t in all_trades:
        equity += t.net_pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Annualised Sharpe on combined trade returns
    sharpe = 0.0
    if n_trades >= 2:
        returns = [t.equity_return_pct for t in all_trades]
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.0
        if std_r > 0:
            trades_per_day = n_trades / max(total_days, 1)
            sharpe = (mean_r / std_r) * math.sqrt(252 * trades_per_day)

    failures = []
    if sharpe < GATE0_MIN_SHARPE:
        failures.append(f"sharpe={sharpe:.2f} < {GATE0_MIN_SHARPE}")
    if max_dd > GATE0_MAX_DD:
        failures.append(f"max_dd={max_dd:.2%} > {GATE0_MAX_DD:.0%}")
    if n_trades < GATE0_MIN_TRADES * len(results):
        failures.append(f"trades={n_trades} < {GATE0_MIN_TRADES * len(results)}")
    if expectancy <= GATE0_MIN_EXPECTANCY:
        failures.append(f"expectancy={expectancy:.4f} <= {GATE0_MIN_EXPECTANCY}")
    if win_rate < GATE0_MIN_WIN_RATE:
        failures.append(f"win_rate={win_rate:.2%} < {GATE0_MIN_WIN_RATE:.0%}")

    total_signals = n_trades + total_liq_blocks
    if total_signals > 0 and total_liq_blocks / total_signals > GATE0_MAX_LIQ_BLOCK_RATIO:
        failures.append(
            f"liq_blocked={total_liq_blocks}/{total_signals} > {GATE0_MAX_LIQ_BLOCK_RATIO:.0%}"
        )
    if total_halts > GATE0_MAX_HALTS * len(results):
        failures.append(f"halt_count={total_halts} > {GATE0_MAX_HALTS * len(results)}")

    return len(failures) == 0, failures


def simulate_window(
    primary_candles: list[dict],
    trend_candles: list[dict],
    capital: float,
    risk_per_trade: float,
    leverage: int,
    max_hold_candles: int = 48,
    params_override: TradingParams | None = None,
) -> tuple[list[BTTrade], int, int]:
    """Simulate one walk-forward window.

    Uses strategy.core for all decisions. Only handles fill simulation
    and equity tracking.

    Args:
        params_override: If provided, use these params instead of building from config.

    Returns (trades, halt_count, liq_blocked_count).
    """
    random.seed(42)

    if params_override is not None:
        params = params_override
    else:
        params = TradingParams(
            vwap_window=VWAP_WINDOW,
            entry_sigma=VWAP_ENTRY_SIGMA,
            exit_sigma=VWAP_EXIT_SIGMA,
            stop_sigma=VWAP_STOP_SIGMA,
            ema_period=TREND_EMA_PERIOD,
            adx_period=ADX_PERIOD,
            adx_threshold=ADX_THRESHOLD,
            funding_block_threshold=FUNDING_RATE_BLOCK,
            risk_per_trade=risk_per_trade,
            target_leverage=leverage,
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
            max_hold_candles=max_hold_candles,
            hourly_funding_rate=BACKTEST_HOURLY_FUNDING_RATE,
            entry_expiry_candles=ENTRY_EXPIRY_CANDLES,
        )

    trades: list[BTTrade] = []
    halt_count = 0
    liq_blocked_count = 0
    equity = capital

    # Position state
    position_side: str | None = None
    position_entry_price = 0.0
    position_stop_price = 0.0
    position_target_price = 0.0
    position_entry_ts = 0
    position_size_usd = 0.0
    position_liq_price = 0.0
    position_liq_buffer = 0.0
    position_equity_at_entry = 0.0
    position_hold_candles = 0
    position_signal_context: dict = {}

    # Daily halt tracking
    current_day: str | None = None
    daily_pnl = 0.0
    halted_today = False

    # Entry expiry tracking
    pending_signal_dir: str | None = None
    pending_signal_count = 0

    # Pre-extract arrays once — O(n) instead of O(n²)
    all_closes = [c["close"] for c in primary_candles]
    all_highs = [c["high"] for c in primary_candles]
    all_lows = [c["low"] for c in primary_candles]
    all_volumes = [c["volume"] for c in primary_candles]
    all_ts = [c["ts"] for c in primary_candles]

    # Pre-extract trend arrays and timestamps for binary search
    trend_ts_arr = [c["ts"] for c in trend_candles]
    trend_closes_arr = [c["close"] for c in trend_candles]
    trend_highs_arr = [c["high"] for c in trend_candles]
    trend_lows_arr = [c["low"] for c in trend_candles]

    import bisect

    vwap_win = params.vwap_window
    for i in range(vwap_win, len(primary_candles)):
        candle = primary_candles[i]
        candle_ts = all_ts[i]
        candle_day = datetime.fromtimestamp(candle_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        # Reset daily state
        if candle_day != current_day:
            current_day = candle_day
            daily_pnl = 0.0
            halted_today = False

        # Slice last vwap_window candles (signal generation only uses these)
        win_start = max(0, i + 1 - vwap_win)
        closes = all_closes[win_start: i + 1]
        highs = all_highs[win_start: i + 1]
        lows = all_lows[win_start: i + 1]
        volumes = all_volumes[win_start: i + 1]

        # Trend candles up to most recent closed 15m boundary — binary search
        # Only pass last ~100 trend candles (ADX needs ~30 + EMA needs ~45 + margin)
        trend_boundary = candle_ts - 900_000
        trend_idx = bisect.bisect_right(trend_ts_arr, trend_boundary)
        trend_start = max(0, trend_idx - 100)
        t_closes = trend_closes_arr[trend_start:trend_idx]
        t_highs = trend_highs_arr[trend_start:trend_idx]
        t_lows = trend_lows_arr[trend_start:trend_idx]

        # If in position, check exits first
        if position_side is not None:
            position_hold_candles += 1

            # Use core's evaluate_exit
            exit_decision = evaluate_exit(
                candle_high=candle["high"],
                candle_low=candle["low"],
                candle_close=candle["close"],
                position_side=position_side,
                position_entry_price=position_entry_price,
                position_stop_price=position_stop_price,
                position_target_price=position_target_price,
                hold_candles=position_hold_candles,
                params=params,
            )

            if exit_decision.action == "exit":
                ea = exit_decision.exit_action

                # Fill rate check for maker exits (target/signal)
                if ea.is_maker and ea.exit_type in ("target", "signal"):
                    if random.random() > BACKTEST_FILL_RATE:
                        continue  # didn't fill, keep holding

                exit_price = ea.exit_price
                hold_hours = (candle_ts - position_entry_ts) / 3_600_000

                # Calc PnL using core
                pnl_result = calc_trade_pnl(
                    side=position_side,
                    entry_price=position_entry_price,
                    exit_price=exit_price,
                    size_usd=position_size_usd,
                    equity=position_equity_at_entry,
                    leverage=leverage,
                    is_maker_exit=ea.is_maker,
                    hold_hours=hold_hours,
                    params=params,
                )

                # Funding at leverage
                funding = calc_funding_at_leverage(
                    position_equity_at_entry, leverage,
                    BACKTEST_HOURLY_FUNDING_RATE, hold_hours,
                )

                bt_trade = BTTrade(
                    side=position_side,
                    entry_ts=position_entry_ts,
                    exit_ts=candle_ts,
                    entry_price=position_entry_price,
                    exit_price=exit_price,
                    size_usd=position_size_usd,
                    notional_usd=position_size_usd,
                    leverage=leverage,
                    liq_price=position_liq_price,
                    exit_reason=ea.exit_type,
                    pnl_usd=pnl_result["pnl_usd"],
                    slippage_usd=pnl_result["slippage_usd"],
                    entry_fee_usd=pnl_result["entry_fee_usd"],
                    exit_fee_usd=pnl_result["exit_fee_usd"],
                    funding_usd=pnl_result["funding_usd"],
                    maker_rebate_usd=pnl_result["maker_rebate_usd"],
                    equity_return_pct=pnl_result["equity_return_pct"],
                    stop_price=position_stop_price,
                    target_price=position_target_price,
                    liq_buffer_ratio=position_liq_buffer,
                    equity_at_entry=position_equity_at_entry,
                    hold_candles=position_hold_candles,
                    **position_signal_context,
                )
                trades.append(bt_trade)
                equity += bt_trade.net_pnl
                daily_pnl += bt_trade.net_pnl

                # Reset position
                position_side = None
                position_hold_candles = 0
                position_signal_context = {}

                # Check daily halt using core
                should_halt, halt_reason = check_daily_halt(
                    daily_pnl, capital, risk_per_trade,
                    MAX_DAILY_LOSS_MULTIPLIER,
                )
                if should_halt and not halted_today:
                    halted_today = True
                    halt_count += 1
                    logger.info("Daily halt: %s", halt_reason)

                continue

        # Skip entries if halted
        if halted_today:
            continue

        # No position — evaluate entry using core
        if position_side is None:
            entry_decision = evaluate_entry(
                closes=closes, highs=highs, lows=lows, volumes=volumes,
                trend_closes=t_closes, trend_highs=t_highs, trend_lows=t_lows,
                equity=equity, current_position_side=None,
                funding_rate=BACKTEST_HOURLY_FUNDING_RATE, params=params,
            )

            if entry_decision.action == "skip":
                # Track liq blocks
                if (entry_decision.block_reason
                        and "liq_buffer" in entry_decision.block_reason):
                    liq_blocked_count += 1

                # Entry expiry tracking
                sig = entry_decision.signal_result
                if sig and sig.signal in ("long_entry", "short_entry"):
                    sig_dir = sig.signal
                    if sig_dir == pending_signal_dir:
                        pending_signal_count += 1
                    else:
                        pending_signal_dir = sig_dir
                        pending_signal_count = 1
                else:
                    pending_signal_dir = None
                    pending_signal_count = 0
                continue

            if entry_decision.action == "enter":
                setup = entry_decision.trade_setup

                # Entry expiry check
                sig_dir = setup.signal
                if sig_dir == pending_signal_dir:
                    pending_signal_count += 1
                    if pending_signal_count > params.entry_expiry_candles:
                        pending_signal_dir = None
                        pending_signal_count = 0
                        continue
                else:
                    pending_signal_dir = sig_dir
                    pending_signal_count = 1

                # Fill rate simulation
                if random.random() > BACKTEST_FILL_RATE:
                    continue

                # Fill!
                position_side = setup.side
                position_entry_price = setup.entry_price
                position_stop_price = setup.stop_price
                position_target_price = setup.target_price
                position_entry_ts = candle_ts
                position_size_usd = setup.size_usd
                position_liq_price = setup.liq_price
                position_liq_buffer = setup.liq_buffer_ratio
                position_equity_at_entry = equity
                position_hold_candles = 0

                # Capture signal context for trade logging
                sig = setup.signal_result
                position_signal_context = {}
                if sig and sig.vwap_state:
                    position_signal_context["vwap_at_entry"] = sig.vwap_state.vwap
                    position_signal_context["sigma_at_entry"] = sig.sigma_dist
                    position_signal_context["vwap_std_dev_at_entry"] = sig.vwap_state.std_dev
                if sig and sig.regime:
                    position_signal_context["adx_at_entry"] = sig.regime.adx
                    position_signal_context["ema_at_entry"] = sig.regime.ema
                    position_signal_context["trend_direction_at_entry"] = sig.regime.trend_direction
                    position_signal_context["regime_trending_at_entry"] = int(sig.regime.is_trending)
                position_signal_context["volume_at_entry"] = candle["volume"]

    # Close any open position at end of window
    if position_side is not None and primary_candles:
        last_candle = primary_candles[-1]
        exit_price = calc_fill_price(
            position_side, last_candle["close"],
            SLIPPAGE_TICKS_STOP, TICK_SIZE,
        )
        hold_hours = (last_candle["ts"] - position_entry_ts) / 3_600_000

        pnl_result = calc_trade_pnl(
            side=position_side,
            entry_price=position_entry_price,
            exit_price=exit_price,
            size_usd=position_size_usd,
            equity=position_equity_at_entry,
            leverage=leverage,
            is_maker_exit=False,
            hold_hours=hold_hours,
            params=params,
        )

        bt_trade = BTTrade(
            side=position_side,
            entry_ts=position_entry_ts,
            exit_ts=last_candle["ts"],
            entry_price=position_entry_price,
            exit_price=exit_price,
            size_usd=position_size_usd,
            notional_usd=position_size_usd,
            leverage=leverage,
            liq_price=position_liq_price,
            exit_reason="end_of_window",
            pnl_usd=pnl_result["pnl_usd"],
            slippage_usd=pnl_result["slippage_usd"],
            entry_fee_usd=pnl_result["entry_fee_usd"],
            exit_fee_usd=pnl_result["exit_fee_usd"],
            funding_usd=pnl_result["funding_usd"],
            maker_rebate_usd=pnl_result["maker_rebate_usd"],
            equity_return_pct=pnl_result["equity_return_pct"],
            stop_price=position_stop_price,
            target_price=position_target_price,
            liq_buffer_ratio=position_liq_buffer,
            equity_at_entry=position_equity_at_entry,
            hold_candles=position_hold_candles,
            **position_signal_context,
        )
        trades.append(bt_trade)
        equity += bt_trade.net_pnl

    return trades, halt_count, liq_blocked_count


def run_walk_forward(
    db_path: str,
    symbol: str,
    primary_tf: str,
    trend_tf: str,
    n_windows: int,
    window_days: int,
) -> None:
    """Run walk-forward backtest across multiple windows."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get data range
    row = conn.execute(
        "SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM candles WHERE symbol = ? AND tf = ?",
        (symbol, primary_tf),
    ).fetchone()

    if not row or not row["max_ts"]:
        print(f"No data found for {symbol} {primary_tf}")
        conn.close()
        return

    max_ts = row["max_ts"]
    window_ms = window_days * 86_400_000

    all_results: list[WindowResult] = []

    for w in range(n_windows):
        end_ts = max_ts - w * window_ms
        start_ts = end_ts - window_ms

        # Load primary candles
        primary_rows = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM candles
               WHERE symbol = ? AND tf = ? AND ts >= ? AND ts <= ?
               ORDER BY ts ASC""",
            (symbol, primary_tf, start_ts, end_ts),
        ).fetchall()

        # Load trend candles with extra lookback for EMA warmup
        trend_start = start_ts - window_ms
        trend_rows = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM candles
               WHERE symbol = ? AND tf = ? AND ts >= ? AND ts <= ?
               ORDER BY ts ASC""",
            (symbol, trend_tf, trend_start, end_ts),
        ).fetchall()

        primary_candles = [dict(r) for r in primary_rows]
        trend_candles = [dict(r) for r in trend_rows]

        if len(primary_candles) < VWAP_WINDOW + 10:
            logger.warning("Window %d: insufficient data (%d candles), skipping",
                          w + 1, len(primary_candles))
            continue

        trades, halts, liq_blocks = simulate_window(
            primary_candles, trend_candles,
            CAPITAL_USDC, RISK_PER_TRADE, TARGET_LEVERAGE,
        )

        result = WindowResult(
            window_idx=w + 1,
            start_ts=start_ts,
            end_ts=end_ts,
            trades=trades,
            halt_count=halts,
            liq_blocked_count=liq_blocks,
            _window_days=window_days,
        )
        all_results.append(result)
        print(result.summary())

    conn.close()

    # Final verdict
    if not all_results:
        print("\nNo windows completed.")
        return

    total_days = n_windows * window_days
    agg_passed, agg_failures = aggregate_gate_0(all_results, total_days)
    total_trades = sum(r.n_trades for r in all_results)
    total_net_pnl = sum(r.net_pnl for r in all_results)
    total_funding = sum(sum(t.funding_usd for t in r.trades) for r in all_results)
    total_rebates = sum(sum(t.maker_rebate_usd for t in r.trades) for r in all_results)

    agg_str = "PASS" if agg_passed else f"FAIL: {', '.join(agg_failures)}"
    print(f"\n{'='*60}")
    print(f"WALK-FORWARD VERDICT: {agg_str}")
    print(f"  Windows: {len(all_results)}")
    print(f"  Total Trades: {total_trades}")
    print(f"  Total Net PnL: ${total_net_pnl:.2f}")
    print(f"  Total Funding: ${total_funding:.2f}")
    print(f"  Total Rebates: ${total_rebates:.2f}")
    print(f"{'='*60}")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="VRAB Walk-Forward Backtest")
    parser.add_argument("--symbol", default="BTC", help="Trading symbol")
    parser.add_argument("--tf", default="5m", help="Primary timeframe")
    parser.add_argument("--trend-tf", default="15m", help="Trend timeframe")
    parser.add_argument("--windows", type=int, default=3, help="Number of windows")
    parser.add_argument("--window-days", type=int, default=30, help="Days per window")
    parser.add_argument("--db", default=DB_PATH, help="Database path")
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    run_walk_forward(
        args.db, args.symbol, args.tf, args.trend_tf,
        args.windows, args.window_days,
    )


if __name__ == "__main__":
    main()
