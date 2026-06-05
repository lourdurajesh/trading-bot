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
from datetime import datetime, timedelta
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
            result = self._select_from_chain(
                chain_data, underlying, short_name, lot_size,
                option_type, target_delta, min_dte, max_dte,
            )
            if result:
                return result

            # No expiry in DTE range from the default (nearest) chain.
            # Fyers timestamp="" always returns the nearest expiry only.
            # Re-fetch targeting min_dte+3 days out to get the next weekly expiry.
            target_date  = datetime.now(tz=IST) + timedelta(days=min_dte + 3)
            target_epoch = int(target_date.timestamp())
            logger.info(
                f"[OptionsExecutor] {underlying}: nearest expiry outside DTE {min_dte}-{max_dte} "
                f"— retrying chain for ~{target_date.strftime('%Y-%m-%d')} expiry"
            )
            chain_next = self._get_chain_for_timestamp(underlying, target_epoch)
            if chain_next:
                return self._select_from_chain(
                    chain_next, underlying, short_name, lot_size,
                    option_type, target_delta, min_dte, max_dte,
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

            # Diagnostic: check where Fyers put the strike rows.
            # Fyers v3 has two observed layouts:
            #   Layout A (older): strikes are inside expiryData[i]["optionsChain"]
            #   Layout B (newer): strikes are in the top-level chain_data["optionsChain"],
            #                     each strike has an "expiry" field; expiryData items are empty.
            expiry_blocks      = chain_data.get("expiryData", [])
            top_level_strikes  = chain_data.get("optionsChain", [])
            per_expiry_strikes = sum(len(b.get("optionsChain", [])) for b in expiry_blocks)
            underlying_val     = chain_data.get("underlyingValue")

            # Also check if underlyingValue lives inside the first expiry block
            if underlying_val is None and expiry_blocks:
                underlying_val = expiry_blocks[0].get("underlyingValue")

            if expiry_blocks and per_expiry_strikes == 0 and not top_level_strikes:
                logger.warning(
                    f"[OptionsExecutor] Chain for {underlying} has {len(expiry_blocks)} expiries "
                    f"but 0 strikes in both layouts (underlyingValue={underlying_val}). "
                    f"Check Fyers F&O / NSE Derivatives segment on the account. "
                    f"Top-level keys: {list(chain_data.keys())}"
                )
            elif per_expiry_strikes == 0 and top_level_strikes:
                # Layout B detected — normalise into Layout A so the rest of the code works.
                logger.debug(
                    f"[OptionsExecutor] {underlying}: Fyers v3 Layout B detected — "
                    f"{len(top_level_strikes)} strikes at top level. Normalising to per-expiry layout."
                )
                chain_data = self._normalise_layout_b(chain_data, underlying_val)

            # Log first expiry block keys at DEBUG to help trace future format changes
            if expiry_blocks:
                logger.debug(
                    f"[OptionsExecutor] {underlying} expiryData[0] keys: "
                    f"{list(expiry_blocks[0].keys())}"
                )

            self._chain_cache[underlying] = (chain_data, now)
            return chain_data

        except Exception as e:
            logger.debug(f"[OptionsExecutor] Chain fetch exception: {e}")
            return None

    def _get_chain_for_timestamp(self, underlying: str, epoch: int) -> Optional[dict]:
        """Fetch chain for a specific expiry epoch (used when nearest expiry is outside DTE range)."""
        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised:
                return None
            resp = fyers_broker._client.optionchain(data={
                "symbol":      underlying,
                "strikecount": 15,
                "timestamp":   str(epoch),
            })
            if resp.get("s") != "ok":
                logger.debug(f"[OptionsExecutor] Chain fetch (ts={epoch}) failed: {resp.get('message')}")
                return None
            chain_data       = resp.get("data", {})
            expiry_blocks    = chain_data.get("expiryData", [])
            top_level_strikes = chain_data.get("optionsChain", [])
            underlying_val   = chain_data.get("underlyingValue")
            if underlying_val is None and expiry_blocks:
                underlying_val = expiry_blocks[0].get("underlyingValue")
            per_expiry_strikes = sum(len(b.get("optionsChain", [])) for b in expiry_blocks)
            if per_expiry_strikes == 0 and top_level_strikes:
                chain_data = self._normalise_layout_b(chain_data, underlying_val)
            return chain_data
        except Exception as e:
            logger.debug(f"[OptionsExecutor] Chain fetch (ts={epoch}) exception: {e}")
            return None

    def _normalise_layout_b(self, chain_data: dict, underlying_val) -> dict:
        """
        Convert Fyers v3 Layout B → Layout A so the rest of the pipeline works unchanged.

        Layout B (newer Fyers v3):
          chain_data["optionsChain"] = [
              {"strikePrice": 24500, "expiry": "2025-01-30", "call_ltp": ..., ...},
              {"strikePrice": 24550, "expiry": "2025-01-30", ...},
              {"strikePrice": 24500, "expiry": "2025-02-27", ...},
              ...
          ]
          chain_data["expiryData"] = [
              {"expiry": "2025-01-30"},        ← optionsChain is missing / empty
              {"expiry": "2025-02-27"},
          ]

        Layout A (what the rest of the code expects):
          chain_data["expiryData"] = [
              {"expiry": "2025-01-30", "optionsChain": [...strikes for that expiry...]},
              {"expiry": "2025-02-27", "optionsChain": [...strikes for that expiry...]},
          ]
          chain_data["underlyingValue"] = <float>

        We group the flat strike list by their "expiry" field, then inject them into
        the matching expiryData blocks.  If a strike carries "expiryDate" instead of
        "expiry", both keys are checked.
        """
        from collections import defaultdict

        top_strikes: list[dict] = chain_data.get("optionsChain", [])
        expiry_blocks: list[dict] = chain_data.get("expiryData", [])

        # ── Group strikes by expiry key ───────────────────────────
        # Fyers has used both "expiry" and "expiryDate" in different API versions.
        expiry_map: dict[str, list[dict]] = defaultdict(list)
        for row in top_strikes:
            key = row.get("expiry") or row.get("expiryDate") or ""
            expiry_map[key].append(row)

        # ── Rebuild expiryData with populated optionsChain ────────
        new_expiry_data = []
        for blk in expiry_blocks:
            expiry_key = blk.get("expiry", "")
            new_blk = dict(blk)                          # shallow copy — preserve metadata
            new_blk["optionsChain"] = expiry_map.get(expiry_key, [])
            new_expiry_data.append(new_blk)

        # If expiryData was empty but strikes exist, synthesise expiry blocks
        # from the unique expiry values found in the top-level strike list.
        if not expiry_blocks and expiry_map:
            logger.info(
                f"[OptionsExecutor] Layout B: no expiryData blocks present — "
                f"synthesising {len(expiry_map)} expiry blocks from strike list."
            )
            for expiry_key, rows in sorted(expiry_map.items()):
                new_expiry_data.append({"expiry": expiry_key, "optionsChain": rows})

        result = dict(chain_data)
        result["expiryData"] = new_expiry_data
        # Ensure underlyingValue is set at the top level (may have been None)
        if underlying_val is not None:
            result["underlyingValue"] = underlying_val

        total_placed = sum(len(b["optionsChain"]) for b in new_expiry_data)
        logger.debug(
            f"[OptionsExecutor] Layout B normalised: "
            f"{len(top_strikes)} flat strikes → "
            f"{len(new_expiry_data)} expiry blocks ({total_placed} strikes placed)"
        )
        return result

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
            spot      = float(chain_data.get("underlyingValue", 0) or 0)
            expiries  = chain_data.get("expiryData", [])

            # Fyers sometimes returns underlyingValue=null even when the chain
            # has strike rows (observed post-holiday restarts).  Fall back to
            # the WebSocket LTP which is always up to date.
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
    def _days_to_expiry(expiry_str: str) -> int:
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d").replace(tzinfo=IST)
            now    = datetime.now(tz=IST)
            delta  = expiry - now
            # Ceil: if any partial day remains, count it as a full day.
            # Without this, Tuesday 9:30 AM → Thursday expiry shows 2 DTE (not 3)
            # because timedelta.days truncates the 14-hour remainder.
            return max(0, delta.days + (1 if delta.seconds > 0 else 0))
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
