"""
directional_options.py
──────────────────────
Directional options strategy.
Active when strong trend signal + IV rank < 40 (cheap options).
Strategy: ATM debit call/put spread.
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from analysis.indicators import ema_alignment, rsi
from analysis.options_engine import options_engine
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

_IST          = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN  = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)

MAX_IV_RANK = 40    # buy options when IV is cheap
MIN_DTE     = 7
MAX_DTE     = 21


class DirectionalOptionsStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.name      = "DirectionalOptions"
        self.timeframe = "1H"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        now = datetime.now(tz=_IST).time()
        if not (_MARKET_OPEN <= now <= _MARKET_CLOSE):
            return None

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

        if symbol not in ("NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX"):
            return None

        # Accept TRENDING, BREAKOUT, or VOLATILE — strategy_selector already
        # filters regime; duplicating the check here blocked VOLATILE path.
        regime = regime_detector.get_regime(symbol, "1H")
        if regime.regime not in (Regime.TRENDING, Regime.BREAKOUT, Regime.VOLATILE):
            self.log_skip(symbol, f"Regime {regime.regime.value} not suitable for directional options")
            return None

        # IV rank should be low — cheap options
        iv_rank = options_engine.get_iv_rank(symbol)
        if iv_rank is None or iv_rank < 0:
            self.log_skip(symbol, "IV rank unavailable (insufficient history) — skipping to avoid overpaying for options")
            return None
        if iv_rank >= MAX_IV_RANK:
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

        # Use 3-EMA stack (9>21>50) for direction — the full 4-EMA stack
        # (including EMA200) blocks all signals during post-crash recovery
        # because EMA50 lags below EMA200 for weeks after a sharp reversal.
        bullish_3 = alignment["ema9"] > alignment["ema21"] > alignment["ema50"]
        bearish_3 = alignment["ema9"] < alignment["ema21"] < alignment["ema50"]

        # Determine direction
        if bullish_3 and rsi_val > 50:
            direction    = Direction.LONG
            option_type  = "call"
            reason = f"Bullish EMA alignment | RSI {rsi_val:.0f} | Buy call debit spread"
        elif bearish_3 and rsi_val < 50:
            direction    = Direction.SHORT
            option_type  = "put"
            reason = f"Bearish EMA alignment | RSI {rsi_val:.0f} | Buy put debit spread"
        else:
            self.log_skip(
                symbol,
                f"No clear directional bias: EMA9={alignment['ema9']:.0f} "
                f"EMA21={alignment['ema21']:.0f} EMA50={alignment['ema50']:.0f} RSI={rsi_val:.0f}"
            )
            return None

        # ── Fetch live option from chain ──────────────────────────
        # Target delta: ~0.40 for ATM-ish debit spread leg
        from execution.options_executor import options_executor
        opt = options_executor.get_best_option(
            underlying   = symbol,
            option_type  = option_type,
            target_delta = 0.40,
            min_dte      = MIN_DTE,
            max_dte      = MAX_DTE,
        )

        if opt:
            # Live data available — use real premium and IV
            iv         = opt.iv if opt.iv > 0 else 0.15
            atm_strike = opt.strike
            # OTM leg for the spread (0.5 delta difference)
            otm_strike = options_engine.get_otm_strike(spot, option_type, 0.5, iv, opt.dte)
            # ATM leg LTP is the debit; OTM leg credit reduces it ~30-40%
            # Net debit ≈ 65% of ATM premium (spread structure)
            debit_cost = round(opt.ltp * 0.65, 2)
            max_profit = round(abs(atm_strike - otm_strike) * 0.35, 2)
            lot_size   = opt.lot_size
            nfo_symbol = opt.symbol
        else:
            # Simulation fallback
            iv         = 0.15
            atm_strike = round(spot / 50) * 50
            otm_strike = options_engine.get_otm_strike(spot, option_type, 0.5, iv, 14)
            debit_cost = spot * iv * 0.015
            max_profit = spot * iv * 0.025
            lot_size   = options_executor.get_lot_size(symbol)
            nfo_symbol = None

        logger.debug(f"[DirectionalOptions] spot={spot}, iv={iv:.2f}, debit={debit_cost:.2f}")
        if debit_cost <= 0 or debit_cost > spot * 0.05:
            self.log_skip(symbol, f"Debit cost {debit_cost:.2f} invalid for spot {spot:.2f}")
            return None

        confidence  = min(regime.confidence * 0.9, 0.85)

        if confidence < MIN_SIGNAL_CONFIDENCE:
            return None

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = direction,
            signal_type = SignalType.OPTIONS,
            entry       = round(debit_cost, 2),
            stop_loss   = round(debit_cost * 0.5, 2),   # exit at 50% premium loss
            target_1    = round(max_profit, 2),
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
                "iv":          round(iv, 4),
                "lot_size":    lot_size,
                "nfo_symbol":  nfo_symbol,   # actual tradeable symbol if live
            }
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal
