"""
breakout_spread.py
──────────────────
BreakoutSpread — 12-bar range breakout with ATR buffer and RSI confirmation.

Entry criteria
──────────────
LONG:  spot > 12-bar high + ATR x 0.20  AND  RSI > 52
SHORT: spot < 12-bar low  - ATR x 0.20  AND  RSI < 48

The 12-bar lookback defines a meaningful consolidation range on hourly charts
(5 bars was too short — pure intraday noise). The ATR buffer (20%) requires
real momentum behind the break, not a 1-tick poke. The loose RSI filter
(>52 / <48) avoids filtering too many valid breakouts.

Note: opening blackout is enforced centrally by MCXSessionCalendar in
run_cycle() — this strategy does not need its own time check.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from strategies.mcx_base import MCXStrategy, MCXStrategyConfig, MCXSignalResult


class BreakoutSpreadStrategy(MCXStrategy):

    @property
    def name(self) -> str:
        return "BreakoutSpread"

    @property
    def priority(self) -> int:
        return 3

    @property
    def default_config(self) -> MCXStrategyConfig:
        return MCXStrategyConfig(
            priority            = 3,
            risk                = "HIGH",
            risk_label          = "Price breakout — frequent false signals, high failure rate",
            risk_color          = "#ef4444",
            cooldown_enabled    = True,
            cooldown_hours      = 3.0,
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
        from analysis.indicators import rsi as calc_rsi, ema as calc_ema, atr as calc_atr

        close   = df["close"]
        rsi_val = calc_rsi(close).iloc[-1]
        ema20   = calc_ema(close, 20).iloc[-1]
        ema5    = calc_ema(close, 5).iloc[-1]
        atr_val = calc_atr(df).iloc[-1]

        if len(close) < 14:
            return None

        prev_high = close.iloc[-13:-1].max()
        prev_low  = close.iloc[-13:-1].min()
        buffer    = round(atr_val * 0.20, 2)

        if spot > prev_high + buffer and rsi_val > 52:
            return MCXSignalResult(
                direction      = "LONG",
                strategy_name  = self.name,
                signal_reason  = f"Breakout above {prev_high:.0f}+{buffer:.0f}buf, RSI={rsi_val:.1f}",
                rsi_val        = round(rsi_val, 1),
                ema5_val       = round(ema5, 2),
                ema20_val      = round(ema20, 2),
                breakout_level = round(prev_high, 2),
            )

        if spot < prev_low - buffer and rsi_val < 48:
            return MCXSignalResult(
                direction      = "SHORT",
                strategy_name  = self.name,
                signal_reason  = f"Breakdown below {prev_low:.0f}-{buffer:.0f}buf, RSI={rsi_val:.1f}",
                rsi_val        = round(rsi_val, 1),
                ema5_val       = round(ema5, 2),
                ema20_val      = round(ema20, 2),
                breakout_level = round(prev_low, 2),
            )

        return None
