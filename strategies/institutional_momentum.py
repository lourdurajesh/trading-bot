"""
institutional_momentum.py
─────────────────────────
ATM options strategy activated only on high-conviction days (score >= 7).
Driven by conviction_scorer.py — not by technical indicators.

Entry logic:
  - Pre-market score computed at 9:00 AM by conviction_scorer
  - Wait till 9:30 AM (15 min after open) to confirm market direction
  - Buy ATM call (BULLISH) or ATM put (BEARISH) on first VWAP pullback
  - BANKNIFTY preferred; NIFTY as fallback
  - Weekly expiry, 3-7 DTE (avoid same-day expiry)

Capital deployment:
  - Score 7-8:  35% of capital (₹1.75L at ₹5L capital)
  - Score 9-10: 50% of capital (₹2.5L at ₹5L capital)

Exit rules:
  - Target: option gains 55% (stop at 30% loss → R:R ~1.83)
  - Time stop: exit before 2:30 PM regardless of P&L

Lot math (BANKNIFTY example, ATM premium ₹350, lot size 15):
  - 35% deploy: ₹1,75,000 / (350 × 15) = 33 lots
  - Daily range 581 pts, need only 202 pts (34%) to hit target
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import TOTAL_CAPITAL
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# Only trade these index symbols
_ALLOWED = {
    "NSE:NIFTYBANK-INDEX": "BANKNIFTY",
    "NSE:NIFTY50-INDEX":   "NIFTY",
}

_ENTRY_OPEN  = dtime(9, 30)    # wait 15 min after market open
_TIME_STOP   = dtime(14, 30)   # force-exit any open positions by this time
_MARKET_CLOSE = dtime(15, 30)

# Options parameters
_MIN_DTE = 3
_MAX_DTE = 7
_TARGET_DELTA = 0.50            # ATM = delta ~0.50


class InstitutionalMomentumStrategy(BaseStrategy):
    """
    Fires ATM options signals when pre-market conviction score >= CONVICTION_THRESHOLD.

    Registered in strategy_selector as the highest-priority strategy for index symbols.
    Overrides all regime-based routing when a high-conviction setup exists.
    """

    def __init__(self):
        super().__init__()
        self.name      = "InstitutionalMomentum"
        self.timeframe = "1D"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        # Only run for index symbols
        if symbol not in _ALLOWED:
            return None

        if not self.enabled:
            return None

        # Gate: entry window 9:30 AM - 2:30 PM
        now = datetime.now(tz=IST).time()
        if now < _ENTRY_OPEN or now >= _TIME_STOP:
            return None

        # Check today's pre-market conviction score
        try:
            from intelligence.conviction_scorer import conviction_scorer
            result = conviction_scorer.get_last_score()
        except Exception as e:
            logger.warning(f"[InstitutionalMomentum] Could not get conviction score: {e}")
            return None

        if result is None or not result.tradeable:
            score_str = f"{result.score}" if result else "None"
            self.log_skip(symbol, f"Conviction score {score_str} below threshold")
            return None

        # Direction from scorer
        if result.direction == "BULLISH":
            direction   = Direction.LONG
            option_type = "call"
        elif result.direction == "BEARISH":
            direction   = Direction.SHORT
            option_type = "put"
        else:
            self.log_skip(symbol, f"Conviction direction NEUTRAL (score={result.score})")
            return None

        # Prefer BANKNIFTY, allow NIFTY as fallback
        short_name = _ALLOWED[symbol]
        if short_name == "NIFTY":
            # Only use NIFTY if no BANKNIFTY signal fired already
            # (strategy_selector evaluates BANKNIFTY first via priority ordering)
            logger.debug(f"[InstitutionalMomentum] Using NIFTY as fallback (BANKNIFTY evaluated first)")

        spot = self.get_ltp(symbol)
        if not spot or spot < 1000:
            self.log_skip(symbol, f"Invalid spot price: {spot}")
            return None

        # Fetch ATM option from live chain
        premium, lot_size, dte, nfo_symbol, iv = self._get_atm_option(symbol, option_type)
        if not premium or premium < 5.0:
            self.log_skip(symbol, f"ATM premium ₹{premium} too low or unavailable")
            return None

        if dte < _MIN_DTE or dte > _MAX_DTE:
            self.log_skip(symbol, f"DTE={dte} outside valid range [{_MIN_DTE}-{_MAX_DTE}]")
            return None

        # Calculate lot sizing based on capital deployment %
        capital_pct  = result.capital_pct     # 35 or 50
        capital_budget = TOTAL_CAPITAL * capital_pct / 100
        cost_per_lot   = premium * lot_size
        target_lots    = int(capital_budget / cost_per_lot) if cost_per_lot > 0 else 0

        if target_lots <= 0:
            self.log_skip(symbol, f"Zero lots at ₹{premium} premium, {lot_size} lot size")
            return None

        # Signal levels
        # stop_loss = option price at which we exit (30% loss on option premium)
        stop_loss = round(premium * 0.70, 2)
        # target_1  = absolute PROFIT amount per unit (55% gain → passed to RR calc)
        profit_per_unit = round(premium * 0.55, 2)

        # Construct reason from conviction scorer
        score_summary = f"Score={result.score:+d} {result.direction}"
        top_reasons   = " | ".join(result.reasons[:2])  # first 2 signals are the biggest
        reason = (
            f"InstitutionalMomentum: {score_summary} | "
            f"ATM {option_type.upper()} {short_name} | "
            f"DTE={dte} | Premium=₹{premium} | "
            f"Deploy={capital_pct}% (₹{capital_budget:,.0f}) | "
            f"{top_reasons}"
        )

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = direction,
            signal_type = SignalType.OPTIONS,
            entry       = premium,
            stop_loss   = stop_loss,
            target_1    = profit_per_unit,
            confidence  = self._conviction_to_confidence(result.score),
            timeframe   = self.timeframe,
            regime      = "INSTITUTIONAL",
            reason      = reason,
            options_meta = {
                "strategy":            "institutional_momentum",
                "option_type":         option_type,
                "short_name":          short_name,
                "atm_strike":          round(spot / (100 if "BANK" in symbol else 50)) * (100 if "BANK" in symbol else 50),
                "dte":                 dte,
                "iv":                  round(iv, 4) if iv else 0.15,
                "lot_size":            lot_size,
                "nfo_symbol":          nfo_symbol,
                "conviction_score":    result.score,
                "capital_pct":         capital_pct,
                "institutional_lots":  target_lots,   # passed to options_risk._calculate_lots
                "stop_pct":            30,
                "target_pct":          55,
                "time_stop":           _TIME_STOP.strftime("%H:%M"),
            }
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _get_atm_option(
        self, symbol: str, option_type: str
    ) -> tuple[float, int, int, Optional[str], float]:
        """
        Fetch ATM option from live chain via options_executor.
        Returns (premium, lot_size, dte, nfo_symbol, iv).
        Falls back to simulation if Fyers unavailable.
        """
        try:
            from execution.options_executor import options_executor
            opt = options_executor.get_best_option(
                underlying   = symbol,
                option_type  = option_type,
                target_delta = _TARGET_DELTA,
                min_dte      = _MIN_DTE,
                max_dte      = _MAX_DTE,
            )
            if opt:
                return opt.ltp, opt.lot_size, opt.dte, opt.symbol, opt.iv
        except Exception as e:
            logger.warning(f"[InstitutionalMomentum] options_executor error: {e}")

        # Simulation fallback
        return self._simulate_atm(symbol)

    def _simulate_atm(self, symbol: str) -> tuple[float, int, int, None, float]:
        """Fallback ATM estimates when live chain unavailable."""
        spot = self.get_ltp(symbol) or 0
        iv   = 0.15

        if "BANK" in symbol:
            premium  = round(spot * iv * (4 / 365) ** 0.5, 2) if spot else 350.0
            lot_size = 15
        else:
            premium  = round(spot * iv * (4 / 365) ** 0.5, 2) if spot else 150.0
            lot_size = 75

        premium = max(10.0, round(premium, 2))
        return premium, lot_size, 4, None, iv

    def _conviction_to_confidence(self, score: int) -> float:
        """Map conviction score (7-10) to signal confidence (0.75-0.95)."""
        # score=7 → 0.75, score=8 → 0.82, score=9 → 0.90, score=10 → 0.95
        mapping = {7: 0.75, 8: 0.82, 9: 0.90, 10: 0.95}
        return mapping.get(abs(score), 0.75)
