"""
performance.py
──────────────
Calculates professional performance metrics from backtest results.
Metrics: Sharpe ratio, Sortino ratio, max drawdown, profit factor,
         win rate, expectancy, CAGR, and full equity curve.
"""

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

from backtesting.backtest_engine import BacktestResult, Trade

logger = logging.getLogger(__name__)

RISK_FREE_RATE = 0.065   # Indian 10-year bond yield ~6.5%
TRADING_DAYS   = 252


def compute_metrics(result: BacktestResult) -> BacktestResult:
    """
    Compute all performance metrics for a backtest result.
    Modifies result in place and returns it.
    """
    trades = result.trades
    if not trades:
        logger.warning(f"[Perf] No trades for {result.symbol}")
        return result

    # Basic counts
    winners = [t for t in trades if t.pnl > 0]
    losers  = [t for t in trades if t.pnl <= 0]

    result.total_trades   = len(trades)
    result.winning_trades = len(winners)
    result.losing_trades  = len(losers)
    result.win_rate       = len(winners) / len(trades) if trades else 0

    # Average winner / loser
    result.avg_winner = sum(t.pnl for t in winners) / len(winners) if winners else 0
    result.avg_loser  = abs(sum(t.pnl for t in losers) / len(losers)) if losers else 0

    # Profit factor (cap at 99 — "∞" misleads; 99 still shows clearly dominant edge)
    gross_profit = sum(t.pnl for t in winners)
    gross_loss   = abs(sum(t.pnl for t in losers))
    result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0

    # Expectancy (avg P&L per trade)
    result.expectancy = round(sum(t.pnl for t in trades) / len(trades), 2)

    # Total return
    total_pnl             = sum(t.pnl for t in trades)
    from config.settings import TOTAL_CAPITAL
    result.total_return_pct = round(total_pnl / TOTAL_CAPITAL * 100, 2)

    # Average holding days
    result.avg_holding_days = round(
        sum(t.holding_days for t in trades) / len(trades), 1
    )

    # Equity curve and drawdown
    equity_curve = _build_equity_curve(trades)
    result.max_drawdown_pct = round(_max_drawdown(equity_curve), 2)

    # Sharpe ratio
    daily_returns = _trade_returns_to_daily(trades)
    result.sharpe_ratio = round(_sharpe(daily_returns), 2)

    return result


def format_report(result: BacktestResult) -> str:
    """Format a readable backtest report string."""
    lines = [
        "=" * 60,
        f"BACKTEST REPORT: {result.symbol}",
        f"Strategy:    {result.strategy}",
        f"Period:      {result.start_date} → {result.end_date}",
        "=" * 60,
        f"Total trades:     {result.total_trades}",
        f"Win rate:         {result.win_rate:.1%}",
        f"Profit factor:    {result.profit_factor:.2f}",
        f"Sharpe ratio:     {result.sharpe_ratio:.2f}",
        f"Max drawdown:     {result.max_drawdown_pct:.1f}%",
        f"Total return:     {result.total_return_pct:+.1f}%",
        f"Avg winner:       ₹{result.avg_winner:,.0f}",
        f"Avg loser:        ₹{result.avg_loser:,.0f}",
        f"Expectancy/trade: ₹{result.expectancy:,.0f}",
        f"Avg holding:      {result.avg_holding_days:.0f} days",
        "-" * 60,
        _grade(result),
        "=" * 60,
    ]
    return "\n".join(lines)


def _grade(result: BacktestResult) -> str:
    """Give a simple A-F grade to the strategy on this symbol."""
    score = 0

    if result.profit_factor > 2.0:  score += 3
    elif result.profit_factor > 1.5: score += 2
    elif result.profit_factor > 1.0: score += 1

    if result.sharpe_ratio > 1.5:   score += 3
    elif result.sharpe_ratio > 1.0: score += 2
    elif result.sharpe_ratio > 0.5: score += 1

    if result.win_rate > 0.6:       score += 2
    elif result.win_rate > 0.45:    score += 1

    if result.max_drawdown_pct < 10: score += 2
    elif result.max_drawdown_pct < 20: score += 1

    grade = {8: "A", 7: "A-", 6: "B+", 5: "B", 4: "B-", 3: "C"}.get(score, "D")
    return f"Grade: {grade} (score {score}/10)"


def _build_equity_curve(trades: list[Trade]) -> list[float]:
    """Build equity curve from trade list."""
    from config.settings import TOTAL_CAPITAL
    equity  = TOTAL_CAPITAL
    curve   = [equity]
    for t in sorted(trades, key=lambda x: x.entry_date):
        equity += t.pnl
        curve.append(equity)
    return curve


def _max_drawdown(equity_curve: list[float]) -> float:
    """Calculate maximum drawdown percentage."""
    if len(equity_curve) < 2:
        return 0.0
    peak    = equity_curve[0]
    max_dd  = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _trade_returns_to_daily(trades: list[Trade]) -> list[float]:
    """Convert trades to approximate daily return series."""
    if not trades:
        return []
    from config.settings import TOTAL_CAPITAL
    returns = []
    for t in trades:
        days = max(1, t.holding_days)
        daily_return = (t.pnl / TOTAL_CAPITAL) / days
        returns.extend([daily_return] * days)
    return returns


def _sharpe(daily_returns: list[float]) -> float:
    """Calculate annualised Sharpe ratio."""
    if len(daily_returns) < 5:
        return 0.0
    arr  = np.array(daily_returns)
    mean = arr.mean()
    std  = arr.std()
    if std == 0:
        return 0.0
    daily_rf = RISK_FREE_RATE / TRADING_DAYS
    return float((mean - daily_rf) / std * math.sqrt(TRADING_DAYS))
