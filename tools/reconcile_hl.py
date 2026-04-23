"""Reconcile DB trades against Hyperliquid fills.

Usage:
  python -m tools.reconcile_hl                          # fetch from HL API
  python -m tools.reconcile_hl --csv path/to/trades.csv # use CSV export
  python -m tools.reconcile_hl --days 7                 # last 7 days only

DB-anchored approach: each DB trade defines a time window, and we find
the HL fills that fall within it. No independent grouping or fuzzy matching.
"""

import argparse
import csv
import os
import sqlite3
from datetime import datetime, timezone, timedelta

# Margin (ms) added to each side of the DB trade window when claiming fills
FILL_MARGIN_MS = 3_600_000  # 1 hour — wide for legacy trades with candle-aligned timestamps


def parse_hl_csv(csv_path: str) -> list[dict]:
    """Parse HL trade history CSV into fill records."""
    fills = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse timestamp: "4/3/2026 - 14:50:14"
            dt = datetime.strptime(row["time"], "%m/%d/%Y - %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp() * 1000)

            fills.append({
                "time": ts_ms,
                "coin": row["coin"],
                "dir": row["dir"],
                "px": float(row["px"]),
                "sz": float(row["sz"]),
                "ntl": float(row["ntl"]),
                "fee": float(row["fee"]),
                "closedPnl": float(row["closedPnl"]),
            })
    return fills


def fetch_fills_from_api(wallet_address: str, start_ts: int,
                         end_ts: int | None = None) -> list[dict]:
    """Fetch fills directly from HL API. No private key needed."""
    from hyperliquid.info import Info

    info = Info(skip_ws=True)
    fills = info.user_fills_by_time(wallet_address, start_ts, end_ts)

    # API returns numeric fields; normalise to same shape as CSV parser
    result = []
    for f in fills:
        result.append({
            "time": f["time"],
            "coin": f["coin"],
            "dir": f["dir"],
            "px": float(f["px"]),
            "sz": float(f["sz"]),
            "ntl": float(f["px"]) * float(f["sz"]),
            "fee": float(f["fee"]),
            "closedPnl": float(f["closedPnl"]),
            "oid": f.get("oid"),
        })
    # Sort chronologically — API doesn't guarantee order
    result.sort(key=lambda f: f["time"])
    return result


def load_db_trades(db_path: str) -> list[dict]:
    """Load live trades from DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE source = 'live' ORDER BY entry_ts"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _coin_matches(fill_coin: str, db_symbol: str) -> bool:
    """Check if a fill's coin matches a DB trade's symbol.

    HL uses 'BTC', DB might use 'BTC' or 'BTCUSD' etc.
    """
    return db_symbol.startswith(fill_coin)


def reconcile(fills: list[dict], db_trades: list[dict],
              margin_ms: int = FILL_MARGIN_MS) -> dict:
    """DB-anchored reconciliation.

    For each DB trade:
    - If entry_oid is set, match fills by oid (exact, ignores time window)
    - Otherwise, fall back to time-window matching

    Returns result dict with totals, per-trade details, orphans, and unmatched.
    """
    # Level 0: totals
    hl_total_pnl = sum(f["closedPnl"] for f in fills)
    db_total_pnl = sum(
        (t["net_pnl_usd"] - (t["funding_usd"] or 0)) for t in db_trades
    )

    # Level 1: per-trade reconciliation
    claimed = set()  # indices of fills already claimed
    per_trade = []

    # DB trades ordered by entry_ts (already sorted from query)
    for db in db_trades:
        # Build set of known order IDs for this trade
        trade_oids = {v for v in (db.get("entry_oid"), db.get("stop_oid"),
                                  db.get("target_oid")) if v is not None}

        matched_fills = []
        if trade_oids:
            # OID match — find all fills whose order ID matches any trade oid
            for i, f in enumerate(fills):
                if i in claimed:
                    continue
                if f.get("oid") in trade_oids:
                    matched_fills.append(f)
                    claimed.add(i)
        else:
            # Time-window fallback for legacy trades (no oid stored)
            window_start = db["entry_ts"] - margin_ms
            window_end = db["exit_ts"] + margin_ms
            for i, f in enumerate(fills):
                if i in claimed:
                    continue
                if not _coin_matches(f["coin"], db["symbol"]):
                    continue
                if window_start <= f["time"] <= window_end:
                    matched_fills.append(f)
                    claimed.add(i)

        hl_pnl = sum(f["closedPnl"] for f in matched_fills)
        db_pnl_ex_funding = db["net_pnl_usd"] - (db["funding_usd"] or 0)

        per_trade.append({
            "db_trade": db,
            "fill_count": len(matched_fills),
            "hl_pnl": hl_pnl,
            "db_pnl_ex_funding": db_pnl_ex_funding,
            "diff": hl_pnl - db_pnl_ex_funding,
        })

    # Orphan fills — not claimed by any DB trade
    orphan_fills = [f for i, f in enumerate(fills) if i not in claimed]

    # Unmatched DB trades — had zero fills
    unmatched_db = [
        pt["db_trade"] for pt in per_trade if pt["fill_count"] == 0
    ]

    return {
        "hl_total_pnl": hl_total_pnl,
        "db_total_pnl": db_total_pnl,
        "total_diff": hl_total_pnl - db_total_pnl,
        "db_count": len(db_trades),
        "fill_count": len(fills),
        "per_trade": per_trade,
        "orphan_fills": orphan_fills,
        "unmatched_db": unmatched_db,
    }


def format_reconcile_report(result: dict) -> str:
    """Format reconcile result as a readable report string."""
    lines = []
    lines.append(f"Fills: {result['fill_count']}  |  DB trades: {result['db_count']}")
    lines.append(f"HL total (closedPnl):    ${result['hl_total_pnl']:+.4f}")
    lines.append(f"DB total (ex-funding):   ${result['db_total_pnl']:+.4f}")
    lines.append(f"Total diff:              ${result['total_diff']:+.4f}")
    lines.append("")

    # Per-trade details — only show mismatches
    mismatches = [pt for pt in result["per_trade"] if abs(pt["diff"]) > 0.01]
    if mismatches:
        lines.append(f"Per-trade mismatches: {len(mismatches)}")
        for pt in mismatches:
            db = pt["db_trade"]
            dt = datetime.fromtimestamp(db["entry_ts"] / 1000, tz=timezone.utc)
            lines.append(
                f"  {db['side']:5s} {dt:%m/%d %H:%M}  "
                f"HL={pt['hl_pnl']:+.4f}  DB={pt['db_pnl_ex_funding']:+.4f}  "
                f"diff={pt['diff']:+.4f}  fills={pt['fill_count']}"
            )
    else:
        lines.append("Per-trade: all match!")

    orphans = result["orphan_fills"]
    if orphans:
        orphan_pnl = sum(f["closedPnl"] for f in orphans)
        lines.append(f"\nOrphan fills: {len(orphans)} (total pnl={orphan_pnl:+.4f})")
        for f in orphans[:10]:  # show first 10
            dt = datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc)
            lines.append(
                f"  {f['coin']} {f['dir']} {dt:%m/%d %H:%M}  "
                f"pnl={f['closedPnl']:+.4f}"
            )
        if len(orphans) > 10:
            lines.append(f"  ... and {len(orphans) - 10} more")

    unmatched = result["unmatched_db"]
    if unmatched:
        lines.append(f"\nDB trades with no fills: {len(unmatched)}")
        for db in unmatched[:10]:
            dt = datetime.fromtimestamp(db["entry_ts"] / 1000, tz=timezone.utc)
            lines.append(
                f"  {db['side']:5s} {dt:%m/%d %H:%M}  "
                f"net={db['net_pnl_usd']:+.4f}"
            )

    return "\n".join(lines)


def format_reconcile_telegram(result: dict) -> str:
    """Format reconcile result for Telegram (compact, markdown)."""
    mismatches = [pt for pt in result["per_trade"] if abs(pt["diff"]) > 0.01]
    orphans = result["orphan_fills"]
    unmatched = result["unmatched_db"]

    issues = len(mismatches) + len(orphans) + len(unmatched)
    status = "ALL MATCH" if issues == 0 else f"{issues} issues"

    lines = [
        "*Reconciliation*",
        f"DB trades: `{result['db_count']}` | Fills: `{result['fill_count']}` — {status}",
        f"HL total: `${result['hl_total_pnl']:+.2f}`",
        f"DB total: `${result['db_total_pnl']:+.2f}`",
        f"Diff: `${result['total_diff']:+.2f}`",
    ]
    if mismatches:
        lines.append(f"PnL mismatches: `{len(mismatches)}`")
    if orphans:
        lines.append(f"Orphan fills: `{len(orphans)}`")
    if unmatched:
        lines.append(f"Unmatched DB trades: `{len(unmatched)}`")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Reconcile DB vs HL fills")
    parser.add_argument("--csv", default=None, help="Path to HL trade_history CSV (if omitted, fetches from API)")
    parser.add_argument("--db", default="data/vrab.db", help="Path to SQLite DB")
    parser.add_argument("--wallet", default=None, help="HL wallet address (default: HL_WALLET_ADDRESS env var)")
    parser.add_argument("--days", type=int, default=30, help="Lookback days when fetching from API (default: 30)")
    args = parser.parse_args()

    if args.csv:
        fills = parse_hl_csv(args.csv)
        print(f"Parsed {len(fills)} fills from {args.csv}")
    else:
        wallet = args.wallet or os.environ.get("HL_WALLET_ADDRESS", "")
        if not wallet:
            print("ERROR: No wallet address. Set HL_WALLET_ADDRESS or use --wallet")
            return
        start_ts = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
        print(f"Fetching fills from HL API (last {args.days} days)...")
        fills = fetch_fills_from_api(wallet, start_ts)
        print(f"Fetched {len(fills)} fills")

    try:
        db_trades = load_db_trades(args.db)
    except Exception as e:
        print(f"Could not load DB ({args.db}): {e}")
        db_trades = []

    result = reconcile(fills, db_trades)
    print(f"\n{'='*60}")
    print(format_reconcile_report(result))
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
