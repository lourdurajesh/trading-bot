"""
iron_condor.py
──────────────
Iron Condor options strategy.

Structure (4 legs):
  - Sell OTM call  (short_call_delta ≈ 0.20)
  - Buy  OTM call  (short call strike + wing_width)   ← caps max loss
  - Sell OTM put   (short_put_delta  ≈ 0.20)
  - Buy  OTM put   (short put strike - wing_width)    ← caps max loss

Compared to a naked short strangle:
  - Max loss is defined (wing_width - net_credit per lot)
  - Lower margin requirement from broker
  - Slightly lower credit collected (long wings cost premium)

Entry conditions:
  - Regime: RANGING
  - IV rank: between min_iv_rank (40) and max_iv_rank (80)
    — not too cheap (need sellable premium), not extreme (gamma risk)
  - DTE: 21–45 days (theta decay starts accelerating)

Supports: NSE indices (NIFTY, BANKNIFTY, FINNIFTY) + liquid equities.

Exit (managed by position_manager):
  - Profit target: 50% of net credit (configurable)
  - Stop: 2× net credit received (configurable)
  - DTE force-exit: ≤3 DTE (configured in OPTIONS_DTE_FORCE_EXIT)
  - EOD exit: 3:15 PM IST (no overnight risk)
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from analysis.options_engine import options_engine
from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_SIGNAL_CONFIDENCE
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType
from strategies.options_strategy_config import get_options_config

logger = logging.getLogger(__name__)

_IST          = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN  = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)

# Supported underlyings — indices always included; equities added when IC_ALLOW_EQUITIES=true
_IC_INDEX_UNDERLYINGS = {
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
}

_IC_EQUITY_UNDERLYINGS = {
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:INFY-EQ",
    "NSE:ICICIBANK-EQ",
}


class IronCondorStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.name      = "IronCondor"
        self.timeframe = "1D"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        now = datetime.now(tz=_IST).time()
        if not (_MARKET_OPEN <= now <= _MARKET_CLOSE):
            return None

        cfg = get_options_config("iron_condor")

        if not cfg["enabled"]:
            return None

        # ── Symbol eligibility ────────────────────────────────────
        allowed = _IC_INDEX_UNDERLYINGS.copy()
        if cfg["allow_equities"]:
            allowed |= _IC_EQUITY_UNDERLYINGS

        if symbol not in allowed:
            return None

        # For equity symbols apply minimum price guard (cheap stocks = bad liquidity)
        if "-EQ" in symbol:
            spot_check = self.get_ltp(symbol)
            if not spot_check or spot_check < cfg["min_stock_price"]:
                self.log_skip(
                    symbol,
                    f"Stock price ₹{spot_check} below minimum ₹{cfg['min_stock_price']} — skipping iron condor"
                )
                return None

        # ── Regime: must be RANGING ───────────────────────────────
        regime = regime_detector.get_regime(symbol, "1D")
        if regime.regime != Regime.RANGING:
            self.log_skip(symbol, f"Regime {regime.regime.value} not suitable for iron condor")
            return None

        # ── IV rank: sweet spot 40–80 ─────────────────────────────
        iv_rank = options_engine.get_iv_rank(symbol)
        if iv_rank is None or iv_rank < 0:
            self.log_skip(symbol, "IV rank unavailable — skipping iron condor")
            return None
        if not (cfg["min_iv_rank"] <= iv_rank <= cfg["max_iv_rank"]):
            self.log_skip(
                symbol,
                f"IV rank {iv_rank:.0f} outside [{cfg['min_iv_rank']:.0f}, {cfg['max_iv_rank']:.0f}]"
            )
            return None

        spot = self.get_ltp(symbol)
        if not spot or spot <= 0:
            return None

        # ── Fetch short legs from live chain ─────────────────────
        from execution.options_executor import options_executor

        short_call = options_executor.get_best_option(
            underlying   = symbol,
            option_type  = "call",
            target_delta = cfg["short_call_delta"],
            min_dte      = cfg["min_dte"],
            max_dte      = cfg["max_dte"],
        )
        short_put = options_executor.get_best_option(
            underlying   = symbol,
            option_type  = "put",
            target_delta = cfg["short_put_delta"],
            min_dte      = cfg["min_dte"],
            max_dte      = cfg["max_dte"],
        )

        strike_step = options_executor.get_strike_step(symbol)
        lot_size    = options_executor.get_lot_size(symbol)

        # ── Calculate wing width in strike increments ─────────────
        # wing_width_pct × spot, then rounded to nearest strike step
        raw_wing    = spot * cfg["wing_width_pct"]
        wing_width  = max(round(raw_wing / strike_step) * strike_step, strike_step)

        # ── Compute net credit and max loss ───────────────────────
        if short_call and short_put:
            iv  = (short_call.iv + short_put.iv) / 2 if (short_call.iv and short_put.iv) else 0.18
            dte = short_call.dte
            T   = dte / 365

            long_call_strike = short_call.strike + wing_width
            long_put_strike  = short_put.strike  - wing_width

            # Estimate long leg costs via Black-Scholes (Fyers chain only returns short legs)
            lc_greeks = options_engine.black_scholes(spot, long_call_strike, T, 0.065, iv, "call")
            lp_greeks = options_engine.black_scholes(spot, long_put_strike,  T, 0.065, iv, "put")

            gross_credit = round(short_call.ltp + short_put.ltp, 2)
            debit_paid   = round(lc_greeks.price + lp_greeks.price, 2)
            net_credit   = round(gross_credit - debit_paid, 2)
            max_loss     = round(wing_width - net_credit, 2)
            expiry       = short_call.expiry

            short_call_strike = short_call.strike
            short_put_strike  = short_put.strike
            nfo_short_call    = short_call.symbol
            nfo_short_put     = short_put.symbol

            # Build NFO symbols for the long (protective) legs
            short_name, _, _ = options_executor._resolve_underlying(symbol)
            nfo_long_call = options_executor._build_nfo_symbol(
                short_name, expiry, long_call_strike, "call"
            ) if short_name else None
            nfo_long_put = options_executor._build_nfo_symbol(
                short_name, expiry, long_put_strike, "put"
            ) if short_name else None

        else:
            # Simulation fallback — no live chain available
            logger.info(f"[IronCondor] Live chain unavailable for {symbol} — using simulation")
            iv   = 0.18
            dte  = cfg["min_dte"] + 7
            T    = dte / 365

            # Estimate strikes at short_call_delta / short_put_delta using ATM ± 1σ approximation
            short_call_strike = options_engine.get_otm_strike(spot, "call", 1.0, iv, dte)
            short_put_strike  = options_engine.get_otm_strike(spot, "put",  1.0, iv, dte)
            long_call_strike  = short_call_strike + wing_width
            long_put_strike   = short_put_strike  - wing_width

            sc_g = options_engine.black_scholes(spot, short_call_strike, T, 0.065, iv, "call")
            sp_g = options_engine.black_scholes(spot, short_put_strike,  T, 0.065, iv, "put")
            lc_g = options_engine.black_scholes(spot, long_call_strike,  T, 0.065, iv, "call")
            lp_g = options_engine.black_scholes(spot, long_put_strike,   T, 0.065, iv, "put")

            net_credit = round((sc_g.price + sp_g.price) - (lc_g.price + lp_g.price), 2)
            max_loss   = round(wing_width - net_credit, 2)
            expiry     = "sim"
            nfo_short_call = None
            nfo_short_put  = None
            nfo_long_call  = None
            nfo_long_put   = None

        if net_credit <= 0:
            self.log_skip(symbol, f"Net credit ₹{net_credit:.2f} ≤ 0 — condor not viable")
            return None

        # ── Signal construction ───────────────────────────────────
        stop_credit   = round(net_credit * cfg["stop_mult"],    2)
        profit_target = round(net_credit * cfg["profit_target"], 2)

        # Confidence: scales with IV rank position within the allowed window
        window = cfg["max_iv_rank"] - cfg["min_iv_rank"]
        iv_score   = (iv_rank - cfg["min_iv_rank"]) / window if window > 0 else 0.5
        confidence = round(min(0.55 + iv_score * 0.30, 0.85), 2)

        if confidence < MIN_SIGNAL_CONFIDENCE:
            return None

        reason = (
            f"Iron Condor | IV rank {iv_rank:.0f} | Ranging | "
            f"Strikes: {long_put_strike:.0f}P / {short_put_strike:.0f}P / "
            f"{short_call_strike:.0f}C / {long_call_strike:.0f}C | "
            f"Net credit ₹{net_credit:.2f} | Max loss ₹{max_loss:.2f}"
        )

        signal = Signal(
            symbol      = symbol,
            strategy    = self.name,
            direction   = Direction.SHORT,
            signal_type = SignalType.OPTIONS,
            entry       = net_credit,
            stop_loss   = stop_credit,
            target_1    = profit_target,
            confidence  = confidence,
            timeframe   = self.timeframe,
            regime      = regime.regime.value,
            reason      = reason,
            options_meta = {
                "strategy":          "iron_condor",
                "short_call_strike": short_call_strike,
                "long_call_strike":  long_call_strike,
                "short_put_strike":  short_put_strike,
                "long_put_strike":   long_put_strike,
                "net_credit":        net_credit,
                "max_loss":          max_loss,
                "wing_width":        wing_width,
                "iv_rank":           iv_rank,
                "iv":                round(iv, 4),
                "lot_size":          lot_size,
                "dte":               dte,
                "expiry":            expiry,
                "nfo_short_call":    nfo_short_call,
                "nfo_long_call":     nfo_long_call,
                "nfo_short_put":     nfo_short_put,
                "nfo_long_put":      nfo_long_put,
                "profit_target_pct": cfg["profit_target"],
            }
        )
        signal.calculate_rr()
        self.log_signal(signal)
        return signal
