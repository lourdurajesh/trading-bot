#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_report.py — AlphaLens Trading Bot Daily Analysis Script
=============================================================
Reads db/trades.db + logs/bot.log + db/conviction_daily.json and prints
a structured plain-text report for Claude to analyse.

Usage (from project root D:\\Tech\\trading-bot):
    python .claude/skills/trading-review/scripts/daily_report.py
    python .claude/skills/trading-review/scripts/daily_report.py --date 2026-05-26
    python .claude/skills/trading-review/scripts/daily_report.py --db db/trades.db --log logs/bot.log
"""

import argparse
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, date
from zoneinfo import ZoneInfo

# Force UTF-8 output on Windows (handles Rs. symbol and other chars cleanly)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

IST = ZoneInfo("Asia/Kolkata")

# -- Defaults — resolve relative to this script's location -----------
# Script lives at: <project>/.claude/skills/trading-review/scripts/
# Project root is: 4 levels up
_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "..", "..", ".."))
DEFAULT_DB         = os.path.join(_PROJECT_ROOT, "db",   "trades.db")
DEFAULT_LOG        = os.path.join(_PROJECT_ROOT, "logs", "bot.log")
DEFAULT_CONVICTION = os.path.join(_PROJECT_ROOT, "db",   "conviction_daily.json")


# --------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------

def fmt_pnl(v):
    if v is None: return "—"
    sign = "+" if v >= 0 else ""
    return f"Rs.{sign}{v:,.0f}"

def fmt_r(v):
    if v is None: return "—"
    return f"{v:+.2f}R"

def pct(num, den):
    return f"{num/den*100:.0f}%" if den else "—"

def hr(label=""):
    w = 68
    if label:
        pad = (w - len(label) - 2) // 2
        print(f"\n{'-'*pad} {label} {'-'*(w - pad - len(label) - 2)}")
    else:
        print("-" * w)


# --------------------------------------------------------------------
# DB QUERIES
# --------------------------------------------------------------------

def load_trades(db_path: str, trade_date: str) -> list[dict]:
    """Return commodity_learning_trades for the given date."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM commodity_learning_trades "
        "WHERE substr(entry_time,1,10)=? ORDER BY entry_time",
        (trade_date,)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def load_wallet(db_path: str) -> float:
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT balance FROM paper_wallet LIMIT 1")
    row = c.fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def load_cooldowns(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute(
            "SELECT symbol, reason, blocked_until FROM symbol_cooldowns "
            "ORDER BY blocked_until DESC"
        )
        rows = [dict(r) for r in c.fetchall()]
    except Exception:
        rows = []
    conn.close()
    return rows


def load_all_strategy_stats(db_path: str) -> list[dict]:
    """All-time strategy breakdown from commodity_learning_trades."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT strategy,
               COUNT(*)  AS total,
               SUM(CASE WHEN pnl_approx > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl_approx < 0 THEN 1 ELSE 0 END) AS losses,
               ROUND(SUM(pnl_approx))   AS total_pnl,
               ROUND(AVG(pnl_r), 3)     AS avg_r
        FROM commodity_learning_trades
        WHERE status != 'OPEN'
        GROUP BY strategy
        ORDER BY total_pnl DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def load_recent_daily_pnl(db_path: str, n: int = 10) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(f"""
        SELECT substr(entry_time,1,10) AS date,
               COUNT(*) AS total,
               SUM(CASE WHEN pnl_approx > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl_approx < 0 THEN 1 ELSE 0 END) AS losses,
               ROUND(SUM(pnl_approx)) AS pnl,
               ROUND(AVG(pnl_r), 3) AS avg_r
        FROM commodity_learning_trades
        GROUP BY substr(entry_time,1,10)
        ORDER BY date DESC
        LIMIT {n}
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# --------------------------------------------------------------------
# CONVICTION JSON
# --------------------------------------------------------------------

def load_conviction(conviction_path: str, trade_date: str) -> list[dict]:
    """Return all conviction entries for trade_date (there may be 2: 09:00 and 09:10)."""
    if not os.path.exists(conviction_path):
        return []
    with open(conviction_path) as f:
        history = json.load(f)
    return [h for h in history if h.get("date") == trade_date]


# --------------------------------------------------------------------
# LOG PARSING
# --------------------------------------------------------------------

_LOG_DATE_RE    = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_IEP_RE         = re.compile(r"(IEP|Fyers LTP|NSE IEP)[^\n]*")
_CONVICTION_RE  = re.compile(r"\[ConvictionScorer\][^\n]*")
_MAIN_SCORE_RE  = re.compile(r"\[Main\] Conviction score[^\n]*")
_RISK_BLOCK_RE  = re.compile(r"blocked by risk budget[^\n]*")
_NO_SPOT_RE     = re.compile(r"no valid spot[^\n]*")
_ENTRY_RE       = re.compile(r"PAPER OPEN[^\n]*")
_EXIT_RE        = re.compile(r"PAPER CLOSE[^\n]*")
_ERROR_RE       = re.compile(r"\[ERROR\][^\n]*|\[WARNING\][^\n]*")
_CAP_RE         = re.compile(r"daily entry cap reached[^\n]*")
_COOLDOWN_RE    = re.compile(r"entry cooldown[^\n]*")
_BUDGET_RE      = re.compile(r"daily loss[^\n]*limit[^\n]*")


def parse_log(log_path: str, trade_date: str) -> dict:
    """
    Stream through bot.log and extract lines relevant to trade_date.
    Returns a dict of categorised line lists.
    """
    result = {
        "iep_lines":       [],
        "conviction_lines": [],
        "main_score_lines": [],
        "risk_blocks":     [],
        "no_spot_lines":   [],
        "entries":         [],
        "exits":           [],
        "errors":          [],
        "cap_hits":        [],
        "cooldowns":       [],
        "budget_lines":    [],
    }

    if not os.path.exists(log_path):
        return result

    # Also look in rotated log files (bot.log.YYYY-MM-DD)
    log_files = [log_path]
    rotated = f"{log_path}.{trade_date}"
    if os.path.exists(rotated):
        log_files = [rotated, log_path]

    date_prefix = trade_date

    for lf in log_files:
        try:
            with open(lf, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    if not raw.startswith(date_prefix):
                        continue
                    line = raw.rstrip()
                    if _IEP_RE.search(line):
                        result["iep_lines"].append(line)
                    if _CONVICTION_RE.search(line):
                        result["conviction_lines"].append(line)
                    if _MAIN_SCORE_RE.search(line):
                        result["main_score_lines"].append(line)
                    if _RISK_BLOCK_RE.search(line):
                        result["risk_blocks"].append(line)
                    if _NO_SPOT_RE.search(line):
                        result["no_spot_lines"].append(line)
                    if _ENTRY_RE.search(line):
                        result["entries"].append(line)
                    if _EXIT_RE.search(line):
                        result["exits"].append(line)
                    if re.search(r"\[ERROR\]", line):
                        result["errors"].append(line)
                    if _CAP_RE.search(line):
                        result["cap_hits"].append(line)
                    if _COOLDOWN_RE.search(line):
                        result["cooldowns"].append(line[-120:])  # trim long lines
                    if _BUDGET_RE.search(line):
                        result["budget_lines"].append(line)
        except Exception as e:
            print(f"[WARN] Could not read {lf}: {e}", file=sys.stderr)

    # Deduplicate no-spot lines to unique symbol messages
    seen = set()
    deduped = []
    for ln in result["no_spot_lines"]:
        key = re.search(r"(no valid spot.*?sym=\S+)", ln)
        if key:
            k = key.group(1)
            if k not in seen:
                seen.add(k)
                deduped.append(ln)
    result["no_spot_lines"] = deduped

    return result


# --------------------------------------------------------------------
# REPORT SECTIONS
# --------------------------------------------------------------------

def section_conviction(conviction_entries: list[dict], log: dict):
    hr("CONVICTION SCORE")
    if not conviction_entries:
        print("  No conviction record found for this date.")
        # Try to show raw log lines
        for ln in log["main_score_lines"]:
            print(" ", ln[23:])  # strip timestamp
        return

    signals = ["fii_score", "oi_score", "iep_score", "vix_score", "gift_score", "rs_score"]
    labels  = ["FII", "OI", "IEP", "VIX", "GIFT", "RS"]

    # Print each run (09:00 and 09:10 are separate if history preserved)
    for entry in conviction_entries:
        ts        = entry.get("timestamp", "?")
        score     = entry.get("score", 0)
        direction = entry.get("direction", "?")
        tradeable = entry.get("tradeable", False)
        threshold = entry.get("threshold", 7)
        cap       = entry.get("capital_pct", 0)
        status    = f"✅ TRADE DAY (capital={cap}%)" if tradeable else f"❌ NO TRADE (below threshold {threshold})"
        print(f"\n  Run @ {ts}  →  Score {score:+d} {direction}  {status}")
        for sig, lbl in zip(signals, labels):
            val = entry.get(sig, 0)
            reasons = entry.get("reasons", [])
            reason_line = next((r for r in reasons if r.startswith(lbl)), "")
            detail = reason_line.split(": ", 1)[1] if ": " in reason_line else ""
            print(f"    {lbl:4s}  {val:+d}   {detail}")

    # IEP drift check
    if len(conviction_entries) >= 2:
        iep_0 = conviction_entries[0].get("iep_score", 0)
        iep_1 = conviction_entries[-1].get("iep_score", 0)
        if iep_0 != iep_1:
            print(f"\n  ⚠️  IEP DRIFT: {iep_0:+d} → {iep_1:+d}  "
                  f"({'dropped' if iep_1 < iep_0 else 'rose'} between runs)")
    elif len(conviction_entries) == 1:
        iep_0 = conviction_entries[0].get("iep_score", 0)
        # Try to detect drift from log lines
        iep_log_scores = re.findall(r"IEP \[([+-]\d+)\]", " ".join(log["conviction_lines"]))
        unique = list(dict.fromkeys(iep_log_scores))
        if len(unique) >= 2 and unique[0] != unique[-1]:
            print(f"\n  ⚠️  IEP DRIFT detected in logs: {unique[0]} → {unique[-1]}")

    # Raw main-score log lines for context
    print()
    for ln in log["main_score_lines"]:
        print(" ", ln[23:])

    # IEP raw log
    if log["iep_lines"]:
        print("\n  --- IEP fetch log ---")
        for ln in log["iep_lines"][:6]:
            print(" ", ln[23:])


def section_pnl(trades: list[dict], wallet: float, capital: float = 500_000):
    hr("P&L SUMMARY")
    closed  = [t for t in trades if t["status"] != "OPEN"]
    open_t  = [t for t in trades if t["status"] == "OPEN"]
    wins    = [t for t in closed if t["pnl_approx"] > 0]
    losses  = [t for t in closed if t["pnl_approx"] < 0]
    total   = sum(t["pnl_approx"] for t in closed)
    win_pnl = sum(t["pnl_approx"] for t in wins)
    los_pnl = sum(t["pnl_approx"] for t in losses)

    daily_loss_pct = abs(min(total, 0)) / capital * 100
    limit_pct      = 3.0

    best  = max(closed, key=lambda t: t["pnl_approx"], default=None)
    worst = min(closed, key=lambda t: t["pnl_approx"], default=None)

    print(f"  Total trades : {len(trades)}  (closed: {len(closed)}, open: {len(open_t)})")
    print(f"  Wins / Losses: {len(wins)} / {len(losses)}  (WR: {pct(len(wins), len(closed))})")
    print(f"  Total PnL    : {fmt_pnl(total)}")
    print(f"  Win PnL      : {fmt_pnl(win_pnl)}")
    print(f"  Loss PnL     : {fmt_pnl(los_pnl)}")
    if wins:
        avg_win_r = sum(t["pnl_r"] for t in wins) / len(wins)
        print(f"  Avg win R    : {avg_win_r:+.2f}R")
    if losses:
        avg_los_r = sum(t["pnl_r"] for t in losses) / len(losses)
        print(f"  Avg loss R   : {avg_los_r:+.2f}R")
    if best:
        print(f"  Best trade   : {fmt_pnl(best['pnl_approx'])} ({fmt_r(best['pnl_r'])}) "
              f"{best['instrument']} {best['strategy']}")
    if worst:
        print(f"  Worst trade  : {fmt_pnl(worst['pnl_approx'])} ({fmt_r(worst['pnl_r'])}) "
              f"{worst['instrument']} {worst['strategy']}")
    bar = "█" * int(daily_loss_pct / limit_pct * 20) if total < 0 else ""
    loss_flag = " ⚠️  NEAR LIMIT" if daily_loss_pct > 2.0 else ("  🚨 LIMIT HIT" if daily_loss_pct >= limit_pct else "")
    print(f"  Daily loss   : {daily_loss_pct:.1f}% of {limit_pct}% limit  {bar}{loss_flag}")
    print(f"  Wallet       : Rs.{wallet:,.0f}")


def section_open_positions(trades: list[dict]):
    open_t = [t for t in trades if t["status"] == "OPEN"]
    if not open_t:
        return
    hr("OPEN POSITIONS")
    now = datetime.now(tz=IST)
    for t in open_t:
        try:
            entry_dt = datetime.fromisoformat(t["entry_time"])
            hours_open = (now - entry_dt).total_seconds() / 3600
        except Exception:
            hours_open = 0

        meta = {}
        try:
            meta = json.loads(t.get("metadata") or "{}")
        except Exception:
            pass

        sl_price = meta.get("sl_price", "?")
        trail    = "trailing ✓" if meta.get("trail_active") else "fixed SL"
        peak     = meta.get("peak_spot", "?")
        print(f"  {t['instrument']:12s} {t['direction']:5s} | entry {t['spot_at_entry']:,.0f} | "
              f"SL {sl_price} | peak {peak} | {trail} | "
              f"{hours_open:.1f}h open | DTE≈{t.get('dte',21)}d")


def section_trade_log(trades: list[dict]):
    hr("TRADE LOG (CLOSED)")
    closed = [t for t in trades if t["status"] != "OPEN"]
    if not closed:
        print("  No closed trades.")
        return

    print(f"  {'Time':11s} {'Symbol':12s} {'Dir':5s} {'Strategy':18s} "
          f"{'Exit':15s} {'PnL':>10s} {'R':>7s}")
    hr()
    for t in closed:
        entry_t = t["entry_time"][11:16] if t["entry_time"] else "?"
        exit_t  = t["exit_time"][11:16]  if t["exit_time"]  else "open"
        pnl     = t["pnl_approx"] or 0
        r_val   = t["pnl_r"] or 0
        pnl_str = fmt_pnl(pnl)
        r_str   = fmt_r(r_val)
        flag    = " ✅" if pnl > 0 else (" ❌" if pnl < 0 else "")
        print(f"  {entry_t}-{exit_t:5s} {t['instrument']:12s} {t['direction']:5s} "
              f"{t['strategy']:18s} {t.get('exit_reason','?'):15s} "
              f"{pnl_str:>10s} {r_str:>7s}{flag}")


def section_strategy_breakdown(trades: list[dict], all_time: list[dict]):
    hr("STRATEGY PERFORMANCE")
    closed = [t for t in trades if t["status"] != "OPEN"]

    # Today per strategy
    today_stats = {}
    for t in closed:
        s = t["strategy"]
        if s not in today_stats:
            today_stats[s] = {"wins": 0, "losses": 0, "pnl": 0, "r": []}
        if t["pnl_approx"] > 0:
            today_stats[s]["wins"] += 1
        elif t["pnl_approx"] < 0:
            today_stats[s]["losses"] += 1
        today_stats[s]["pnl"] += t["pnl_approx"] or 0
        today_stats[s]["r"].append(t["pnl_r"] or 0)

    # Merge with all-time
    all_map = {r["strategy"]: r for r in all_time}
    strategies = sorted(set(list(today_stats.keys()) + list(all_map.keys())))

    print(f"  {'Strategy':20s} {'Today W/L':10s} {'Today PnL':12s} "
          f"{'All-time W%':12s} {'All-time PnL':14s} {'AvgR':>6s}")
    hr()
    for s in strategies:
        td = today_stats.get(s)
        at = all_map.get(s)
        today_wl  = f"{td['wins']}/{td['losses']}" if td else "—/—"
        today_pnl = fmt_pnl(td["pnl"]) if td else "—"
        at_wp     = pct(at["wins"], at["total"]) if at else "—"
        at_pnl    = fmt_pnl(at["total_pnl"]) if at else "—"
        at_r      = f"{at['avg_r']:+.3f}R" if at else "—"
        warn = "  ⚠️" if at and at["wins"] / max(at["total"], 1) < 0.30 else ""
        print(f"  {s:20s} {today_wl:10s} {today_pnl:12s} {at_wp:12s} {at_pnl:14s} {at_r:>6s}{warn}")


def section_anomalies(trades: list[dict], log: dict):
    hr("ANOMALIES")
    found = False

    # 1. no valid spot
    if log["no_spot_lines"]:
        found = True
        # Group by symbol
        symbols = {}
        for ln in log["no_spot_lines"]:
            m = re.search(r"(\w+) no valid spot \(got (\S+), sym=(\S+)\)", ln)
            if m:
                instr, val, sym = m.group(1), m.group(2), m.group(3)
                symbols[instr] = (val, sym)
        for instr, (val, sym) in symbols.items():
            if val == "None":
                print(f"  🔴 STALE CONTRACT: {instr} → {sym} returns None spot.")
                print(f"       Fix: increase rollover_buffer in commodity_instruments for {instr}.")
            else:
                print(f"  🔴 PRICE RANGE: {instr} spot={val} out of min/max for {sym}.")
                print(f"       Fix: UPDATE commodity_instruments SET max_price=... WHERE name='{instr}';")

    # 2. risk budget blocks
    if log["risk_blocks"]:
        found = True
        pcts = re.findall(r"push daily loss to ([\d.]+)%", " ".join(log["risk_blocks"]))
        blocked_syms = re.findall(r"\[CommOpts\] (\w+) blocked by risk", " ".join(log["risk_blocks"]))
        max_loss = max((float(p) for p in pcts), default=0)
        print(f"  🟠 RISK BUDGET: {len(log['risk_blocks'])} entries blocked "
              f"(peak projected loss {max_loss:.1f}%). "
              f"Instruments: {', '.join(set(blocked_syms))}")

    # 3. IEP drift
    iep_scores_in_log = re.findall(r"IEP \[([+-]\d+)\]", " ".join(log["conviction_lines"]))
    unique_iep = list(dict.fromkeys(iep_scores_in_log))
    if len(unique_iep) >= 2 and unique_iep[0] != unique_iep[-1]:
        found = True
        print(f"  🟠 IEP DRIFT: score changed {unique_iep[0]} → {unique_iep[-1]} during pre-open window.")
        print(f"       This may cause a TRADE DAY to flip to NO TRADE.")
        print(f"       Fix: intelligence/conviction_scorer.py — lock-in on first non-zero IEP.")

    # 4. false breakouts
    fb = [t for t in trades if t.get("exit_reason") == "FALSE_BREAKOUT"]
    if fb:
        found = True
        for t in fb:
            dur_mins = "?"
            try:
                entry = datetime.fromisoformat(t["entry_time"])
                exit_ = datetime.fromisoformat(t["exit_time"])
                dur_mins = int((exit_ - entry).total_seconds() / 60)
            except Exception:
                pass
            print(f"  🟡 FALSE BREAKOUT: {t['instrument']} {t['direction']} "
                  f"lasted {dur_mins}min — {fmt_pnl(t['pnl_approx'])} loss.")
        print(f"       Fix: add EMA trend gate to BreakoutSpread entries.")

    # 5. cap hits
    if log["cap_hits"]:
        found = True
        seen_caps = set()
        for ln in log["cap_hits"]:
            m = re.search(r"(\w+) daily entry cap reached \((\d+/\d+)\)", ln)
            if m:
                key = f"{m.group(1)} {m.group(2)}"
                if key not in seen_caps:
                    seen_caps.add(key)
                    print(f"  🟡 ENTRY CAP: {m.group(1)} hit {m.group(2)} cap — "
                          f"further signals skipped.")

    # 6. errors in log
    if log["errors"]:
        found = True
        print(f"  🔴 ERRORS in log: {len(log['errors'])} error/warning lines.")
        for ln in log["errors"][:5]:
            print(f"       {ln[23:80]}")
        if len(log["errors"]) > 5:
            print(f"       ... and {len(log['errors'])-5} more.")

    if not found:
        print("  ✅  No anomalies detected.")


def section_cooldowns(cooldowns: list[dict], log: dict):
    if not cooldowns:
        return
    hr("ACTIVE COOLDOWNS")
    now = datetime.now(tz=IST)
    for cd in cooldowns:
        sym    = cd.get("symbol", "?").replace("MCX:", "").replace("26JUNFUT", "").replace("26MAYFUT", "")
        reason = cd.get("reason", "?")
        until  = cd.get("blocked_until", "?")
        try:
            until_dt = datetime.fromisoformat(until)
            hrs_left = (until_dt - now).total_seconds() / 3600
            eta = f"{hrs_left:.1f}h"
        except Exception:
            eta = until
        print(f"  {sym:15s}  reason={reason:20s}  until {until[:16]}  ({eta} remaining)")


def section_recent_history(recent: list[dict]):
    hr("RECENT DAILY PnL (last 10 trading days)")
    print(f"  {'Date':12s} {'Trades':>7s} {'W/L':>5s} {'WR':>6s} {'PnL':>12s} {'AvgR':>7s}")
    hr()
    for r in recent:
        w = r["wins"] or 0
        l = r["losses"] or 0
        wr = pct(w, w + l)
        pnl_str = fmt_pnl(r["pnl"])
        flag = " ✅" if (r["pnl"] or 0) > 0 else (" ❌" if (r["pnl"] or 0) < 0 else "")
        print(f"  {r['date']:12s} {r['total']:>7d} {w}/{l:>2d}  {wr:>6s} {pnl_str:>12s} "
              f"{(r['avg_r'] or 0):>+7.3f}R{flag}")


# --------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AlphaLens Daily Trade Report")
    parser.add_argument("--date", default=None,
                        help="Date to analyse (YYYY-MM-DD). Default: today IST.")
    parser.add_argument("--db",  default=DEFAULT_DB,  help="Path to trades.db")
    parser.add_argument("--log", default=DEFAULT_LOG, help="Path to bot.log")
    parser.add_argument("--conviction", default=DEFAULT_CONVICTION,
                        help="Path to conviction_daily.json")
    parser.add_argument("--capital", type=float, default=500_000,
                        help="Total capital for loss% calculation (default 500000)")
    args = parser.parse_args()

    trade_date = args.date or datetime.now(tz=IST).date().isoformat()

    # Normalise paths
    db_path    = os.path.normpath(args.db)
    log_path   = os.path.normpath(args.log)
    conv_path  = os.path.normpath(args.conviction)

    print("=" * 68)
    print(f"  ALPHALENS TRADING BOT — DAILY REPORT")
    print(f"  Date : {trade_date}")
    print(f"  DB   : {db_path}")
    print(f"  Log  : {log_path}")
    print(f"  Time : {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 68)

    # Load everything
    trades     = load_trades(db_path, trade_date)
    wallet     = load_wallet(db_path)
    cooldowns  = load_cooldowns(db_path)
    all_strat  = load_all_strategy_stats(db_path)
    recent     = load_recent_daily_pnl(db_path)
    conviction = load_conviction(conv_path, trade_date)
    log        = parse_log(log_path, trade_date)

    if not trades and not conviction:
        print(f"\n  No data found for {trade_date}.")
        print("  Check that the date is a trading day and the bot ran.")
        sys.exit(0)

    # Print all sections
    section_conviction(conviction, log)
    section_pnl(trades, wallet, args.capital)
    section_open_positions(trades)
    section_trade_log(trades)
    section_strategy_breakdown(trades, all_strat)
    section_anomalies(trades, log)
    section_cooldowns(cooldowns, log)
    section_recent_history(recent)

    print("\n" + "=" * 68)
    print("  END OF REPORT — feed this to Claude with /trading-review")
    print("=" * 68)


if __name__ == "__main__":
    main()
