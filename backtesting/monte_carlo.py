"""
monte_carlo.py
──────────────
Monte Carlo robustness analysis for strategy trade results.

Method: Bootstrap with replacement (10,000 simulations).
Samples the SAME number of trades from the actual P&L distribution,
randomising the sequence to stress-test path dependency and tail risk.

Outputs:
  - Max drawdown distribution  (P5 / P25 / P50 / P75 / P95)
  - Sharpe ratio distribution  (P5 / P50 / P95)
  - Win rate confidence band   (P5 / P50 / P95)
  - Profit factor confidence   (P5 / P50 / P95)
  - Total return distribution  (P5 / P50 / P95)
  - Ruin probability           P(max_dd > threshold)

Usage:
    from backtesting.monte_carlo import MonteCarlo
    from backtesting.backtest_engine import BacktestEngine, BacktestResult

    engine  = BacktestEngine()
    result  = engine.run(symbol, df, strategy)
    mc      = MonteCarlo()
    mc_res  = mc.run(result.trades)
    print(mc_res.summary())
"""

import logging
import math
from dataclasses import dataclass

import numpy as np

RISK_FREE_RATE = 0.065
TRADING_DAYS   = 252

logger = logging.getLogger(__name__)


@dataclass
class MCResult:
    n_simulations:      int
    n_trades:           int
    # Max drawdown percentiles
    dd_p5:              float = 0.0
    dd_p25:             float = 0.0
    dd_p50:             float = 0.0
    dd_p75:             float = 0.0
    dd_p95:             float = 0.0
    # Sharpe percentiles
    sharpe_p5:          float = 0.0
    sharpe_p50:         float = 0.0
    sharpe_p95:         float = 0.0
    # Win rate percentiles
    wr_p5:              float = 0.0
    wr_p50:             float = 0.0
    wr_p95:             float = 0.0
    # Profit factor percentiles
    pf_p5:              float = 0.0
    pf_p50:             float = 0.0
    pf_p95:             float = 0.0
    # Total return percentiles
    return_p5:          float = 0.0
    return_p50:         float = 0.0
    return_p95:         float = 0.0
    # Ruin
    ruin_probability:   float = 0.0
    ruin_threshold_pct: float = 0.0

    def summary(self) -> str:
        lines = [
            "=" * 64,
            f"MONTE CARLO ANALYSIS  ({self.n_simulations:,} sims  |  {self.n_trades} trades)",
            "=" * 64,
            f"{'Metric':<22}  {'P5':>7}  {'P50 (median)':>12}  {'P95':>7}",
            "-" * 64,
            f"{'Max Drawdown %':<22}  {self.dd_p5:>6.1f}%  {self.dd_p50:>11.1f}%  {self.dd_p95:>6.1f}%",
            f"{'Sharpe Ratio':<22}  {self.sharpe_p5:>7.2f}  {self.sharpe_p50:>12.2f}  {self.sharpe_p95:>7.2f}",
            f"{'Win Rate':<22}  {self.wr_p5:>6.1%}  {self.wr_p50:>11.1%}  {self.wr_p95:>6.1%}",
            f"{'Profit Factor':<22}  {self.pf_p5:>7.2f}  {self.pf_p50:>12.2f}  {self.pf_p95:>7.2f}",
            f"{'Total Return %':<22}  {self.return_p5:>6.1f}%  {self.return_p50:>11.1f}%  {self.return_p95:>6.1f}%",
            "-" * 64,
            f"Ruin Probability (DD > {self.ruin_threshold_pct:.0f}%):  {self.ruin_probability:.1%}",
            "=" * 64,
            self._verdict(),
            "=" * 64,
        ]
        return "\n".join(lines)

    def _verdict(self) -> str:
        if self.dd_p95 < 20 and self.pf_p5 > 1.0 and self.ruin_probability < 0.05:
            return "VERDICT: ROBUST — Worst-case tail scenarios are acceptable."
        elif self.dd_p95 < 35 and self.pf_p5 > 0.8 and self.ruin_probability < 0.15:
            return "VERDICT: MODERATE — Manageable tail risk. Consider 25% size reduction."
        else:
            return "VERDICT: HIGH RISK — Tail scenarios dangerous. Do not deploy at full size."


class MonteCarlo:
    """Bootstrap Monte Carlo simulation of strategy robustness."""

    def run(
        self,
        trades,
        n_simulations:     int   = 10_000,
        initial_capital:   float = None,
        ruin_threshold_pct: float = 30.0,
    ) -> MCResult:
        from config.settings import TOTAL_CAPITAL
        capital = initial_capital or TOTAL_CAPITAL

        if not trades:
            logger.warning("[MonteCarlo] No trades provided.")
            return MCResult(n_simulations=n_simulations, n_trades=0)

        pnl_series = np.array([t.pnl for t in trades])
        n          = len(pnl_series)

        logger.info(f"[MonteCarlo] {n_simulations:,} simulations on {n} trades...")

        rng = np.random.default_rng(seed=42)

        all_dd     = np.empty(n_simulations)
        all_sharpe = np.empty(n_simulations)
        all_wr     = np.empty(n_simulations)
        all_pf     = np.empty(n_simulations)
        all_ret    = np.empty(n_simulations)
        ruin_count = 0

        rf_daily = RISK_FREE_RATE / TRADING_DAYS

        for i in range(n_simulations):
            sim = rng.choice(pnl_series, size=n, replace=True)

            # Equity curve
            eq   = np.concatenate([[capital], capital + np.cumsum(sim)])
            peak = np.maximum.accumulate(eq)
            dd   = (peak - eq) / peak * 100
            all_dd[i] = dd.max()

            # Return
            all_ret[i] = (eq[-1] - capital) / capital * 100

            # Win rate
            all_wr[i] = float((sim > 0).sum()) / n

            # Profit factor
            gw = float(sim[sim > 0].sum())
            gl = float(abs(sim[sim < 0].sum()))
            all_pf[i] = min(gw / gl, 99.0) if gl > 0 else 99.0

            # Sharpe (daily approximation)
            daily = sim / capital
            mean_d = daily.mean()
            std_d  = daily.std()
            all_sharpe[i] = (
                (mean_d - rf_daily) / std_d * math.sqrt(TRADING_DAYS)
                if std_d > 1e-10 else 0.0
            )

            if all_dd[i] >= ruin_threshold_pct:
                ruin_count += 1

        def p(arr, q):
            return float(np.percentile(arr, q))

        result = MCResult(
            n_simulations      = n_simulations,
            n_trades           = n,
            dd_p5              = p(all_dd, 5),
            dd_p25             = p(all_dd, 25),
            dd_p50             = p(all_dd, 50),
            dd_p75             = p(all_dd, 75),
            dd_p95             = p(all_dd, 95),
            sharpe_p5          = p(all_sharpe, 5),
            sharpe_p50         = p(all_sharpe, 50),
            sharpe_p95         = p(all_sharpe, 95),
            wr_p5              = p(all_wr, 5),
            wr_p50             = p(all_wr, 50),
            wr_p95             = p(all_wr, 95),
            pf_p5              = p(all_pf, 5),
            pf_p50             = p(all_pf, 50),
            pf_p95             = p(all_pf, 95),
            return_p5          = p(all_ret, 5),
            return_p50         = p(all_ret, 50),
            return_p95         = p(all_ret, 95),
            ruin_probability   = ruin_count / n_simulations,
            ruin_threshold_pct = ruin_threshold_pct,
        )
        logger.info(
            f"[MonteCarlo] Done. DD P95={result.dd_p95:.1f}%  "
            f"PF P50={result.pf_p50:.2f}  Ruin={result.ruin_probability:.1%}"
        )
        return result
