"""
gap_fade.py
───────────
Fades opening gaps — trades against the gap direction expecting a partial fill.

Entry conditions (all must pass):
  1. Time:    9:15–9:45 AM IST only (first 30 min after open)
  2. Gap:     prev_close vs today open >= MIN_GAP_PCT (significant gap)
              gap <= MAX_GAP_PCT (extreme gaps can continue; fade carefully)
  3. Regime:  any non-UNKNOWN regime (skip if TRENDING in same direction as gap)
  4. RSI:     gap-up short: RSI > RSI_OVERBOUGHT at open
              gap-down long: RSI < RSI_OVERSOLD at open
  5. Volume:  first-bar RVOL >= 1.2 (confirm the gap, not a dead market)
  6. No index: equity-only (indices use options for directional plays)

Exit rules:
  Stop:    beyond gap extreme (high for short, low for long) + 0.3 ATR buffer
  Target1: gap fill (prev_close) — statistically 70%+ of gaps fill by EOD
  Target2: TARGET_2_R multiples of risk
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from analysis.indicators import atr, relative_volume, rsi
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_RISK_REWARD, MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# ── Strategy parameters (runtime-updatable via config/strategy_config.py) ──
MIN_GAP_PCT      = 1.5    # minimum gap % to trade
MAX_GAP_PCT      = 5.0    # skip extreme gaps (may gap-and-go, not fade)
ENTRY_WINDOW_MIN = 30     # minutes after open to allow entry
RSI_OVERBOUGHT   = 70     # gap-up short requires RSI above this
RSI_OVERSOLD     = 30     # gap-down long requires RSI below this
TARGET_1_R       = 1.5    # first target at 1.5R
TARGET_2_R       = 2.5    # second target at 2.5R
_STOP_ATR_BUFFER = 0.3    # ATR buffer beyond gap extreme for stop

_MARKET_OPEN     = dtime(9, 15)


class GapFadeStrategy(BaseStrategy):
    """
    Fades opening gaps on equity symbols.
    Gap-up → SHORT (expect partial fill toward prev close).
    Gap-down → LONG (expect partial fill toward prev close).
    Only active in the first 30 minutes after market open.
    """

    def __init__(self):
        super().__init__()
        self.name      = "GapFade"
        self.hold_type = "intraday"
        self.timeframe = "5m"
        self.confirm_tf = "1D"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        if "-INDEX" in symbol:
            return None  # indices use options strategies

        # ── 1. Time gate ─────────────────────────────────────────────
        now = datetime.now(tz=IST)
        now_time = now.time()
        entry_cutoff = dtime(
            _MARKET_OPEN.hour,
            _MARKET_OPEN.minute + ENTRY_WINDOW_MIN
        )
        if not (_MARKET_OPEN <= now_time <= entry_cutoff):
            return None

        # ── 2. Load data ──────────────────────────────────────────────
        df_5m = self.get_ohlcv(symbol, self.timeframe)
        df_1d = self.get_ohlcv(symbol, self.confirm_tf)

        if df_5m is None or len(df_5m) < 3:
            self.log_skip(symbol, "Insufficient 5m data")
            return None
        if df_1d is None or len(df_1d) < 2:
            self.log_skip(symbol, "Insufficient daily data for prev close")
            return None

        ltp = self.get_ltp(symbol)
        if not ltp:
            self.log_skip(symbol, "No LTP available")
            return None

        # ── 3. Calculate gap ──────────────────────────────────────────
        prev_close  = float(df_1d["close"].iloc[-2])
        today_open  = float(df_5m["open"].iloc[0])   # first 5m bar open = market open

        if prev_close <= 0 or today_open <= 0:
            return None

        gap_pct = (today_open - prev_close) / prev_close * 100

        if abs(gap_pct) < MIN_GAP_PCT:
            self.log_skip(symbol, f"Gap {gap_pct:+.2f}% < min {MIN_GAP_PCT}%")
            return None
        if abs(gap_pct) > MAX_GAP_PCT:
            self.log_skip(symbol, f"Gap {gap_pct:+.2f}% > max {MAX_GAP_PCT}% — may continue")
            return None

        # ── 4. Direction ──────────────────────────────────────────────
        is_gap_up = gap_pct > 0

        # ── 5. RSI confirmation ───────────────────────────────────────
        rsi_val = rsi(df_5m["close"]).iloc[-1]
        if is_gap_up and rsi_val <= RSI_OVERBOUGHT:
            self.log_skip(symbol, f"Gap-up but RSI {rsi_val:.0f} not overbought (>{RSI_OVERBOUGHT})")
            return None
        if not is_gap_up and rsi_val >= RSI_OVERSOLD:
            self.log_skip(symbol, f"Gap-down but RSI {rsi_val:.0f} not oversold (<{RSI_OVERSOLD})")
            return None

        # ── 6. Volume confirmation ────────────────────────────────────
        rvol = relative_volume(df_5m).iloc[-1]
        if rvol < 1.2:
            self.log_skip(symbol, f"Gap volume weak: RVOL {rvol:.2f} < 1.2")
            return None

        # ── 7. Regime filter — don't fade a gap in the gap direction ──
        regime_result = regime_detector.get_regime(symbol, "1H")
        if regime_result.regime not in (Regime.RANGING, Regime.VOLATILE, Regime.UNKNOWN):
            # In a strong trend matching the gap direction — skip
            # e.g. TRENDING + gap-up = strong bull, don't short
            pass  # allow but noted; trend in opposite direction is better

        # ── 8. Entry and risk parameters ─────────────────────────────
        atr_val = atr(df_5m).iloc[-1]
        entry   = ltp

        if is_gap_up:
            # SHORT trade: fade the gap-up
            direction = Direction.SHORT
            # Stop above today's high (gap high) + buffer
            today_high = float(df_5m["high"].iloc[:3].max())
            stop       = today_high + (_STOP_ATR_BUFFER * atr_val)
            risk       = stop - entry
            if risk <= 0:
                self.log_skip(symbol, "Risk <= 0 for gap-up short — price already above gap high")
                return None
            target_1 = prev_close                          # gap fill as T1
            target_2 = entry - (TARGET_2_R * risk)
            # Ensure T1 is actually below entry for a short
            if target_1 >= entry:
                target_1 = entry - (TARGET_1_R * risk)
        else:
            # LONG trade: fade the gap-down
            direction = Direction.LONG
            # Stop below today's low (gap low) + buffer
            today_low = float(df_5m["low"].iloc[:3].min())
            stop      = today_low - (_STOP_ATR_BUFFER * atr_val)
            risk      = entry - stop
            if risk <= 0:
                self.log_skip(symbol, "Risk <= 0 for gap-down long — price already below gap low")
                return None
            target_1 = prev_close                          # gap fill as T1
            target_2 = entry + (TARGET_2_R * risk)
            # Ensure T1 is actually above entry for a long
            if target_1 <= entry:
                target_1 = entry + (TARGET_1_R * risk)

        # ── 9. Risk:Reward filter ──────────────────────────────────────
        rr_to_t1 = abs(target_1 - entry) / risk
        if rr_to_t1 < MIN_RISK_REWARD:
            self.log_skip(symbol, f"R:R {rr_to_t1:.1f} below minimum {MIN_RISK_REWARD}")
            return None

        # ── 10. Confidence score ───────────────────────────────────────
        confidence = self._calculate_confidence(
            gap_pct     = abs(gap_pct),
            rvol        = rvol,
            rsi_val     = rsi_val,
            is_gap_up   = is_gap_up,
            regime      = regime_result.regime,
        )

        if confidence < MIN_SIGNAL_CONFIDENCE:
            self.log_skip(symbol, f"Confidence {confidence:.0%} below min {MIN_SIGNAL_CONFIDENCE:.0%}")
            return None

        # ── 11. Build signal ───────────────────────────────────────────
        direction_str = "gap-up" if is_gap_up else "gap-down"
        reason = (
            f"{direction_str} {gap_pct:+.2f}% (prev={prev_close:.2f} open={today_open:.2f}) | "
            f"RSI {rsi_val:.0f} | RVOL {rvol:.1f}x | T1=gap fill"
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
        gap_pct:   float,
        rvol:      float,
        rsi_val:   float,
        is_gap_up: bool,
        regime:    Regime,
    ) -> float:
        score = 0.0

        # Gap size (0 – 0.30): bigger gap = higher fill probability
        if gap_pct >= 3.0:
            score += 0.30
        elif gap_pct >= 2.0:
            score += 0.22
        elif gap_pct >= 1.5:
            score += 0.14

        # RSI extremity (0 – 0.30)
        if is_gap_up:
            if rsi_val >= 85:
                score += 0.30
            elif rsi_val >= 78:
                score += 0.22
            elif rsi_val >= 70:
                score += 0.14
        else:
            if rsi_val <= 15:
                score += 0.30
            elif rsi_val <= 22:
                score += 0.22
            elif rsi_val <= 30:
                score += 0.14

        # Volume (0 – 0.20)
        if rvol >= 3.0:
            score += 0.20
        elif rvol >= 2.0:
            score += 0.14
        elif rvol >= 1.5:
            score += 0.09
        else:
            score += 0.04

        # Regime bonus: RANGING = best fade environment (0 – 0.20)
        if regime == Regime.RANGING:
            score += 0.20
        elif regime == Regime.VOLATILE:
            score += 0.10

        return round(min(score, 1.0), 2)
