"""
oi_analyzer.py
──────────────
Real-time OI analysis during market hours.
Fetches options chain every 5 minutes via Fyers API and computes:
  - PCR (Put-Call Ratio by OI)
  - Max pain strike
  - Gamma walls (top 3 call OI strikes = resistance, top 3 put OI strikes = support)
  - ATM OI change (building or unwinding)

Signal rules fed into conviction_scorer:
  PCR < 0.7 on falling market  → +2 (oversold, contrarian bullish)
  PCR > 1.2 on rising market   → -2 (overbought, contrarian bearish)
  OI unwinding at support      → +2
  OI buildup at resistance     → -2

Snapshots saved at market close (15:25 IST) to db/oi_snapshots/YYYY-MM-DD.json
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

_SNAPSHOTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "oi_snapshots")

# Supported index symbols (Fyers format)
INDEX_SYMBOLS = {
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "NIFTY":     "NSE:NIFTY50-INDEX",
    "FINNIFTY":  "NSE:NIFTYFINSERVICE-INDEX",
}

# Lot sizes (2026 revised)
LOT_SIZES = {
    "BANKNIFTY": 15,
    "NIFTY":     75,
    "FINNIFTY":  40,
}


@dataclass
class OISnapshot:
    date: str                       # YYYY-MM-DD
    time: str                       # HH:MM IST
    symbol: str                     # BANKNIFTY / NIFTY
    spot: float
    atm_strike: float
    pcr: float                      # put OI / call OI
    max_pain: float                 # strike with min payout to buyers
    call_walls: list[float]         # top 3 call OI strikes (resistance)
    put_walls: list[float]          # top 3 put OI strikes (support)
    atm_call_oi: int
    atm_put_oi: int
    atm_call_oi_prev: int           # OI from previous snapshot (for change)
    atm_put_oi_prev: int
    total_call_oi: int
    total_put_oi: int
    pcr_signal: int = 0             # +2 / -2 / 0
    pcr_reason: str = ""
    oi_signal: int = 0              # +2 / -2 / 0
    oi_reason: str = ""

    @property
    def combined_oi_score(self) -> int:
        return self.pcr_signal + self.oi_signal

    @property
    def atm_call_oi_change(self) -> int:
        return self.atm_call_oi - self.atm_call_oi_prev

    @property
    def atm_put_oi_change(self) -> int:
        return self.atm_put_oi - self.atm_put_oi_prev


@dataclass
class ChainRow:
    strike: float
    call_oi: int
    put_oi: int
    call_ltp: float
    put_ltp: float


class OIAnalyzer:
    """
    Real-time OI analysis for BANKNIFTY/NIFTY options chain.

    Usage:
        analyzer = OIAnalyzer()
        analyzer.initialise()          # connect Fyers
        snapshot = analyzer.refresh("BANKNIFTY")   # call every 5 min
        score, reason = analyzer.get_oi_signal("BANKNIFTY")
    """

    def __init__(self):
        self._fyers = None
        self._snapshots: dict[str, OISnapshot] = {}          # symbol → latest snapshot
        self._prev_atm_oi: dict[str, tuple[int, int]] = {}   # symbol → (call_oi, put_oi)
        # Track consecutive chain-fetch failures per symbol to suppress log spam.
        # First failure → WARNING; subsequent failures → DEBUG only.
        self._consecutive_failures: dict[str, int] = {}
        self._load_today_snapshots()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def initialise(self) -> None:
        """Connect to Fyers API for options chain fetching. Verifies token is live."""
        try:
            from fyers_apiv3 import fyersModel
            from config import settings
            if not settings.FYERS_ACCESS_TOKEN:
                logger.warning("[OIAnalyzer] No FYERS_ACCESS_TOKEN — running in simulation mode")
                return

            client = fyersModel.FyersModel(
                client_id=settings.FYERS_APP_ID,
                token=settings.FYERS_ACCESS_TOKEN,
                is_async=False,
            )
            # Verify token works before accepting it
            resp = client.get_profile()
            if resp.get("s") == "ok":
                self._fyers = client
                self._consecutive_failures.clear()
                logger.info("[OIAnalyzer] Fyers client initialised")
            else:
                logger.warning(
                    f"[OIAnalyzer] Token verification failed: {resp.get('message')} "
                    f"— falling back to simulation mode"
                )
                self._fyers = None
        except Exception as e:
            logger.warning(f"[OIAnalyzer] Fyers init failed: {e}")

    def refresh(self, symbol: str = "BANKNIFTY") -> Optional[OISnapshot]:
        """
        Fetch latest options chain and compute OI metrics.
        Call every 5 minutes during market hours.
        Returns OISnapshot or None if data unavailable.
        """
        chain = self._fetch_chain(symbol)
        if not chain:
            logger.debug(f"[OIAnalyzer] No chain data for {symbol}")
            return None

        spot = self._get_spot(symbol)
        if not spot:
            return None

        snapshot = self._compute_snapshot(symbol, spot, chain)
        self._snapshots[symbol] = snapshot
        logger.debug(
            f"[OIAnalyzer] {symbol}: PCR={snapshot.pcr:.2f}, "
            f"MaxPain={snapshot.max_pain:.0f}, "
            f"OI signal={snapshot.combined_oi_score}"
        )
        return snapshot

    def save_close_snapshot(self) -> None:
        """Save end-of-day snapshots to disk. Call at 15:25 IST."""
        today = datetime.now(tz=IST).date().isoformat()
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)

        for symbol, snap in self._snapshots.items():
            path = os.path.join(_SNAPSHOTS_DIR, f"{today}_{symbol}.json")
            try:
                with open(path, "w") as f:
                    json.dump(asdict(snap), f, indent=2)
                logger.info(f"[OIAnalyzer] Saved close snapshot: {path}")
            except Exception as e:
                logger.warning(f"[OIAnalyzer] Could not save snapshot: {e}")

    def get_oi_signal(self, symbol: str = "BANKNIFTY") -> tuple[int, str]:
        """
        Returns (score, reason) for conviction_scorer.
        Uses latest in-memory snapshot or last saved close snapshot.

        Score range: -4 to +4 (PCR signal + OI change signal)
        """
        snap = self._snapshots.get(symbol) or self._load_latest_close_snapshot(symbol)
        if snap is None:
            return 0, f"No OI data for {symbol}"

        reasons = []
        score = 0

        if snap.pcr_signal != 0:
            score += snap.pcr_signal
            reasons.append(snap.pcr_reason)
        if snap.oi_signal != 0:
            score += snap.oi_signal
            reasons.append(snap.oi_reason)

        reason = "; ".join(reasons) if reasons else f"PCR={snap.pcr:.2f} (neutral)"
        return score, reason

    def get_gamma_walls(self, symbol: str = "BANKNIFTY") -> tuple[list[float], list[float]]:
        """Returns (resistance_levels, support_levels) from gamma walls."""
        snap = self._snapshots.get(symbol) or self._load_latest_close_snapshot(symbol)
        if snap is None:
            return [], []
        return snap.call_walls, snap.put_walls

    def get_pcr(self, symbol: str = "BANKNIFTY") -> float:
        """Returns current PCR or 0.0 if unavailable."""
        snap = self._snapshots.get(symbol)
        return snap.pcr if snap else 0.0

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — chain fetch
    # ─────────────────────────────────────────────────────────────

    def _fetch_chain(self, symbol: str) -> list[ChainRow]:
        """Fetch options chain from Fyers. Returns simulated data if unavailable."""
        if self._fyers is None:
            return self._simulate_chain(symbol)

        fyers_sym = INDEX_SYMBOLS.get(symbol)
        if not fyers_sym:
            return []

        try:
            resp = self._fyers.optionchain(data={"symbol": fyers_sym, "strikecount": 20})
            if resp.get("s") != "ok":
                msg = resp.get("message", "unknown")
                fail_count = self._consecutive_failures.get(symbol, 0) + 1
                self._consecutive_failures[symbol] = fail_count

                # Log at WARNING for first failure; DEBUG for all subsequent ones
                if fail_count == 1:
                    logger.warning(
                        f"[OIAnalyzer] Chain fetch error for {symbol}: {msg} "
                        f"— subsequent errors suppressed until resolved"
                    )
                    # Report to system health and attempt token refresh if auth error
                    is_token_error = any(
                        k in msg.lower()
                        for k in ("token", "auth", "unauthori", "invalid")
                    )
                    if is_token_error:
                        try:
                            from system_health import system_health
                            system_health.set_alert(
                                "oi_chain",
                                f"Options chain unavailable: {msg}. "
                                f"Possible cause: Fyers app may not have options-chain data subscription. "
                                f"Auto-refresh attempted.",
                                severity="warning",
                            )
                            from token_manager import token_manager
                            token_manager.notify_token_failure("OIAnalyzer", msg)
                        except Exception:
                            pass
                    else:
                        try:
                            from system_health import system_health
                            system_health.set_alert(
                                "oi_chain",
                                f"Options chain unavailable ({symbol}): {msg}",
                                severity="warning",
                            )
                        except Exception:
                            pass
                else:
                    logger.debug(f"[OIAnalyzer] Chain fetch error for {symbol} (#{fail_count}): {msg}")
                return self._simulate_chain(symbol)

            # Success — reset failure counter and clear alert
            if self._consecutive_failures.get(symbol, 0) > 0:
                self._consecutive_failures[symbol] = 0
                try:
                    from system_health import system_health
                    system_health.clear_alert("oi_chain")
                    logger.info(f"[OIAnalyzer] Chain fetch restored for {symbol}")
                except Exception:
                    pass

            rows = []
            for item in resp.get("data", {}).get("optionsChain", []):
                try:
                    rows.append(ChainRow(
                        strike   = float(item.get("strikePrice", 0)),
                        call_oi  = int(item.get("CE", {}).get("openInterest", 0)),
                        put_oi   = int(item.get("PE", {}).get("openInterest", 0)),
                        call_ltp = float(item.get("CE", {}).get("ltp", 0)),
                        put_ltp  = float(item.get("PE", {}).get("ltp", 0)),
                    ))
                except (TypeError, ValueError):
                    continue
            return rows
        except Exception as e:
            fail_count = self._consecutive_failures.get(symbol, 0) + 1
            self._consecutive_failures[symbol] = fail_count
            if fail_count == 1:
                logger.warning(f"[OIAnalyzer] Chain fetch exception for {symbol}: {e}")
            else:
                logger.debug(f"[OIAnalyzer] Chain fetch exception for {symbol} (#{fail_count}): {e}")
            return self._simulate_chain(symbol)

    def _get_spot(self, symbol: str) -> Optional[float]:
        """Get current spot price from Fyers or data_store."""
        fyers_sym = INDEX_SYMBOLS.get(symbol)
        if not fyers_sym:
            return None

        if self._fyers:
            try:
                resp = self._fyers.quotes(data={"symbols": fyers_sym})
                if resp.get("s") == "ok":
                    return float(resp["d"][0]["v"]["lp"])
            except Exception:
                pass

        # Fallback: data_store last close
        try:
            from data.data_store import store
            df = store.get(fyers_sym, "1D")
            if df is not None and len(df) > 0:
                return float(df["close"].iloc[-1])
        except Exception:
            pass

        return None

    def _simulate_chain(self, symbol: str) -> list[ChainRow]:
        """
        Simulate a realistic options chain when Fyers is unavailable.
        Used in paper trading / test mode.
        """
        base_prices = {"BANKNIFTY": 48000, "NIFTY": 22000, "FINNIFTY": 21000}
        spot = base_prices.get(symbol, 48000)
        step = 100 if symbol == "BANKNIFTY" else 50

        atm = round(spot / step) * step
        rows = []
        for i in range(-10, 11):
            strike = atm + i * step
            # Simulate: more put OI below ATM (support), more call OI above (resistance)
            call_oi = max(1000, int(500_000 / (1 + abs(i) * 0.5) * (1.2 if i > 0 else 0.8)))
            put_oi  = max(1000, int(500_000 / (1 + abs(i) * 0.5) * (1.2 if i < 0 else 0.8)))
            rows.append(ChainRow(
                strike=strike, call_oi=call_oi, put_oi=put_oi,
                call_ltp=max(1.0, (atm - strike + 200) / 10),
                put_ltp=max(1.0, (strike - atm + 200) / 10),
            ))
        return rows

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — computation
    # ─────────────────────────────────────────────────────────────

    def _compute_snapshot(self, symbol: str, spot: float, chain: list[ChainRow]) -> OISnapshot:
        """Compute all OI metrics from the chain and spot price."""
        now = datetime.now(tz=IST)
        step = 100 if symbol == "BANKNIFTY" else 50
        atm = round(spot / step) * step

        # PCR
        total_call_oi = sum(r.call_oi for r in chain)
        total_put_oi  = sum(r.put_oi  for r in chain)
        pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else 1.0

        # ATM OI
        atm_row = min(chain, key=lambda r: abs(r.strike - atm), default=None)
        atm_call_oi = atm_row.call_oi if atm_row else 0
        atm_put_oi  = atm_row.put_oi  if atm_row else 0

        prev_call, prev_put = self._prev_atm_oi.get(symbol, (atm_call_oi, atm_put_oi))
        self._prev_atm_oi[symbol] = (atm_call_oi, atm_put_oi)

        # Gamma walls: top 3 by OI
        sorted_calls = sorted(chain, key=lambda r: r.call_oi, reverse=True)
        sorted_puts  = sorted(chain, key=lambda r: r.put_oi,  reverse=True)
        call_walls   = [r.strike for r in sorted_calls[:3]]
        put_walls    = [r.strike for r in sorted_puts[:3]]

        # Max pain
        max_pain = self._calc_max_pain(chain)

        # PCR signal
        pcr_signal, pcr_reason = self._score_pcr(pcr, spot, atm)

        # ATM OI change signal
        oi_signal, oi_reason = self._score_oi_change(
            atm_call_oi, atm_put_oi, prev_call, prev_put, spot, atm
        )

        return OISnapshot(
            date          = now.date().isoformat(),
            time          = now.strftime("%H:%M"),
            symbol        = symbol,
            spot          = round(spot, 2),
            atm_strike    = atm,
            pcr           = pcr,
            max_pain      = max_pain,
            call_walls    = call_walls,
            put_walls     = put_walls,
            atm_call_oi   = atm_call_oi,
            atm_put_oi    = atm_put_oi,
            atm_call_oi_prev = prev_call,
            atm_put_oi_prev  = prev_put,
            total_call_oi = total_call_oi,
            total_put_oi  = total_put_oi,
            pcr_signal    = pcr_signal,
            pcr_reason    = pcr_reason,
            oi_signal     = oi_signal,
            oi_reason     = oi_reason,
        )

    def _calc_max_pain(self, chain: list[ChainRow]) -> float:
        """
        Max pain = strike where total payout to option buyers is minimized.
        At expiry, writers want price here — institutional magnet level.
        """
        if not chain:
            return 0.0

        min_pain = float("inf")
        max_pain_strike = chain[0].strike

        for test_strike in [r.strike for r in chain]:
            # Total payout if price settles at test_strike
            call_payout = sum(
                max(0, test_strike - r.strike) * r.call_oi for r in chain
            )
            put_payout = sum(
                max(0, r.strike - test_strike) * r.put_oi for r in chain
            )
            total = call_payout + put_payout
            if total < min_pain:
                min_pain = total
                max_pain_strike = test_strike

        return max_pain_strike

    def _score_pcr(self, pcr: float, spot: float, atm: float) -> tuple[int, str]:
        """
        PCR scoring for conviction_scorer.
        Contrarian: extreme PCR at market inflection = mean reversion signal.
        """
        if pcr < 0.7:
            return 2, f"PCR={pcr:.2f} (extreme bearishness, contrarian bullish signal)"
        elif pcr < 0.85:
            return 1, f"PCR={pcr:.2f} (moderately bearish positioning, mild bullish)"
        elif pcr > 1.3:
            return -2, f"PCR={pcr:.2f} (extreme bullishness, contrarian bearish signal)"
        elif pcr > 1.15:
            return -1, f"PCR={pcr:.2f} (moderately bullish positioning, mild bearish)"
        else:
            return 0, f"PCR={pcr:.2f} (neutral range)"

    def _score_oi_change(
        self,
        call_oi: int, put_oi: int,
        prev_call: int, prev_put: int,
        spot: float, atm: float,
    ) -> tuple[int, str]:
        """
        OI change scoring.
        - Put OI unwinding at support (prev put OI > current): bulls covering shorts = bullish
        - Call OI unwinding at resistance: bears covering shorts = bullish
        - New call OI building above ATM: bears adding = bearish
        - New put OI building below ATM: bears adding = bearish
        """
        call_change = call_oi - prev_call
        put_change  = put_oi  - prev_put
        threshold   = max(1000, int(min(prev_call, prev_put) * 0.05))  # 5% change threshold

        # Put OI unwinding while price holds = bullish (bears closing shorts)
        if put_change < -threshold and call_change <= 0:
            return 2, f"ATM put OI unwinding ({put_change:+,}) — bears closing, bullish"

        # Call OI unwinding while price holds = bullish (sellers closing positions)
        if call_change < -threshold and put_change <= 0:
            return 1, f"ATM call OI unwinding ({call_change:+,}) — mild bullish"

        # New call OI buildup above ATM = resistance strengthening = bearish
        if call_change > threshold and put_change >= 0:
            return -2, f"ATM call OI buildup ({call_change:+,}) — resistance building, bearish"

        # New put OI buildup below ATM = support weakening = bearish
        if put_change > threshold and call_change >= 0:
            return -1, f"ATM put OI buildup ({put_change:+,}) — mild bearish"

        return 0, ""

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — snapshot persistence
    # ─────────────────────────────────────────────────────────────

    def _load_today_snapshots(self) -> None:
        """Load today's close snapshot into memory on startup."""
        today = datetime.now(tz=IST).date().isoformat()
        for symbol in INDEX_SYMBOLS:
            snap = self._load_close_snapshot_for_date(symbol, today)
            if snap:
                self._snapshots[symbol] = snap

    def _load_latest_close_snapshot(self, symbol: str) -> Optional[OISnapshot]:
        """Load the most recent saved close snapshot for a symbol."""
        if not os.path.exists(_SNAPSHOTS_DIR):
            return None

        files = sorted([
            f for f in os.listdir(_SNAPSHOTS_DIR)
            if f.endswith(f"_{symbol}.json")
        ], reverse=True)

        for fname in files[:5]:   # check last 5 trading days
            path = os.path.join(_SNAPSHOTS_DIR, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
                return OISnapshot(**data)
            except Exception:
                continue
        return None

    def _load_close_snapshot_for_date(self, symbol: str, date_str: str) -> Optional[OISnapshot]:
        path = os.path.join(_SNAPSHOTS_DIR, f"{date_str}_{symbol}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            return OISnapshot(**data)
        except Exception:
            return None


# ── Module-level singleton ────────────────────────────────────────
oi_analyzer = OIAnalyzer()
