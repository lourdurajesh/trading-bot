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

            # No strikes in DTE range from the default (nearest) chain.
            # Fyers ships strikes for the NEAREST expiry only, but expiryData lists
            # the full expiry calendar. Pick the nearest expiry within the DTE window
            # and re-request the chain with its EXACT epoch (an arbitrary/computed
            # epoch is rejected: "Please provide valid expiry").
            picked = self._pick_expiry_epoch(chain_data, min_dte, max_dte)
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

        # Log chain shape at WARNING so we can diagnose what Fyers returned.
        if chain_data:
            expiry_blocks = chain_data.get("expiryData", [])
            top_level     = chain_data.get("optionsChain", [])
            per_expiry    = sum(len(b.get("optionsChain", [])) for b in expiry_blocks)
            first_expiry_keys = list(expiry_blocks[0].keys()) if expiry_blocks else []
            first_strike_keys = list(top_level[0].keys())[:6] if top_level else []
            logger.warning(
                f"[OptionsExecutor] {underlying}: chain fetched but no tradeable strike found. "
                f"expiries={len(expiry_blocks)} per_expiry_strikes={per_expiry} "
                f"top_level_strikes={len(top_level)} "
                f"first_expiry_keys={first_expiry_keys} first_strike_keys={first_strike_keys}"
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
                logger.warning(f"[OptionsExecutor] Chain fetch failed for {underlying}: {resp}")
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
            underlying_val = chain_data.get("underlyingValue")

            # Fyers sometimes moves underlyingValue between keys across versions.
            # Check expiryData block, then top-level contract rows as fallbacks.
            if underlying_val is None and expiry_blocks:
                underlying_val = expiry_blocks[0].get("underlyingValue")
            if underlying_val is None and top_level_strikes:
                # Fyers v3+: first row in optionsChain is the underlying itself
                # (option_type="", strike_price=-1) with spot in "ltp" or "fp".
                for row in top_level_strikes[:3]:
                    ot = str(row.get("option_type", "") or "").upper().strip()
                    if ot in ("CE", "PE"):
                        continue   # real option row — skip
                    for uv_key in ("ltp", "fp", "underlyingValue", "underlying_value", "spot", "up"):
                        v = row.get(uv_key)
                        if v is not None:
                            try:
                                fval = float(v)
                                if fval > 100:   # plausible spot price
                                    underlying_val = fval
                                    break
                            except (ValueError, TypeError):
                                pass
                    if underlying_val:
                        break

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
                # After B→A, check if the per-expiry strikes are still Layout C format
                # (individual contract rows: fp/ex_symbol/bid/ask, no call_ltp/strikePrice).
                # Fyers now sends flat individual CE/PE contracts at the top level — the two
                # normalizations must run in sequence: B first (group by expiry), then C (pair CE/PE).
                _new_expiries = chain_data.get("expiryData", [])
                if _new_expiries:
                    _sample = _new_expiries[0].get("optionsChain", [{}])[0]
                    if "call_ltp" not in _sample and "strikePrice" not in _sample:
                        _spot_val = float(chain_data.get("underlyingValue") or 0)
                        if not _spot_val:
                            try:
                                from data.data_store import store as _ds
                                _spot_val = float(_ds.get_ltp(underlying) or 0)
                            except Exception:
                                pass
                        logger.info(
                            f"[OptionsExecutor] {underlying}: Layout B+C detected "
                            f"({len(top_level_strikes)} individual contract rows) — normalising to strike pairs."
                        )
                        chain_data = self._normalise_layout_c(chain_data, _spot_val)
            elif per_expiry_strikes > 0 and expiry_blocks:
                # Layout C detection: rows have individual contract keys (fp/ex_symbol/bid/ask)
                # instead of paired strike keys (call_ltp/put_ltp/strikePrice).
                sample = expiry_blocks[0].get("optionsChain", [{}])[0]
                if "call_ltp" not in sample and "strikePrice" not in sample:
                    spot_val = float(underlying_val or 0)
                    logger.info(
                        f"[OptionsExecutor] {underlying}: Layout C detected "
                        f"({per_expiry_strikes} individual contract rows) — normalising to strike pairs."
                    )
                    chain_data = self._normalise_layout_c(chain_data, spot_val)

            # Log first expiry block keys at DEBUG to help trace future format changes
            if expiry_blocks:
                logger.debug(
                    f"[OptionsExecutor] {underlying} expiryData[0] keys: "
                    f"{list(expiry_blocks[0].keys())}"
                )

            # Preserve the full raw expiry calendar (epoch + DD-MM-YYYY date for every
            # listed expiry). Normalization above can collapse expiryData down to only
            # the nearest expiry's strikes; _pick_expiry_epoch needs the complete
            # calendar to re-request a valid future expiry by its exact epoch.
            if expiry_blocks:
                chain_data["_fyers_expiry_calendar"] = expiry_blocks

            self._chain_cache[underlying] = (chain_data, now)
            return chain_data

        except Exception as e:
            logger.warning(f"[OptionsExecutor] Chain fetch exception for {underlying}: {e}")
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
            chain_data         = resp.get("data", {})
            expiry_blocks      = chain_data.get("expiryData", [])
            top_level_strikes  = chain_data.get("optionsChain", [])
            underlying_val     = chain_data.get("underlyingValue")
            if underlying_val is None and expiry_blocks:
                underlying_val = expiry_blocks[0].get("underlyingValue")
            if underlying_val is None:
                # Fyers omits underlyingValue on future-expiry fetches — pull spot
                # from the embedded underlying row so _select_from_chain has a spot.
                underlying_val = self._underlying_from_rows(top_level_strikes)
            if underlying_val is not None:
                chain_data["underlyingValue"] = underlying_val
            per_expiry_strikes = sum(len(b.get("optionsChain", [])) for b in expiry_blocks)
            if per_expiry_strikes == 0 and top_level_strikes:
                chain_data = self._normalise_layout_b(chain_data, underlying_val)
                # Layout B+C combo: after grouping strikes by expiry, pair CE/PE rows
                _new_expiries = chain_data.get("expiryData", [])
                if _new_expiries:
                    _sample = _new_expiries[0].get("optionsChain", [{}])[0]
                    if "call_ltp" not in _sample and "strikePrice" not in _sample:
                        _spot_val = float(chain_data.get("underlyingValue") or 0)
                        if not _spot_val:
                            try:
                                from data.data_store import store as _ds
                                _spot_val = float(_ds.get_ltp(underlying) or 0)
                            except Exception:
                                pass
                        chain_data = self._normalise_layout_c(chain_data, _spot_val)
            elif per_expiry_strikes > 0 and expiry_blocks:
                sample = expiry_blocks[0].get("optionsChain", [{}])[0]
                if "call_ltp" not in sample and "strikePrice" not in sample:
                    chain_data = self._normalise_layout_c(chain_data, float(underlying_val or 0))
            return chain_data
        except Exception as e:
            logger.warning(f"[OptionsExecutor] Chain fetch (ts={epoch}) exception for {underlying}: {e}")
            return None

    @staticmethod
    def _underlying_from_rows(rows: list):
        """
        Extract spot from the embedded underlying/spot row Fyers includes in the
        top-level optionsChain (option_type="", strike_price=-1, spot in ltp/fp).
        Returns float spot or None.
        """
        for row in (rows or [])[:3]:
            ot = str(row.get("option_type", "") or "").upper().strip()
            if ot in ("CE", "PE"):
                continue   # real option row — skip
            for uv_key in ("ltp", "fp", "underlyingValue", "underlying_value", "spot", "up"):
                v = row.get(uv_key)
                if v is not None:
                    try:
                        fval = float(v)
                        if fval > 100:   # plausible index spot
                            return fval
                    except (ValueError, TypeError):
                        pass
        return None

    def _pick_expiry_epoch(self, chain_data: dict, min_dte: int, max_dte: int):
        """
        From a chain's expiryData calendar, return (epoch:int, date_str, dte:int) of the
        NEAREST expiry whose DTE falls within [min_dte, max_dte], or None if none qualify.

        Fyers ships the full expiry calendar in expiryData (each block: an epoch 'expiry'
        and a 'date' DD-MM-YYYY) but only includes strikes for the NEAREST expiry in the
        top-level optionsChain. To trade a further-out expiry we must re-request the chain
        passing that expiry's EXACT epoch as 'timestamp' — an arbitrary/computed epoch is
        rejected by Fyers with "Please provide valid expiry".
        """
        today = datetime.now(tz=IST).date()
        candidates = []
        # Prefer the preserved raw calendar — normalization may have collapsed expiryData.
        calendar = chain_data.get("_fyers_expiry_calendar") or chain_data.get("expiryData", [])
        for blk in calendar:
            epoch_raw = blk.get("expiry")
            if epoch_raw is None:
                continue
            try:
                epoch_int = int(str(epoch_raw))
            except (ValueError, TypeError):
                continue
            # DTE from the reliable DD-MM-YYYY 'date' field; fall back to epoch math.
            date_str = blk.get("date", "") or ""
            dte = None
            if date_str:
                try:
                    d = datetime.strptime(date_str, "%d-%m-%Y").date()
                    dte = (d - today).days
                except ValueError:
                    dte = None
            if dte is None:
                dte = self._days_to_expiry(epoch_raw)
            if min_dte <= dte <= max_dte:
                candidates.append((epoch_int, date_str, dte))
        if not candidates:
            return None
        # Nearest qualifying expiry (smallest DTE) — most liquid, least time-decay risk.
        return min(candidates, key=lambda x: x[2])

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
        # Fyers has used "expiry", "expiryDate", and sometimes no key at all.
        # When the key is absent, try description[1] (date token), then fall back
        # to parsing the expiry date out of the NFO symbol itself (ex_symbol).
        # A "" key produces an expiry="" block that _days_to_expiry() returns 0 for,
        # causing every DTE check to fail — so we must extract a real date here.
        expiry_map: dict[str, list[dict]] = defaultdict(list)
        for row in top_strikes:
            # Skip the embedded underlying/spot row (option_type="", strike_price=-1).
            # It carries no expiry and otherwise creates a junk "" expiry block.
            otype = str(row.get("option_type", "") or "").upper().strip()
            if otype not in ("CE", "PE"):
                continue
            key = row.get("expiry") or row.get("expiryDate") or ""
            if not key:
                desc = (row.get("description") or "").strip().split()
                if len(desc) >= 2:
                    key = desc[1]   # e.g. "2026JUN25" from "BANKNIFTY 2026JUN25 49000 CE"
            if not key:
                # description is empty or only 1 token — extract date from NFO symbol.
                # "NSE:NIFTY2661724800CE" → "2026-06-17"
                # Fyers puts short name in ex_symbol ("NIFTY") and full symbol in "symbol".
                # Try both; the full symbol is the reliable one.
                key = (self._expiry_from_ex_symbol(row.get("ex_symbol", "")) or
                       self._expiry_from_ex_symbol(row.get("symbol", "")))
            expiry_map[key].append(row)

        # ── Rebuild expiryData with populated optionsChain ────────
        new_expiry_data = []
        for blk in expiry_blocks:
            expiry_key = blk.get("expiry", "")
            new_blk = dict(blk)                          # shallow copy — preserve metadata
            new_blk["optionsChain"] = expiry_map.get(expiry_key, [])
            new_expiry_data.append(new_blk)

        # ── Synthesise when expiryData was empty OR key mismatch left 0 strikes ──
        # Fyers sometimes uses different date formats in expiryData vs. top-level
        # strike rows (e.g. expiryData has epoch int, strikes have "YYYY-MM-DD").
        # In that case expiry_map.get(expiry_key, []) returns [] for every block.
        # Detect the mismatch and rebuild directly from the strike-row keys.
        total_placed = sum(len(b["optionsChain"]) for b in new_expiry_data)
        if total_placed == 0 and expiry_map:
            # Either expiryData was empty, or all keys mismatched.
            logger.warning(
                f"[OptionsExecutor] Layout B key mismatch for {len(expiry_blocks)} expiry "
                f"blocks ({len(top_strikes)} strikes, map keys: "
                f"{sorted(expiry_map.keys())[:3]}...) — synthesising from strike rows"
            )
            new_expiry_data = []
            for expiry_key, rows in sorted(expiry_map.items()):
                new_expiry_data.append({"expiry": expiry_key, "optionsChain": rows})
            total_placed = sum(len(b["optionsChain"]) for b in new_expiry_data)
        elif not expiry_blocks and expiry_map:
            logger.info(
                f"[OptionsExecutor] Layout B: no expiryData blocks — "
                f"synthesising {len(expiry_map)} expiry blocks from strike list."
            )
            for expiry_key, rows in sorted(expiry_map.items()):
                new_expiry_data.append({"expiry": expiry_key, "optionsChain": rows})
            total_placed = sum(len(b["optionsChain"]) for b in new_expiry_data)

        # ── Safety net: fix any blocks that still have expiry="" ────
        # Happens when Fyers rows all hash to "" (no expiry field, short ex_symbol).
        # Skip the embedded underlying row (option_type != CE/PE) and try both
        # the full "symbol" field and the shorter "ex_symbol" field on actual options.
        for blk in new_expiry_data:
            if not blk.get("expiry") and blk.get("optionsChain"):
                derived = ""
                for row in blk["optionsChain"]:
                    otype = str(row.get("option_type", "") or "").upper().strip()
                    if otype not in ("CE", "PE"):
                        continue   # skip embedded underlying row
                    derived = (self._expiry_from_ex_symbol(row.get("symbol", "") or "") or
                               self._expiry_from_ex_symbol(row.get("ex_symbol", "") or ""))
                    if derived:
                        break
                if derived:
                    blk["expiry"] = derived
                    logger.info(
                        f"[OptionsExecutor] Layout B: expiry was '' — "
                        f"derived '{derived}' from option symbol"
                    )

        result = dict(chain_data)
        result["expiryData"] = new_expiry_data
        # Ensure underlyingValue is set at the top level (may have been None)
        if underlying_val is not None:
            result["underlyingValue"] = underlying_val

        logger.info(
            f"[OptionsExecutor] Layout B normalised: "
            f"{len(top_strikes)} flat strikes → "
            f"{len(new_expiry_data)} expiry blocks ({total_placed} strikes placed)"
        )
        return result

    def _normalise_layout_c(self, chain_data: dict, spot: float) -> dict:
        """
        Layout C: each row in expiryData[i]["optionsChain"] is a single CE or PE contract.
        Keys observed: ask, bid, fp, ex_symbol, exchange, description (may have more).
        Normalise into Layout A: one row per strike pair with call_ltp, put_ltp, strikePrice.
        """
        import re, math

        def _parse_contract(row: dict) -> tuple:
            sym         = row.get("ex_symbol", "") or ""
            description = row.get("description", "") or ""

            # 1. Direct keys on the row — most reliable if Fyers adds them.
            strike_val = None
            for k in ("strike_price", "strikePrice", "strike"):
                v = row.get(k)
                if v is not None:
                    try:
                        strike_val = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
            side_val = None
            for k in ("option_type", "optionType", "type", "side", "cp_type"):
                tv = str(row.get(k, "") or "").upper().strip()
                if tv in ("CE", "CALL", "C"):
                    side_val = "CE"
                    break
                if tv in ("PE", "PUT", "P"):
                    side_val = "PE"
                    break
            if strike_val and side_val:
                return strike_val, side_val

            # 2. Description (space-delimited): last token = CE/PE/CALL/PUT, second-last = strike.
            if description:
                parts = description.strip().split()
                if parts:
                    last = parts[-1].upper()
                    if last in ("CE", "PE", "CALL", "PUT"):
                        side_val = "CE" if last in ("CE", "CALL") else "PE"
                        try:
                            strike_val = float(parts[-2])
                            return strike_val, side_val
                        except (ValueError, IndexError):
                            pass

            # 3. ex_symbol regex: digits (possibly decimal) followed by CE/PE at end.
            m = re.search(r'(\d+(?:\.\d+)?)(CE|PE)$', sym, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1)), m.group(2).upper()
                except ValueError:
                    pass

            return None, None

        def _ltp_from_row(row: dict) -> float:
            # Fyers uses different price field names across versions: ltp, lp, fp, last_price.
            for price_key in ("ltp", "lp", "last_price", "fp"):
                v = row.get(price_key)
                if v is not None:
                    try:
                        fv = float(v)
                        if fv > 0:
                            return fv
                    except (ValueError, TypeError):
                        pass
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return max(bid, ask)

        def _call_delta(strike: float, spot: float) -> float:
            if spot <= 0:
                return 0.5
            return max(0.01, min(0.99, 0.5 + 0.5 * math.tanh((spot - strike) / (spot * 0.05))))

        new_expiry_data = []
        for blk in chain_data.get("expiryData", []):
            pairs: dict[float, dict] = {}
            rows = blk.get("optionsChain", [])
            for row in rows:
                strike, side = _parse_contract(row)
                if strike is None:
                    continue
                ltp = _ltp_from_row(row)
                if strike not in pairs:
                    cd = _call_delta(strike, spot)
                    pairs[strike] = {
                        "strikePrice": strike,
                        "call_ltp":    0.0,
                        "put_ltp":     0.0,
                        "call_delta":  cd,
                        "put_delta":   round(1.0 - cd, 4),
                        "call_iv":     0.0,
                        "put_iv":      0.0,
                        "call_oi":     0,
                        "put_oi":      0,
                    }
                if side == "CE":
                    pairs[strike]["call_ltp"] = ltp
                else:
                    pairs[strike]["put_ltp"] = ltp

            if not pairs and rows:
                # Log the first row so we can diagnose any future format changes.
                sample = rows[0]
                logger.warning(
                    f"[OptionsExecutor] Layout C: 0 pairs from {len(rows)} rows — "
                    f"_parse_contract failed for all. "
                    f"Row keys={list(sample.keys())} "
                    f"ex_symbol={sample.get('ex_symbol', 'N/A')!r} "
                    f"description={sample.get('description', 'N/A')!r} "
                    f"full_row={sample}"
                )

            new_blk = dict(blk)
            new_blk["optionsChain"] = sorted(pairs.values(), key=lambda r: r["strikePrice"])
            new_expiry_data.append(new_blk)

        result = dict(chain_data)
        result["expiryData"] = new_expiry_data
        total = sum(len(b["optionsChain"]) for b in new_expiry_data)
        logger.info(
            f"[OptionsExecutor] Layout C normalised: "
            f"individual contracts → {total} strike pairs across {len(new_expiry_data)} expiry blocks"
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

            if not spot:
                logger.warning(
                    f"[OptionsExecutor] {underlying}: spot=0 — "
                    f"underlyingValue absent from chain AND store.get_ltp() returned 0. "
                    f"Chain expiries={len(expiries)} per_expiry={sum(len(b.get('optionsChain',[])) for b in expiries)}"
                )
                return None
            if not expiries:
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
                all_dtes = [(b.get("expiry", "?"), self._days_to_expiry(b.get("expiry", "")))
                            for b in expiries]
                logger.info(
                    f"[OptionsExecutor] {underlying}: no expiry in {min_dte}-{max_dte} DTE. "
                    f"spot={spot:.0f} expiry_keys_and_dte={all_dtes}"
                )
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
                # All strikes were skipped (ltp=0). Log a sample to diagnose price field names.
                if chosen_rows:
                    s = chosen_rows[len(chosen_rows) // 2]   # mid-chain row (near ATM)
                    logger.warning(
                        f"[OptionsExecutor] {underlying}: {len(chosen_rows)} paired strikes "
                        f"(expiry={chosen_expiry} DTE={chosen_dte}) all have ltp=0 "
                        f"for option_type={option_type}. "
                        f"Sample row keys={list(s.keys())} "
                        f"call_ltp={s.get('call_ltp')} put_ltp={s.get('put_ltp')} "
                        f"strikePrice={s.get('strikePrice')}"
                    )
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
