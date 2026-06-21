"""
run_analysis.py
───────────────
Comprehensive strategy validation CLI.

Runs all four layers of analysis in sequence:
  1. Trade Analytics      — portfolio-wide stats from trades.db
  2. Edge Monitor         — rolling decay check (no Telegram alert in CLI mode)
  3. Walk-Forward         — OOS validation on historical data (all strategies)
  4. Monte Carlo          — tail-risk / ruin probability on actual trade history

Usage:
    python run_analysis.py                  # full suite
    python run_analysis.py --analytics      # only trade analytics
    python run_analysis.py --edge           # only edge monitor
    python run_analysis.py --wf             # only walk-forward
    python run_analysis.py --mc             # only Monte Carlo on trade history
    python run_analysis.py --save           # save all reports to db/reports/
"""

from dotenv import load_dotenv
load_dotenv(override=True)

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level  = logging.WARNING,   # suppress noisy INFO from sub-modules
    format = "%(levelname)s | %(name)s: %(message)s",
)
# Show our own analysis output
root = logging.getLogger()
root.setLevel(logging.WARNING)
logging.getLogger("__main__").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


def run_analytics(save: bool = False) -> None:
    print("\n" + "─" * 72)
    print("TRADE ANALYTICS")
    print("─" * 72)
    try:
        from analysis.trade_analytics import trade_analytics
        report = trade_analytics.full_report(include_paper=True)
        print(report)
        if save:
            path = trade_analytics.save_report()
            if path:
                print(f"  → Saved: {path}")
    except Exception as e:
        print(f"  [ERROR] Trade analytics failed: {e}")
        import traceback; traceback.print_exc()


def run_edge_monitor(save: bool = False) -> None:
    print("\n" + "─" * 72)
    print("EDGE MONITOR")
    print("─" * 72)
    try:
        from analysis.edge_monitor import edge_monitor
        report = edge_monitor.run(send_alert=False)
        print(report.text())
        if save:
            print(f"  → Report auto-saved to db/edge_reports/")
    except Exception as e:
        print(f"  [ERROR] Edge monitor failed: {e}")


def run_walk_forward(save: bool = False) -> None:
    print("\n" + "─" * 72)
    print("WALK-FORWARD VALIDATION")
    print("─" * 72)
    try:
        import pandas as pd
        from backtesting.walk_forward import WalkForwardValidator
        from backtesting.data_fetcher import fetch_historical
        from strategies.trend_follow import TrendFollowStrategy
        from strategies.mean_reversion import MeanReversionStrategy

        wf = WalkForwardValidator()

        candidates = [
            ("NSE:NIFTY50-INDEX",  TrendFollowStrategy(),    252, 63),
            ("NSE:NIFTYBANK-INDEX", TrendFollowStrategy(),   252, 63),
            ("NSE:RELIANCE-EQ",    MeanReversionStrategy(),  252, 63),
        ]

        os.makedirs("db/wf_reports", exist_ok=True)
        any_run = False
        for symbol, strategy, train_bars, test_bars in candidates:
            print(f"\nRunning: {symbol}  [{strategy.name}]")
            df = fetch_historical(symbol, timeframe="1D")
            if df is None or len(df) < train_bars + test_bars + 60:
                print(f"  [SKIP] Insufficient data ({len(df) if df is not None else 0} bars)")
                continue
            t0 = time.time()
            report = wf.run(symbol, df, strategy, train_bars=train_bars, test_bars=test_bars)
            elapsed = time.time() - t0
            print(report.summary())
            print(f"  Computed in {elapsed:.1f}s")
            if save:
                slug = symbol.replace(":", "_").replace("-", "_")
                path = f"db/wf_reports/{slug}_{strategy.name}.json"
                wf.save_report(report, path)
                print(f"  → Saved: {path}")
            any_run = True

        if not any_run:
            print("  No symbols had enough historical data for walk-forward.")
    except Exception as e:
        print(f"  [ERROR] Walk-forward failed: {e}")
        import traceback; traceback.print_exc()


def run_monte_carlo(save: bool = False) -> None:
    print("\n" + "─" * 72)
    print("MONTE CARLO SIMULATION")
    print("─" * 72)
    try:
        import sqlite3, json
        from config.settings import DB_PATH
        from backtesting.monte_carlo import MonteCarlo

        # Gather all closed trades from both databases
        class _Trade:
            def __init__(self, pnl):
                self.pnl = pnl

        trades = []
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT realised_pnl FROM trades WHERE status='CLOSED'"
                ).fetchall()
                trades.extend(_Trade(r[0]) for r in rows)
        except Exception:
            pass

        try:
            paper_db = os.path.join(os.path.dirname(DB_PATH), "paper_trades.db")
            with sqlite3.connect(paper_db) as conn:
                rows = conn.execute(
                    "SELECT pnl FROM learning_trades WHERE status='CLOSED'"
                ).fetchall()
                trades.extend(_Trade(r[0]) for r in rows)
        except Exception:
            pass

        if not trades:
            print("  No closed trades in database — cannot run Monte Carlo.")
            return

        print(f"  Running on {len(trades)} closed trades...")
        mc     = MonteCarlo()
        result = mc.run(trades, n_simulations=10_000, ruin_threshold_pct=30.0)
        print(result.summary())

        if save:
            os.makedirs("db/reports", exist_ok=True)
            date_str = datetime.now(tz=IST).strftime("%Y-%m-%d")
            path = f"db/reports/monte_carlo_{date_str}.json"
            data = {
                "generated_at":   datetime.now(tz=IST).isoformat(),
                "n_trades":       result.n_trades,
                "n_simulations":  result.n_simulations,
                "drawdown":       {"p5": result.dd_p5, "p50": result.dd_p50, "p95": result.dd_p95},
                "sharpe":         {"p5": result.sharpe_p5, "p50": result.sharpe_p50, "p95": result.sharpe_p95},
                "win_rate":       {"p5": result.wr_p5, "p50": result.wr_p50, "p95": result.wr_p95},
                "profit_factor":  {"p5": result.pf_p5, "p50": result.pf_p50, "p95": result.pf_p95},
                "total_return":   {"p5": result.return_p5, "p50": result.return_p50, "p95": result.return_p95},
                "ruin_probability": result.ruin_probability,
                "ruin_threshold_pct": result.ruin_threshold_pct,
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"  → Saved: {path}")

    except Exception as e:
        print(f"  [ERROR] Monte Carlo failed: {e}")
        import traceback; traceback.print_exc()


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading bot strategy validation suite")
    parser.add_argument("--analytics", action="store_true", help="Run trade analytics only")
    parser.add_argument("--edge",      action="store_true", help="Run edge monitor only")
    parser.add_argument("--wf",        action="store_true", help="Run walk-forward only")
    parser.add_argument("--mc",        action="store_true", help="Run Monte Carlo only")
    parser.add_argument("--save",      action="store_true", help="Save reports to db/reports/")
    args = parser.parse_args()

    run_all = not any([args.analytics, args.edge, args.wf, args.mc])

    print("=" * 72)
    print(f"  TRADING BOT — STRATEGY VALIDATION SUITE")
    print(f"  {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 72)

    if run_all or args.analytics:
        run_analytics(save=args.save)

    if run_all or args.edge:
        run_edge_monitor(save=args.save)

    if run_all or args.wf:
        run_walk_forward(save=args.save)

    if run_all or args.mc:
        run_monte_carlo(save=args.save)

    print("\n" + "=" * 72)
    print("  Analysis complete.")
    print("=" * 72)


if __name__ == "__main__":
    main()
