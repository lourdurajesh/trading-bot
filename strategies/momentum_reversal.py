"""
momentum_reversal.py
────────────────────
Extreme RSI mean-reversion strategy.
Fires when price reaches an extreme RSI reading in a NON-trending market,
expecting a snap-back to the mean.

Entry conditions (all must pass):
  1. RSI:    >= RSI_OVERBOUGHT (82 default) → SHORT reversal
             <= RSI_OVERSOLD  (18 default) → LONG reversal
  2. ADX:    < MAX_ADX (25) — low trend strength; in trends, extremes extend
  3. RVOL:   >= 1.3 — volume spike into the extreme (capitulation/euphoria)
  4. RSI was extreme for >= LOOKBACK_BARS consecutive bars (exhaustion)
  5. Regime: RANGING or VOLATILE (best reversal environments)
  6. No index: equity-only (index options handle directional index plays)

Exit rules:
  Stop:    ATR_STOP_BUFFER * ATR beyond the extreme (swing high for short,
           swing low for long) — a new extreme invalidates the reversal thesis
  Target1: 1.5R
  Target2: 2.5R, or EMA21 (mean) if closer
"""

import logging
from typing import Optional

from analysis.indicators import adx, atr, ema, relative_volume, rsi
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_RISK_REWARD, MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

# ── Strategy parameters (runtime-updatable via config/strategy_config.py) ──
RSI_OVERBOUGHT = 82    # SHORT reversal trigger
RSI_OVERSOLD   = 18    # LONG reversal trigger
MAX_ADX        = 25    # ADX must be BELOW this — not a strong trend
ATR_STOP_BUFFER= 0.5   # ATR buffer beyond swing extreme for stop
TARGET_1_R     = 1.5   # first target
TARGET_2_R     = 2.5   # second target
LOOKBACK_BARS  = 10    # bars to check for RSI extremity confirmation
_MIN_RVOL      = 1.3   # minimum volume spike for capitulation signal


class MomentumReversalStrategy(BaseStrategy):
    """
    Fires SHORT at extreme overbought RSI and LONG at extreme oversold RSI
    in non-trending, range-bound or volatile markets.
    """

    def __init__(self):
        super().__init__()
        self.name      = "MomentumReversal"
        self.hold_type = "intraday"
        self.timeframe = "1H"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        if "-INDEX" in symbol:
            return None  # indices use options strategies

        # ── 1. Load data ──────────────────────────────────────────────
        df_1h = self.get_ohlcv(symbol, self.timeframe)
        if df_1h is None or len(df_1h) < LOOKBACK_BARS + 5:
            self.log_skip(symbol, "Insufficient 1H data")
            return None

        ltp = self.get_ltp(symbol)
        if not ltp:
            self.log_skip(symbol, "No LTP available")
            return None

        # ── 2. Regime filter ──────────────────────────────────────────
        regime_result = regime_detector.get_regime(symbol, self.timeframe)
        if regime_result.regime == Regime.UNKNOWN:
            return None
        if regime_result.regime in (Regime.TRENDING, Regime.BREAKOUT):
            self.log_skip(symbol, f"Regime {regime_result.regime.value} — extremes extend in trends")
            return None

        # ── 3. RSI check ──────────────────────────────────────────────
        rsi_series = rsi(df_1h["close"])
        rsi_val    = rsi_series.iloc[-1]

        is_short_setup = rsi_val >= RSI_OVERBOUGHT
        is_long_setup  = rsi_val <= RSI_OVERSOLD

        if not (is_short_setup or is_long_setup):
            self.log_skip(symbol, f"RSI {rsi_val:.0f} not extreme (need >={RSI_OVERBOUGHT} or <={RSI_OVERSOLD})")
            return None

        # ── 4. ADX filter — must NOT be in a strong trend ─────────────
        adx_series, plus_di, minus_di = adx(df_1h)
        adx_val = adx_series.iloc[-1]
        if adx_val >= MAX_ADX:
            self.log_skip(symbol, f"ADX {adx_val:.0f} >= {MAX_ADX} — strong trend, extremes may extend")
            return None

        # ── 5. RSI extremity duration ─────────────────────────────────
        # Require RSI to have been extreme for at least a few bars
        lookback = min(LOOKBACK_BARS, len(rsi_series) - 1)
        recent_rsi = rsi_series.iloc[-lookback:]
        if is_short_setup:
            extreme_bars = (recent_rsi >= RSI_OVERBOUGHT).sum()
        else:
            extreme_bars = (recent_rsi <= RSI_OVERSOLD).sum()

        if extreme_bars < 2:
            self.log_skip(symbol, f"RSI {rsi_val:.0f} extreme for only {extreme_bars} bars — wait for exhaustion")
            return None

        # ── 6. Volume spike ───────────────────────────────────────────
        rvol = relative_volume(df_1h).iloc[-1]
        if rvol < _MIN_RVOL:
            self.log_skip(symbol, f"Volume weak: RVOL {rvol:.2f} < {_MIN_RVOL} — no capitulation")
            return None

        # ── 7. Calculate entry, stop, targets ────────────────────────
        atr_val = atr(df_1h).iloc[-1]
        entry   = ltp
        ema21   = ema(df_1h["close"], 21).iloc[-1]

        if is_short_setup:
            direction  = Direction.SHORT
            swing_high = df_1h["high"].iloc[-LOOKBACK_BARS:-1].max()
            stop       = max(swing_high, entry) + (ATR_STOP_BUFFER * atr_val)
            risk       = stop - entry
            if risk <= 0:
                self.log_skip(symbol, "Risk calculation error for SHORT")
                return None
            target_1 = entry - (TARGET_1_R * risk)
            # Use EMA21 as T2 if it's a better mean target
            ema_target = ema21 if ema21 < entry else entry - (TARGET_2_R * risk)
            target_2   = max(ema_target, entry - (TARGET_2_R * risk))
        else:
            direction  = Direction.LONG
            swing_low  = df_1h["low"].iloc[-LOOKBACK_BARS:-1].min()
            stop       = min(swing_low, entry) - (ATR_STOP_BUFFER * atr_val)
            risk       = entry - stop
            if risk <= 0:
                self.log_skip(symbol, "Risk calculation error for LONG")
                return None
            target_1 = entry + (TARGET_1_R * risk)
            ema_target = ema21 if ema21 > entry else entry + (TARGET_2_R * risk)
            target_2   = min(ema_target, entry + (TARGET_2_R * risk))

        # ── 8. Risk:Reward filter ──────────────────────────────────────
        rr = abs(target_1 - entry) / risk
        if rr < MIN_RISK_REWARD:
            self.log_skip(symbol, f"R:R {rr:.1f} below minimum {MIN_RISK_REWARD}")
            return None

        # ── 9. Confidence score ───────────────────────────────────────
        confidence = self._calculate_confidence(
            rsi_val      = rsi_val,
            is_short     = is_short_setup,
            adx_val      = adx_val,
            rvol         = rvol,
            extreme_bars = extreme_bars,
            regime       = regime_result.regime,
        )

        if confidence < MIN_SIGNAL_CONFIDENCE:
            self.log_skip(symbol, f"Confidence {confidence:.0%} below min {MIN_SIGNAL_CONFIDENCE:.0%}")
            return None

        # ── 10. Build signal ──────────────────────────────────────────
        setup_type = "overbought short" if is_short_setup else "oversold long"
        reason = (
            f"RSI {rsi_val:.0f} extreme {setup_type} | "
            f"ADX {adx_val:.0f} (no trend) | RVOL {rvol:.1f}x | "
            f"Extreme for {extreme_bars} bars"
        )

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = direction,
            signal_type = SignalType.EQUITY,
            entry       = round(entry, 2),
            stop_loss   = round(stop, 2),
            target_1    = round(target_1, 2),
            target_2    = round(target_2, 2),
            target_3    = 0.0,
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
        rsi_val:      float,
        is_short:     bool,
        adx_val:      float,
        rvol:         float,
        extreme_bars: int,
        regime:       Regime,
    ) -> float:
        score = 0.0

        # RSI extremity (0 – 0.30)
        if is_short:
            if rsi_val >= 90:
                score += 0.30
            elif rsi_val >= 85:
                score += 0.22
            elif rsi_val >= 82:
                score += 0.14
        else:
            if rsi_val <= 10:
                score += 0.30
            elif rsi_val <= 15:
                score += 0.22
            elif rsi_val <= 18:
                score += 0.14

        # ADX weakness (0 – 0.20): lower ADX = stronger reversal signal
        if adx_val < 15:
            score += 0.20
        elif adx_val < 20:
            score += 0.13
        elif adx_val < 25:
            score += 0.07

        # Volume spike (0 – 0.20)
        if rvol >= 3.0:
            score += 0.20
        elif rvol >= 2.0:
            score += 0.14
        elif rvol >= 1.5:
            score += 0.09
        else:
            score += 0.04

        # Exhaustion duration (0 – 0.15): more bars = more exhausted
        if extreme_bars >= 5:
            score += 0.15
        elif extreme_bars >= 3:
            score += 0.09
        elif extreme_bars >= 2:
            score += 0.05

        # Regime bonus (0 – 0.15)
        if regime == Regime.RANGING:
            score += 0.15
        elif regime == Regime.VOLATILE:
            score += 0.08

        return round(min(score, 1.0), 2)
