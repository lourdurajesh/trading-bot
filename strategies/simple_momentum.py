"""
simple_momentum.py
──────────────────
Learning strategy #2 — Simple EMA Crossover Momentum.

Rules (intentionally straightforward):
  LONG  entry: EMA(9) crosses above EMA(21) on 1H AND RSI > 50
  SHORT entry: EMA(9) crosses below EMA(21) on 1H AND RSI < 50

  Stop:   1.5× ATR
  Target: 3× risk (3R — trend trades need room)

Works on trending stocks. No regime pre-filter. Paper-only.
Logs full metadata for learning review.
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from analysis.indicators import atr, ema, rsi, adx, relative_volume
from data.data_store import store

IST           = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN  = dtime(9, 45)
_MARKET_CLOSE = dtime(15, 15)

TIMEFRAME   = "1H"
MIN_BARS    = 30
ATR_MULT    = 1.5
TARGET_R    = 3.0

logger = logging.getLogger(__name__)


class SimpleMomentumStrategy:
    """
    Paper-only EMA crossover strategy. Returns LearningSignal dict or None.
    """

    name = "SimpleMomentum"

    def evaluate(self, symbol: str) -> Optional[dict]:
        now = datetime.now(tz=IST).time()
        if not (_MARKET_OPEN <= now <= _MARKET_CLOSE):
            return None

        df = store.get_ohlcv(symbol, TIMEFRAME, n=100)
        if df is None or len(df) < MIN_BARS:
            return None

        ltp = store.get_ltp(symbol)
        if not ltp or ltp <= 0:
            return None

        close     = df["close"]
        ema9      = ema(close, 9)
        ema21     = ema(close, 21)
        ema50_val = ema(close, 50).iloc[-1]
        rsi_val   = rsi(close).iloc[-1]
        atr_val   = atr(df).iloc[-1]
        rvol_val  = relative_volume(df).iloc[-1]

        adx_series, plus_di, minus_di = adx(df)
        adx_val = adx_series.iloc[-1]

        # Detect crossover: current bar crosses, prior bar was opposite
        curr_diff = ema9.iloc[-1]  - ema21.iloc[-1]
        prev_diff = ema9.iloc[-2]  - ema21.iloc[-2]

        crossed_up   = prev_diff <= 0 and curr_diff > 0
        crossed_down = prev_diff >= 0 and curr_diff < 0

        if crossed_up and rsi_val > 50:
            direction = "LONG"
        elif crossed_down and rsi_val < 50:
            direction = "SHORT"
        else:
            return None

        if direction == "LONG":
            stop   = ltp - ATR_MULT * atr_val
            target = ltp + TARGET_R * (ltp - stop)
        else:
            stop   = ltp + ATR_MULT * atr_val
            target = ltp - TARGET_R * (stop - ltp)

        risk = abs(ltp - stop)
        if risk <= 0:
            return None

        rr = abs(target - ltp) / risk

        signal = {
            "strategy":    self.name,
            "symbol":      symbol,
            "direction":   direction,
            "entry_price": round(ltp, 2),
            "stop_loss":   round(stop, 2),
            "target":      round(target, 2),
            "rr":          round(rr, 2),
            "metadata": {
                "ema9":        round(ema9.iloc[-1], 2),
                "ema21":       round(ema21.iloc[-1], 2),
                "ema50":       round(ema50_val, 2),
                "rsi":         round(rsi_val, 1),
                "adx":         round(adx_val, 1),
                "plus_di":     round(plus_di.iloc[-1], 1),
                "minus_di":    round(minus_di.iloc[-1], 1),
                "rvol":        round(rvol_val, 2),
                "atr":         round(atr_val, 2),
                "crossover":   "golden" if direction == "LONG" else "death",
                "timeframe":   TIMEFRAME,
                "ts":          datetime.now(tz=IST).isoformat(),
            },
        }
        logger.info(
            f"[SimpleMomentum] PAPER {direction} {symbol} | "
            f"Entry {ltp:.2f} SL {stop:.2f} T {target:.2f} | "
            f"EMA9 {'>' if direction == 'LONG' else '<'} EMA21 | ADX {adx_val:.0f}"
        )
        return signal
