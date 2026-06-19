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

Lot sizes are loaded from config/nse_instruments.json at startup.
  Update that file when NSE revises lot sizes (quarterly: Mar/Jun/Sep/Dec).

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

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

# ── Lot sizes and strike steps loaded from config/nse_instruments.json ─────
# Edit that file (or use the dashboard config UI) — no code change needed.
_INSTRUMENTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "nse_instruments.json"
)

def _load_instruments() -> dict:
    try:
        with open(_INSTRUMENTS_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[OptionsExecutor] Could not load {_INSTRUMENTS_PATH}: {e} — using defaults")
        return {}

_inst = _load_instruments()

LOT_SIZES: dict[str, int]      = _inst.get("index_lot_sizes",   {"NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25, "MIDCPNIFTY": 75, "SENSEX": 20})
STRIKE_STEPS: dict[str, int]   = _inst.get("index_strike_steps",{"NIFTY": 50, "BANKNIFTY": 100,"FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100})
EQUITY_LOT_SIZES: dict[str,int]= _inst.get("equity_lot_sizes",  {})
EQUITY_STRIKE_STEPS: dict[str,int]=_inst.get("equity_strike_steps",{})
# Underlying index-point exits for intraday call-buying (config-driven).
INDEX_TRAIL_POINTS: dict[str,dict] = _inst.get("index_trail_points", {})

logger.info(
    f"[OptionsExecutor] Lot sizes loaded: "
    f"NIFTY={LOT_SIZES.get('NIFTY')} BANKNIFTY={LOT_SIZES.get('BANKNIFTY')} "
    f"FINNIFTY={LOT_SIZES.get('FINNIFTY')}"
)

# Map index symbols → short name used in NFO
INDEX_SHORT: dict[str, str] = {
    "NSE:NIFTY50-INDEX":   "NIFTY",
    "NSE:NIFTYBANK-INDEX": "BANKNIFTY",
    "NSE:FINNIFTY-INDEX":  "FINNIFTY",
}

# Map equity symbols → short name used in NFO symbol construction
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
    lot_size:      int
    pcr:           float  # put-call ratio for this expiry (0 = unknown)
    is_simulated:  bool = False  # True when price came from BS fallback, not live chain


class OptionsExecutor:
    """
    Fetches live NFO options chain and selects the best contract.

    Works in two modes:
      Live (Fyers connected) → real chain data, real IVs, real deltas
      Simulation             → falls back to Black-Scholes estimates
    """

    def __init__(self):
        # Chain fetch + 60s caching + flat-format parsing now live in
        # analysis/options_chain.chain_service (U1). Nothing to hold here.
        pass

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
            result = self._select_from_chain(
                chain_data, underlying, short_name, lot_size,
                option_type, target_delta, min_dte, max_dte,
            )
            if result:
                return result

            # No strikes in DTE range from the default (nearest) chain.
            # Fyers ships strikes for the NEAREST expiry only, but expiryData lists
            # the full expiry calendar. Pick the nearest expiry within the DTE window
            # and re-request the chain with its EXACT epoch (an arbitrary/computed
            # epoch is rejected: "Please provide valid expiry").
            from analysis.options_chain import chain_service
            picked = chain_service.pick_expiry_epoch(chain_data, min_dte, max_dte)
            if picked:
                target_epoch, target_date_str, target_dte = picked
                logger.info(
                    f"[OptionsExecutor] {underlying}: nearest expiry outside DTE "
                    f"{min_dte}-{max_dte} — re-requesting chain for {target_date_str} "
                    f"(DTE {target_dte}, epoch {target_epoch})"
                )
                chain_next = self._get_chain_for_timestamp(underlying, target_epoch)
                if chain_next:
                    result = self._select_from_chain(
                        chain_next, underlying, short_name, lot_size,
                        option_type, target_delta, min_dte, max_dte,
                    )
                    if result:
                        return result

            # Both chain fetches returned no match in the ideal DTE window.
            # Fall back to the original chain with no DTE floor so we get a
            # real market price (e.g. a 4-DTE expiry) rather than a BS estimate.
            if chain_data:
                result = self._select_from_chain(
                    chain_data, underlying, short_name, lot_size,
                    option_type, target_delta, min_dte=1, max_dte=max_dte,
                )
                if result:
                    logger.info(
                        f"[OptionsExecutor] {underlying}: using nearest available expiry "
                        f"(DTE {result.dte}, below preferred {min_dte}) — real price ₹{result.ltp:.2f}"
                    )
                    return result

        # Log chain shape at WARNING so we can diagnose what Fyers returned (flat layout).
        if chain_data:
            from analysis.options_chain import chain_service
            expiry_blocks = chain_data.get("expiryData", [])
            top_level     = chain_data.get("optionsChain", [])
            call_strikes  = chain_service.strikes(chain_data, "call")
            put_strikes   = chain_service.strikes(chain_data, "put")
            first_strike_keys = list(top_level[0].keys())[:6] if top_level else []
            logger.warning(
                f"[OptionsExecutor] {underlying}: chain fetched but no tradeable strike found. "
                f"expiry_calendar={len(expiry_blocks)} top_level_rows={len(top_level)} "
                f"call_strikes={len(call_strikes)} put_strikes={len(put_strikes)} "
                f"spot={chain_service.underlying_spot(chain_data)} first_strike_keys={first_strike_keys}"
            )
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

    def get_trail_points(self, underlying: str) -> Optional[tuple[float, float]]:
        """Config-driven (sl_pts, trail_pts) for an index's underlying-trailing exit,
        or None if not configured. See config/nse_instruments.json:index_trail_points."""
        short = INDEX_SHORT.get(underlying)
        cfg = INDEX_TRAIL_POINTS.get(short) if short else None
        if not cfg:
            return None
        try:
            return float(cfg["sl"]), float(cfg["trail"])
        except (KeyError, ValueError, TypeError):
            return None

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
                atm_iv = self._atm_iv_from_chain(chain_data)
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
        """
        Fetch the NSE option chain via the shared chain service (U1b-slice-2).

        The flat-format parser now lives entirely in chain_service — there is NO
        local normalisation here. The raw flat chain (top-level CE/PE rows +
        expiry calendar) is consumed directly by chain_service.strikes / leg_quote /
        underlying_spot / pick_expiry_epoch.
        """
        from analysis.options_chain import chain_service
        return chain_service.get_chain(underlying, strikecount=15, force=force)

    def _get_chain_for_timestamp(self, underlying: str, epoch: int) -> Optional[dict]:
        """Fetch the chain for a specific expiry epoch via the shared chain service."""
        from analysis.options_chain import chain_service
        return chain_service.get_chain(underlying, strikecount=15, timestamp=str(epoch))

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
        """
        Pick the best strike from a FLAT chain (one expiry's worth of strikes) via
        chain_service — the single chain parser. The chain carries strikes for exactly
        one expiry (the nearest, or the one re-requested by get_best_option for a
        specific epoch); this method derives that expiry's DTE from a strike symbol and
        only trades it when the DTE falls inside [min_dte, max_dte].
        """
        try:
            from analysis.options_chain import chain_service

            spot = float(chain_service.underlying_spot(chain_data) or 0)

            # Fyers sometimes omits the underlying spot (observed post-holiday
            # restarts / future-epoch fetches). Fall back to the WebSocket LTP.
            if not spot:
                try:
                    from data.data_store import store as _store
                    spot = float(_store.get_ltp(underlying) or 0)
                    if spot:
                        logger.debug(
                            f"[OptionsExecutor] underlyingValue null for {underlying} — "
                            f"using WebSocket LTP {spot:.2f} as spot fallback"
                        )
                except Exception:
                    pass

            if not spot:
                logger.warning(
                    f"[OptionsExecutor] {underlying}: spot=0 — underlyingValue absent "
                    f"from chain AND store.get_ltp() returned 0."
                )
                return None

            strikes = chain_service.strikes(chain_data, option_type)
            if not strikes:
                return None

            # ── Step 1: this chain's single expiry + DTE (from a real leg symbol) ──
            chosen_expiry = ""
            for strike in strikes:
                q = chain_service.leg_quote(chain_data, strike, option_type)
                if q and q.get("symbol"):
                    chosen_expiry = self._expiry_from_ex_symbol(q["symbol"])
                    if chosen_expiry:
                        break
            chosen_dte = self._days_to_expiry(chosen_expiry) if chosen_expiry else 0
            if not chosen_expiry or not (min_dte <= chosen_dte <= max_dte):
                logger.info(
                    f"[OptionsExecutor] {underlying}: chain expiry {chosen_expiry or '?'} "
                    f"(DTE {chosen_dte}) outside {min_dte}-{max_dte}. spot={spot:.0f}"
                )
                return None

            # ── Step 2: pick strike nearest to target delta (real quote required) ──
            best_strike = None
            best_quote  = None
            best_delta  = 0.0
            best_diff   = float("inf")

            for strike in strikes:
                q = chain_service.leg_quote(chain_data, strike, option_type)
                if not q or q["ltp"] <= 0:
                    continue   # skip illiquid / zero-price strikes
                delta = chain_service.synthetic_delta(strike, spot, option_type)
                diff  = abs(delta - abs(target_delta))
                if diff < best_diff:
                    best_diff   = diff
                    best_strike = strike
                    best_quote  = q
                    best_delta  = delta

            if best_strike is None:
                logger.warning(
                    f"[OptionsExecutor] {underlying}: {len(strikes)} {option_type} strikes "
                    f"(expiry={chosen_expiry} DTE={chosen_dte}) all have ltp=0 — no tradeable strike."
                )
                return None

            # ── Step 3: real PCR from live OI across both sides of this expiry ──
            total_call_oi = sum((chain_service.leg_quote(chain_data, s, "call") or {}).get("oi", 0)
                                for s in chain_service.strikes(chain_data, "call"))
            total_put_oi  = sum((chain_service.leg_quote(chain_data, s, "put") or {}).get("oi", 0)
                                for s in chain_service.strikes(chain_data, "put"))
            pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0.0

            # ── Step 4: prefer the chain's real NFO symbol; rebuild only if absent ──
            fyers_symbol = best_quote.get("symbol") or self._build_nfo_symbol(
                short_name, chosen_expiry, best_strike, option_type
            )
            if not fyers_symbol:
                return None

            result = OptionResult(
                symbol      = fyers_symbol,
                underlying  = underlying,
                option_type = option_type,
                strike      = best_strike,
                expiry      = chosen_expiry,
                dte         = chosen_dte,
                ltp         = best_quote["ltp"],
                iv          = best_quote.get("iv", 0.0) or 0.0,
                delta       = best_delta,
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

            # Approximate expiry date — snap to the next Thursday (NSE expiry day)
            from datetime import timedelta, date as _date
            today      = datetime.now(tz=IST).date()
            rough      = today + timedelta(days=dte)
            days_ahead = (3 - rough.weekday()) % 7   # 3 = Thursday
            expiry_date = rough + timedelta(days=days_ahead)
            # Guarantee it still satisfies min_dte after the snap
            if (expiry_date - today).days < min_dte:
                expiry_date += timedelta(days=7)
            expiry_dt  = datetime(expiry_date.year, expiry_date.month, expiry_date.day, tzinfo=IST)
            expiry_str = expiry_date.strftime("%Y-%m-%d")

            fyers_symbol = self._build_nfo_symbol(short_name, expiry_str, strike, option_type)
            if not fyers_symbol:
                return None

            return OptionResult(
                symbol        = fyers_symbol,
                underlying    = underlying,
                option_type   = option_type,
                strike        = strike,
                expiry        = expiry_str,
                dte           = dte,
                ltp           = greeks.price,
                iv            = iv,
                delta         = delta,
                lot_size      = lot_size,
                pcr           = 0.0,
                is_simulated  = True,
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
        Weekly format:  NSE:NIFTY2562524750CE  (YY + M + DD)

        Fyers weekly format uses single-digit month (1-9 for Jan-Sep,
        O/N/D for Oct/Nov/Dec) with 2-digit zero-padded day.
        e.g. June 25 → "6" + "25" → "625", not "0625".
        Using %m%d (zero-padded month) produces the WRONG symbol and
        no WS tick data is received because the key never matches.
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
                # Weekly: NSE:NIFTY2562524750CE  (YY + M + DD)
                # Month is single-digit (no leading zero); day is 2-digit zero-padded.
                m = expiry_dt.month
                month_code = str(m) if m <= 9 else ("O" if m == 10 else ("N" if m == 11 else "D"))
                dd = expiry_dt.strftime("%d")  # "25" or "05"
                return f"NSE:{short_name}{yy}{month_code}{dd}{strike_str}{suffix}"

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
    def _days_to_expiry(expiry_str) -> int:
        """
        Parse an expiry value from a Fyers chain response and return days to expiry.

        Fyers has returned expiry in multiple formats across API versions:
          "2026-06-25"    ISO date (Layout A original)
          "2026JUN25"     YYYY+abbr_month+DD (Layout C description-derived)
          "25-Jun-2026"   DD-Mon-YYYY
          1750601400      Unix epoch (int or string)
        """
        if not expiry_str:
            return 0
        now = datetime.now(tz=IST)
        # Unix epoch (integer or digit-only string)
        try:
            epoch = int(str(expiry_str))
            expiry = datetime.fromtimestamp(epoch, tz=IST)
            delta  = expiry - now
            return max(0, delta.days + (1 if delta.seconds > 0 else 0))
        except (ValueError, OSError):
            pass
        # Try known string formats
        for fmt in ("%Y-%m-%d", "%Y%b%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y"):
            try:
                expiry = datetime.strptime(str(expiry_str), fmt).replace(tzinfo=IST)
                delta  = expiry - now
                # Ceil: if any partial day remains, count it as a full day.
                return max(0, delta.days + (1 if delta.seconds > 0 else 0))
            except ValueError:
                pass
        return 0

    @staticmethod
    def _expiry_from_ex_symbol(sym: str) -> str:
        """
        Extract YYYY-MM-DD expiry from a Fyers NFO option symbol.

        Handles both weekly (NSE:NIFTY2661724800CE) and monthly (NSE:NIFTY26JUN24500CE).
        Weekly format: UNDERLYING + YY + M(1-9 / O=Oct / N=Nov / D=Dec) + DD + strike + CE/PE
        Monthly format: UNDERLYING + YY + MON(3-letter) + strike + CE/PE

        Returns "" if the symbol cannot be parsed.
        """
        import re
        import calendar as _cal

        sym_part = sym.split(":")[-1]   # strip NSE:/MCX: prefix

        # Weekly: e.g. NIFTY2661724800CE  →  YY=26 M=6 DD=17 → 2026-06-17
        m = re.match(r'^[A-Z]+(\d{2})([1-9OND])(\d{2})\d+(CE|PE)$', sym_part, re.IGNORECASE)
        if m:
            try:
                yy = int(m.group(1))
                mc = m.group(2).upper()
                dd = int(m.group(3))
                month_map = {"O": 10, "N": 11, "D": 12}
                month = month_map.get(mc, int(mc) if mc.isdigit() else 0)
                if month and 1 <= dd <= 31:
                    return datetime(2000 + yy, month, dd).strftime("%Y-%m-%d")
            except (ValueError, Exception):
                pass

        # Monthly: e.g. NIFTY26JUN24500CE → last Thursday of June 2026
        m = re.match(r'^[A-Z]+(\d{2})([A-Z]{3})\d+(CE|PE)$', sym_part, re.IGNORECASE)
        if m:
            try:
                yy  = int(m.group(1))
                mon = m.group(2).upper()
                dt_base = datetime.strptime(f"{2000 + yy}{mon}01", "%Y%b%d")
                year, month = dt_base.year, dt_base.month
                last_day = _cal.monthrange(year, month)[1]
                for day in range(last_day, last_day - 7, -1):
                    if datetime(year, month, day).weekday() == 3:   # last Thursday
                        return datetime(year, month, day).strftime("%Y-%m-%d")
            except (ValueError, Exception):
                pass

        return ""

    @staticmethod
    def _atm_iv_from_chain(chain_data: dict) -> float:
        """ATM strike's average call+put IV, read from the flat chain via chain_service."""
        from analysis.options_chain import chain_service
        spot = float(chain_service.underlying_spot(chain_data) or 0)
        if not spot:
            return 0.0
        call_strikes = chain_service.strikes(chain_data, "call")
        if not call_strikes:
            return 0.0
        atm = min(call_strikes, key=lambda s: abs(s - spot))
        cq = chain_service.leg_quote(chain_data, atm, "call") or {}
        pq = chain_service.leg_quote(chain_data, atm, "put") or {}
        call_iv = float(cq.get("iv", 0) or 0)
        put_iv  = float(pq.get("iv", 0) or 0)
        if call_iv > 0 and put_iv > 0:
            return (call_iv + put_iv) / 2
        return call_iv or put_iv


# ── Module-level singleton ────────────────────────────────────────
options_executor = OptionsExecutor()
