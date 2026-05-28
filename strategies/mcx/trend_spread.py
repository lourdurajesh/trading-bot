"""
trend_spread.py
───────────────
TrendSpread — EMA5/EMA20 crossover with RSI trend confirmation.

Entry criteria
──────────────
LONG:  EMA5 > EMA20  AND  spot > EMA20  AND  55 < RSI < 68  AND  EMA gap >= 0.3%
SHORT: EMA5 < EMA20  AND  spot < EMA20  AND  32 < RSI < 45  AND  EMA gap >= 0.3%

The 0.3% EMA-gap requirement filters flat crossovers (pure noise).
The RSI band (55-68 / 32-45) avoids extremely overbought/oversold levels
that are likely to snap back before the trade develops.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from strategies.mcx_base import MCXStrategy, MCXStrategyConfig, MCXSignalResult


class TrendSpreadStrategy(MCXStrategy):

    @property
    def name(self) -> str:
        return "TrendSpread"

    @property
    def priority(self) -> int:
        return 1

    @property
    def default_config(self) -> MCXStrategyConfig:
        return MCXStrategyConfig(
            priority            = 1,
            risk                = "MEDIUM",
            risk_label          = "Trend-following EMA crossover — well-defined conditions, lower failure rate",
            risk_color          = "#f59e0b",
            cooldown_enabled    = True,
            cooldown_hours      = 2.0,
            sl_debit_pct        = 0.40,
            trail_debit_pct     = 0.30,
            trail_trigger_pct   = 0.50,
            target_pct          = 0.65,
            target_upgraded_pct = 0.80,
            target_upgrade_mult = 2.0,
        )

    def generate_signal(
        self, df: pd.DataFrame, spot: float, now: datetime
    ) -> Optional[MCXSignalResult]:
        from analysis.indicators import rsi as calc_rsi, ema as calc_ema

        close       = df["close"]
        rsi_val     = calc_rsi(close).iloc[-1]
        ema20       = calc_ema(close, 20).iloc[-1]
        ema5        = calc_ema(close, 5).iloc[-1]
        ema_gap_pct = abs(ema5 - ema20) / ema20 * 100

        cfg = self.config   # read once — thresholds editable via /commodity/config API

        if (ema5 > ema20 and spot > ema20
                and cfg.rsi_long_min < rsi_val < cfg.rsi_long_max
                and ema_gap_pct >= cfg.ema_gap_min_pct):
            return MCXSignalResult(
                direction     = "LONG",
                strategy_name = self.name,
                signal_reason = (
                    f"EMA5={ema5:.0f}>EMA20={ema20:.0f}(+{ema_gap_pct:.1f}%), "
                    f"RSI={rsi_val:.1f} in [{cfg.rsi_long_min:.0f},{cfg.rsi_long_max:.0f}]"
                ),
                rsi_val       = round(rsi_val, 1),
                ema5_val      = round(ema5, 2),
                ema20_val     = round(ema20, 2),
            )

        if (ema5 < ema20 and spot < ema20
                and cfg.rsi_short_min < rsi_val < cfg.rsi_short_max
                and ema_gap_pct >= cfg.ema_gap_min_pct):
            return MCXSignalResult(
                direction     = "SHORT",
                strategy_name = self.name,
                signal_reason = (
                    f"EMA5={ema5:.0f}<EMA20={ema20:.0f}(-{ema_gap_pct:.1f}%), "
                    f"RSI={rsi_val:.1f} in [{cfg.rsi_short_min:.0f},{cfg.rsi_short_max:.0f}]"
                ),
                rsi_val       = round(rsi_val, 1),
                ema5_val      = round(ema5, 2),
                ema20_val     = round(ema20, 2),
            )

        return None
