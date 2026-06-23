"""
strategy_pnl.py
───────────────
Strategy-level P&L ranking across all books (learning NSE + MCX), to decide which
strategies to keep/disable. Only counts CLOSED trades on/after the analysis_epoch
(app_meta) — so pre-fix data from before a reset is never mixed in.

Run:  PYTHONPATH=. venv/bin/python scripts/strategy_pnl.py
"""
import sqlite3
from collections import defaultdict

from config.settings import DB_PATH
from learning_engine import learning_engine
from commodity_options_learning import commodity_options


def _epoch() -> str:
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT value FROM app_meta WHERE key='analysis_epoch'").fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _agg(trades, inr_key, epoch):
    d = defaultdict(lambda: {"n": 0, "wins": 0, "r": 0.0, "inr": 0.0})
    for t in trades:
        if t.get("status") != "CLOSED":
            continue
        if epoch and (t.get("entry_time") or "") < epoch:
            continue
        s = t.get("strategy", "?")
        r = float(t.get("pnl_r") or 0)
        e = d[s]
        e["n"] += 1
        e["wins"] += 1 if r > 0 else 0
        e["r"] += r
        e["inr"] += float(t.get(inr_key) or 0)
    return d


def main():
    epoch = _epoch()
    print(f"analysis_epoch: {epoch or '(none — counting all trades)'}\n")
    rows = []
    for s, e in _agg(learning_engine.get_trades(limit=10000), "pnl_inr", epoch).items():
        rows.append(("NSE", s, e))
    for s, e in _agg(commodity_options.get_trades(limit=10000), "pnl_approx", epoch).items():
        rows.append(("MCX", s, e))
    rows.sort(key=lambda x: x[2]["inr"])

    print(f"{'BOOK':4} {'STRATEGY':26} {'N':>4} {'WIN%':>5} {'AVG_R':>6} {'TOT_R':>7} {'TOT_INR':>10}")
    print("-" * 70)
    if not rows:
        print("(no closed trades since the epoch yet)")
    for book, s, e in rows:
        win = round(e["wins"] / e["n"] * 100) if e["n"] else 0
        avgr = round(e["r"] / e["n"], 2) if e["n"] else 0
        print(f"{book:4} {s[:26]:26} {e['n']:>4} {win:>4}% {avgr:>6} {round(e['r'],1):>7} {round(e['inr']):>10}")


if __name__ == "__main__":
    main()
