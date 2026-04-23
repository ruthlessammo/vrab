"""Reconcile DB trades against Hyperliquid fills.

Usage:
  python -m tools.reconcile_hl                          # fetch from HL API
  python -m tools.reconcile_hl --csv path/to/trades.csv # use CSV export
  python -m tools.reconcile_hl --days 7                 # last 7 days only

Fetches fills from the HL API (or parses a CSV), groups into round-trip
trades, and compares against DB trades. Reports discrepancies in PnL.
"""

import argparse
import csv
import os
import sqlite3
from datetime import datetime, timezone, timedelta


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
        })
    # Sort chronologically — API doesn't guarantee order
    result.sort(key=lambda f: f["time"])
    return result


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


def reconcile(hl_trades: list[dict], db_trades: list[dict]) -> dict:
    """Compare HL trades against DB trades. Returns result dict."""
    if not db_trades:
        hl_total = sum(t["net_pnl"] for t in hl_trades)
        return {
            "hl_count": len(hl_trades),
            "db_count": 0,
            "matched": 0,
            "mismatches": [],
            "hl_total_net": hl_total,
            "db_total_net": 0.0,
        }

    # Match by entry_ts (within 2h window) + side
    matched = 0
    mismatches = []
    used = set()
    matched_hl_net = 0.0
    matched_db_net = 0.0

    for hl in hl_trades:
        best_match = None
        best_idx = None
        best_delta = float("inf")
        for i, db in enumerate(db_trades):
            if i in used:
                continue
            delta = abs(hl["entry_ts"] - db["entry_ts"])
            if delta < best_delta and delta < 7_200_000 and hl["side"] == db["side"]:
                best_delta = delta
                best_match = db
                best_idx = i

        if best_match is None:
            mismatches.append(("MISSING_IN_DB", hl, None))
            continue

        used.add(best_idx)
        matched += 1
        matched_hl_net += hl["net_pnl"]
        matched_db_net += best_match["net_pnl_usd"]

        # Compare excluding funding (HL closedPnl doesn't include funding)
        db_net_ex_funding = best_match["net_pnl_usd"] - (best_match["funding_usd"] or 0)
        net_diff = abs(hl["net_pnl"] - db_net_ex_funding)
        if net_diff > 0.01:
            mismatches.append(("NET_PNL_MISMATCH", hl, best_match))

    return {
        "hl_count": len(hl_trades),
        "db_count": len(db_trades),
        "matched": matched,
        "mismatches": mismatches,
        "hl_total_net": sum(t["net_pnl"] for t in hl_trades),
        "db_total_net": sum(t["net_pnl_usd"] for t in db_trades),
        "matched_hl_net": matched_hl_net,
        "matched_db_net": matched_db_net,
    }


def format_reconcile_report(result: dict) -> str:
    """Format reconcile result as a readable report string."""
    lines = []
    lines.append(f"HL trades: {result['hl_count']}  |  DB trades: {result['db_count']}")
    lines.append(f"Matched: {result['matched']}/{result['hl_count']}")

    mismatches = result["mismatches"]
    if mismatches:
        lines.append(f"Issues: {len(mismatches)}")
        lines.append("")
        for issue_type, hl, db in mismatches:
            if issue_type == "MISSING_IN_DB":
                dt = datetime.fromtimestamp(hl["entry_ts"] / 1000, tz=timezone.utc)
                lines.append(
                    f"  MISSING: {hl['side']} {dt:%m/%d %H:%M} "
                    f"net={hl['net_pnl']:+.4f}"
                )
            else:
                db_ex_fund = db["net_pnl_usd"] - (db["funding_usd"] or 0)
                lines.append(
                    f"  MISMATCH: {hl['side']} "
                    f"HL={hl['net_pnl']:+.4f} "
                    f"DB={db_ex_fund:+.4f} "
                    f"diff={hl['net_pnl'] - db_ex_fund:+.4f} "
                    f"(fund={db['funding_usd'] or 0:+.4f})"
                )
    else:
        lines.append("All trades match!")

    lines.append("")
    lines.append(f"HL total:  ${result['hl_total_net']:+.4f}")
    lines.append(f"DB total:  ${result['db_total_net']:+.4f}")

    return "\n".join(lines)


def format_reconcile_telegram(result: dict) -> str:
    """Format reconcile result for Telegram (compact, markdown)."""
    matched = result["matched"]
    total = result["hl_count"]
    mismatches = result["mismatches"]
    missing = sum(1 for t, _, _ in mismatches if t == "MISSING_IN_DB")
    pnl_mismatches = sum(1 for t, _, _ in mismatches if t == "NET_PNL_MISMATCH")

    status = "ALL MATCH" if not mismatches else f"{len(mismatches)} issues"

    lines = [
        "*Reconciliation*",
        f"Matched: `{matched}/{total}` — {status}",
    ]
    if missing:
        lines.append(f"Missing in DB: `{missing}`")
    if pnl_mismatches:
        lines.append(f"PnL mismatches: `{pnl_mismatches}`")
    lines.append(f"HL total: `${result['hl_total_net']:+.2f}`")
    lines.append(f"DB total: `${result['db_total_net']:+.2f}`")

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

    hl_trades = group_into_trades(fills)
    print(f"Grouped into {len(hl_trades)} round-trip trades")

    try:
        db_trades = load_db_trades(args.db)
    except Exception as e:
        print(f"Could not load DB ({args.db}): {e}")
        db_trades = []

    result = reconcile(hl_trades, db_trades)
    print(f"\n{'='*60}")
    print(format_reconcile_report(result))
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
