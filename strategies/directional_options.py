"""
directional_options.py
──────────────────────
Directional options strategy.
Active when strong trend signal + IV rank < 40 (cheap options).
Strategy: ATM debit call/put spread.
"""

import logging
from typing import Optional

from analysis.indicators import ema_alignment, rsi
from analysis.options_engine import options_engine
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

MAX_IV_RANK = 40    # buy options when IV is cheap
MIN_DTE     = 7
MAX_DTE     = 21


class DirectionalOptionsStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.name      = "DirectionalOptions"
        self.timeframe = "1H"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        # Only trade indices, not equity symbols
        if "-EQ" in symbol:
            return None
        
        # Also skip if price data looks wrong (Nifty should be > 10000)
        ltp = self.get_ltp(symbol)
        if not ltp or ltp < 1000:
            self.log_skip(symbol, f"Price ₹{ltp} looks incorrect for options")
            return None
        
        if not self.enabled:
            return None

        if symbol not in ("NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"):
            return None

        # Need TRENDING or BREAKOUT regime
        regime = regime_detector.get_regime(symbol, "1H")
        if regime.regime not in (Regime.TRENDING, Regime.BREAKOUT):
            self.log_skip(symbol, f"Regime {regime.regime.value} not suitable for directional options")
            return None

        # IV rank should be low — cheap options
        iv_rank = options_engine.get_iv_rank(symbol)
        if 0 <= iv_rank > MAX_IV_RANK:
            self.log_skip(symbol, f"IV rank {iv_rank:.0f} too high — options expensive")
            return None

        df = self.get_ohlcv(symbol, self.timeframe)
        if df is None:
            return None

        spot = self.get_ltp(symbol)
        if not spot:
            return None

        alignment = ema_alignment(df)
        rsi_val   = rsi(df["close"]).iloc[-1]

        # Determine direction
        if alignment["bullish"] and rsi_val > 50:
            direction    = Direction.LONG
            option_type  = "call"
            reason = f"Bullish EMA alignment | RSI {rsi_val:.0f} | Buy call debit spread"
        elif alignment["bearish"] and rsi_val < 50:
            direction    = Direction.SHORT
            option_type  = "put"
            reason = f"Bearish EMA alignment | RSI {rsi_val:.0f} | Buy put debit spread"
        else:
            self.log_skip(symbol, "No clear directional bias")
            return None

        iv = 0.15
        atm_strike  = round(spot / 50) * 50
        otm_strike  = options_engine.get_otm_strike(spot, option_type, 0.5, iv, 14)

        # Debit spread cost estimate
        debit_cost  = spot * iv * 0.015
        max_profit  = spot * iv * 0.025
        confidence  = min(regime.confidence * 0.9, 0.85)

        if confidence < MIN_SIGNAL_CONFIDENCE:
            return None

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = direction,
            signal_type = SignalType.OPTIONS,
            entry       = debit_cost,
            stop_loss   = debit_cost,      # max loss = premium paid
            target_1    = max_profit,
            confidence  = round(confidence, 2),
            timeframe   = self.timeframe,
            regime      = regime.regime.value,
            reason      = reason,
            options_meta = {
                "strategy":    "debit_spread",
                "option_type": option_type,
                "atm_strike":  atm_strike,
                "otm_strike":  otm_strike,
                "iv_rank":     iv_rank,
            }
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal
