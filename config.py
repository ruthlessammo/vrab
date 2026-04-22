"""Single source of truth for all VRAB configuration."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Credentials — from .env
# ---------------------------------------------------------------------------
HL_PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "")
HL_WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------
SYMBOL = "BTC"
CANDLE_TF = "5m"
TREND_TF = "15m"

# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------
CAPITAL_USDC = float(os.getenv("CAPITAL_USDC", "500.0"))
RISK_PER_TRADE = 0.015          # 1.5% of equity per trade
MAKER_ONLY = True

# ---------------------------------------------------------------------------
# Leverage
# ---------------------------------------------------------------------------
TARGET_LEVERAGE = 10
MAX_LEVERAGE = 20
MIN_LIQUIDATION_BUFFER = 0.30   # stop must be ≤ 30% of way from entry to liq
MARGIN_UTILISATION_CAP = 0.80   # never use > 80% of available margin
HL_MAINTENANCE_MARGIN = 0.005   # HL BTC maintenance margin rate

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
VWAP_WINDOW = 36                # candles — 36 × 5m = 3h (sweep: 36 >> 96)
VWAP_ENTRY_SIGMA = 2.5          # sweep: 2.5σ entry (was 2.0)
VWAP_EXIT_SIGMA = 0.0
VWAP_STOP_SIGMA = 4.5           # sweep: 4.5σ stop (was 3.0, wider = higher WR)
ENTRY_EXPIRY_CANDLES = 2
TREND_EMA_PERIOD = 15
ADX_PERIOD = 14
ADX_THRESHOLD = 35.0            # sweep: 35 (was 30, allows mildly trending setups)
COUNTER_TREND_MIN_ADX = 20.0    # only apply counter-trend filter when ADX >= this
GRADUATION_CUTOVER_TS = 1776455700000  # 2026-04-17 19:55 UTC — filter changes deployed

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
MAX_DAILY_LOSS_MULTIPLIER = 3   # halt after 3× single-trade risk loss in a day
MAX_DRAWDOWN_PCT = 0.10         # 10% from peak equity → circuit breaker
MAX_POSITION_HOLD_MINS = 240
MAX_OPEN_POSITIONS = 1
NEWS_BUFFER_MINS = 5
FUNDING_RATE_BLOCK = 0.0003

# ---------------------------------------------------------------------------
# Transaction costs
# ---------------------------------------------------------------------------
MAKER_REBATE_RATE = 0.0002
TAKER_FEE_RATE = 0.00035
TICK_SIZE = 1.0
SLIPPAGE_TICKS_ENTRY = 1
SLIPPAGE_TICKS_STOP = 3

# ---------------------------------------------------------------------------
# Backtest assumptions
# ---------------------------------------------------------------------------
BACKTEST_HOURLY_FUNDING_RATE = 0.0001   # conservative assumption
BACKTEST_FILL_RATE = 0.70               # realistic limit fill rate

# ---------------------------------------------------------------------------
# Gate 0 thresholds
# ---------------------------------------------------------------------------
GATE0_MIN_SHARPE = 1.5
GATE0_MAX_DD = 0.08
GATE0_MIN_TRADES = 30
GATE0_MIN_WIN_RATE = 0.35
GATE0_MIN_EXPECTANCY = 0.0
GATE0_MAX_LIQ_BLOCK_RATIO = 0.10
GATE0_MAX_HALTS = 2

# ---------------------------------------------------------------------------
# Live engine
# ---------------------------------------------------------------------------
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
TELEGRAM_POLL_INTERVAL = 2              # seconds between bot getUpdates polls
DAILY_SUMMARY_ENABLED = True            # send end-of-day summary via Telegram
CANDLE_BACKFILL_COUNT = 200             # candles to backfill on startup per TF
HEARTBEAT_INTERVAL_CANDLES = 12         # log heartbeat every 12 candles (1 hour)
WS_RECONNECT_MAX_BACKOFF = 30           # max reconnect backoff in seconds
SHADOW_BOOK_ENABLED = True              # track hypothetical PnL of blocked trades

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DB_PATH = "data/vrab.db"
LOG_PATH = "logs/vrab.log"
KILL_SWITCH_PATH = "/tmp/VRAB_KILL"

# ---------------------------------------------------------------------------
# HL API
# ---------------------------------------------------------------------------
HL_BASE_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_MAX_CANDLES_PER_REQUEST = 500


def is_kill_switch_active() -> bool:
    """Check whether the kill switch file exists."""
    return Path(KILL_SWITCH_PATH).exists()
