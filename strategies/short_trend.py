"""
short_trend.py
──────────────
Bearish counterpart to TrendFollowStrategy.
Enters SHORT equity positions when price breaks down through a multi-bar low
in a confirmed downtrend. Captures the full leg of trending bear moves.

Entry conditions (all must pass):
  1. Regime:    TRENDING (regime_detector confirms directional bias)
  2. EMA stack: EMA9 < EMA21 < EMA50 — all bearish
  3. Breakdown: close < 20-bar low  (momentum confirmation)
  4. RSI range: 30–55  — trend confirmed but not yet exhausted
                          RSI < 30 = already oversold, don't chase
                          RSI > 55 = bounce too strong, wait for failure
  5. ADX:       >= 20   — strong directional trend, not a choppy fade
  6. Volume:    RVOL > 1.2× average (institutional selling, not thin air)
  7. Daily:     daily EMA9 < EMA21 (longer timeframe agrees)

Exit rules (managed by position_manager.py):
  Stop:    15-bar swing HIGH + 0.5 ATR buffer (above entry)
  Target1: entry − 1.5R  (first scale-out)
  Target2: entry − 2.5R  (full exit)

Works on all NSE equities. Indices use DirectionalOptions for bearish exposure.
Product type: INTRADAY (NSE allows intraday short selling without delivery).
"""

import logging
from typing import Optional

from analysis.indicators import (
    adx, atr, ema_alignment, momentum_score,
    relative_volume, rsi,
)
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_RISK_REWARD, MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────
BREAKDOWN_LOOKBACK   = 20    # bars to look back for the N-bar low
SWING_HIGH_LOOKBACK  = 15    # bars to look back for the stop-loss swing high
MIN_RVOL             = 1.2   # lower than TrendFollow — sustained selloffs need less RVOL
ATR_STOP_BUFFER      = 0.5   # stop = swing_high + (ATR × buffer)
TARGET_1_R           = 1.5   # first target at 1.5R
TARGET_2_R           = 2.5   # second target at 2.5R
MIN_ADX              = 20    # trend must be directional
RSI_MIN              = 30    # RSI floor — don't short exhausted selloffs
RSI_MAX              = 55    # RSI ceiling — momentum must be bearish, not bouncing


class ShortTrendStrategy(BaseStrategy):
    """
    Fires SHORT signals on equities in confirmed downtrends.
    Symmetric counterpart to TrendFollowStrategy — covers bear markets.
    """

    def __init__(self):
        super().__init__()
        self.name       = "ShortTrend"
        self.hold_type  = "swing"
        self.timeframe  = "1H"
        self.confirm_tf = "1D"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        if "-INDEX" in symbol:
            return None  # indices use DirectionalOptions for bearish exposure

        # ── 1. Regime check ───────────────────────────────────────
        regime_result = regime_detector.get_regime(symbol, self.timeframe)
        if regime_result.regime not in (Regime.TRENDING, Regime.BREAKOUT):
            self.log_skip(symbol, f"Regime {regime_result.regime.value} — not suitable for short trend")
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

        # ── 3. EMA alignment — must be fully bearish ──────────────
        alignment = ema_alignment(df_1h)
        if not (alignment["ema9"] < alignment["ema21"] < alignment["ema50"]):
            self.log_skip(
                symbol,
                f"EMA not bearishly aligned (9/21/50): "
                f"{alignment['ema9']:.2f}/{alignment['ema21']:.2f}/{alignment['ema50']:.2f}"
            )
            return None

        # ── 4. Breakdown confirmation ─────────────────────────────
        # Price must close below the N-bar low (range excludes the current bar).
        recent_low    = df_1h["low"].iloc[-(BREAKDOWN_LOOKBACK + 1):-1].min()
        current_close = df_1h["close"].iloc[-1]

        if current_close >= recent_low:
            self.log_skip(
                symbol,
                f"No breakdown: close {current_close:.2f} >= "
                f"{BREAKDOWN_LOOKBACK}-bar low {recent_low:.2f}"
            )
            return None

        # ── 5. Volume confirmation ────────────────────────────────
        rvol = relative_volume(df_1h).iloc[-1]
        if rvol < MIN_RVOL:
            self.log_skip(symbol, f"Weak volume: RVOL {rvol:.2f} < {MIN_RVOL}")
            return None

        # ── 6. RSI — confirm momentum without chasing exhaustion ──
        rsi_val = rsi(df_1h["close"]).iloc[-1]
        if rsi_val < RSI_MIN:
            self.log_skip(symbol, f"RSI oversold ({rsi_val:.0f} < {RSI_MIN}) — too late to short")
            return None
        if rsi_val > RSI_MAX:
            self.log_skip(symbol, f"RSI too strong ({rsi_val:.0f} > {RSI_MAX}) — bounce in progress")
            return None

        # ── 7. ADX strength ───────────────────────────────────────
        adx_series, plus_di, minus_di = adx(df_1h)
        adx_val = adx_series.iloc[-1]
        if adx_val < MIN_ADX:
            self.log_skip(symbol, f"ADX too weak: {adx_val:.0f} < {MIN_ADX}")
            return None

        # Confirm DI- > DI+ (bearish directional pressure)
        if minus_di.iloc[-1] <= plus_di.iloc[-1]:
            self.log_skip(
                symbol,
                f"DI not bearish: DI-={minus_di.iloc[-1]:.0f} <= DI+={plus_di.iloc[-1]:.0f}"
            )
            return None

        # ── 8. Daily trend confirmation ───────────────────────────
        if df_1d is not None:
            daily_align = ema_alignment(df_1d)
            if not (daily_align["ema9"] < daily_align["ema21"]):
                self.log_skip(symbol, "Daily EMA9 > EMA21 — downtrend not confirmed on daily")
                return None

        # ── 9. Calculate entry, stop, targets ────────────────────
        atr_val    = atr(df_1h).iloc[-1]
        entry      = ltp

        # Stop above the recent swing high + buffer (must be > entry for SHORT)
        swing_high = df_1h["high"].iloc[-SWING_HIGH_LOOKBACK:-1].max()
        stop       = max(swing_high, entry) + (ATR_STOP_BUFFER * atr_val)
        risk       = stop - entry   # positive number

        if risk <= 0:
            self.log_skip(symbol, "Risk calculation error — invalid stop")
            return None

        # Targets are BELOW entry for a short position
        target_1 = entry - (TARGET_1_R * risk)
        target_2 = entry - (TARGET_2_R * risk)

        # 52-week low as a structural target reference (if available)
        target_3 = 0.0
        if df_1d is not None and len(df_1d) >= 50:
            year_low = df_1d["low"].tail(252).min()
            if year_low < target_2:
                target_3 = year_low

        # ── 10. Risk:Reward filter ────────────────────────────────
        rr = (entry - target_1) / risk   # reward / risk for short
        if rr < MIN_RISK_REWARD:
            self.log_skip(symbol, f"R:R {rr:.1f} below minimum {MIN_RISK_REWARD}")
            return None

        # ── 11. Confidence score ──────────────────────────────────
        daily_3ema = (
            df_1d is not None
            and (a := ema_alignment(df_1d))
            and a["ema9"] < a["ema21"] < a["ema50"]
        )
        ema200_bonus = alignment["ema50"] < alignment["ema200"]  # full bearish stack
        confidence = self._calculate_confidence(
            regime_result  = regime_result,
            adx_val        = adx_val,
            rvol           = rvol,
            rsi_val        = rsi_val,
            daily_aligned  = daily_3ema,
            ema200_bonus   = ema200_bonus,
            mom_score      = momentum_score(df_1h),
            minus_di_edge  = minus_di.iloc[-1] - plus_di.iloc[-1],
        )

        if confidence < MIN_SIGNAL_CONFIDENCE:
            self.log_skip(
                symbol,
                f"Confidence {confidence:.0%} below minimum {MIN_SIGNAL_CONFIDENCE:.0%}"
            )
            return None

        # ── 12. Build signal ──────────────────────────────────────
        reason = (
            f"Breakdown below {BREAKDOWN_LOOKBACK}-bar low {recent_low:.2f} | "
            f"EMA bearish | ADX {adx_val:.0f} | DI- {minus_di.iloc[-1]:.0f} | "
            f"RVOL {rvol:.1f}x | RSI {rsi_val:.0f}"
        )

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = Direction.SHORT,
            signal_type = SignalType.EQUITY,
            entry       = round(entry, 2),
            stop_loss   = round(stop, 2),       # above entry for SHORT
            target_1    = round(target_1, 2),   # below entry for SHORT
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
        adx_val:       float,
        rvol:          float,
        rsi_val:       float,
        daily_aligned: bool,
        ema200_bonus:  bool  = False,
        mom_score:     float = 0.0,
        minus_di_edge: float = 0.0,
    ) -> float:
        """
        Weighted confidence for a SHORT setup.
        Optimal RSI for shorting is 40–55 (confirmed but not exhausted).
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

        # RSI quality — optimal short zone is 40-55 (0 – 0.15)
        if 40 <= rsi_val <= 55:
            score += 0.15
        elif 30 <= rsi_val < 40 or 55 < rsi_val <= RSI_MAX:
            score += 0.08

        # Daily EMA alignment (0 – 0.12)
        if daily_aligned:
            score += 0.12

        # Full EMA stack (50 < 200 = fully bearish) (0 – 0.05)
        if ema200_bonus:
            score += 0.05

        # DI- vs DI+ edge (0 – 0.10) — stronger directional pressure
        if minus_di_edge > 15:
            score += 0.10
        elif minus_di_edge > 8:
            score += 0.06

        return round(min(score, 1.0), 2)
