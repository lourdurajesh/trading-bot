"""
options_income.py
─────────────────
Options premium selling strategy.
Active when IV rank > 50 and regime = RANGING.
Strategy: Short Strangle (sell OTM call + OTM put).
"""

import logging
from typing import Optional

from analysis.indicators import atr
from analysis.options_engine import options_engine
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

MIN_IV_RANK   = 50     # minimum IV rank to sell premium
MIN_DTE       = 20     # minimum days to expiry
MAX_DTE       = 45     # maximum days to expiry
SD_WINGS      = 1.0    # sell strikes 1 SD OTM
PROFIT_TARGET = 0.50   # close at 50% of max credit


class OptionsIncomeStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.name      = "OptionsIncome"
        self.timeframe = "1D"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        # Only trade Nifty and BankNifty for now
        if symbol not in ("NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"):
            return None

        # Regime must be RANGING
        regime = regime_detector.get_regime(symbol, "1D")
        if regime.regime != Regime.RANGING:
            self.log_skip(symbol, f"Regime {regime.regime.value} not suitable for premium selling")
            return None

        # IV rank check
        iv_rank = options_engine.get_iv_rank(symbol)
        if iv_rank < MIN_IV_RANK:
            self.log_skip(symbol, f"IV rank {iv_rank:.0f} below minimum {MIN_IV_RANK}")
            return None

        spot = self.get_ltp(symbol)
        if not spot:
            return None

        df = self.get_ohlcv(symbol, "1D")
        if df is None:
            return None

        atr_val = atr(df).iloc[-1]
        iv      = 0.15   # default IV estimate — replace with live chain data in production

        # Calculate strangle strikes (1 SD OTM each side)
        call_strike = options_engine.get_otm_strike(spot, 'call', SD_WINGS, iv, 30)
        put_strike  = options_engine.get_otm_strike(spot, 'put',  SD_WINGS, iv, 30)

        # Estimate credit (simplified — use actual chain prices in production)
        estimated_credit = spot * iv * 0.02   # rough estimate
        stop_loss_credit = estimated_credit * 2   # stop at 2× credit received

        confidence = min(0.5 + (iv_rank - MIN_IV_RANK) / 100, 0.85)
        if confidence < MIN_SIGNAL_CONFIDENCE:
            return None

        reason = (
            f"IV Rank {iv_rank:.0f} | Ranging market | "
            f"Strangle: {put_strike:.0f}P / {call_strike:.0f}C"
        )

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = Direction.SHORT,
            signal_type = SignalType.OPTIONS,
            entry       = estimated_credit,
            stop_loss   = stop_loss_credit,
            target_1    = estimated_credit * PROFIT_TARGET,
            confidence  = round(confidence, 2),
            timeframe   = self.timeframe,
            regime      = regime.regime.value,
            reason      = reason,
            options_meta = {
                "strategy":     "short_strangle",
                "call_strike":  call_strike,
                "put_strike":   put_strike,
                "iv_rank":      iv_rank,
                "profit_target_pct": PROFIT_TARGET,
            }
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal
