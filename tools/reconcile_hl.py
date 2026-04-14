"""Reconcile DB trades against Hyperliquid CSV export.

Usage: python -m tools.reconcile_hl path/to/trade_history.csv [--db path/to/vrab.db]

Parses the HL CSV, groups fills into round-trip trades, and compares against
DB trades by matching on entry timestamp and side. Reports discrepancies in
PnL, fees, and net_pnl.
"""

import argparse
import csv
import sqlite3
from datetime import datetime, timezone


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


def group_into_trades(fills: list[dict]) -> list[dict]:
    """Group sequential fills into round-trip trades.

    Each trade starts with Open fill(s) and ends with Close fill(s).
    """
    trades = []
    current_opens = []

    for fill in fills:
        if "Open" in fill["dir"]:
            current_opens.append(fill)
        elif "Close" in fill["dir"]:
            if not current_opens:
                print(f"  WARNING: Close fill without opens at {fill['time']}")
                continue

            # Build trade from accumulated opens + this close
            all_fills = current_opens + [fill]
            entry_ts = current_opens[0]["time"]
            exit_ts = fill["time"]

            # Side from first open
            side = "long" if "Long" in current_opens[0]["dir"] else "short"

            # PnL from closedPnl semantics
            total_closed_pnl = sum(f["closedPnl"] for f in all_fills)
            total_fees = sum(f["fee"] for f in all_fills)
            open_fees = sum(f["fee"] for f in current_opens)
            close_fees = fill["fee"]

            # Weighted average entry price
            total_ntl = sum(f["ntl"] for f in current_opens)
            total_sz = sum(f["sz"] for f in current_opens)
            entry_price = total_ntl / total_sz if total_sz > 0 else 0
            exit_price = fill["px"]

            trades.append({
                "side": side,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size_btc": total_sz,
                "gross_pnl": total_closed_pnl + total_fees,
                "total_fees": total_fees,
                "open_fees": open_fees,
                "close_fees": close_fees,
                "net_pnl": total_closed_pnl,  # closedPnl already has fees baked in
                "fill_count": len(all_fills),
            })
            current_opens = []

    if current_opens:
        print(f"  WARNING: {len(current_opens)} unclosed open fill(s) remaining")

    return trades


def load_db_trades(db_path: str) -> list[dict]:
    """Load live trades from DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE source = 'live' ORDER BY entry_ts"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reconcile(hl_trades: list[dict], db_trades: list[dict]) -> None:
    """Compare HL trades against DB trades and report discrepancies."""
    print(f"\n{'='*80}")
    print(f"RECONCILIATION: {len(hl_trades)} HL trades vs {len(db_trades)} DB trades")
    print(f"{'='*80}\n")

    if not db_trades:
        print("No live trades in DB. Showing HL summary only.\n")
        total_net = 0.0
        total_fees = 0.0
        for i, t in enumerate(hl_trades, 1):
            print(f"  #{i:2d} {t['side']:5s} "
                  f"entry=${t['entry_price']:.1f} exit=${t['exit_price']:.1f} "
                  f"gross={t['gross_pnl']:+.4f} fees={t['total_fees']:.4f} "
                  f"net={t['net_pnl']:+.4f}")
            total_net += t["net_pnl"]
            total_fees += t["total_fees"]
        print(f"\n  TOTAL: net={total_net:+.4f} fees={total_fees:.4f}")
        return

    # Match by entry_ts (within 120s window)
    matched = 0
    mismatches = []
    for hl in hl_trades:
        best_match = None
        best_delta = float("inf")
        for db in db_trades:
            delta = abs(hl["entry_ts"] - db["entry_ts"])
            if delta < best_delta and delta < 120_000:
                best_delta = delta
                best_match = db

        if best_match is None:
            mismatches.append(("MISSING_IN_DB", hl, None))
            continue

        matched += 1
        # Compare fields
        net_diff = abs(hl["net_pnl"] - best_match["net_pnl_usd"])
        if net_diff > 0.01:
            mismatches.append(("NET_PNL_MISMATCH", hl, best_match))

    print(f"  Matched: {matched}/{len(hl_trades)}")
    if mismatches:
        print(f"  Issues:  {len(mismatches)}\n")
        for issue_type, hl, db in mismatches:
            if issue_type == "MISSING_IN_DB":
                print(f"  MISSING: {hl['side']} entry_ts={hl['entry_ts']} "
                      f"net={hl['net_pnl']:+.4f}")
            else:
                print(f"  MISMATCH: {hl['side']} "
                      f"HL_net={hl['net_pnl']:+.4f} "
                      f"DB_net={db['net_pnl_usd']:+.4f} "
                      f"diff={hl['net_pnl'] - db['net_pnl_usd']:+.4f}")
    else:
        print("  All trades match!\n")


def main():
    parser = argparse.ArgumentParser(description="Reconcile DB vs HL CSV")
    parser.add_argument("csv_path", help="Path to HL trade_history CSV")
    parser.add_argument("--db", default="data/vrab.db", help="Path to SQLite DB")
    args = parser.parse_args()

    fills = parse_hl_csv(args.csv_path)
    print(f"Parsed {len(fills)} fills from {args.csv_path}")

    hl_trades = group_into_trades(fills)
    print(f"Grouped into {len(hl_trades)} round-trip trades")

    try:
        db_trades = load_db_trades(args.db)
    except Exception as e:
        print(f"Could not load DB ({args.db}): {e}")
        db_trades = []

    reconcile(hl_trades, db_trades)


if __name__ == "__main__":
    main()
