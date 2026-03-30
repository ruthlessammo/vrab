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

    uptime_h = status.uptime_seconds / 3600
    return (
        f"*VRAB Status*\n"
        f"Mode: `{status.mode}`\n"
        f"Equity: `${status.equity:.2f}`\n"
        f"Daily PnL: `{status.daily_pnl:+.2f}`\n"
        f"Halted: `{status.halted}`\n"
        f"Uptime: `{uptime_h:.1f}h` ({status.candle_count} candles)\n\n"
        f"*Position*\n{pos_str}"
    )


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


def format_daily_summary(
    date: str,
    pnl: float,
    trade_count: int,
    equity: float,
    signals_generated: int,
    signals_blocked: int,
) -> str:
    """Format end-of-day auto-summary."""
    return (
        f"*Daily Summary — {date}*\n"
        f"PnL: `{pnl:+.2f}` USD\n"
        f"Trades: `{trade_count}`\n"
        f"Equity: `${equity:.2f}`\n"
        f"Signals: `{signals_generated}` generated, `{signals_blocked}` blocked"
    )
