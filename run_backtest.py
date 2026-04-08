"""
run_backtest.py
───────────────
Standalone backtest runner for MeanReversion (and TrendFollow) strategy.

Usage:
    python run_backtest.py
    python run_backtest.py --strategy mean_reversion
    python run_backtest.py --symbols NSE:RELIANCE-EQ NSE:HDFCBANK-EQ
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run backtests on NSE watchlist")
    parser.add_argument(
        "--strategy",
        choices=["mean_reversion", "trend_follow", "all"],
        default="all",
        help="Strategy to test (default: all)",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Override symbols (default: full watchlist)",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=3,
        help="Years of history to fetch (default: 3)",
    )
    args = parser.parse_args()

    from backtesting.backtest_engine import BacktestEngine
    from backtesting.data_fetcher import fetch_all
    from backtesting.performance import compute_metrics
    from config.watchlist import ALL_NSE_SYMBOLS
    from strategies.mean_reversion import MeanReversionStrategy
    from strategies.trend_follow import TrendFollowStrategy

    symbols = args.symbols or ALL_NSE_SYMBOLS

    strategies = {}
    if args.strategy in ("mean_reversion", "all"):
        strategies["MeanReversion"] = MeanReversionStrategy()
    if args.strategy in ("trend_follow", "all"):
        strategies["TrendFollow"] = TrendFollowStrategy()

    logger.info(f"Fetching {args.years}y daily data for {len(symbols)} symbols...")
    all_data = fetch_all(symbols, "1D", years_back=args.years)
    logger.info(f"  Got data for {len(all_data)} symbols.")

    engine = BacktestEngine()

    for strat_name, strategy in strategies.items():
        logger.info(f"\n{'─'*60}")
        logger.info(f"  Strategy: {strat_name}")
        logger.info(f"{'─'*60}")

        results = []
        for symbol, df in all_data.items():
            try:
                r = engine.run(symbol, df, strategy, "1D")
                r = compute_metrics(r)
                results.append(r)
                logger.info(f"  {r.summary()}")
            except Exception as e:
                logger.warning(f"  {symbol} failed: {e}")

        if not results:
            logger.info("  No results.")
            continue

        # Aggregate summary
        traded = [r for r in results if r.total_trades > 0]
        if not traded:
            logger.info("  No trades generated.")
            continue

        avg_wr  = sum(r.win_rate for r in traded) / len(traded)
        avg_pf  = sum(r.profit_factor for r in traded) / len(traded)
        avg_sh  = sum(r.sharpe_ratio for r in traded) / len(traded)
        avg_dd  = sum(r.max_drawdown_pct for r in traded) / len(traded)
        avg_ret = sum(r.total_return_pct for r in traded) / len(traded)
        total_t = sum(r.total_trades for r in traded)
        avg_exp = sum(r.expectancy for r in traded) / len(traded)

        top3 = sorted(traded, key=lambda r: r.profit_factor, reverse=True)[:3]
        bot3 = sorted(traded, key=lambda r: r.profit_factor)[:3]

        logger.info(f"\n  ── {strat_name} aggregate ({len(traded)} symbols, {total_t} trades) ──")
        logger.info(f"  Avg Win Rate   : {avg_wr:.0%}")
        logger.info(f"  Avg PF         : {avg_pf:.2f}")
        logger.info(f"  Avg Sharpe     : {avg_sh:.2f}")
        logger.info(f"  Avg MaxDD      : {avg_dd:.1f}%")
        logger.info(f"  Avg Return     : {avg_ret:+.1f}%")
        logger.info(f"  Avg Expectancy : ₹{avg_exp:+.0f} per trade")
        logger.info(f"  Top 3 by PF    : {[r.symbol for r in top3]}")
        logger.info(f"  Bottom 3 by PF : {[r.symbol for r in bot3]}")

    logger.info("\nBacktest complete.")


if __name__ == "__main__":
    main()
