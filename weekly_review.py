"""
weekly_review.py
────────────────
Weekly trade review report. Run every Sunday evening (or any time).

Usage:
    python weekly_review.py             # current week
    python weekly_review.py --weeks 2   # last 2 weeks
    python weekly_review.py --all       # all time

Output:
    - Prints summary to console
    - Saves CSV  → db/reports/weekly_YYYYMMDD.csv
    - Saves JSON → db/reports/weekly_YYYYMMDD.json
"""

import argparse
import csv
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

DB_PATH    = os.getenv("DB_PATH",    "db/trades.db")
AUDIT_DB   = DB_PATH.replace("trades.db", "audit.db")
REPORT_DIR = "db/reports"
os.makedirs(REPORT_DIR, exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────

def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def _ist(utc_iso: str) -> datetime:
    """Convert UTC ISO string from DB → IST datetime."""
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        return dt.astimezone(IST)
    except Exception:
        return datetime.now(tz=IST)

def _fmt_pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"₹{sign}{v:,.0f}"

def _pct(v: float, total: float) -> str:
    if not total:
        return "0.0%"
    return f"{v / total * 100:+.1f}%"


# ── Fetch trades ─────────────────────────────────────────────────

def fetch_closed_trades(since: date) -> list[dict]:
    """Fetch all closed trades on or after `since` date (IST)."""
    trades = []
    try:
        with _conn(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status != 'OPEN' ORDER BY entry_time"
            ).fetchall()
        for row in rows:
            row = dict(row)
            entry_ist = _ist(row["entry_time"]) if row["entry_time"] else None
            exit_ist  = _ist(row["exit_time"])  if row["exit_time"]  else None
            if entry_ist and entry_ist.date() >= since:
                row["entry_ist"] = entry_ist.strftime("%Y-%m-%d %H:%M")
                row["exit_ist"]  = exit_ist.strftime("%Y-%m-%d %H:%M") if exit_ist else "-"
                row["date"]      = entry_ist.date().isoformat()
                row["pnl"]       = float(row["realised_pnl"] or 0)
                trades.append(row)
    except Exception as e:
        print(f"  [DB] Could not read trades: {e}")
    return trades


def fetch_audit_events(since: date, event_types: list[str]) -> list[dict]:
    """Fetch audit events of given types on or after `since` date."""
    events = []
    try:
        placeholders = ",".join("?" * len(event_types))
        with _conn(AUDIT_DB) as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_log WHERE event_type IN ({placeholders}) ORDER BY id",
                event_types,
            ).fetchall()
        for row in rows:
            row = dict(row)
            ts = _ist(row["timestamp"]) if row["timestamp"] else None
            if ts and ts.date() >= since:
                row["ts_ist"] = ts.strftime("%Y-%m-%d %H:%M")
                events.append(row)
    except Exception as e:
        print(f"  [AuditDB] Could not read events: {e}")
    return events


# ── Analysis ─────────────────────────────────────────────────────

def analyse(trades: list[dict], capital: float) -> dict:
    if not trades:
        return {}

    pnls      = [t["pnl"] for t in trades]
    winners   = [p for p in pnls if p > 0]
    losers    = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate  = len(winners) / len(pnls) * 100 if pnls else 0

    avg_win   = sum(winners) / len(winners) if winners else 0
    avg_loss  = sum(losers)  / len(losers)  if losers  else 0
    profit_factor = abs(sum(winners) / sum(losers)) if sum(losers) != 0 else float("inf")

    # Best / worst
    best  = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])

    # By strategy
    by_strategy: dict[str, dict] = {}
    for t in trades:
        s = t.get("strategy") or "unknown"
        if s not in by_strategy:
            by_strategy[s] = {"trades": 0, "pnl": 0.0, "wins": 0}
        by_strategy[s]["trades"] += 1
        by_strategy[s]["pnl"]    += t["pnl"]
        if t["pnl"] > 0:
            by_strategy[s]["wins"] += 1

    # By exit reason
    by_reason: dict[str, dict] = {}
    for t in trades:
        r = (t.get("exit_reason") or "unknown").upper()
        if r not in by_reason:
            by_reason[r] = {"trades": 0, "pnl": 0.0}
        by_reason[r]["trades"] += 1
        by_reason[r]["pnl"]    += t["pnl"]

    # Daily P&L
    daily: dict[str, float] = {}
    for t in trades:
        d = t.get("date", "?")
        daily[d] = daily.get(d, 0.0) + t["pnl"]

    # Consecutive losses (max drawdown streak)
    max_consec_loss = 0
    cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    return {
        "total_trades":     len(trades),
        "winners":          len(winners),
        "losers":           len(losers),
        "win_rate_pct":     round(win_rate, 1),
        "total_pnl":        round(total_pnl, 2),
        "total_pnl_pct":    round(total_pnl / capital * 100, 2) if capital else 0,
        "avg_win":          round(avg_win, 2),
        "avg_loss":         round(avg_loss, 2),
        "profit_factor":    round(profit_factor, 2),
        "best_trade":       {"symbol": best["symbol"], "pnl": best["pnl"], "date": best["date"]},
        "worst_trade":      {"symbol": worst["symbol"], "pnl": worst["pnl"], "date": worst["date"]},
        "max_consec_losses": max_consec_loss,
        "by_strategy":      by_strategy,
        "by_exit_reason":   by_reason,
        "daily_pnl":        daily,
    }


# ── Print report ─────────────────────────────────────────────────

def print_report(trades: list[dict], stats: dict, since: date, capital: float,
                 kill_events: list[dict], rejected_count: int) -> None:
    sep = "─" * 60

    print(f"\n{'═' * 60}")
    print(f"  TRADING REVIEW  |  {since}  →  {date.today()}")
    print(f"{'═' * 60}")

    if not trades:
        print("  No closed trades in this period.")
        print(f"{'═' * 60}\n")
        return

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n  SUMMARY")
    print(sep)
    print(f"  Trades          : {stats['total_trades']}  "
          f"({stats['winners']} wins / {stats['losers']} losses)")
    print(f"  Win rate        : {stats['win_rate_pct']}%")
    print(f"  Total P&L       : {_fmt_pnl(stats['total_pnl'])}  "
          f"({stats['total_pnl_pct']:+.2f}% of capital)")
    print(f"  Avg win         : {_fmt_pnl(stats['avg_win'])}")
    print(f"  Avg loss        : {_fmt_pnl(stats['avg_loss'])}")
    print(f"  Profit factor   : {stats['profit_factor']:.2f}x")
    print(f"  Max consec loss : {stats['max_consec_losses']}")

    if kill_events:
        print(f"\n  ⚠  Kill switch triggered {len(kill_events)}x this period:")
        for e in kill_events:
            print(f"     {e['ts_ist']}  {e.get('reason','')[:70]}")

    print(f"  Signals rejected: {rejected_count}  (risk/intelligence filters)")

    # ── By strategy ──────────────────────────────────────────────
    print(f"\n  BY STRATEGY")
    print(sep)
    for strat, s in sorted(stats["by_strategy"].items(),
                           key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        print(f"  {strat:<22}  {s['trades']:>3} trades  "
              f"WR {wr:>5.1f}%  P&L {_fmt_pnl(s['pnl'])}")

    # ── By exit reason ───────────────────────────────────────────
    print(f"\n  BY EXIT REASON")
    print(sep)
    for reason, r in sorted(stats["by_exit_reason"].items(),
                            key=lambda x: x[1]["trades"], reverse=True):
        print(f"  {reason:<22}  {r['trades']:>3} trades  P&L {_fmt_pnl(r['pnl'])}")

    # ── Daily P&L ────────────────────────────────────────────────
    print(f"\n  DAILY P&L")
    print(sep)
    for day, pnl in sorted(stats["daily_pnl"].items()):
        bar_len = int(abs(pnl) / max(abs(v) for v in stats["daily_pnl"].values()) * 20)
        bar = ("█" * bar_len) if pnl >= 0 else ("░" * bar_len)
        sign = "+" if pnl >= 0 else " "
        print(f"  {day}  {sign}{bar:<20}  {_fmt_pnl(pnl)}")

    # ── Best / Worst ─────────────────────────────────────────────
    print(f"\n  BEST TRADE  : {stats['best_trade']['symbol']:25}  "
          f"{_fmt_pnl(stats['best_trade']['pnl'])}  ({stats['best_trade']['date']})")
    print(f"  WORST TRADE : {stats['worst_trade']['symbol']:25}  "
          f"{_fmt_pnl(stats['worst_trade']['pnl'])}  ({stats['worst_trade']['date']})")

    # ── Trade log ────────────────────────────────────────────────
    print(f"\n  ALL TRADES")
    print(sep)
    print(f"  {'DATE':<12} {'SYMBOL':<22} {'DIR':<6} {'STRATEGY':<22} "
          f"{'EXIT':<12} {'P&L':>10}")
    print(f"  {'-'*12} {'-'*22} {'-'*6} {'-'*22} {'-'*12} {'-'*10}")
    for t in trades:
        sym    = (t.get("symbol") or "").replace("NSE:","").replace("-EQ","")[:22]
        strat  = (t.get("strategy") or "")[:22]
        reason = (t.get("exit_reason") or "")[:12]
        pnl    = t["pnl"]
        marker = "✓" if pnl > 0 else "✗"
        print(f"  {t['date']:<12} {sym:<22} {t.get('direction',''):<6} "
              f"{strat:<22} {reason:<12} {_fmt_pnl(pnl):>10} {marker}")

    print(f"\n{'═' * 60}\n")


# ── Save outputs ─────────────────────────────────────────────────

def save_csv(trades: list[dict], path: str) -> None:
    if not trades:
        return
    keys = ["date", "entry_ist", "exit_ist", "symbol", "direction", "strategy",
            "entry_price", "exit_price", "position_size", "realised_pnl",
            "capital_at_risk", "exit_reason", "signal_type"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)
    print(f"  CSV saved  → {path}")


def save_json(stats: dict, trades: list[dict], path: str) -> None:
    out = {"stats": stats, "trades": trades}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"  JSON saved → {path}")


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weekly trade review")
    parser.add_argument("--weeks", type=int, default=1,
                        help="How many weeks to look back (default: 1)")
    parser.add_argument("--all",   action="store_true",
                        help="Report on all trades ever")
    parser.add_argument("--capital", type=float, default=500000,
                        help="Starting capital for % calculations (default: 500000)")
    args = parser.parse_args()

    if args.all:
        since = date(2020, 1, 1)
        label = "all_time"
    else:
        since = date.today() - timedelta(weeks=args.weeks)
        label = date.today().strftime("%Y%m%d")

    capital = args.capital

    print(f"\n  Loading trades since {since}...")
    trades = fetch_closed_trades(since)
    stats  = analyse(trades, capital)

    kill_events     = fetch_audit_events(since, ["KILL_SWITCH"])
    kill_activations = [e for e in kill_events if '"activated": true' in e.get("details", "")]
    rejected_count  = len(fetch_audit_events(since, ["SIGNAL_REJECTED", "INTELLIGENCE_VETO"]))

    print_report(trades, stats, since, capital, kill_activations, rejected_count)

    # Save files
    csv_path  = os.path.join(REPORT_DIR, f"weekly_{label}.csv")
    json_path = os.path.join(REPORT_DIR, f"weekly_{label}.json")
    save_csv(trades, csv_path)
    if stats:
        save_json(stats, trades, json_path)


if __name__ == "__main__":
    main()
