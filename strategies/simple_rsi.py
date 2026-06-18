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

from analysis.indicators import atr, rsi, bollinger_bands, ema, relative_volume
from data.data_store import store
from strategies.base_strategy import Signal, Direction, SignalType

IST             = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN    = dtime(10, 0)    # avoid opening candle gap risk (data: -196pt slippage on 9:45 entry)
_MARKET_CLOSE   = dtime(15, 15)   # exit 15 min before close

RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
ATR_STOP_MULT   = 1.5
TARGET_R_1      = 1.0   # T1 — partial booking trigger (50% exits here)
TARGET_R_2      = 2.0   # T2 — full exit; let remaining 50% run to 2R
MIN_RVOL        = 1.2   # minimum relative volume — filters low-conviction signals
TIMEFRAME       = "15m"
MIN_BARS        = 30

logger = logging.getLogger(__name__)


class SimpleRSIStrategy:
    """
    Paper-only learning strategy. Returns a Signal (EQUITY) or None.
    """

    name      = "SimpleRSI"
    hold_type = "intraday"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        if "-INDEX" in symbol:
            return None  # indices need options strategies, not equity RSI

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
        rvol_val  = relative_volume(df).iloc[-1]
        upper, middle, lower = bollinger_bands(close)
        ema21_val = ema(close, 21).iloc[-1]
        ema50_val = ema(close, 50).iloc[-1]

        # Low-volume signals have poor follow-through — skip quiet bars
        if rvol_val < MIN_RVOL:
            return None

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

        # Price above BB upper in an uptrend = momentum, not reversal (data: 5/6 such shorts lost)
        bb_pos = upper.iloc[-1], lower.iloc[-1]
        if direction == "SHORT" and ltp > bb_pos[0] and ema21_val > ema50_val:
            return None

        if direction == "LONG":
            stop     = ltp - ATR_STOP_MULT * atr_val
            target_1 = ltp + TARGET_R_1 * (ltp - stop)
            target_2 = ltp + TARGET_R_2 * (ltp - stop)
        else:
            stop     = ltp + ATR_STOP_MULT * atr_val
            target_1 = ltp - TARGET_R_1 * (stop - ltp)
            target_2 = ltp - TARGET_R_2 * (stop - ltp)

        risk = abs(ltp - stop)
        if risk <= 0:
            return None

        rr = abs(target_2 - ltp) / risk   # overall 2R potential

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = Direction(direction),
            signal_type = SignalType.EQUITY,
            entry       = round(ltp, 2),
            stop_loss   = round(stop, 2),
            target_1    = round(target_1, 2),   # T1 — partial booking trigger
            target_2    = round(target_2, 2),   # T2 — final exit
            risk_reward = round(rr, 2),         # overall 2R potential (vs T2, not T1)
            timeframe   = TIMEFRAME,
            reason      = f"RSI {rsi_val:.0f} {'oversold' if direction == 'LONG' else 'overbought'} reversal",
            meta = {
                "rsi":           round(rsi_val, 1),
                "rvol":          round(rvol_val, 2),
                "atr":           round(atr_val, 2),
                "entry_atr":     round(atr_val, 2),   # for volatility exit comparison
                "risk_pts":      round(risk, 2),       # for partial booking R calc
                "original_stop": round(stop, 2),       # for trail-stop R reporting
                "bb_upper":      round(upper.iloc[-1], 2),
                "bb_lower":      round(lower.iloc[-1], 2),
                "bb_middle":     round(middle.iloc[-1], 2),
                "ema21":         round(ema21_val, 2),
                "ema50":         round(ema50_val, 2),
                "price_vs_bb":   "above_upper" if ltp > upper.iloc[-1] else
                                 "below_lower" if ltp < lower.iloc[-1] else "inside",
                "timeframe":     TIMEFRAME,
                "ts":            datetime.now(tz=IST).isoformat(),
            },
        )
        logger.info(
            f"[SimpleRSI] PAPER {direction} {symbol} | "
            f"Entry {ltp:.2f} SL {stop:.2f} T1 {target_1:.2f} T2 {target_2:.2f} | RSI {rsi_val:.0f}"
        )
        return signal
