"""
rsi_reversal.py
───────────────
RSIReversalSpread — Fade short-term RSI extremes within the prevailing trend.

Entry criteria
──────────────
LONG:  RSI < 32 (oversold)  AND  EMA5 >= EMA20 x 0.995 (trend intact)
SHORT: RSI > 68 (overbought) AND  EMA5 <= EMA20 x 1.005 (trend intact)

The EMA proximity check keeps us with the prevailing trend — we only fade
short-term extremes, not enter against a fully established counter-trend.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from strategies.mcx_base import MCXStrategy, MCXStrategyConfig, MCXSignalResult


class RSIReversalSpreadStrategy(MCXStrategy):

    @property
    def name(self) -> str:
        return "RSIReversalSpread"

    @property
    def priority(self) -> int:
        return 2

    @property
    def default_config(self) -> MCXStrategyConfig:
        return MCXStrategyConfig(
            priority            = 2,
            risk                = "HIGH",
            risk_label          = "Counter-trend reversal — may catch a falling knife if trend continues",
            risk_color          = "#f97316",
            cooldown_enabled    = True,
            cooldown_hours      = 3.0,
            sl_debit_pct        = 0.35,
            trail_debit_pct     = 0.25,
            trail_trigger_pct   = 0.45,
            target_pct          = 0.65,
            target_upgraded_pct = 0.78,
            target_upgrade_mult = 2.0,
        )

    def generate_signal(
        self, df: pd.DataFrame, spot: float, now: datetime
    ) -> Optional[MCXSignalResult]:
        from analysis.indicators import rsi as calc_rsi, ema as calc_ema

        close   = df["close"]
        rsi_val = calc_rsi(close).iloc[-1]
        ema20   = calc_ema(close, 20).iloc[-1]
        ema5    = calc_ema(close, 5).iloc[-1]

        cfg = self.config   # thresholds editable via /commodity/config API

        # Require EMA5 >= EMA20 for LONG (uptrend confirmed before fading oversold dip).
        # The old proximity check allowed EMA5 up to 0.5% below EMA20, which fired
        # in downtrends where RSI hit oversold by continued selling — catching a knife.
        if rsi_val < cfg.rsi_oversold and ema5 >= ema20:
            return MCXSignalResult(
                direction     = "LONG",
                strategy_name = self.name,
                signal_reason = (
                    f"RSI={rsi_val:.1f} < {cfg.rsi_oversold:.0f} (oversold), "
                    f"EMA5 {ema5:.1f} >= EMA20 {ema20:.1f} (uptrend intact)"
                ),
                rsi_val       = round(rsi_val, 1),
                ema5_val      = round(ema5, 2),
                ema20_val     = round(ema20, 2),
            )

        if rsi_val > cfg.rsi_overbought and ema5 <= ema20:
            return MCXSignalResult(
                direction     = "SHORT",
                strategy_name = self.name,
                signal_reason = (
                    f"RSI={rsi_val:.1f} > {cfg.rsi_overbought:.0f} (overbought), "
                    f"EMA5 {ema5:.1f} <= EMA20 {ema20:.1f} (downtrend intact)"
                ),
                rsi_val       = round(rsi_val, 1),
                ema5_val      = round(ema5, 2),
                ema20_val     = round(ema20, 2),
            )

        return None
