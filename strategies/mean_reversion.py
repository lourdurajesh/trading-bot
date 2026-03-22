"""
mean_reversion.py
─────────────────
Mean reversion strategy for range-bound markets.

Logic:
  - Active when regime = RANGING
  - LONG entry: RSI < 35 + price near/below lower Bollinger Band
  - SHORT entry: RSI > 65 + price near/above upper Bollinger Band
  - EMA(50) used as dynamic S/R filter — only trade in its direction
  - Stop: below recent swing low (LONG) or above swing high (SHORT)
  - Target: EMA(21) as mean, then upper/lower band
  - Signal timeframe: 15m, filter on 1H
"""

import logging
from typing import Optional

from analysis.indicators import (
    atr, bollinger_bands, ema, relative_volume, rsi, swing_highs, swing_lows,
)
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_RISK_REWARD, MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────
RSI_OVERSOLD        = 35
RSI_OVERBOUGHT      = 65
BB_PROXIMITY_PCT    = 0.005    # price within 0.5% of band = "near band"
MIN_RVOL            = 1.0      # lower than trend — reversals can be quiet
ATR_STOP_BUFFER     = 0.5      # extra ATR buffer below swing low


class MeanReversionStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.name       = "MeanReversion"
        self.timeframe  = "15m"
        self.confirm_tf = "1H"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        """
        Returns LONG or SHORT signal if mean reversion conditions are met.
        """
        if not self.enabled:
            return None

        # ── 1. Regime check ───────────────────────────────────────
        regime_1h = regime_detector.get_regime(symbol, "1H")
        if regime_1h.regime not in (Regime.RANGING,):
            self.log_skip(symbol, f"Regime {regime_1h.regime.value} not suitable for mean reversion")
            return None

        # ── 2. Load data ──────────────────────────────────────────
        df_15m = self.get_ohlcv(symbol, self.timeframe)
        df_1h  = self.get_ohlcv(symbol, self.confirm_tf)

        if df_15m is None:
            self.log_skip(symbol, "Insufficient 15m data")
            return None

        ltp = self.get_ltp(symbol)
        if not ltp:
            return None

        close   = df_15m["close"]
        rsi_val = rsi(close).iloc[-1]

        # ── 3. Bollinger Bands ────────────────────────────────────
        upper, middle, lower = bollinger_bands(close)
        upper_val  = upper.iloc[-1]
        middle_val = middle.iloc[-1]
        lower_val  = lower.iloc[-1]

        # ── 4. EMA(50) direction filter on 1H ────────────────────
        ema50_direction = "neutral"
        if df_1h is not None:
            ema50 = ema(df_1h["close"], 50)
            ema21 = ema(df_1h["close"], 21)
            if ema50.iloc[-1] > ema50.iloc[-5]:
                ema50_direction = "up"
            elif ema50.iloc[-1] < ema50.iloc[-5]:
                ema50_direction = "down"

        # ── 5. Evaluate LONG setup ────────────────────────────────
        near_lower = ltp <= lower_val * (1 + BB_PROXIMITY_PCT)
        oversold   = rsi_val < RSI_OVERSOLD

        if near_lower and oversold and ema50_direction in ("up", "neutral"):
            return self._build_long_signal(symbol, df_15m, ltp, rsi_val,
                                           lower_val, middle_val, regime_1h)

        # ── 6. Evaluate SHORT setup ───────────────────────────────
        near_upper  = ltp >= upper_val * (1 - BB_PROXIMITY_PCT)
        overbought  = rsi_val > RSI_OVERBOUGHT

        if near_upper and overbought and ema50_direction in ("down", "neutral"):
            return self._build_short_signal(symbol, df_15m, ltp, rsi_val,
                                            upper_val, middle_val, regime_1h)

        self.log_skip(
            symbol,
            f"No reversion setup: RSI {rsi_val:.0f}, price {ltp:.2f}, "
            f"BB [{lower_val:.2f} – {upper_val:.2f}]"
        )
        return None

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — signal builders
    # ─────────────────────────────────────────────────────────────

    def _build_long_signal(
        self, symbol, df, ltp, rsi_val, lower_val, middle_val, regime_result
    ) -> Optional[Signal]:
        """Build a LONG mean reversion signal."""

        atr_val = atr(df).iloc[-1]

        # Stop: below recent swing low with ATR buffer
        swing_low_series = swing_lows(df["low"], lookback=5)
        recent_lows = df["low"][swing_low_series].tail(3)
        if len(recent_lows) == 0:
            stop = ltp - (2.0 * atr_val)
        else:
            stop = recent_lows.min() - (ATR_STOP_BUFFER * atr_val)

        risk = ltp - stop
        if risk <= 0:
            return None

        target_1 = middle_val                        # mean reversion to EMA(21)
        target_2 = ltp + (2.0 * risk)               # 2R extension

        rr = (target_1 - ltp) / risk
        if rr < MIN_RISK_REWARD:
            self.log_skip(symbol, f"Long reversion R:R {rr:.1f} too low")
            return None

        confidence = self._score_long(rsi_val, ltp, lower_val, atr_val, regime_result)
        if confidence < MIN_SIGNAL_CONFIDENCE:
            self.log_skip(symbol, f"Long reversion confidence {confidence:.0%} too low")
            return None

        reason = (
            f"RSI oversold {rsi_val:.0f} | Price at lower BB {lower_val:.2f} | "
            f"Target: mean {middle_val:.2f}"
        )

        signal = Signal(
            symbol     = symbol,
            strategy   = self.name,
            direction  = Direction.LONG,
            signal_type = SignalType.EQUITY,
            entry      = round(ltp, 2),
            stop_loss  = round(stop, 2),
            target_1   = round(target_1, 2),
            target_2   = round(target_2, 2),
            confidence = confidence,
            timeframe  = self.timeframe,
            regime     = regime_result.regime.value,
            reason     = reason,
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal

    def _build_short_signal(
        self, symbol, df, ltp, rsi_val, upper_val, middle_val, regime_result
    ) -> Optional[Signal]:
        """Build a SHORT mean reversion signal."""

        atr_val = atr(df).iloc[-1]

        # Stop: above recent swing high with ATR buffer
        swing_high_series = swing_highs(df["high"], lookback=5)
        recent_highs = df["high"][swing_high_series].tail(3)
        if len(recent_highs) == 0:
            stop = ltp + (2.0 * atr_val)
        else:
            stop = recent_highs.max() + (ATR_STOP_BUFFER * atr_val)

        risk = stop - ltp
        if risk <= 0:
            return None

        target_1 = middle_val
        target_2 = ltp - (2.0 * risk)

        rr = (ltp - target_1) / risk
        if rr < MIN_RISK_REWARD:
            self.log_skip(symbol, f"Short reversion R:R {rr:.1f} too low")
            return None

        confidence = self._score_short(rsi_val, ltp, upper_val, atr_val, regime_result)
        if confidence < MIN_SIGNAL_CONFIDENCE:
            self.log_skip(symbol, f"Short reversion confidence {confidence:.0%} too low")
            return None

        reason = (
            f"RSI overbought {rsi_val:.0f} | Price at upper BB {upper_val:.2f} | "
            f"Target: mean {middle_val:.2f}"
        )

        signal = Signal(
            symbol     = symbol,
            strategy   = self.name,
            direction  = Direction.SHORT,
            signal_type = SignalType.EQUITY,
            entry      = round(ltp, 2),
            stop_loss  = round(stop, 2),
            target_1   = round(target_1, 2),
            target_2   = round(target_2, 2),
            confidence = confidence,
            timeframe  = self.timeframe,
            regime     = regime_result.regime.value,
            reason     = reason,
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal

    def _score_long(self, rsi_val, ltp, lower_val, atr_val, regime_result) -> float:
        score = 0.0
        score += 0.25 * regime_result.confidence
        if rsi_val < 25:
            score += 0.30
        elif rsi_val < 30:
            score += 0.22
        elif rsi_val < 35:
            score += 0.15
        # Price below lower band (deeper = stronger signal)
        penetration = (lower_val - ltp) / lower_val
        if penetration > 0.01:
            score += 0.25
        elif penetration > 0:
            score += 0.15
        else:
            score += 0.08
        score += 0.20    # base for passing all filters
        return round(min(score, 1.0), 2)

    def _score_short(self, rsi_val, ltp, upper_val, atr_val, regime_result) -> float:
        score = 0.0
        score += 0.25 * regime_result.confidence
        if rsi_val > 75:
            score += 0.30
        elif rsi_val > 70:
            score += 0.22
        elif rsi_val > 65:
            score += 0.15
        penetration = (ltp - upper_val) / upper_val
        if penetration > 0.01:
            score += 0.25
        elif penetration > 0:
            score += 0.15
        else:
            score += 0.08
        score += 0.20
        return round(min(score, 1.0), 2)
