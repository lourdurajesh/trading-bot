"""
opening_range.py
────────────────
Spec TASK 6: Opening Range Engine.

Classifies the opening session behavior from first-candle data:
  OPENING_DRIVE   — strong directional move at open (gap + momentum)
  OPEN_REJECTION  — price opened high/low but reversed quickly
  GAP_FILL        — gap open that is being filled toward previous close
  ORB_BREAKOUT    — price broke and held outside the opening range
  INSIDE_RANGE    — price contained within first 15-30 min range

Uses 5-minute bars for detection. Results are cached per symbol for 15 minutes
(range states don't change rapidly — re-check is overkill in a fast loop).

Feature flag: OPENING_RANGE_ENABLED (default True).
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED           = os.getenv("OPENING_RANGE_ENABLED", "true").lower() != "false"
CACHE_TTL_SECONDS = 900   # 15 minutes — range state is stable

# NSE session start
_SESSION_OPEN = dtime(9, 15)
# Opening range formed after first 30 minutes
_ORB_BARS_5M  = 6    # 6 × 5-minute bars = 30 minutes
_ORB_BARS_15M = 2    # 2 × 15-minute bars = 30 minutes

# Gap threshold: gap > this % of previous close → classified as gap open
GAP_THRESHOLD_PCT = 0.3


class ORBState(str):
    OPENING_DRIVE  = "OPENING_DRIVE"
    OPEN_REJECTION = "OPEN_REJECTION"
    GAP_FILL       = "GAP_FILL"
    ORB_BREAKOUT   = "ORB_BREAKOUT"
    INSIDE_RANGE   = "INSIDE_RANGE"
    TOO_EARLY      = "TOO_EARLY"        # opening range not yet formed
    UNKNOWN        = "UNKNOWN"


@dataclass
class OpeningRangeResult:
    state:          str   = ORBState.UNKNOWN
    orb_high:       float = 0.0    # opening range high (first 30 min)
    orb_low:        float = 0.0    # opening range low
    orb_width_pct:  float = 0.0    # (high - low) / low * 100
    gap_pct:        float = 0.0    # gap vs previous close (signed %)
    breakout_dir:   str   = ""     # "UP" / "DOWN" / "" if no breakout
    confidence:     float = 0.5
    notes:          str   = ""
    timestamp:      datetime = field(default_factory=lambda: datetime.now(tz=IST))

    def to_dict(self) -> dict:
        return {
            "state":         self.state,
            "orb_high":      round(self.orb_high, 2),
            "orb_low":       round(self.orb_low, 2),
            "orb_width_pct": round(self.orb_width_pct, 2),
            "gap_pct":       round(self.gap_pct, 2),
            "breakout_dir":  self.breakout_dir,
            "confidence":    round(self.confidence, 2),
            "notes":         self.notes,
        }


class OpeningRangeEngine:
    """
    Detects opening range breakout state for a symbol.

    Usage:
        from analysis.opening_range import opening_range_engine

        result = opening_range_engine.classify("NSE:NIFTY50-INDEX", df_5m)
        if result.state == "ORB_BREAKOUT" and result.breakout_dir == "UP":
            # long bias for the session
    """

    def __init__(self):
        self._cache: dict[str, OpeningRangeResult] = {}
        self._lock  = threading.Lock()

    def classify(
        self,
        symbol:        str,
        df_5m,                          # pd.DataFrame with 5-minute OHLCV
        force_refresh: bool = False,
    ) -> OpeningRangeResult:
        if not ENABLED:
            return OpeningRangeResult(state=ORBState.UNKNOWN, notes="opening_range disabled")

        with self._lock:
            cached = self._cache.get(symbol)
            if (not force_refresh and cached and
                    (datetime.now(tz=IST) - cached.timestamp).total_seconds() < CACHE_TTL_SECONDS):
                return cached

        try:
            result = self._classify(df_5m)
            result.timestamp = datetime.now(tz=IST)
            with self._lock:
                self._cache[symbol] = result
            logger.debug(
                f"[ORB] {symbol} → {result.state} "
                f"ORB [{result.orb_low:.2f}–{result.orb_high:.2f}] "
                f"gap={result.gap_pct:+.2f}%"
            )
            return result
        except Exception as e:
            logger.debug(f"[ORB] {symbol} classify failed: {e}")
            return OpeningRangeResult(state=ORBState.UNKNOWN, notes=str(e))

    def get_cached(self, symbol: str) -> Optional[OpeningRangeResult]:
        with self._lock:
            return self._cache.get(symbol)

    # ── Classification logic ─────────────────────────────────────

    def _classify(self, df) -> OpeningRangeResult:
        if df is None or len(df) < 3:
            return OpeningRangeResult(state=ORBState.UNKNOWN, notes="insufficient data")

        # Sort by timestamp to get today's bars in order
        if "timestamp" in df.columns:
            df = df.sort_values("timestamp").reset_index(drop=True)

        # Today's bars only (filter to today's date)
        today = date.today()
        if "timestamp" in df.columns:
            try:
                mask = df["timestamp"].dt.date == today
                today_df = df[mask].copy().reset_index(drop=True)
            except Exception:
                today_df = df.copy()
        else:
            today_df = df.copy()

        if len(today_df) < 2:
            return OpeningRangeResult(state=ORBState.TOO_EARLY, notes="not enough today's bars yet")

        # Opening range = first 6 bars (30 min) of today's session
        orb_bars = today_df.iloc[:min(_ORB_BARS_5M, len(today_df))]
        orb_high = orb_bars["high"].max()
        orb_low  = orb_bars["low"].min()
        orb_width_pct = (orb_high - orb_low) / orb_low * 100 if orb_low > 0 else 0

        # Current bar
        current_close = today_df["close"].iloc[-1]
        current_high  = today_df["high"].iloc[-1]
        current_low   = today_df["low"].iloc[-1]

        # Opening gap vs yesterday's close
        # Use last bar of previous session as prev_close (first bar of today as open)
        open_price = today_df["open"].iloc[0] if len(today_df) > 0 else 0
        # Use day-before data (last bar in df before today)
        if "timestamp" in df.columns:
            prev_df = df[df["timestamp"].dt.date < today]
        else:
            prev_df = df.iloc[:-len(today_df)] if len(today_df) < len(df) else df.iloc[:0]
        prev_close = prev_df["close"].iloc[-1] if len(prev_df) > 0 else open_price
        gap_pct = (open_price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        result = OpeningRangeResult(
            orb_high      = round(orb_high, 2),
            orb_low       = round(orb_low, 2),
            orb_width_pct = round(orb_width_pct, 2),
            gap_pct       = round(gap_pct, 2),
        )

        # Not enough bars to form the opening range yet
        if len(today_df) < _ORB_BARS_5M:
            result.state  = ORBState.TOO_EARLY
            result.notes  = f"forming ORB — {len(today_df)}/{_ORB_BARS_5M} bars so far"
            return result

        # ── ORB Breakout ──────────────────────────────────────────
        if current_close > orb_high * 1.001:
            result.state        = ORBState.ORB_BREAKOUT
            result.breakout_dir = "UP"
            result.confidence   = min(0.90, 0.60 + (current_close - orb_high) / orb_high * 20)
            result.notes        = f"above ORB high {orb_high:.2f} → bullish"
            return result

        if current_close < orb_low * 0.999:
            result.state        = ORBState.ORB_BREAKOUT
            result.breakout_dir = "DOWN"
            result.confidence   = min(0.90, 0.60 + (orb_low - current_close) / orb_low * 20)
            result.notes        = f"below ORB low {orb_low:.2f} → bearish"
            return result

        # ── Gap Fill ──────────────────────────────────────────────
        # Gap > threshold, price now moving back toward prev_close
        if abs(gap_pct) > GAP_THRESHOLD_PCT:
            # Is price moving to fill the gap?
            if gap_pct > 0 and current_close < open_price:   # gap up, filling down
                result.state = ORBState.GAP_FILL
                result.notes = f"gap-up {gap_pct:+.2f}% filling"
                result.confidence = 0.65
                return result
            if gap_pct < 0 and current_close > open_price:   # gap down, filling up
                result.state = ORBState.GAP_FILL
                result.notes = f"gap-down {gap_pct:+.2f}% filling"
                result.confidence = 0.65
                return result

        # ── Opening Drive ─────────────────────────────────────────
        # First bar has large directional move with volume expansion
        first_bar_range = orb_bars["high"].iloc[0] - orb_bars["low"].iloc[0]
        typical_range   = today_df["high"].sub(today_df["low"]).mean()
        if first_bar_range > typical_range * 1.5 and abs(gap_pct) < GAP_THRESHOLD_PCT:
            result.state     = ORBState.OPENING_DRIVE
            result.confidence = 0.65
            result.notes     = f"first-bar range {first_bar_range:.2f} vs avg {typical_range:.2f}"
            return result

        # ── Open Rejection ────────────────────────────────────────
        # Price opened at extreme (high/low of ORB) and reversed
        first_close = today_df["close"].iloc[0] if len(today_df) > 0 else open_price
        if open_price >= orb_high * 0.995 and first_close < orb_high * 0.99:
            result.state     = ORBState.OPEN_REJECTION
            result.notes     = f"opened at ORB high {orb_high:.2f} and rejected"
            result.confidence = 0.65
            return result
        if open_price <= orb_low * 1.005 and first_close > orb_low * 1.01:
            result.state     = ORBState.OPEN_REJECTION
            result.notes     = f"opened at ORB low {orb_low:.2f} and rejected"
            result.confidence = 0.65
            return result

        # ── Inside Range (default) ────────────────────────────────
        result.state     = ORBState.INSIDE_RANGE
        result.confidence = 0.55
        result.notes     = f"contained in ORB [{orb_low:.2f}–{orb_high:.2f}]"
        return result


# Singleton
opening_range_engine = OpeningRangeEngine()
