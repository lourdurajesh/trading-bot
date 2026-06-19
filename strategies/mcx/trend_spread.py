"""
trend_spread.py
───────────────
TrendSpread — EMA5/EMA20 crossover with RSI, ADX, and volume confirmation.

Entry criteria
──────────────
LONG:  EMA5 > EMA20  AND  spot > EMA20  AND  55 < RSI < 68
       AND  EMA gap ≥ 0.3%  AND  ADX ≥ 20  AND  RVOL ≥ 1.3×

SHORT: EMA5 < EMA20  AND  spot < EMA20  AND  32 < RSI < 45
       AND  EMA gap ≥ 0.3%  AND  ADX ≥ 20  AND  RVOL ≥ 1.3×

Design decisions
────────────────
  EMA gap ≥ 0.3% (existing)
    Filters flat/noise crossovers — EMA5 one tick above EMA20 is not a trend.

  RSI band 55-68 / 32-45 (existing)
    Avoids extremely overbought/oversold levels that snap back before the
    trade develops. Also avoids weak momentum setups at the 50 midline.

  ADX ≥ 20 (added)
    EMA crossovers occur in both trending AND ranging markets. In a ranging
    market, the crossover is just noise flipping back and forth. ADX > 20
    confirms a directional move is behind the crossover, not chop.

  RVOL ≥ 1.3× (added)
    A structural crossover without volume is often a fade. Participants who
    matter are not behind it. Set lower than BreakoutSpread (1.5×) because
    EMA crossover is a slower, more mature signal that doesn't require the
    same volume surge that a price breakout does.

Signal invalidation exit
────────────────────────
When an open TrendSpread position's EMA alignment flips back against the
trade direction (with ≥ 0.3% gap — same noise filter as entry), the trade
thesis is gone. The exit manager in commodity_options_learning._check_exits()
fires a SIGNAL_INVALIDATED close rather than waiting for the SL to grind in.
This is the correct behaviour: sitting in a debit spread that's decaying theta
when the entry signal has reversed is irrational. Exit immediately.

All thresholds are runtime-configurable via the /commodity/config API.

Note: opening blackout is enforced centrally by MCXSessionCalendar in
run_cycle() — this strategy does not need its own time check.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from strategies.mcx_base import MCXStrategy, MCXStrategyConfig
from strategies.base_strategy import Signal, Direction


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
            risk_label          = "Trend-following EMA crossover — confirmed by ADX and volume",
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
    ) -> Optional[Signal]:
        from analysis.indicators import (
            rsi as calc_rsi,
            ema as calc_ema,
            adx as calc_adx,
            relative_volume as calc_rvol,
        )

        cfg = self.config

        close       = df["close"]
        rsi_val     = calc_rsi(close).iloc[-1]
        ema20       = calc_ema(close, 20).iloc[-1]
        ema5        = calc_ema(close, 5).iloc[-1]
        ema_gap_pct = abs(ema5 - ema20) / ema20 * 100

        adx_series, _, _ = calc_adx(df)
        adx_now          = adx_series.iloc[-1]
        rvol             = calc_rvol(df).iloc[-1]

        # Fyers MCX data feed does not provide real-time volume for commodity
        # futures (vol_traded_today=0 on every tick). When no volume data exists
        # at all, skip the RVOL gate so RSI+EMA+ADX can still fire trades.
        volume_available = df["volume"].sum() > 0
        rvol_ok_long     = (not volume_available) or (rvol >= cfg.min_rvol_trend)
        rvol_ok_short    = rvol_ok_long
        rvol_label       = f"RVOL={rvol:.1f}x" if volume_available else "RVOL=N/A(no feed)"

        # ── LONG ────────────────────────────────────────────────────
        if (ema5 > ema20
                and spot > ema20
                and cfg.rsi_long_min < rsi_val < cfg.rsi_long_max
                and ema_gap_pct >= cfg.ema_gap_min_pct
                and adx_now >= cfg.min_adx_trend
                and rvol_ok_long):
            return Signal(
                symbol    = "",
                strategy  = self.name,
                direction = Direction.LONG,
                reason    = (
                    f"EMA5={ema5:.0f}>EMA20={ema20:.0f}(+{ema_gap_pct:.1f}%), "
                    f"RSI={rsi_val:.1f} in [{cfg.rsi_long_min:.0f},{cfg.rsi_long_max:.0f}], "
                    f"ADX={adx_now:.0f}, {rvol_label}"
                ),
                meta = {
                    "rsi_val":   round(rsi_val, 1),
                    "ema5_val":  round(ema5, 2),
                    "ema20_val": round(ema20, 2),
                },
            )

        # ── SHORT ───────────────────────────────────────────────────
        if (ema5 < ema20
                and spot < ema20
                and cfg.rsi_short_min < rsi_val < cfg.rsi_short_max
                and ema_gap_pct >= cfg.ema_gap_min_pct
                and adx_now >= cfg.min_adx_trend
                and rvol_ok_short):
            return Signal(
                symbol    = "",
                strategy  = self.name,
                direction = Direction.SHORT,
                reason    = (
                    f"EMA5={ema5:.0f}<EMA20={ema20:.0f}(-{ema_gap_pct:.1f}%), "
                    f"RSI={rsi_val:.1f} in [{cfg.rsi_short_min:.0f},{cfg.rsi_short_max:.0f}], "
                    f"ADX={adx_now:.0f}, {rvol_label}"
                ),
                meta = {
                    "rsi_val":   round(rsi_val, 1),
                    "ema5_val":  round(ema5, 2),
                    "ema20_val": round(ema20, 2),
                },
            )

        return None
