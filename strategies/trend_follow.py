"""
trend_follow.py
───────────────
Momentum breakout swing strategy.

Logic:
  - Active when regime = TRENDING or BREAKOUT
  - Entry: price breaks above N-day high with volume surge
  - EMA(9) > EMA(21) > EMA(50) alignment required
  - Stop: 1.5× ATR below entry candle low
  - Target 1: 2R, Target 2: 3R, Target 3: swing structure target
  - Signal timeframe: 1H, confirmed on Daily
"""

import logging
from typing import Optional

from analysis.indicators import (
    adx, atr, ema, ema_alignment, momentum_score,
    relative_volume, rsi,
)
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_RISK_REWARD, MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────
BREAKOUT_LOOKBACK    = 20     # bars to look back for high/low breakout
MIN_RVOL             = 1.4    # minimum relative volume for breakout confirm
ATR_STOP_MULTIPLIER  = 1.5    # stop = entry - (ATR × multiplier)
TARGET_1_R           = 2.0    # first target at 2R
TARGET_2_R           = 3.0    # second target at 3R
MIN_ADX              = 20     # minimum ADX for trend confirmation
MAX_RSI_ENTRY        = 75     # avoid chasing overbought entries


class TrendFollowStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.name       = "TrendFollow"
        self.timeframe  = "1H"
        self.confirm_tf = "1D"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        """
        Returns a LONG signal if all trend breakout conditions are met.
        Returns None if no setup found or conditions not satisfied.
        """
        if not self.enabled:
            return None

        # ── 1. Regime check ───────────────────────────────────────
        regime_result = regime_detector.get_regime(symbol, self.timeframe)
        if regime_result.regime not in (Regime.TRENDING, Regime.BREAKOUT):
            self.log_skip(symbol, f"Regime is {regime_result.regime.value} — not suitable for trend follow")
            return None

        # ── 2. Load data ──────────────────────────────────────────
        df_1h = self.get_ohlcv(symbol, self.timeframe)
        df_1d = self.get_ohlcv(symbol, self.confirm_tf)

        if df_1h is None:
            self.log_skip(symbol, "Insufficient 1H data")
            return None

        ltp = self.get_ltp(symbol)
        if not ltp:
            self.log_skip(symbol, "No LTP available")
            return None

        # ── 3. EMA alignment (trend filter) ──────────────────────
        # Require 3-EMA stack (9>21>50) rather than 4-EMA (9>21>50>200).
        # After a crash + recovery the 50 can lag below the 200 for 6-8 weeks
        # even when the trend is clearly resumed — the 200 gate creates a long
        # dead zone with zero LONG signals. EMA(200) still boosts confidence.
        alignment = ema_alignment(df_1h)
        if not (alignment["ema9"] > alignment["ema21"] > alignment["ema50"]):
            self.log_skip(symbol, f"EMA not bullishly aligned (9/21/50): {alignment['ema9']:.2f}/{alignment['ema21']:.2f}/{alignment['ema50']:.2f}")
            return None

        # ── 4. Breakout detection ─────────────────────────────────
        recent_high = df_1h["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max()
        current_close = df_1h["close"].iloc[-1]
        current_high  = df_1h["high"].iloc[-1]

        if current_close <= recent_high:
            self.log_skip(symbol, f"No breakout: close {current_close:.2f} <= {BREAKOUT_LOOKBACK}-bar high {recent_high:.2f}")
            return None

        # ── 5. Volume confirmation ────────────────────────────────
        rvol = relative_volume(df_1h).iloc[-1]
        if rvol < MIN_RVOL:
            self.log_skip(symbol, f"Weak volume: RVOL {rvol:.2f} < {MIN_RVOL}")
            return None

        # ── 6. RSI — avoid chasing ────────────────────────────────
        rsi_val = rsi(df_1h["close"]).iloc[-1]
        if rsi_val > MAX_RSI_ENTRY:
            self.log_skip(symbol, f"RSI overbought: {rsi_val:.0f} > {MAX_RSI_ENTRY}")
            return None

        # ── 7. ADX strength ───────────────────────────────────────
        adx_series, plus_di, minus_di = adx(df_1h)
        adx_val = adx_series.iloc[-1]
        if adx_val < MIN_ADX:
            self.log_skip(symbol, f"ADX too weak: {adx_val:.0f} < {MIN_ADX}")
            return None

        # ── 8. Daily trend confirmation ───────────────────────────
        if df_1d is not None:
            daily_align = ema_alignment(df_1d)
            if not (daily_align["ema9"] > daily_align["ema21"]):
                self.log_skip(symbol, "Daily EMA9 < EMA21 — trend not confirmed on daily")
                return None
            # EMA(200) on daily gives a confidence bonus (not a hard gate)


        # ── 9. Calculate entry, stop, targets ────────────────────
        atr_val  = atr(df_1h).iloc[-1]
        entry    = ltp                                        # enter at current market price
        stop     = entry - (ATR_STOP_MULTIPLIER * atr_val)   # 1.5 ATR below entry
        risk     = entry - stop

        if risk <= 0:
            self.log_skip(symbol, "Risk calculation error — invalid stop")
            return None

        target_1 = entry + (TARGET_1_R * risk)
        target_2 = entry + (TARGET_2_R * risk)

        # Swing structure target — 52-week high on daily if available
        target_3 = 0.0
        if df_1d is not None and len(df_1d) >= 50:
            target_3 = df_1d["high"].tail(252).max()
            if target_3 <= target_2:
                target_3 = 0.0

        # ── 10. Risk:Reward filter ────────────────────────────────
        rr = (target_1 - entry) / risk
        if rr < MIN_RISK_REWARD:
            self.log_skip(symbol, f"R:R {rr:.1f} below minimum {MIN_RISK_REWARD}")
            return None

        # ── 11. Confidence score ──────────────────────────────────
        daily_3ema = (
            df_1d is not None
            and (a := ema_alignment(df_1d))
            and a["ema9"] > a["ema21"] > a["ema50"]
        )
        ema200_bonus = alignment["ema50"] > alignment["ema200"]  # full stack bonus
        confidence = self._calculate_confidence(
            regime_result=regime_result,
            adx_val=adx_val,
            rvol=rvol,
            rsi_val=rsi_val,
            daily_aligned=daily_3ema,
            ema200_bonus=ema200_bonus,
            mom_score=momentum_score(df_1h),
        )

        if confidence < MIN_SIGNAL_CONFIDENCE:
            self.log_skip(symbol, f"Confidence {confidence:.0%} below minimum {MIN_SIGNAL_CONFIDENCE:.0%}")
            return None

        # ── 12. Build signal ──────────────────────────────────────
        reason = (
            f"Breakout above {BREAKOUT_LOOKBACK}-bar high {recent_high:.2f} | "
            f"EMA bullish | ADX {adx_val:.0f} | RVOL {rvol:.1f}x | RSI {rsi_val:.0f}"
        )

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = Direction.LONG,
            signal_type = SignalType.EQUITY,
            entry       = round(entry, 2),
            stop_loss   = round(stop, 2),
            target_1    = round(target_1, 2),
            target_2    = round(target_2, 2),
            target_3    = round(target_3, 2),
            confidence  = confidence,
            timeframe   = self.timeframe,
            regime      = regime_result.regime.value,
            reason      = reason,
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _calculate_confidence(
        self,
        regime_result,
        adx_val: float,
        rvol: float,
        rsi_val: float,
        daily_aligned: bool,
        ema200_bonus: bool = False,
        mom_score: float = 0.0,
    ) -> float:
        """
        Weighted confidence score based on signal quality factors.
        Returns 0.0 – 1.0.
        """
        score = 0.0

        # Regime quality (0 – 0.20)
        score += 0.20 * regime_result.confidence

        # ADX strength (0 – 0.20)
        if adx_val > 35:
            score += 0.20
        elif adx_val > 25:
            score += 0.14
        elif adx_val > 20:
            score += 0.08

        # Volume confirmation (0 – 0.18)
        if rvol > 2.5:
            score += 0.18
        elif rvol > 2.0:
            score += 0.13
        elif rvol > 1.5:
            score += 0.09
        else:
            score += 0.04

        # RSI quality — not overbought, ideally 55-70 (0 – 0.15)
        if 55 <= rsi_val <= 70:
            score += 0.15
        elif 50 <= rsi_val < 55 or 70 < rsi_val <= 75:
            score += 0.08

        # Daily timeframe alignment (0 – 0.12)
        if daily_aligned:
            score += 0.12

        # Full EMA stack (50>200) — extra confirmation (0 – 0.05)
        if ema200_bonus:
            score += 0.05

        # Momentum score (0 – 0.10)
        score += 0.10 * (mom_score / 10.0)

        return round(min(score, 1.0), 2)
