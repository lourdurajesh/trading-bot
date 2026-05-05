"""
simple_rsi.py
─────────────
Learning strategy #1 — Simple RSI Reversal.

Rules (intentionally straightforward):
  LONG  entry: RSI(14) < 35  (oversold)
  SHORT entry: RSI(14) > 65  (overbought)

  Stop:   1.5× ATR below/above entry
  Target: 2× risk (2R)

No regime filter, no intelligence layer, no options — pure price-action
RSI on 15-minute bars. Logs rich metadata so you can review what the
market looked like at entry and learn from it.

Only fires in paper-trade learning mode — never touches live funds.
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from analysis.indicators import atr, rsi, bollinger_bands, ema
from data.data_store import store

IST             = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN    = dtime(9, 45)    # avoid opening 30-minute gap noise
_MARKET_CLOSE   = dtime(15, 15)   # exit 15 min before close

RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
ATR_STOP_MULT   = 1.5
TARGET_R        = 1.0
TIMEFRAME       = "15m"
MIN_BARS        = 30

logger = logging.getLogger(__name__)


class SimpleRSIStrategy:
    """
    Paper-only learning strategy. Returns a LearningSignal dict or None.
    """

    name = "SimpleRSI"

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
        rsi_val   = rsi(close).iloc[-1]
        atr_val   = atr(df).iloc[-1]
        upper, middle, lower = bollinger_bands(close)
        ema21_val = ema(close, 21).iloc[-1]
        ema50_val = ema(close, 50).iloc[-1]

        direction = None
        if rsi_val < RSI_OVERSOLD:
            direction = "LONG"
        elif rsi_val > RSI_OVERBOUGHT:
            direction = "SHORT"

        if not direction:
            return None

        # Trend filter: only trade with the EMA21/EMA50 trend
        if direction == "LONG" and ema21_val < ema50_val:
            return None
        if direction == "SHORT" and ema21_val > ema50_val:
            return None

        if direction == "LONG":
            stop   = ltp - ATR_STOP_MULT * atr_val
            target = ltp + TARGET_R * (ltp - stop)
        else:
            stop   = ltp + ATR_STOP_MULT * atr_val
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
                "rsi":          round(rsi_val, 1),
                "atr":          round(atr_val, 2),
                "bb_upper":     round(upper.iloc[-1], 2),
                "bb_lower":     round(lower.iloc[-1], 2),
                "bb_middle":    round(middle.iloc[-1], 2),
                "ema21":        round(ema21_val, 2),
                "ema50":        round(ema50_val, 2),
                "price_vs_bb":  "above_upper" if ltp > upper.iloc[-1] else
                                "below_lower" if ltp < lower.iloc[-1] else "inside",
                "timeframe":    TIMEFRAME,
                "ts":           datetime.now(tz=IST).isoformat(),
            },
        }
        logger.info(
            f"[SimpleRSI] PAPER {direction} {symbol} | "
            f"Entry {ltp:.2f} SL {stop:.2f} T {target:.2f} | RSI {rsi_val:.0f}"
        )
        return signal
