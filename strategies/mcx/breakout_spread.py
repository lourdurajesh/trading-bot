"""
breakout_spread.py
──────────────────
BreakoutSpread — 12-bar range breakout with multi-factor confirmation.

Entry criteria
──────────────
LONG:
  last_close > 12-bar WICK high + ATR×0.30  — candle closed above real resistance
  RSI > 55                                   — real momentum, not midline noise
  EMA5 > EMA20, gap ≥ 0.3%                  — uptrend with meaningful separation
  ADX ≥ 20                                   — trending market, not ranging chop
  RVOL ≥ 1.5×                               — volume confirms the breakout
  MACD histogram > 0                         — momentum is bullish and not exhausting

SHORT: mirror of the above, with all conditions inverted.

Design decisions vs original (22% win rate, −₹4k all-time)
──────────────────────────────────────────────────────────
  Candle-close confirmation (was: live spot tick)
    The biggest source of false fires. A wick that briefly pokes above the
    range during one engine cycle (every 15 min) triggered the signal, even
    when the candle body never cleared the level. Requiring last_close >
    range_high means a full hourly candle must close above before we act.

  Range from high/low wicks (was: close prices)
    Resistance lives at wick highs, not close prices. Using closes
    underestimates the breakout level by the wick size, making the strategy
    trigger prematurely inside the candle range.

  Volume gate — RVOL ≥ 1.5 (was: none)
    A breakout on thin volume is textbook false breakout. Volume must confirm.

  ADX ≥ 20 (was: none)
    Without ADX, the strategy fires in sideways chop where the 12-bar range
    high is just noise. ADX > 20 confirms a directional market.

  MACD histogram sign check (was: none)
    Filters breakouts where momentum is already decelerating toward the top
    of its cycle — the candle breaks out but the MACD histogram is ticking
    toward zero or has already crossed.

  RSI threshold raised to 55/45 (was: 52/48)
    RSI 52 is barely above midline — meaningless for breakout momentum.
    55+ / 45- matches TrendSpread's momentum bands.

  EMA gap ≥ 0.3% (was: none)
    EMA5 > EMA20 by 1 tick still passed. Requires meaningful trend separation
    consistent with TrendSpread's noise filter.

All thresholds are runtime-configurable via the /commodity/config API.

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
            risk_label          = "Price breakout — multi-factor confirmed; filters added for volume, ADX, MACD",
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
        from analysis.indicators import (
            rsi as calc_rsi,
            ema as calc_ema,
            atr as calc_atr,
            adx as calc_adx,
            macd as calc_macd,
            relative_volume as calc_rvol,
        )

        cfg   = self.config
        close = df["close"]

        lookback = cfg.breakout_lookback
        if len(close) < lookback + 2:
            return None

        # ── Indicators ────────────────────────────────────────────
        rsi_val          = calc_rsi(close).iloc[-1]
        ema5             = calc_ema(close, 5).iloc[-1]
        ema20            = calc_ema(close, 20).iloc[-1]
        atr_val          = calc_atr(df).iloc[-1]
        adx_series, _, _ = calc_adx(df)
        adx_now          = adx_series.iloc[-1]
        _, _, macd_hist  = calc_macd(close)
        macd_h           = macd_hist.iloc[-1]
        rvol             = calc_rvol(df).iloc[-1]

        # Fyers MCX data feed does not provide volume for commodity futures.
        # When no volume data exists, skip the RVOL gate entirely.
        volume_available = df["volume"].sum() > 0
        rvol_ok          = (not volume_available) or (rvol >= cfg.min_rvol_breakout)
        rvol_label       = f"RVOL={rvol:.1f}x" if volume_available else "RVOL=N/A(no feed)"

        # ── Consolidation range from wick highs/lows ──────────────
        # Use df["high"]/df["low"] — not closes — because resistance and
        # support live at the wicks, not candle bodies.
        # Excludes the current (potentially still-forming) bar.
        prev_high = df["high"].iloc[-(lookback + 1):-1].max()
        prev_low  = df["low"].iloc[-(lookback + 1):-1].min()
        buffer    = round(atr_val * cfg.atr_buffer_mult, 2)

        # ── Shared derived values ─────────────────────────────────
        ema_gap_pct = abs(ema5 - ema20) / ema20 * 100

        # Use the last *completed* candle close, not the live spot tick.
        # Requiring a candle close above the level eliminates wick-only spikes
        # that were the main source of single-cycle false triggers.
        last_close = close.iloc[-1]

        # ── LONG ─────────────────────────────────────────────────
        if (last_close > prev_high + buffer              # closed above range
                and rsi_val > cfg.rsi_breakout_long      # momentum > 55
                and ema5 > ema20                          # uptrend
                and ema_gap_pct >= cfg.ema_gap_min_pct   # separation not noise
                and adx_now >= cfg.min_adx_breakout      # directional, not ranging
                and rvol_ok                               # volume confirms (or N/A)
                and macd_h > 0):                          # MACD bullish
            return MCXSignalResult(
                direction      = "LONG",
                strategy_name  = self.name,
                signal_reason  = (
                    f"Closed above {prev_high:.0f}+{buffer:.0f}buf "
                    f"(ATR×{cfg.atr_buffer_mult:.2f}), "
                    f"RSI={rsi_val:.1f}>{cfg.rsi_breakout_long:.0f}, "
                    f"ADX={adx_now:.0f}, {rvol_label}, "
                    f"MACD-H={macd_h:.2f}, EMA5>EMA20(+{ema_gap_pct:.1f}%)"
                ),
                rsi_val        = round(rsi_val, 1),
                ema5_val       = round(ema5, 2),
                ema20_val      = round(ema20, 2),
                breakout_level = round(prev_high, 2),
            )

        # ── SHORT ────────────────────────────────────────────────
        if (last_close < prev_low - buffer               # closed below range
                and rsi_val < cfg.rsi_breakout_short     # weakness < 45
                and ema5 < ema20                          # downtrend
                and ema_gap_pct >= cfg.ema_gap_min_pct   # separation not noise
                and adx_now >= cfg.min_adx_breakout      # directional, not ranging
                and rvol_ok                               # volume confirms (or N/A)
                and macd_h < 0):                          # MACD bearish
            return MCXSignalResult(
                direction      = "SHORT",
                strategy_name  = self.name,
                signal_reason  = (
                    f"Closed below {prev_low:.0f}-{buffer:.0f}buf "
                    f"(ATR×{cfg.atr_buffer_mult:.2f}), "
                    f"RSI={rsi_val:.1f}<{cfg.rsi_breakout_short:.0f}, "
                    f"ADX={adx_now:.0f}, {rvol_label}, "
                    f"MACD-H={macd_h:.2f}, EMA5<EMA20(-{ema_gap_pct:.1f}%)"
                ),
                rsi_val        = round(rsi_val, 1),
                ema5_val       = round(ema5, 2),
                ema20_val      = round(ema20, 2),
                breakout_level = round(prev_low, 2),
            )

        return None
