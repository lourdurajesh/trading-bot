"""
daily_review.py
───────────────
Review today's bot activity: open positions, closed trades, audit trail.

Usage:
    python daily_review.py              # today (IST)
    python daily_review.py --date 2026-03-24
    python daily_review.py --capital 1000000

Output:
    - Printed report to console
    - Saved to db/reports/daily_YYYYMMDD.json
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# Force UTF-8 output on Windows terminals that default to cp1252
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

IST = ZoneInfo("Asia/Kolkata")

DB_PATH    = os.getenv("DB_PATH", "db/trades.db")
AUDIT_DB   = DB_PATH.replace("trades.db", "audit.db")
REPORT_DIR = "db/reports"
os.makedirs(REPORT_DIR, exist_ok=True)


# ── DB helpers ────────────────────────────────────────────────────

def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _to_ist(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(IST)
    except Exception:
        return None


def _fmt(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"Rs.{sign}{v:,.0f}"


def _bar(pnl: float, scale: float = 1.0) -> str:
    length = min(int(abs(pnl) / max(scale, 1) * 20), 20)
    return ("█" * length) if pnl >= 0 else ("░" * length)


# ── Data fetch ───────────────────────────────────────────────────

def fetch_open_positions(day: date) -> list[dict]:
    rows = []
    try:
        with _conn(DB_PATH) as conn:
            for row in conn.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY entry_time"
            ).fetchall():
                r = dict(row)
                t = _to_ist(r["entry_time"])
                if t and t.date() == day:
                    r["entry_ist"] = t.strftime("%H:%M:%S")
                    rows.append(r)
    except Exception as e:
        print(f"  [DB] open positions error: {e}")
    return rows


def fetch_closed_today(day: date) -> list[dict]:
    rows = []
    try:
        with _conn(DB_PATH) as conn:
            for row in conn.execute(
                "SELECT * FROM trades WHERE status != 'OPEN' ORDER BY exit_time"
            ).fetchall():
                r = dict(row)
                t = _to_ist(r["exit_time"])
                if t and t.date() == day:
                    r["entry_ist"] = (_to_ist(r["entry_time"]) or t).strftime("%H:%M:%S")
                    r["exit_ist"]  = t.strftime("%H:%M:%S")
                    r["pnl"]       = float(r["realised_pnl"] or 0)
                    rows.append(r)
    except Exception as e:
        print(f"  [DB] closed trades error: {e}")
    return rows


def fetch_paper_closed_today(day: date) -> list[dict]:
    rows = []
    try:
        with _conn(DB_PATH) as conn:
            for row in conn.execute(
                "SELECT * FROM paper_trades WHERE status != 'OPEN' ORDER BY exit_time"
            ).fetchall():
                r = dict(row)
                t = _to_ist(r["exit_time"])
                if t and t.date() == day:
                    r["entry_ist"] = (_to_ist(r["entry_time"]) or t).strftime("%H:%M:%S")
                    r["exit_ist"]  = t.strftime("%H:%M:%S")
                    r["pnl"]       = float(r["realised_pnl"] or 0)
                    r["signal_type"] = r.get("signal_type", "EQUITY")
                    rows.append(r)
    except Exception as e:
        print(f"  [DB] paper trades error: {e}")
    return rows


def fetch_audit_today(day: date) -> list[dict]:
    rows = []
    try:
        with _conn(AUDIT_DB) as conn:
            for row in conn.execute(
                "SELECT * FROM audit_log ORDER BY id"
            ).fetchall():
                r = dict(row)
                t = _to_ist(r["timestamp"])
                if t and t.date() == day:
                    r["ts_ist"] = t.strftime("%H:%M:%S")
                    rows.append(r)
    except Exception as e:
        print(f"  [AuditDB] error: {e}")
    return rows


# ── Report ───────────────────────────────────────────────────────

def print_report(
    day: date,
    open_pos: list[dict],
    closed: list[dict],
    paper: list[dict],
    audit: list[dict],
    capital: float,
) -> dict:

    sep  = "-" * 62
    sep2 = "=" * 62

    print(f"\n{sep2}")
    print(f"  DAILY REVIEW  |  {day.strftime('%A, %d %b %Y')}  (IST)")
    print(sep2)

    # ── Realised P&L summary ─────────────────────────────────────
    pnls      = [t["pnl"] for t in closed]
    total_pnl = sum(pnls)
    winners   = [p for p in pnls if p > 0]
    losers    = [p for p in pnls if p <= 0]
    win_rate  = len(winners) / len(pnls) * 100 if pnls else 0.0

    paper_pnls = [t["pnl"] for t in paper]
    paper_total = sum(paper_pnls)

    print(f"\n  LIVE TRADING SUMMARY")
    print(sep)
    if not closed and not open_pos:
        print("  No live trades today.")
    else:
        print(f"  Closed trades   : {len(closed)}"
              f"  ({len(winners)} W / {len(losers)} L)"
              + (f"  |  Win rate: {win_rate:.0f}%" if closed else ""))
        print(f"  Open positions  : {len(open_pos)}")
        print(f"  Realised P&L    : {_fmt(total_pnl)}"
              + (f"  ({total_pnl / capital * 100:+.3f}% of capital)" if capital else ""))
        if closed:
            max_pnl = max(abs(p) for p in pnls) or 1
            for t in closed:
                marker = "W" if t["pnl"] > 0 else "L"
                bar = _bar(t["pnl"], max_pnl)
                sym = (t.get("symbol") or "").replace("NSE:", "").replace("-EQ", "")[:20]
                print(f"  [{marker}] {t['exit_ist']}  {sym:<20}  "
                      f"{t.get('strategy','')[:18]:<18}  "
                      f"{t.get('exit_reason','')[:10]:<10}  {_fmt(t['pnl']):>12}  {bar}")

    if paper:
        print(f"\n  PAPER TRADING SUMMARY")
        print(sep)
        print(f"  Closed trades   : {len(paper)}")
        print(f"  Realised P&L    : {_fmt(paper_total)}")
        for t in paper:
            marker = "W" if t["pnl"] > 0 else "L"
            sym = (t.get("symbol") or "").replace("NSE:", "").replace("-EQ", "")[:20]
            print(f"  [{marker}] {t['exit_ist']}  {sym:<20}  "
                  f"{t.get('strategy','')[:18]:<18}  "
                  f"{t.get('exit_reason','')[:10]:<10}  {_fmt(t['pnl']):>12}")

    # ── Open positions detail ────────────────────────────────────
    if open_pos:
        print(f"\n  OPEN POSITIONS (unrealised P&L not available without live feed)")
        print(sep)
        print(f"  {'ENTERED':<10}  {'SYMBOL':<22}  {'DIR':<5}  {'STRATEGY':<20}"
              f"  {'QTY':>5}  {'ENTRY':>8}  {'SL':>8}  {'T1':>8}")
        print(f"  {'-'*10}  {'-'*22}  {'-'*5}  {'-'*20}  {'-'*5}  {'-'*8}  {'-'*8}  {'-'*8}")
        for p in open_pos:
            sym = (p.get("symbol") or "").replace("NSE:", "").replace("-EQ", "")[:22]
            print(f"  {p['entry_ist']:<10}  {sym:<22}  {p.get('direction',''):<5}  "
                  f"{p.get('strategy','')[:20]:<20}  {p.get('position_size',0):>5}  "
                  f"{p.get('entry_price',0):>8.2f}  {p.get('stop_loss',0):>8.2f}  "
                  f"{p.get('target_1',0):>8.2f}")

    # ── Audit timeline ───────────────────────────────────────────
    print(f"\n  TODAY'S AUDIT TIMELINE")
    print(sep)

    if not audit:
        print("  No audit events recorded today.")
    else:
        # Group counts by event type
        type_counts: dict[str, int] = {}
        for e in audit:
            et = e["event_type"]
            type_counts[et] = type_counts.get(et, 0) + 1

        print(f"  Total events: {len(audit)}")
        for et, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"    {et:<28}  {cnt:>3}x")

        print(f"\n  {'TIME':<10}  {'EVENT':<25}  {'SYMBOL':<22}  DETAIL")
        print(f"  {'-'*10}  {'-'*25}  {'-'*22}  {'-'*30}")
        for e in audit:
            sym    = (e.get("symbol") or "")[:22]
            detail = (e.get("reason") or e.get("details") or "")[:50]
            print(f"  {e['ts_ist']:<10}  {e['event_type']:<25}  {sym:<22}  {detail}")

    # ── Rejected signals analysis ────────────────────────────────
    rejected = [e for e in audit if e["event_type"] in ("SIGNAL_REJECTED", "INTELLIGENCE_VETO")]
    if rejected:
        print(f"\n  REJECTED SIGNALS ({len(rejected)} total — risk/intelligence filters)")
        print(sep)
        reason_counts: dict[str, int] = {}
        for e in rejected:
            r = (e.get("reason") or "unknown")[:50]
            reason_counts[r] = reason_counts.get(r, 0) + 1
        for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {cnt:>3}x  {reason}")

    # ── Kill switch check ────────────────────────────────────────
    kill = [e for e in audit if e["event_type"] == "KILL_SWITCH"]
    if kill:
        print(f"\n  KILL SWITCH EVENTS")
        print(sep)
        for e in kill:
            print(f"  {e['ts_ist']}  {e.get('reason','')}")
    else:
        print(f"\n  Kill switch: NOT triggered today")

    print(f"\n{sep2}\n")

    # Return structured data for JSON save
    return {
        "date":          day.isoformat(),
        "capital":       capital,
        "live": {
            "closed_trades":  len(closed),
            "open_positions": len(open_pos),
            "realised_pnl":   round(total_pnl, 2),
            "pnl_pct":        round(total_pnl / capital * 100, 4) if capital else 0,
            "win_rate_pct":   round(win_rate, 1),
            "trades":         closed,
            "open":           open_pos,
        },
        "paper": {
            "closed_trades": len(paper),
            "realised_pnl":  round(paper_total, 2),
            "trades":        paper,
        },
        "audit": {
            "total_events":    len(audit),
            "rejected_signals": len(rejected),
            "kill_switch_fired": len(kill) > 0,
            "event_counts":    type_counts if audit else {},
            "timeline":        audit,
        },
    }


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily bot performance review")
    parser.add_argument("--date",    type=str,   default=None,
                        help="Date to review YYYY-MM-DD (default: today IST)")
    parser.add_argument("--capital", type=float, default=500000,
                        help="Capital for %% P&L calculation (default: 500000)")
    args = parser.parse_args()

    if args.date:
        day = date.fromisoformat(args.date)
    else:
        day = datetime.now(tz=IST).date()

    capital = args.capital

    open_pos = fetch_open_positions(day)
    closed   = fetch_closed_today(day)
    paper    = fetch_paper_closed_today(day)
    audit    = fetch_audit_today(day)

    result = print_report(day, open_pos, closed, paper, audit, capital)

    # Save JSON
    label     = day.strftime("%Y%m%d")
    json_path = os.path.join(REPORT_DIR, f"daily_{label}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"  Saved -> {json_path}\n")


if __name__ == "__main__":
    main()
