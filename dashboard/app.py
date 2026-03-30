"""Read-only Flask dashboard for VRAB.

Queries SQLite directly — no Store import.
"""

import logging
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from config import DB_PATH, is_kill_switch_active

logger = logging.getLogger(__name__)

_start_time = datetime.now(timezone.utc)


def _get_db() -> sqlite3.Connection:
    """Get a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def create_app(db_path: str | None = None) -> Flask:
    """Create the Flask dashboard app."""
    app = Flask(__name__)

    actual_db_path = db_path or DB_PATH

    def get_conn():
        conn = sqlite3.connect(f"file:{actual_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @app.route("/")
    def index():
        """Status page."""
        try:
            conn = get_conn()
            last_trade = conn.execute(
                "SELECT * FROM trades ORDER BY exit_ts DESC LIMIT 1"
            ).fetchone()
            daily_pnl = conn.execute(
                "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 1"
            ).fetchone()
            conn.close()
        except Exception:
            last_trade = None
            daily_pnl = None

        return jsonify({
            "status": "halted" if is_kill_switch_active() else "running",
            "uptime_seconds": (datetime.now(timezone.utc) - _start_time).total_seconds(),
            "last_trade": dict(last_trade) if last_trade else None,
            "daily_pnl": dict(daily_pnl) if daily_pnl else None,
        })

    @app.route("/api/trades")
    def api_trades():
        """JSON trade history."""
        days = request.args.get("days", 30, type=int)
        limit = request.args.get("limit", 100, type=int)
        try:
            conn = get_conn()
            rows = conn.execute(
                """SELECT * FROM trades
                   WHERE entry_ts >= ?
                   ORDER BY entry_ts DESC LIMIT ?""",
                (int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000), limit),
            ).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/daily")
    def api_daily():
        """JSON daily PnL series."""
        days = request.args.get("days", 30, type=int)
        try:
            conn = get_conn()
            rows = conn.execute(
                "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/signals")
    def api_signals():
        """JSON recent signals."""
        limit = request.args.get("limit", 100, type=int)
        try:
            conn = get_conn()
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/health")
    def api_health():
        """Health check endpoint."""
        try:
            conn = get_conn()
            last_trade_row = conn.execute(
                "SELECT MAX(exit_ts) as last_ts FROM trades"
            ).fetchone()
            trade_count = conn.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()
            conn.close()
            last_ts = last_trade_row["last_ts"] if last_trade_row else None
        except Exception:
            last_ts = None
            trade_count = None

        return jsonify({
            "healthy": True,
            "kill_switch_active": is_kill_switch_active(),
            "uptime_seconds": (datetime.now(timezone.utc) - _start_time).total_seconds(),
            "last_trade_ts": last_ts,
            "total_trades": trade_count["cnt"] if trade_count else 0,
        })

    return app


if __name__ == "__main__":
    from logging_config import setup_logging
    setup_logging()
    app = create_app()
    app.run(host="0.0.0.0", port=5555, debug=False)
