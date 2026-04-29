"""Backfill post-graduation trades with correct PnL from HL fills.

Usage:
  python -m tools.backfill_pnl --csv path/to/trades.csv          # dry run (default)
  python -m tools.backfill_pnl --csv path/to/trades.csv --apply  # write to DB
  python -m tools.backfill_pnl --days 30 --apply                 # fetch from API

Direction-aware matching: long trades only grab Open Long + Close Long fills,
short trades only grab Open Short + Close Short fills. Tight time windows
prevent cross-trade fill stealing.
"""

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta

from config import GRADUATION_CUTOVER_TS, DB_PATH
from live.pnl import calc_pnl_from_fills
from tools.reconcile_hl import parse_hl_csv, fetch_fills_from_api

# Time margins for fill matching (ms)
ENTRY_MARGIN_MS = 5_000    # 5s before entry_ts
EXIT_MARGIN_MS = 30_000    # 30s after exit_ts

_LONG_DIRS = {"Open Long", "Close Long"}
_SHORT_DIRS = {"Open Short", "Close Short"}


def _expected_dirs(side: str) -> set[str]:
    """Return expected fill directions for a trade side."""
    return _LONG_DIRS if side == "long" else _SHORT_DIRS


def match_fills_to_trade(trade: dict, fills: list[dict]) -> list[dict]:
    """Match fills to a single trade using direction-aware time-window matching.

    Args:
        trade: DB trade dict with entry_ts, exit_ts, side.
        fills: Available (unclaimed) HL fills, sorted by time.

    Returns:
        List of matched fills for this trade.
    """
    expected = _expected_dirs(trade["side"])
    window_start = trade["entry_ts"] - ENTRY_MARGIN_MS
    window_end = trade["exit_ts"] + EXIT_MARGIN_MS

    return [
        f for f in fills
        if window_start <= f["time"] <= window_end
        and f["dir"] in expected
    ]


def extract_prices_from_fills(
    fills: list[dict], side: str,
) -> tuple[float | None, float | None]:
    """Extract size-weighted average entry and exit prices from fills.

    Args:
        fills: Matched fills for one trade.
        side: "long" or "short".

    Returns:
        (entry_price, exit_price) — either can be None if no matching fills.
    """
    if side == "long":
        open_dir, close_dir = "Open Long", "Close Long"
    else:
        open_dir, close_dir = "Open Short", "Close Short"

    open_fills = [f for f in fills if f["dir"] == open_dir]
    close_fills = [f for f in fills if f["dir"] == close_dir]

    entry_price = None
    if open_fills:
        total_ntl = sum(float(f["px"]) * float(f["sz"]) for f in open_fills)
        total_sz = sum(float(f["sz"]) for f in open_fills)
        if total_sz > 0:
            entry_price = total_ntl / total_sz

    exit_price = None
    if close_fills:
        total_ntl = sum(float(f["px"]) * float(f["sz"]) for f in close_fills)
        total_sz = sum(float(f["sz"]) for f in close_fills)
        if total_sz > 0:
            exit_price = total_ntl / total_sz

    return entry_price, exit_price


def load_post_grad_trades(db_path: str) -> list[dict]:
    """Load live trades from DB since graduation cutover."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM trades
           WHERE source = 'live' AND entry_ts >= ?
           ORDER BY entry_ts""",
        (GRADUATION_CUTOVER_TS,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def backfill(fills: list[dict], db_trades: list[dict]) -> list[dict]:
    """Match fills to trades and compute corrected PnL.

    Returns list of dicts with old and new values for each trade.
    """
    # Sort fills chronologically
    fills = sorted(fills, key=lambda f: f["time"])

    # Filter fills to post-graduation
    fills = [f for f in fills if f["time"] >= GRADUATION_CUTOVER_TS - ENTRY_MARGIN_MS]

    available = list(fills)  # copy — we remove claimed fills
    results = []

    for trade in db_trades:
        matched = match_fills_to_trade(trade, available)

        # Remove claimed fills from available pool
        matched_set = set(id(f) for f in matched)
        available = [f for f in available if id(f) not in matched_set]

        # Compute corrected PnL
        pnl_result = calc_pnl_from_fills(matched, funding_usd=0.0,
                                          equity=trade.get("equity_at_entry", 500.0) or 500.0)

        # Extract actual prices
        entry_px, exit_px = extract_prices_from_fills(matched, trade["side"])

        results.append({
            "trade_id": trade["id"],
            "entry_ts": trade["entry_ts"],
            "side": trade["side"],
            "fill_count": len(matched),
            # Old values
            "old_entry_price": trade["entry_price"],
            "old_exit_price": trade["exit_price"],
            "old_net_pnl": trade["net_pnl_usd"],
            "old_pnl": trade.get("pnl_usd", 0.0),
            "old_entry_fee": trade.get("entry_fee_usd", 0.0),
            "old_exit_fee": trade.get("exit_fee_usd", 0.0),
            # New values
            "new_entry_price": entry_px,
            "new_exit_price": exit_px,
            "new_net_pnl": pnl_result["net_pnl_usd"],
            "new_pnl": pnl_result["pnl_usd"],
            "new_entry_fee": pnl_result["entry_fee_usd"],
            "new_exit_fee": pnl_result["exit_fee_usd"],
            # Diff
            "pnl_diff": pnl_result["net_pnl_usd"] - trade["net_pnl_usd"],
        })

    return results


def format_backfill_report(results: list[dict]) -> str:
    """Format backfill results as a readable table."""
    lines = []
    total_old = 0.0
    total_new = 0.0

    lines.append(f"{'Side':>5s}  {'Date':>11s}  {'Fills':>5s}  "
                 f"{'Old Net':>9s}  {'New Net':>9s}  {'Diff':>9s}  "
                 f"{'Old Entry':>10s}  {'New Entry':>10s}")
    lines.append("-" * 85)

    for r in results:
        dt = datetime.fromtimestamp(r["entry_ts"] / 1000, tz=timezone.utc)
        new_entry = f"{r['new_entry_price']:.1f}" if r["new_entry_price"] else "N/A"
        lines.append(
            f"{r['side']:>5s}  {dt:%m/%d %H:%M}  {r['fill_count']:>5d}  "
            f"${r['old_net_pnl']:>+8.4f}  ${r['new_net_pnl']:>+8.4f}  "
            f"${r['pnl_diff']:>+8.4f}  "
            f"{r['old_entry_price']:>10.1f}  {new_entry:>10s}"
        )
        total_old += r["old_net_pnl"]
        total_new += r["new_net_pnl"]

    lines.append("-" * 85)
    lines.append(
        f"{'TOTAL':>5s}  {'':>11s}  {'':>5s}  "
        f"${total_old:>+8.4f}  ${total_new:>+8.4f}  "
        f"${total_new - total_old:>+8.4f}"
    )
    return "\n".join(lines)


def apply_backfill(db_path: str, results: list[dict]) -> None:
    """Write corrected values to DB."""
    # Backup first
    backup_path = db_path + ".bak"
    shutil.copy2(db_path, backup_path)
    print(f"DB backed up to {backup_path}")

    conn = sqlite3.connect(db_path)
    updated = 0
    for r in results:
        if r["fill_count"] == 0:
            continue  # skip trades with no matched fills

        params = {
            "net_pnl_usd": r["new_net_pnl"],
            "pnl_usd": r["new_pnl"],
            "entry_fee_usd": r["new_entry_fee"],
            "exit_fee_usd": r["new_exit_fee"],
        }
        if r["new_entry_price"] is not None:
            params["entry_price"] = r["new_entry_price"]
        if r["new_exit_price"] is not None:
            params["exit_price"] = r["new_exit_price"]

        set_clause = ", ".join(f"{k} = ?" for k in params)
        values = list(params.values()) + [r["trade_id"]]
        conn.execute(f"UPDATE trades SET {set_clause} WHERE id = ?", values)
        updated += 1

    conn.commit()
    conn.close()
    print(f"Updated {updated} trades")


def main():
    parser = argparse.ArgumentParser(description="Backfill post-graduation PnL from HL fills")
    parser.add_argument("--csv", default=None, help="Path to HL trade_history CSV")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite DB")
    parser.add_argument("--wallet", default=None, help="HL wallet address")
    parser.add_argument("--days", type=int, default=30, help="API lookback days")
    parser.add_argument("--apply", action="store_true", help="Write changes to DB (default: dry run)")
    args = parser.parse_args()

    # Load fills
    if args.csv:
        fills = parse_hl_csv(args.csv)
        print(f"Parsed {len(fills)} fills from {args.csv}")
    else:
        wallet = args.wallet or os.environ.get("HL_WALLET_ADDRESS", "")
        if not wallet:
            print("ERROR: No wallet address. Set HL_WALLET_ADDRESS or use --wallet / --csv")
            return
        start_ts = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
        print(f"Fetching fills from HL API (last {args.days} days)...")
        fills = fetch_fills_from_api(wallet, start_ts)
        print(f"Fetched {len(fills)} fills")

    # Load DB trades
    db_trades = load_post_grad_trades(args.db)
    print(f"Loaded {len(db_trades)} post-graduation trades from DB")

    if not db_trades:
        print("No trades to backfill.")
        return

    # Run backfill
    results = backfill(fills, db_trades)

    # Report
    print(f"\n{'=' * 85}")
    print(format_backfill_report(results))
    print(f"{'=' * 85}")

    unmatched = [r for r in results if r["fill_count"] == 0]
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} trades had no matched fills")

    if args.apply:
        apply_backfill(args.db, results)
    else:
        print("\nDry run — use --apply to write changes to DB")


if __name__ == "__main__":
    main()
