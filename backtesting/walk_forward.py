"""
walk_forward.py
───────────────
Walk-forward (out-of-sample) validation for trading strategies.

Eliminates look-ahead bias by separating training and test windows,
then rolling both forward across the full history.

Two modes:
  rolling   — fixed N-bar training window slides forward with test window
  anchored  — training always starts at bar 0 (expanding window)

Usage:
    from backtesting.walk_forward import WalkForwardValidator
    wf     = WalkForwardValidator()
    report = wf.run("NSE:NIFTY50-INDEX", df, strategy)
    print(report.summary())
    wf.save_report(report, "db/wf_reports/NIFTY50_TrendFollow.json")
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

import numpy as np
import pandas as pd

from backtesting.backtest_engine import BacktestEngine
from backtesting.performance import compute_metrics

logger = logging.getLogger(__name__)


@dataclass
class WFWindow:
    window_id:           int
    train_start:         str
    train_end:           str
    test_start:          str
    test_end:            str
    train_trades:        int   = 0
    train_win_rate:      float = 0.0
    train_profit_factor: float = 0.0
    train_sharpe:        float = 0.0
    test_trades:         int   = 0
    test_win_rate:       float = 0.0
    test_profit_factor:  float = 0.0
    test_sharpe:         float = 0.0
    test_return_pct:     float = 0.0
    degradation_pf:      float = 0.0   # test_pf / train_pf


@dataclass
class WalkForwardReport:
    symbol:           str
    strategy:         str
    mode:             str
    train_bars:       int
    test_bars:        int
    windows:          list[WFWindow] = field(default_factory=list)
    oos_win_rate:     float = 0.0
    oos_profit_factor: float = 0.0
    oos_sharpe:       float = 0.0
    oos_total_return: float = 0.0
    efficiency_ratio: float = 0.0
    generated_at:     str   = ""

    def summary(self) -> str:
        lines = [
            "=" * 72,
            f"WALK-FORWARD: {self.symbol}  [{self.strategy}]",
            f"Mode: {self.mode}  |  Train: {self.train_bars} bars  |  Test: {self.test_bars} bars  |  Windows: {len(self.windows)}",
            "-" * 72,
            f"OOS Win Rate:      {self.oos_win_rate:.1%}",
            f"OOS Profit Factor: {self.oos_profit_factor:.2f}",
            f"OOS Sharpe:        {self.oos_sharpe:.2f}",
            f"OOS Total Return:  {self.oos_total_return:+.1f}%",
            f"Efficiency Ratio:  {self.efficiency_ratio:.2f}  (1.0 = no OOS degradation)",
            "-" * 72,
            f"{'W':>3}  {'Test Period':^23}  {'Trades':>6}  {'WR':>5}  {'PF':>5}  {'Sharpe':>6}  {'Degr':>5}",
        ]
        for w in self.windows:
            lines.append(
                f"W{w.window_id:02d}  {w.test_start} → {w.test_end}  "
                f"{w.test_trades:>6}  {w.test_win_rate:>4.0%}  {w.test_profit_factor:>5.2f}  "
                f"{w.test_sharpe:>6.2f}  {w.degradation_pf:>5.2f}"
            )
        lines += ["-" * 72, self._verdict(), "=" * 72]
        return "\n".join(lines)

    def _verdict(self) -> str:
        pf  = self.oos_profit_factor
        eff = self.efficiency_ratio
        if pf >= 1.5 and eff >= 0.70:
            return "VERDICT: ROBUST — Edge survives out-of-sample. Deployable."
        elif pf >= 1.2 and eff >= 0.50:
            return "VERDICT: MODERATE — Edge partially survives OOS. Reduce size 30-50%."
        elif pf >= 1.0:
            return "VERDICT: MARGINAL — Barely positive OOS. Monitor closely; do not scale."
        else:
            return "VERDICT: FAIL — Edge disappears OOS. Strategy likely overfit."


class WalkForwardValidator:
    """Walk-forward validation. Train on N bars, test on M bars, slide by M."""

    def __init__(self):
        self._engine = BacktestEngine()

    def run(
        self,
        symbol:      str,
        df:          pd.DataFrame,
        strategy,
        train_bars:  int = 252,
        test_bars:   int = 63,
        mode:        str = "rolling",
        warmup_bars: int = 60,
    ) -> WalkForwardReport:
        report = WalkForwardReport(
            symbol       = symbol,
            strategy     = strategy.name,
            mode         = mode,
            train_bars   = train_bars,
            test_bars    = test_bars,
            generated_at = datetime.now().isoformat(),
        )

        n           = len(df)
        min_needed  = warmup_bars + train_bars + test_bars
        if n < min_needed:
            logger.warning(
                f"[WalkForward] {symbol}: {n} bars < {min_needed} required. Skipping."
            )
            return report

        window_id  = 0
        cursor     = warmup_bars   # first train window starts here

        while True:
            train_start = cursor if mode == "rolling" else warmup_bars
            train_end   = cursor + train_bars
            test_start  = train_end
            test_end    = test_start + test_bars

            if test_end > n:
                break

            train_df = df.iloc[train_start:train_end].copy().reset_index(drop=True)
            test_df  = df.iloc[test_start:test_end].copy().reset_index(drop=True)

            # Run on training window
            train_res = self._engine.run(symbol, train_df, strategy, warmup_bars=warmup_bars)
            train_res = compute_metrics(train_res)

            # Run on test window — prepend last warmup_bars from train so indicators
            # have enough history; only count trades that fire in the true test period.
            warmup_tail = df.iloc[max(0, test_start - warmup_bars):test_start]
            test_with_warmup = pd.concat([warmup_tail, test_df]).reset_index(drop=True)
            test_res = self._engine.run(
                symbol, test_with_warmup, strategy, warmup_bars=len(warmup_tail)
            )
            cutoff_ts = df.iloc[test_start]["timestamp"]
            test_res.trades = [t for t in test_res.trades if t.entry_date >= cutoff_ts]
            test_res = compute_metrics(test_res)

            degradation = (
                test_res.profit_factor / train_res.profit_factor
                if train_res.profit_factor > 0 else 0.0
            )

            window = WFWindow(
                window_id            = window_id,
                train_start          = str(train_df["timestamp"].iloc[0].date()),
                train_end            = str(train_df["timestamp"].iloc[-1].date()),
                test_start           = str(test_df["timestamp"].iloc[0].date()),
                test_end             = str(test_df["timestamp"].iloc[-1].date()),
                train_trades         = train_res.total_trades,
                train_win_rate       = train_res.win_rate,
                train_profit_factor  = train_res.profit_factor,
                train_sharpe         = train_res.sharpe_ratio,
                test_trades          = test_res.total_trades,
                test_win_rate        = test_res.win_rate,
                test_profit_factor   = test_res.profit_factor,
                test_sharpe          = test_res.sharpe_ratio,
                test_return_pct      = test_res.total_return_pct,
                degradation_pf       = round(degradation, 2),
            )
            report.windows.append(window)
            logger.info(
                f"[WalkForward] {symbol} W{window_id:02d} {window.test_start}→{window.test_end} "
                f"OOS PF={test_res.profit_factor:.2f} WR={test_res.win_rate:.0%} "
                f"Trades={test_res.total_trades} Degr={degradation:.2f}"
            )

            cursor    += test_bars
            window_id += 1

        self._aggregate(report)
        return report

    def _aggregate(self, report: WalkForwardReport) -> None:
        ws = [w for w in report.windows if w.test_trades > 0]
        if not ws:
            return

        total_test_wins  = sum(int(w.test_win_rate * w.test_trades) for w in ws)
        total_test_trades = sum(w.test_trades for w in ws)
        test_pfs   = [w.test_profit_factor for w in ws]
        test_sharpes = [w.test_sharpe for w in ws]
        train_pfs  = [w.train_profit_factor for w in report.windows if w.train_trades > 0]

        report.oos_win_rate      = total_test_wins / total_test_trades if total_test_trades else 0.0
        report.oos_profit_factor = float(np.mean(test_pfs))
        report.oos_sharpe        = float(np.mean(test_sharpes))
        report.oos_total_return  = sum(w.test_return_pct for w in ws)
        report.efficiency_ratio  = round(
            float(np.mean(test_pfs)) / float(np.mean(train_pfs))
            if train_pfs and np.mean(train_pfs) > 0 else 0.0,
            2
        )

    def save_report(self, report: WalkForwardReport, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "symbol":            report.symbol,
                "strategy":          report.strategy,
                "mode":              report.mode,
                "train_bars":        report.train_bars,
                "test_bars":         report.test_bars,
                "oos_win_rate":      report.oos_win_rate,
                "oos_profit_factor": report.oos_profit_factor,
                "oos_sharpe":        report.oos_sharpe,
                "oos_total_return":  report.oos_total_return,
                "efficiency_ratio":  report.efficiency_ratio,
                "generated_at":      report.generated_at,
                "windows":           [asdict(w) for w in report.windows],
            }, f, indent=2)
        logger.info(f"[WalkForward] Report saved: {path}")
