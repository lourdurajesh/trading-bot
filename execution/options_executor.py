"""
options_executor.py
───────────────────
Phase 7E — Live NFO options chain integration.

Responsibilities:
  1. Fetch live options chain from Fyers for NIFTY / BANKNIFTY
  2. Select the right expiry (nearest weekly within DTE range)
  3. Select the best strike by target delta (not hardcoded strike math)
  4. Return the correct Fyers NFO symbol for order placement
  5. Update daily IV history in options_engine for IV rank
  6. Compute PCR (put-call ratio) for market sentiment

Lot sizes (NSE as of 2025):
  NIFTY:       75 lots
  BANKNIFTY:   35 lots
  FINNIFTY:    65 lots
  MIDCPNIFTY: 120 lots

Usage:
    result = options_executor.get_best_option(
        underlying = "NSE:NIFTY50-INDEX",
        option_type = "call",
        target_delta = 0.35,
        min_dte = 7,
        max_dte = 21,
    )
    if result:
        # result.symbol     → "NSE:NIFTY2526124500CE"
        # result.strike     → 24500.0
        # result.expiry     → "2025-01-26"
        # result.ltp        → 87.5   (live premium)
        # result.iv         → 0.156  (implied vol)
        # result.delta      → 0.34
        # result.lot_size   → 75
        # result.dte        → 14
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

# ── Lot sizes (NSE NFO as of Jan 2025) ───────────────────────────
LOT_SIZES: dict[str, int] = {
    "NIFTY":       75,
    "BANKNIFTY":   35,
    "FINNIFTY":    65,
    "MIDCPNIFTY": 120,
    "SENSEX":      20,
}

# Strike rounding step per underlying
STRIKE_STEPS: dict[str, int] = {
    "NIFTY":       50,
    "BANKNIFTY":  100,
    "FINNIFTY":    50,
    "MIDCPNIFTY":  25,
    "SENSEX":     100,
}

# Map index symbols → short name used in NFO
INDEX_SHORT: dict[str, str] = {
    "NSE:NIFTY50-INDEX":   "NIFTY",
    "NSE:NIFTYBANK-INDEX": "BANKNIFTY",
    "NSE:FINNIFTY-INDEX":  "FINNIFTY",
}

# Map equity symbols → short name used in NFO symbol construction
# TODO: verify lot sizes on each quarterly NSE rollover (typically Mar/Jun/Sep/Dec)
EQUITY_SHORT: dict[str, str] = {
    "NSE:RELIANCE-EQ":    "RELIANCE",
    "NSE:TCS-EQ":         "TCS",
    "NSE:HDFCBANK-EQ":    "HDFCBANK",
    "NSE:INFY-EQ":        "INFY",
    "NSE:ICICIBANK-EQ":   "ICICIBANK",
    "NSE:SBIN-EQ":        "SBIN",
    "NSE:AXISBANK-EQ":    "AXISBANK",
    "NSE:KOTAKBANK-EQ":   "KOTAKBANK",
    "NSE:BHARTIARTL-EQ":  "BHARTIARTL",
    "NSE:LT-EQ":          "LT",
    "NSE:WIPRO-EQ":       "WIPRO",
    "NSE:HCLTECH-EQ":     "HCLTECH",
    "NSE:BAJFINANCE-EQ":  "BAJFINANCE",
    "NSE:MARUTI-EQ":      "MARUTI",
}

# Equity options lot sizes (NSE NFO as of 2025)
EQUITY_LOT_SIZES: dict[str, int] = {
    "RELIANCE":    250,
    "TCS":         150,
    "HDFCBANK":    550,
    "INFY":        300,
    "ICICIBANK":   700,
    "SBIN":       1500,
    "AXISBANK":    625,
    "KOTAKBANK":   400,
    "BHARTIARTL":  950,
    "LT":          175,
    "WIPRO":       800,
    "HCLTECH":     350,
    "BAJFINANCE":  125,
    "MARUTI":       75,
}

# Strike step per equity (₹ increments)
EQUITY_STRIKE_STEPS: dict[str, int] = {
    "RELIANCE":   20,
    "TCS":        50,
    "HDFCBANK":   20,
    "INFY":       20,
    "ICICIBANK":  10,
    "SBIN":        5,
    "AXISBANK":   10,
    "KOTAKBANK":  20,
    "BHARTIARTL": 10,
    "LT":         20,
    "WIPRO":       5,
    "HCLTECH":    20,
    "BAJFINANCE": 50,
    "MARUTI":    100,
}


@dataclass
class OptionResult:
    """Fully resolved option for order placement."""
    symbol:     str       # Fyers NFO symbol, e.g. NSE:NIFTY2526124500CE
    underlying: str       # NSE:NIFTY50-INDEX
    option_type: str      # "call" or "put"
    strike:     float
    expiry:     str       # YYYY-MM-DD
    dte:        int       # calendar days to expiry
    ltp:        float     # live last traded price
    iv:         float     # implied volatility (annualised decimal)
    delta:      float
    lot_size:   int
    pcr:        float     # put-call ratio for this expiry (0 = unknown)


class OptionsExecutor:
    """
    Fetches live NFO options chain and selects the best contract.

    Works in two modes:
      Live (Fyers connected) → real chain data, real IVs, real deltas
      Simulation             → falls back to Black-Scholes estimates
    """

    def __init__(self):
        self._chain_cache:  dict[str, tuple[dict, datetime]] = {}  # symbol → (chain, fetched_at)
        self._cache_ttl_s   = 60   # refresh chain every 60 seconds

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def _resolve_underlying(self, underlying: str) -> tuple[Optional[str], int, int]:
        """
        Resolve any underlying symbol to (short_name, lot_size, strike_step).

        Supports NSE indices and NSE equity options.
        Returns (None, 0, 0) for unsupported symbols.

        Examples:
            "NSE:NIFTY50-INDEX"  → ("NIFTY",     75,   50)
            "NSE:RELIANCE-EQ"    → ("RELIANCE",  250,   20)
            "NSE:UNKNOWN-EQ"     → (None,          0,    0)
        """
        short = INDEX_SHORT.get(underlying)
        if short:
            return short, LOT_SIZES.get(short, 1), STRIKE_STEPS.get(short, 50)

        short = EQUITY_SHORT.get(underlying)
        if short:
            return short, EQUITY_LOT_SIZES.get(short, 1), EQUITY_STRIKE_STEPS.get(short, 10)

        return None, 0, 0

    def get_best_option(
        self,
        underlying:   str,
        option_type:  str,      # "call" or "put"
        target_delta: float,    # e.g. 0.35 for OTM, 0.50 for ATM
        min_dte:      int = 7,
        max_dte:      int = 21,
    ) -> Optional[OptionResult]:
        """
        Fetch live chain, select expiry + strike, return ready-to-trade OptionResult.
        Falls back to Black-Scholes simulation if chain unavailable.
        Supports both NSE indices and NSE equity underlyings.
        """
        short_name, lot_size, strike_step = self._resolve_underlying(underlying)
        if not short_name:
            logger.warning(f"[OptionsExecutor] Unsupported underlying: {underlying}")
            return None

        chain_data = self._get_chain(underlying)

        if chain_data:
            return self._select_from_chain(
                chain_data, underlying, short_name, lot_size,
                option_type, target_delta, min_dte, max_dte,
            )
        else:
            logger.info(f"[OptionsExecutor] Chain unavailable for {underlying} — using BS estimate")
            return self._simulate_option(
                underlying, short_name, lot_size, strike_step,
                option_type, target_delta, min_dte,
            )

    def get_lot_size(self, underlying: str) -> int:
        _, lot_size, _ = self._resolve_underlying(underlying)
        return lot_size or 1

    def get_strike_step(self, underlying: str) -> int:
        _, _, step = self._resolve_underlying(underlying)
        return step or 10

    def update_iv_history(self, underlying: str) -> None:
        """
        Call once daily (nightly agent) to update the IV rank history
        in options_engine with today's ATM IV from the live chain.
        Falls back to India VIX as an IV proxy when chain is unavailable.
        """
        from analysis.options_engine import options_engine

        chain_data = self._get_chain(underlying, force=True)
        if chain_data:
            try:
                expiries = chain_data.get("expiryData", [])
                if expiries:
                    row_list = expiries[0].get("optionsChain", [])
                    spot     = float(chain_data.get("underlyingValue", 0))
                    atm_iv   = self._get_atm_iv(row_list, spot)
                    if atm_iv and atm_iv > 0:
                        options_engine.update_iv_history(underlying, atm_iv)
                        logger.info(f"[OptionsExecutor] IV history updated: {underlying} ATM IV={atm_iv:.1%}")
                        return
            except Exception as e:
                logger.warning(f"[OptionsExecutor] IV history update failed: {e}")

        # Chain unavailable — use India VIX as IV proxy
        try:
            from intelligence.macro_data import macro_collector
            macro = macro_collector.get_snapshot()
            if macro.nifty_vix > 0:
                vix_iv = macro.nifty_vix / 100.0   # VIX 15.0 → IV 0.15
                options_engine.update_iv_history(underlying, vix_iv)
                logger.info(
                    f"[OptionsExecutor] IV history updated (VIX proxy): "
                    f"{underlying} VIX={macro.nifty_vix:.1f} → IV={vix_iv:.1%}"
                )
        except Exception as e:
            logger.debug(f"[OptionsExecutor] VIX proxy fallback failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # CHAIN FETCHING
    # ─────────────────────────────────────────────────────────────

    def _get_chain(self, underlying: str, force: bool = False) -> Optional[dict]:
        """Fetch options chain from Fyers, with 60-second cache."""
        now = datetime.now(tz=IST)

        if not force and underlying in self._chain_cache:
            cached, fetched_at = self._chain_cache[underlying]
            age = (now - fetched_at).total_seconds()
            if age < self._cache_ttl_s:
                return cached

        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised:
                return None

            resp = fyers_broker._client.optionchain(data={
                "symbol":      underlying,
                "strikecount": 15,   # 15 strikes each side of ATM
                "timestamp":   "",
            })

            if resp.get("s") != "ok":
                logger.debug(f"[OptionsExecutor] Chain fetch failed: {resp.get('message')}")
                return None

            chain_data = resp.get("data", {})
            self._chain_cache[underlying] = (chain_data, now)
            return chain_data

        except Exception as e:
            logger.debug(f"[OptionsExecutor] Chain fetch exception: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # STRIKE + EXPIRY SELECTION
    # ─────────────────────────────────────────────────────────────

    def _select_from_chain(
        self,
        chain_data:   dict,
        underlying:   str,
        short_name:   str,
        lot_size:     int,
        option_type:  str,
        target_delta: float,
        min_dte:      int,
        max_dte:      int,
    ) -> Optional[OptionResult]:
        """Pick best expiry and strike from live chain data."""
        try:
            spot      = float(chain_data.get("underlyingValue", 0))
            expiries  = chain_data.get("expiryData", [])

            if not spot or not expiries:
                return None

            # ── Step 1: pick expiry within DTE range ─────────────
            chosen_expiry = None
            chosen_rows   = None
            chosen_dte    = 0

            for exp_block in expiries:
                expiry_str = exp_block.get("expiry", "")
                dte        = self._days_to_expiry(expiry_str)
                if min_dte <= dte <= max_dte:
                    chosen_expiry = expiry_str
                    chosen_rows   = exp_block.get("optionsChain", [])
                    chosen_dte    = dte
                    break

            if not chosen_expiry or not chosen_rows:
                logger.debug(f"[OptionsExecutor] No expiry in {min_dte}-{max_dte} DTE range for {underlying}")
                return None

            # ── Step 2: pick strike nearest to target delta ───────
            best_row  = None
            best_diff = float("inf")

            for row in chosen_rows:
                if option_type == "call":
                    delta = float(row.get("call_delta", 0) or 0)
                    ltp   = float(row.get("call_ltp",   0) or 0)
                    iv    = float(row.get("call_iv",    0) or 0)
                else:
                    delta = abs(float(row.get("put_delta", 0) or 0))
                    ltp   = float(row.get("put_ltp",    0) or 0)
                    iv    = float(row.get("put_iv",     0) or 0)

                if ltp <= 0:
                    continue   # skip illiquid / zero-price strikes

                diff = abs(delta - abs(target_delta))
                if diff < best_diff:
                    best_diff = diff
                    best_row  = {
                        "strike": float(row.get("strikePrice", 0)),
                        "delta":  delta,
                        "ltp":    ltp,
                        "iv":     iv,
                        "call_oi": int(row.get("call_oi", 0) or 0),
                        "put_oi":  int(row.get("put_oi",  0) or 0),
                    }

            if not best_row or best_row["strike"] <= 0:
                return None

            # ── Step 3: compute PCR for the chosen expiry ─────────
            total_call_oi = sum(int(r.get("call_oi", 0) or 0) for r in chosen_rows)
            total_put_oi  = sum(int(r.get("put_oi",  0) or 0) for r in chosen_rows)
            pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0.0

            # ── Step 4: build Fyers NFO symbol ────────────────────
            fyers_symbol = self._build_nfo_symbol(
                short_name, chosen_expiry, best_row["strike"], option_type
            )
            if not fyers_symbol:
                return None

            result = OptionResult(
                symbol      = fyers_symbol,
                underlying  = underlying,
                option_type = option_type,
                strike      = best_row["strike"],
                expiry      = chosen_expiry,
                dte         = chosen_dte,
                ltp         = best_row["ltp"],
                iv          = best_row["iv"],
                delta       = best_row["delta"],
                lot_size    = lot_size,
                pcr         = pcr,
            )
            logger.info(
                f"[OptionsExecutor] Selected: {fyers_symbol} "
                f"LTP=₹{result.ltp:.2f} IV={result.iv:.1%} "
                f"Δ={result.delta:.2f} DTE={result.dte} PCR={pcr:.2f}"
            )
            return result

        except Exception as e:
            logger.error(f"[OptionsExecutor] Selection failed: {e}")
            return None

    def _simulate_option(
        self,
        underlying:   str,
        short_name:   str,
        lot_size:     int,
        strike_step:  int,
        option_type:  str,
        target_delta: float,
        min_dte:      int,
    ) -> Optional[OptionResult]:
        """
        Simulation fallback when live chain unavailable.
        Uses Black-Scholes to estimate premium and strike from spot + default IV.
        """
        try:
            from data.data_store import store
            from analysis.options_engine import options_engine

            spot = store.get_ltp(underlying)
            if not spot or spot <= 0:
                return None

            iv   = 0.15    # conservative default — real IV typically 12-25%
            dte  = min_dte + 7
            T    = dte / 365
            step = strike_step

            # Estimate strike from target delta using approximate inverse N(d1)
            import math
            from scipy.stats import norm
            r   = 0.065
            d1_target = norm.ppf(target_delta if option_type == "call" else 1 - target_delta)
            log_moneyness = d1_target * iv * math.sqrt(T) - (r + 0.5 * iv**2) * T
            strike = spot * math.exp(-log_moneyness)
            strike = round(strike / step) * step

            # BS price at that strike
            greeks = options_engine.black_scholes(spot, strike, T, 0.065, iv, option_type)
            delta  = greeks.delta if option_type == "call" else abs(greeks.delta)

            # Approximate expiry date
            from datetime import timedelta
            expiry_dt  = datetime.now(tz=IST) + timedelta(days=dte)
            expiry_str = expiry_dt.strftime("%Y-%m-%d")

            fyers_symbol = self._build_nfo_symbol(short_name, expiry_str, strike, option_type)
            if not fyers_symbol:
                return None

            return OptionResult(
                symbol      = fyers_symbol,
                underlying  = underlying,
                option_type = option_type,
                strike      = strike,
                expiry      = expiry_str,
                dte         = dte,
                ltp         = greeks.price,
                iv          = iv,
                delta       = delta,
                lot_size    = lot_size,
                pcr         = 0.0,
            )
        except Exception as e:
            logger.error(f"[OptionsExecutor] Simulation fallback failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # SYMBOL CONSTRUCTION
    # ─────────────────────────────────────────────────────────────

    def _build_nfo_symbol(
        self,
        short_name:  str,
        expiry_str:  str,    # YYYY-MM-DD
        strike:      float,
        option_type: str,    # "call" or "put"
    ) -> Optional[str]:
        """
        Build Fyers NFO symbol.

        Monthly format: NSE:NIFTY25JAN24500CE
        Weekly format:  NSE:NIFTY2501234500CE  (YY + MMDD)

        Fyers uses monthly format for monthly expiry, weekly for weekly.
        We detect by checking if expiry is the last Thursday of the month.
        """
        try:
            expiry_dt  = datetime.strptime(expiry_str, "%Y-%m-%d")
            suffix     = "CE" if option_type == "call" else "PE"
            strike_str = str(int(strike))
            yy         = expiry_dt.strftime("%y")       # "25"
            month_abbr = expiry_dt.strftime("%b").upper()  # "JAN"

            if self._is_monthly_expiry(expiry_dt):
                # Monthly: NSE:NIFTY25JAN24500CE
                return f"NSE:{short_name}{yy}{month_abbr}{strike_str}{suffix}"
            else:
                # Weekly: NSE:NIFTY2501234500CE  (YYMMDD)
                mmdd = expiry_dt.strftime("%m%d")  # "0123"
                return f"NSE:{short_name}{yy}{mmdd}{strike_str}{suffix}"

        except Exception as e:
            logger.error(f"[OptionsExecutor] Symbol build failed: {e}")
            return None

    @staticmethod
    def _is_monthly_expiry(dt: datetime) -> bool:
        """
        Returns True if the given date is the last Thursday of the month.
        NSE monthly options expire on the last Thursday of the expiry month.
        """
        import calendar
        # Find the last Thursday of dt's month
        year, month = dt.year, dt.month
        last_day    = calendar.monthrange(year, month)[1]
        # Walk backward from last day to find last Thursday (weekday=3)
        for day in range(last_day, last_day - 7, -1):
            if datetime(year, month, day).weekday() == 3:
                return dt.day == day
        return False

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _days_to_expiry(expiry_str: str) -> int:
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d").replace(tzinfo=IST)
            now    = datetime.now(tz=IST)
            return max(0, (expiry - now).days)
        except Exception:
            return 0

    @staticmethod
    def _get_atm_iv(rows: list[dict], spot: float) -> float:
        """Find the ATM strike and return its average call+put IV."""
        if not rows or not spot:
            return 0.0
        closest = min(rows, key=lambda r: abs(float(r.get("strikePrice", 0)) - spot))
        call_iv = float(closest.get("call_iv", 0) or 0)
        put_iv  = float(closest.get("put_iv",  0) or 0)
        if call_iv > 0 and put_iv > 0:
            return (call_iv + put_iv) / 2
        return call_iv or put_iv


# ── Module-level singleton ────────────────────────────────────────
options_executor = OptionsExecutor()
