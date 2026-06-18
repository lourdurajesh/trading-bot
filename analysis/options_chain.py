"""
options_chain.py
────────────────
SINGLE SOURCE OF TRUTH for Fyers option-chain access (U1 of the unification plan).

Used by BOTH the NSE options path (execution/options_executor.py) and the MCX engine
(commodity_options_learning.py). Before this, each had its own chain fetch + parser,
which is why the flat-format Fyers bug had to be fixed twice (B2.3). One parser now.

Handles both Fyers layouts:
  • FLAT (current): top-level `optionsChain` = individual CE/PE rows
    (option_type / strike_price / ltp / bid / ask / oi / volume / symbol),
    with `expiryData` holding only the expiry calendar (epoch + DD-MM-YYYY date).
  • PAIRED (legacy): `expiryData[i].optionsChain` rows with CE/PE sub-dicts.

Per the trading-architecture skill: do NOT add a second chain parser. Extend this.
"""
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class OptionsChainService:
    """One fetch + parse for every option chain, NSE and MCX."""

    def __init__(self, cache_ttl_s: int = 60):
        self._cache: dict[str, tuple] = {}   # symbol -> (raw_data, fetched_at)
        self._ttl = cache_ttl_s

    # ── Fetch ────────────────────────────────────────────────────
    def get_chain(self, symbol: str, *, strikecount: int = 15,
                  timestamp: str = "", force: bool = False) -> Optional[dict]:
        """Fetch the raw Fyers option chain for `symbol` (cached). None on failure."""
        now = datetime.now(tz=IST)
        if not force and timestamp == "" and symbol in self._cache:
            cached, ts = self._cache[symbol]
            if (now - ts).total_seconds() < self._ttl:
                return cached
        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised or not fyers_broker._client:
                return None
            resp = fyers_broker._client.optionchain(data={
                "symbol": symbol, "strikecount": strikecount, "timestamp": timestamp,
            })
            if resp.get("s") != "ok":
                logger.debug(f"[OptionsChain] fetch failed {symbol}: {resp.get('message')}")
                return None
            data = resp.get("data", {})
            if timestamp == "":
                self._cache[symbol] = (data, now)
            return data
        except Exception as e:
            logger.debug(f"[OptionsChain] fetch exception {symbol}: {e}")
            return None

    # ── Leg lookup (the bit that used to be duplicated) ──────────
    def leg_quote(self, chain: dict, strike: float, opt_type: str) -> Optional[dict]:
        """
        Return a strike's CE/PE quote {ltp, bid, ask, oi, volume, iv, symbol} or None.
        Handles flat (primary) and paired (legacy) layouts. `opt_type` = call|put.
        """
        if not chain:
            return None
        field = "CE" if opt_type == "call" else "PE"
        try:
            # FLAT: individual CE/PE rows at the top level
            for r in (chain.get("optionsChain", []) or []):
                if str(r.get("option_type", "")).upper() != field:
                    continue
                if abs(float(r.get("strike_price", 0) or 0) - strike) < 1:
                    ltp = float(r.get("ltp") or r.get("close_price") or 0)
                    if ltp > 0:
                        return {
                            "ltp": ltp,
                            "bid": float(r.get("bid") or 0),
                            "ask": float(r.get("ask") or 0),
                            "oi":  int(r.get("oi") or 0),
                            "volume": int(r.get("volume") or 0),
                            "iv":  float(r.get("iv") or 0),
                            "symbol": r.get("symbol", ""),
                        }
            # PAIRED (legacy): expiryData[i].optionsChain rows with CE/PE sub-dicts
            for exp in (chain.get("expiryData", []) or []):
                for row in (exp.get("optionsChain", []) or []):
                    if abs(float(row.get("strikePrice", 0) or 0) - strike) < 1:
                        leg = row.get(field, {}) or {}
                        ltp = float(leg.get("ltp") or leg.get("close_price") or 0)
                        if ltp > 0:
                            return {
                                "ltp": ltp,
                                "bid": float(leg.get("bid") or 0),
                                "ask": float(leg.get("ask") or 0),
                                "oi":  int(leg.get("oi") or leg.get("openInterest") or 0),
                                "volume": int(leg.get("volume") or 0),
                                "iv":  float(leg.get("iv") or 0),
                                "symbol": leg.get("symbol", ""),
                            }
        except Exception as exc:
            logger.debug(f"[OptionsChain] leg_quote error: {exc}")
        return None

    # ── Underlying spot (embedded row) ───────────────────────────
    def underlying_spot(self, chain: dict) -> Optional[float]:
        """Spot from the embedded underlying row (option_type='', strike=-1, spot in ltp/fp)."""
        if not chain:
            return None
        for row in (chain.get("optionsChain", []) or [])[:3]:
            ot = str(row.get("option_type", "") or "").upper().strip()
            if ot in ("CE", "PE"):
                continue
            for key in ("ltp", "fp", "underlyingValue", "underlying_value", "spot", "up"):
                v = row.get(key)
                if v is not None:
                    try:
                        fv = float(v)
                        if fv > 100:
                            return fv
                    except (ValueError, TypeError):
                        pass
        uv = chain.get("underlyingValue")
        try:
            return float(uv) if uv is not None else None
        except (ValueError, TypeError):
            return None

    # ── Expiry calendar (NSE needs this for DTE selection) ───────
    def expiries(self, chain: dict) -> list[tuple]:
        """Return [(epoch_int, date_str, dte_int), ...] from the chain's expiry calendar."""
        out = []
        today = datetime.now(tz=IST).date()
        for blk in (chain.get("expiryData", []) or []):
            epoch_raw = blk.get("expiry")
            if epoch_raw is None:
                continue
            try:
                epoch = int(str(epoch_raw))
            except (ValueError, TypeError):
                continue
            date_str = blk.get("date", "") or ""
            dte = None
            if date_str:
                try:
                    dte = (datetime.strptime(date_str, "%d-%m-%Y").date() - today).days
                except ValueError:
                    dte = None
            if dte is None:
                try:
                    dte = max(0, (datetime.fromtimestamp(epoch, tz=IST).date() - today).days)
                except (OSError, ValueError):
                    dte = 0
            out.append((epoch, date_str, dte))
        return out

    def pick_expiry_epoch(self, chain: dict, min_dte: int, max_dte: int):
        """Nearest expiry within [min_dte, max_dte] → (epoch, date_str, dte) or None."""
        cands = [(e, d, t) for (e, d, t) in self.expiries(chain) if min_dte <= t <= max_dte]
        return min(cands, key=lambda x: x[2]) if cands else None


# Module-level singleton — import this, don't instantiate per-caller.
chain_service = OptionsChainService()
