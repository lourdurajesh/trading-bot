"""
premarket_analyzer.py
─────────────────────
Predicts Nifty opening direction using pre-open order book data.

NSE runs a call-auction session from 9:00–9:08 AM where all pending orders
are collected and a single Indicative Equilibrium Price (IEP) is computed.
The IEP finalises at ~9:08 AM and the market opens at 9:15 AM.

By reading the IEP vs previous close, we know the expected gap-up/gap-down
BEFORE the market opens — this is real order-book consensus, not a proxy.

Signal:
  IEP gap > +0.5%  → +2 (strong bullish)
  IEP gap > +0.15% → +1 (mildly bullish)
  IEP gap < -0.5%  → -2 (strong bearish)
  IEP gap < -0.15% → -1 (mildly bearish)
  flat             →  0

Data source:
  Primary: NSE pre-open API (equity-stockIndices endpoint)
  Fallback: Nifty spot LTP from Fyers data_store vs previous close

Usage:
    from intelligence.premarket_analyzer import premarket_analyzer
    score, reason = premarket_analyzer.get_signal()
"""

import logging
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# NSE pre-open IEP is finalised ~9:08 AM; this module is only meaningful
# between 9:00 and 9:20 AM IST. Calling outside this window returns 0.
_IEP_WINDOW_START = dtime(9, 0)
_IEP_WINDOW_END   = dtime(9, 20)   # allow a few minutes past open for late fetches

# Gap thresholds are read from engine_settings at call time so they can be
# adjusted via the dashboard without restarting the bot.
# Defaults: strong=0.50% (±2 score), mild=0.15% (±1 score)

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

# NSE API endpoints
_NSE_HOME     = "https://www.nseindia.com/"
_NSE_PREOPN   = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050"
_NSE_PREOPN2  = "https://www.nseindia.com/api/market-data-pre-open?key=NIFTY"


class PreMarketAnalyzer:
    """
    Reads the NSE pre-open IEP to predict Nifty opening direction.
    Used as a signal in conviction_scorer.score().
    """

    def get_signal(self) -> tuple[int, str]:
        """
        Returns (score, reason).
        score: -2, -1, 0, +1, +2
        Only meaningful 9:00–9:20 AM IST on weekdays.
        """
        now = datetime.now(tz=IST)
        if not (_IEP_WINDOW_START <= now.time() <= _IEP_WINDOW_END):
            return 0, "Outside pre-open window"
        if now.weekday() >= 5:
            return 0, "Weekend"

        # Try primary: NSE IEP API
        score, reason = self._score_from_iep()
        if score != 0 or "gap" in reason.lower():
            return score, reason

        # Fallback: Fyers spot LTP vs previous close from data_store.
        # This is less accurate than the real NSE IEP — the Fyers LTP during
        # 09:00–09:08 reflects early auction indicative prices which can move
        # significantly before the auction finalises at 09:08.
        logger.warning(
            "[PreMarket] NSE IEP API unavailable — falling back to Fyers LTP proxy. "
            "The reading may differ from the final NSE call-auction IEP."
        )
        # Escalate to dashboard so operator sees it without checking logs.
        # The IEP is time-critical (09:00–09:20 window) — a degraded reading
        # at this point may under-score the conviction score for today.
        try:
            from system_health import system_health
            system_health.set_alert(
                "nse_iep_api",
                "NSE pre-open IEP API unavailable — conviction IEP score is based on Fyers LTP "
                "proxy (less accurate). NSE API may be rate-limiting or unreachable.",
                severity="warning",
            )
        except Exception:
            pass
        return self._score_from_fyers()

    def get_iep(self) -> Optional[dict]:
        """
        Returns raw IEP data dict: {iep, prev_close, gap_pct, symbol}
        or None if unavailable.
        """
        iep, prev_close = self._fetch_iep_nse()
        if iep > 0 and prev_close > 0:
            return {
                "iep":        iep,
                "prev_close": prev_close,
                "gap_pct":    round((iep - prev_close) / prev_close * 100, 3),
                "source":     "nse_api",
            }
        # Fallback: data_store
        iep2, prev2 = self._fetch_iep_fyers()
        if iep2 > 0 and prev2 > 0:
            return {
                "iep":        iep2,
                "prev_close": prev2,
                "gap_pct":    round((iep2 - prev2) / prev2 * 100, 3),
                "source":     "fyers_ltp",
            }
        return None

    # ─── SCORING ─────────────────────────────────────────────────────

    def _score_from_iep(self) -> tuple[int, str]:
        iep, prev_close = self._fetch_iep_nse()
        if iep <= 0 or prev_close <= 0:
            return 0, "NSE IEP unavailable"

        gap_pct = (iep - prev_close) / prev_close * 100
        return self._gap_to_score(gap_pct, source="IEP")

    def _score_from_fyers(self) -> tuple[int, str]:
        iep, prev_close = self._fetch_iep_fyers()
        if iep <= 0 or prev_close <= 0:
            return 0, "Pre-open price unavailable"

        gap_pct = (iep - prev_close) / prev_close * 100
        return self._gap_to_score(gap_pct, source="spot")

    @staticmethod
    def _gap_to_score(gap_pct: float, source: str) -> tuple[int, str]:
        # Thresholds are read at call time from engine_settings (DB-backed, editable via UI)
        try:
            from config.mcx_engine_settings import engine_settings
            strong = engine_settings.premarket_strong_thresh_pct()
            mild   = engine_settings.premarket_mild_thresh_pct()
        except Exception:
            strong, mild = 0.50, 0.15   # fallback to defaults if settings unavailable
        if gap_pct > strong:
            return 2, f"Pre-open {source} gap +{gap_pct:.2f}% → gap-up open (bullish)"
        if gap_pct > mild:
            return 1, f"Pre-open {source} gap +{gap_pct:.2f}% → mild gap-up (mildly bullish)"
        if gap_pct < -strong:
            return -2, f"Pre-open {source} gap {gap_pct:.2f}% → gap-down open (bearish)"
        if gap_pct < -mild:
            return -1, f"Pre-open {source} gap {gap_pct:.2f}% → mild gap-down (mildly bearish)"
        return 0, f"Pre-open {source} gap {gap_pct:.2f}% → flat open (neutral)"

    # ─── DATA FETCHERS ───────────────────────────────────────────────

    def _fetch_iep_nse(self) -> tuple[float, float]:
        """
        Fetch IEP and previous close from NSE pre-open API.
        Returns (iep, prev_close); both 0.0 on failure.

        NSE requires a valid session cookie — we get one by visiting the home page first.
        The equity-stockIndices endpoint returns a data[] array where the first entry
        is the Nifty 50 index itself (symbol "Nifty 50") with fields iep, previousClose.

        Falls back to the market-data-pre-open endpoint if the first one fails.
        """
        try:
            import requests
            session = requests.Session()
            session.get(_NSE_HOME, headers=_NSE_HEADERS, timeout=8)
            resp = session.get(_NSE_PREOPN, headers=_NSE_HEADERS, timeout=8)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("data", []):
                sym = str(item.get("symbol", "")).upper()
                if sym in ("NIFTY 50", "NIFTY50"):
                    iep = float(item.get("iep") or item.get("open") or 0)
                    prev = float(
                        item.get("previousClose") or item.get("prevClose")
                        or item.get("lastPrice") or 0
                    )
                    if iep > 1000 and prev > 1000:
                        logger.info(
                            f"[PreMarket] NSE IEP={iep:.2f} prevClose={prev:.2f} "
                            f"gap={((iep-prev)/prev*100):+.2f}%"
                        )
                        # NSE API restored — clear any outstanding dashboard alert
                        try:
                            from system_health import system_health
                            system_health.clear_alert("nse_iep_api")
                        except Exception:
                            pass
                        return iep, prev

        except Exception as e:
            logger.debug(f"[PreMarket] Primary NSE API failed: {e}")

        # Fallback: pre-open specific endpoint
        try:
            import requests
            session = requests.Session()
            session.get(_NSE_HOME, headers=_NSE_HEADERS, timeout=8)
            resp = session.get(_NSE_PREOPN2, headers=_NSE_HEADERS, timeout=8)
            resp.raise_for_status()
            data = resp.json()

            rows = data.get("data", {}).get("dataList", []) or data.get("data", [])
            for item in rows:
                mdata = item.get("metadata", item)
                sym = str(mdata.get("symbol", "")).upper()
                if sym in ("NIFTY 50", "NIFTY50", "NIFTY"):
                    iep = float(mdata.get("iep") or 0)
                    prev = float(mdata.get("previousClose") or mdata.get("prevClose") or 0)
                    if iep > 1000 and prev > 1000:
                        logger.info(
                            f"[PreMarket] NSE IEP (endpoint2)={iep:.2f} prev={prev:.2f} "
                            f"gap={((iep-prev)/prev*100):+.2f}%"
                        )
                        try:
                            from system_health import system_health
                            system_health.clear_alert("nse_iep_api")
                        except Exception:
                            pass
                        return iep, prev
        except Exception as e:
            logger.debug(f"[PreMarket] Fallback NSE API failed: {e}")

        return 0.0, 0.0

    def _fetch_iep_fyers(self) -> tuple[float, float]:
        """
        During pre-open, the Fyers WebSocket streams LTP for NSE:NIFTY50-INDEX.
        Compare live LTP (proxy for IEP) vs the previous day's close from data_store.
        Returns (ltp, prev_close); both 0.0 on failure.
        """
        try:
            from data.data_store import store
            from execution.fyers_broker import fyers_broker

            # Previous close from data_store daily candles
            df = store.get_ohlcv("NSE:NIFTY50-INDEX", "1D", n=5)
            if df is None or len(df) < 2:
                return 0.0, 0.0
            prev_close = float(df["close"].iloc[-2])   # yesterday's close

            # Current LTP from Fyers quotes
            if not fyers_broker._initialised:
                return 0.0, 0.0
            resp = fyers_broker._client.quotes(
                data={"symbols": "NSE:NIFTY50-INDEX"}
            )
            if resp.get("s") != "ok":
                return 0.0, 0.0
            quotes = resp.get("d", [])
            if not quotes:
                return 0.0, 0.0
            ltp = float(quotes[0].get("v", {}).get("lp", 0))
            if ltp > 1000 and prev_close > 1000:
                logger.info(
                    f"[PreMarket] Fyers LTP={ltp:.2f} prevClose={prev_close:.2f} "
                    f"gap={((ltp-prev_close)/prev_close*100):+.2f}%"
                )
                return ltp, prev_close
        except Exception as e:
            logger.debug(f"[PreMarket] Fyers LTP fetch failed: {e}")
        return 0.0, 0.0


# ── Module-level singleton ─────────────────────────────────────────
premarket_analyzer = PreMarketAnalyzer()
