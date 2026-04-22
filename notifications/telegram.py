"""Async Telegram notification alerts.

Silent failure — trading continues even if Telegram is down.
Rate limited: max 1 message per 2 seconds.
"""

import asyncio
import logging
import time

import aiohttp

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_last_send_time = 0.0
_RATE_LIMIT_SECONDS = 2.0
_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    """Return a reusable aiohttp session, creating one if needed."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def send_alert(message: str) -> bool:
    """Send a Telegram message. Returns True on success, False on failure.

    Never raises — logs warnings on failure.
    """
    global _last_send_time

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured, skipping alert")
        return False

    # Rate limiting
    now = time.time()
    elapsed = now - _last_send_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        session = await _get_session()
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            _last_send_time = time.time()
            if resp.status == 200:
                return True
            logger.warning("Telegram API returned %d", resp.status)
            return False
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def format_trade_alert(trade) -> str:
    """Format a trade for Telegram notification."""
    return (
        f"*Trade Closed* ({trade.exit_reason})\n"
        f"Symbol: `{trade.symbol}` | Side: `{trade.side}`\n"
        f"Entry: `{trade.entry_price:.1f}` → Exit: `{trade.exit_price:.1f}`\n"
        f"Net PnL: `{trade.net_pnl:+.2f}` USD "
        f"({trade.equity_return_pct:+.2%})\n"
        f"Hold: {trade.hold_minutes:.0f}m | Leverage: {trade.leverage:.0f}x"
    )


def format_halt_alert(reason: str, daily_pnl: float) -> str:
    """Format a trading halt alert."""
    return (
        f"*TRADING HALTED*\n"
        f"Reason: `{reason}`\n"
        f"Daily PnL: `{daily_pnl:.2f}` USD"
    )


def format_error_alert(error: str) -> str:
    """Format an error alert."""
    return f"*ERROR*\n`{error[:500]}`"


def format_status(status, position) -> str:
    """Format /status response."""
    pos_str = "_No position_"
    if position:
        pos_str = (
            f"`{position.side}` {position.size_btc:.5f} BTC @ `{position.entry_price:.1f}`\n"
            f"  Stop: `{position.stop_price:.1f}` | Target: `{position.target_price:.1f}`\n"
            f"  Hold: {position.hold_candles} candles"
        )

    market_str = "_Waiting for data_"
    if status.price > 0:
        sigma_bar = _sigma_bar(status.sigma_dist)
        market_str = (
            f"Price: `{status.price:.1f}` | VWAP: `{status.vwap:.1f}`\n"
            f"σ: `{status.sigma_dist:+.2f}` {sigma_bar} (entry at ±2.5)\n"
            f"ADX: `{status.adx:.1f}` | Trend: `{status.trend}`"
        )

    uptime_h = status.uptime_seconds / 3600
    return (
        f"*VRAB Status*\n"
        f"Mode: `{status.mode}`\n"
        f"Equity: `${status.equity:.2f}`\n"
        f"Daily PnL: `{status.daily_pnl:+.2f}`\n"
        f"Halted: `{status.halted}`\n"
        f"Uptime: `{uptime_h:.1f}h` ({status.candle_count} candles)\n\n"
        f"*Market*\n{market_str}\n\n"
        f"*Position*\n{pos_str}"
    )


def _sigma_bar(sigma: float) -> str:
    """Visual bar showing distance to ±2.5σ entry threshold."""
    clamped = max(-3.0, min(3.0, sigma))
    pct = abs(clamped) / 2.5
    filled = int(pct * 10)
    return "|" + "█" * filled + "░" * (10 - filled) + "|"


def format_pnl_summary(
    daily_pnl: float,
    weekly_pnl: float,
    total_pnl: float,
    total_trades: int,
    win_rate: float,
) -> str:
    """Format /pnl response."""
    return (
        f"*PnL Summary*\n"
        f"Today: `{daily_pnl:+.2f}` USD\n"
        f"7 Day: `{weekly_pnl:+.2f}` USD\n"
        f"Total: `{total_pnl:+.2f}` USD\n\n"
        f"Trades: `{total_trades}` | Win Rate: `{win_rate:.1%}`"
    )


def format_equity(equity: float, capital: float, total_pnl: float) -> str:
    """Format /equity response."""
    ret_pct = (equity - capital) / capital if capital > 0 else 0
    return (
        f"*Equity*\n"
        f"Current: `${equity:.2f}`\n"
        f"Starting: `${capital:.2f}`\n"
        f"Total PnL: `{total_pnl:+.2f}` USD\n"
        f"Return: `{ret_pct:+.1%}`"
    )


def format_trades_list(trades: list) -> str:
    """Format /trades response."""
    if not trades:
        return "*Recent Trades*\n_No trades yet_"

    lines = ["*Recent Trades*"]
    for t in trades[:5]:
        pnl = t.net_pnl
        lines.append(
            f"`{t.side}` {t.entry_price:.1f} → {t.exit_price:.1f} "
            f"| `{pnl:+.2f}` ({t.equity_return_pct:+.2%}) "
            f"| {t.exit_reason}"
        )
    return "\n".join(lines)


def format_graduation(trades: list, daily_records: list, equity: float, peak_equity: float, cb_trips: int, since_date: str | None = None) -> str:
    """Format /graduation response showing progress toward capital scaling."""
    import math

    # --- Compute metrics from live trades ---
    count = len(trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    win_rate = wins / count if count else 0.0
    total_pnl = sum(t.net_pnl for t in trades)
    expectancy = total_pnl / count if count else 0.0
    max_dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0

    # Live Sharpe from daily PnL records
    if len(daily_records) >= 2:
        daily_pnls = [r["pnl_usd"] for r in daily_records]
        mean_pnl = sum(daily_pnls) / len(daily_pnls)
        var = sum((p - mean_pnl) ** 2 for p in daily_pnls) / (len(daily_pnls) - 1)
        std_pnl = math.sqrt(var) if var > 0 else 0
        sharpe = (mean_pnl / std_pnl * math.sqrt(365)) if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    days_running = len(daily_records)

    # --- Gate 1: System Reliability ---
    g1_days = f"{days_running}/7"
    g1_pass = days_running >= 7
    g1_icon = "✅" if g1_pass else "🔄"

    # --- Gate 2: Statistical Significance ---
    g2_trades = count >= 30
    g2_wr = win_rate >= 0.30
    g2_exp = expectancy > 0
    g2_cb = cb_trips == 0
    g2_pass = g2_trades and g2_wr and g2_exp and g2_cb
    g2_icon = "✅" if g2_pass else "🔄"

    def check(val): return "✅" if val else "❌"

    # --- Gate 3: Performance ---
    g3_pnl = total_pnl > 0
    g3_sharpe = sharpe >= 1.0
    g3_dd = max_dd <= 0.10
    g3_pass = g2_pass and g3_pnl and g3_sharpe and g3_dd
    g3_icon = "✅" if g3_pass else ("🔄" if g2_pass else "⏳")

    # --- Scaling tier ---
    if g3_pass:
        tier = "$120 → `$1,000-$2,000`"
    elif g2_pass:
        tier = "$120 → `$500`"
    else:
        remaining = 30 - count
        tier = f"$120 → $500 (need {remaining} more trades)" if remaining > 0 else "$120 → $500"

    lines = [
        f"*Graduation Status*",
        f"",
        f"*Gate 1: Reliability* {g1_icon}",
        f"  Days: `{g1_days}`",
        f"",
        f"*Gate 2: Significance* {g2_icon}",
        f"  Trades: `{count}/30` {check(g2_trades)}",
        f"  Win Rate: `{win_rate:.1%}` (>= 30%) {check(g2_wr)}",
        f"  Expectancy: `{expectancy:+.2f}` {check(g2_exp)}",
        f"  CB Trips: `{cb_trips}` {check(g2_cb)}",
        f"",
        f"*Gate 3: Performance* {g3_icon}",
        f"  PnL: `{total_pnl:+.2f}` {check(g3_pnl)}",
        f"  Sharpe: `{sharpe:.2f}` (>= 1.0) {check(g3_sharpe)}",
        f"  Max DD: `{max_dd:.1%}` (<= 10%) {check(g3_dd)}",
        f"",
        f"*Tier*: {tier}",
    ]
    if since_date:
        lines.append(f"Since: `{since_date}`")
    return "\n".join(lines)


def format_blocked_signal(
    signal_type: str,
    block_reason: str,
    price: float,
    vwap: float,
    sigma: float,
    adx: float,
    trend: str,
) -> str:
    """Format a blocked entry signal alert."""
    side = "LONG" if "long" in signal_type else "SHORT"
    sigma_bar = _sigma_bar(sigma)
    return (
        f"*Signal Blocked* — {side}\n"
        f"Reason: `{block_reason}`\n"
        f"Price: `{price:.1f}` | VWAP: `{vwap:.1f}`\n"
        f"σ: `{sigma:+.2f}` {sigma_bar}\n"
        f"ADX: `{adx:.1f}` | Trend: `{trend}`"
    )


def format_daily_summary(
    date: str,
    pnl: float,
    trade_count: int,
    equity: float,
    signals_generated: int,
    signals_blocked: int,
    shadow_trades: list | None = None,
) -> str:
    """Format end-of-day auto-summary."""
    lines = [
        f"*Daily Summary — {date}*",
        f"PnL: `{pnl:+.2f}` USD",
        f"Trades: `{trade_count}`",
        f"Equity: `${equity:.2f}`",
        f"Signals: `{signals_generated}` generated, `{signals_blocked}` blocked",
    ]
    if shadow_trades:
        wins = sum(1 for t in shadow_trades if t.net_pnl_usd > 0)
        losses = len(shadow_trades) - wins
        avg = sum(t.net_pnl_usd for t in shadow_trades) / len(shadow_trades)
        lines.append(f"Shadow: `{len(shadow_trades)}` blocked, avg `{avg:+.2f}` ({wins}W/{losses}L)")
    return "\n".join(lines)
