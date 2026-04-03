"""Read-only Flask dashboard for VRAB.

Queries SQLite directly — no Store import.
"""

import logging
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request, abort

from config import DB_PATH, is_kill_switch_active, PAPER_MODE, DASHBOARD_TOKEN

logger = logging.getLogger(__name__)

_start_time = datetime.now(timezone.utc)


def create_app(db_path: str | None = None) -> Flask:
    """Create the Flask dashboard app."""
    app = Flask(__name__)

    actual_db_path = db_path or DB_PATH

    def get_conn():
        conn = sqlite3.connect(f"file:{actual_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _check_token():
        """Verify dashboard token on protected routes."""
        if not DASHBOARD_TOKEN:
            return  # no token configured = auth disabled
        if request.endpoint == "api_health":
            return  # health check is public
        token = request.args.get("token") or ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if token != DASHBOARD_TOKEN:
            abort(401)

    app.before_request(_check_token)

    @app.route("/")
    def index():
        """Serve the dashboard HTML."""
        return render_template("index.html", token=DASHBOARD_TOKEN)

    @app.route("/api/status")
    def api_status():
        """Engine status summary."""
        try:
            conn = get_conn()
            last_trade = conn.execute(
                "SELECT * FROM trades ORDER BY exit_ts DESC LIMIT 1"
            ).fetchone()
            daily_pnl = conn.execute(
                "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 1"
            ).fetchone()
            peak_equity = conn.execute(
                "SELECT value FROM meta WHERE key = 'peak_equity'"
            ).fetchone()
            circuit_breaker = conn.execute(
                "SELECT value FROM meta WHERE key = 'circuit_breaker'"
            ).fetchone()
            live_equity = conn.execute(
                "SELECT value FROM meta WHERE key = 'live_equity'"
            ).fetchone()
            live_daily_pnl = conn.execute(
                "SELECT value FROM meta WHERE key = 'live_daily_pnl'"
            ).fetchone()
            conn.close()
        except Exception:
            last_trade = None
            daily_pnl = None
            peak_equity = None
            circuit_breaker = None
            live_equity = None
            live_daily_pnl = None

        return jsonify({
            "mode": "paper" if PAPER_MODE else "live",
            "status": "halted" if is_kill_switch_active() else "running",
            "uptime_seconds": (datetime.now(timezone.utc) - _start_time).total_seconds(),
            "last_trade": dict(last_trade) if last_trade else None,
            "daily_pnl": dict(daily_pnl) if daily_pnl else None,
            "peak_equity": float(peak_equity[0]) if peak_equity else None,
            "circuit_breaker": circuit_breaker[0] == "1" if circuit_breaker else False,
            "live_equity": float(live_equity[0]) if live_equity else None,
            "live_daily_pnl": float(live_daily_pnl[0]) if live_daily_pnl else None,
        })

    @app.route("/api/market")
    def api_market():
        """Latest market state from signals table."""
        try:
            conn = get_conn()
            row = conn.execute(
                "SELECT price, vwap, sigma_dist, adx, trend_direction, ts "
                "FROM signals ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            conn.close()
            return jsonify(dict(row) if row else {})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

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
