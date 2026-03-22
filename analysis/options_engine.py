"""
options_engine.py
─────────────────
Options chain analysis, Greeks calculation, IV rank.
Used by options_income.py and directional_options.py.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from scipy.stats import norm

logger = logging.getLogger(__name__)


@dataclass
class GreeksResult:
    delta:  float
    gamma:  float
    theta:  float
    vega:   float
    iv:     float
    price:  float


@dataclass
class OptionsChainRow:
    strike:      float
    expiry:      str
    call_ltp:    float
    put_ltp:     float
    call_oi:     int
    put_oi:      int
    call_iv:     float
    put_iv:      float
    call_delta:  float
    put_delta:   float


class OptionsEngine:
    """
    Calculates options Greeks and analyses options chains.

    Usage:
        engine = OptionsEngine()
        greeks = engine.black_scholes(S=100, K=100, T=0.1, r=0.065, sigma=0.2, option='call')
        chain  = engine.get_chain('NSE:NIFTY50-INDEX')
        iv_rank = engine.get_iv_rank('NSE:NIFTY50-INDEX')
    """

    def __init__(self):
        self._iv_history: dict[str, list[float]] = {}   # symbol → list of daily IVs
        self._fyers_client = None

    def initialise(self) -> None:
        """Connect Fyers REST for options chain fetching."""
        try:
            from fyers_apiv3 import fyersModel
            from config import settings
            if settings.FYERS_ACCESS_TOKEN:
                self._fyers_client = fyersModel.FyersModel(
                    client_id = settings.FYERS_APP_ID,
                    token     = settings.FYERS_ACCESS_TOKEN,
                    is_async  = False,
                )
        except Exception as e:
            logger.warning(f"[OptionsEngine] Could not initialise: {e}")

    # ─────────────────────────────────────────────────────────────
    # BLACK-SCHOLES GREEKS
    # ─────────────────────────────────────────────────────────────

    def black_scholes(
        self,
        S: float,      # current price
        K: float,      # strike price
        T: float,      # time to expiry in years
        r: float,      # risk-free rate (use 0.065 for India)
        sigma: float,  # implied volatility (annualised)
        option: str,   # 'call' or 'put'
    ) -> GreeksResult:
        """Calculate option price and Greeks using Black-Scholes."""
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return GreeksResult(0, 0, 0, 0, sigma, 0)

        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option == 'call':
            price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
            delta = norm.cdf(d1)
            theta = (
                -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
                - r * K * math.exp(-r * T) * norm.cdf(d2)
            ) / 365
        else:
            price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
            delta = norm.cdf(d1) - 1
            theta = (
                -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
                + r * K * math.exp(-r * T) * norm.cdf(-d2)
            ) / 365

        gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
        vega  = S * norm.pdf(d1) * math.sqrt(T) / 100

        return GreeksResult(
            delta = round(delta, 4),
            gamma = round(gamma, 6),
            theta = round(theta, 2),
            vega  = round(vega, 2),
            iv    = round(sigma, 4),
            price = round(price, 2),
        )

    def implied_volatility(
        self,
        market_price: float,
        S: float, K: float, T: float,
        r: float = 0.065,
        option: str = 'call',
    ) -> float:
        """
        Calculate implied volatility via Newton-Raphson iteration.
        Returns annualised IV as a decimal (e.g. 0.20 = 20%).
        """
        if market_price <= 0 or T <= 0:
            return 0.0

        sigma = 0.3   # initial guess
        for _ in range(100):
            bs = self.black_scholes(S, K, T, r, sigma, option)
            diff = bs.price - market_price
            if abs(diff) < 0.001:
                break
            vega = bs.vega * 100   # undo the /100 in vega calculation
            if vega < 1e-10:
                break
            sigma -= diff / vega
            sigma = max(0.001, min(sigma, 10.0))

        return round(sigma, 4)

    # ─────────────────────────────────────────────────────────────
    # IV RANK
    # ─────────────────────────────────────────────────────────────

    def get_iv_rank(self, symbol: str) -> float:
        """
        IV Rank = (current IV - 52w low IV) / (52w high IV - 52w low IV) × 100
        Returns 0-100. > 50 = elevated IV (good for selling premium).
        Returns -1 if insufficient history.
        """
        history = self._iv_history.get(symbol, [])
        if len(history) < 30:
            return -1.0

        current_iv = history[-1]
        iv_low     = min(history)
        iv_high    = max(history)

        if iv_high == iv_low:
            return 50.0

        return round((current_iv - iv_low) / (iv_high - iv_low) * 100, 1)

    def update_iv_history(self, symbol: str, iv: float) -> None:
        """Called daily to record current IV for rank calculation."""
        if symbol not in self._iv_history:
            self._iv_history[symbol] = []
        self._iv_history[symbol].append(iv)
        # Keep 252 trading days (1 year)
        if len(self._iv_history[symbol]) > 252:
            self._iv_history[symbol] = self._iv_history[symbol][-252:]

    # ─────────────────────────────────────────────────────────────
    # STRIKE SELECTION
    # ─────────────────────────────────────────────────────────────

    def get_otm_strike(
        self,
        spot: float,
        direction: str,      # 'call' or 'put'
        sd_multiple: float,  # standard deviations OTM
        iv: float,           # current IV
        dte: int,            # days to expiry
    ) -> float:
        """
        Calculate OTM strike at N standard deviations from spot.
        Used for Iron Condor wing selection.
        """
        T = dte / 365
        move = spot * iv * math.sqrt(T) * sd_multiple
        if direction == 'call':
            strike = spot + move
        else:
            strike = spot - move
        # Round to nearest 50 (Nifty) or 100
        step = 50 if spot > 10000 else 10
        return round(strike / step) * step

    def days_to_expiry(self, expiry_date: str) -> int:
        """Calculate calendar days to expiry from date string YYYY-MM-DD."""
        try:
            expiry = datetime.strptime(expiry_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            now    = datetime.now(tz=timezone.utc)
            return max(0, (expiry - now).days)
        except Exception:
            return 0

    # ─────────────────────────────────────────────────────────────
    # PCR ANALYSIS
    # ─────────────────────────────────────────────────────────────

    def put_call_ratio(self, chain_rows: list[OptionsChainRow]) -> float:
        """
        Put-Call Ratio by OI.
        < 0.7 = bullish, > 1.3 = bearish, 0.7-1.3 = neutral.
        """
        total_call_oi = sum(r.call_oi for r in chain_rows)
        total_put_oi  = sum(r.put_oi  for r in chain_rows)
        if total_call_oi == 0:
            return 0.0
        return round(total_put_oi / total_call_oi, 2)


# ── Module-level singleton ────────────────────────────────────────
options_engine = OptionsEngine()
