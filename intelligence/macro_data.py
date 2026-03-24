"""
macro_data.py
─────────────
Collects macro-economic context before every trade analysis.

Data collected:
  - Nifty VIX (fear index)
  - FII/DII net flows (NSE daily data)
  - S&P 500 / Dow overnight direction
  - USD/INR exchange rate
  - Crude oil price (impacts many Indian stocks)
  - RBI repo rate (last known + any recent change)

All data cached for 30 minutes — no need to fetch on every signal.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 8
CACHE_MINUTES   = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class MacroSnapshot:
    # Market sentiment
    nifty_vix:          float = 0.0     # < 15 calm, 15-20 normal, > 20 fearful, > 25 panic
    vix_signal:         str   = "neutral"

    # FII/DII flows (INR Crores, positive = buying)
    fii_net_flow:       float = 0.0
    dii_net_flow:       float = 0.0
    fii_signal:         str   = "neutral"

    # Global markets
    sp500_change_pct:   float = 0.0
    dow_change_pct:     float = 0.0
    global_signal:      str   = "neutral"

    # Currency & commodities
    usdinr:             float = 0.0
    crude_oil_usd:      float = 0.0
    crude_signal:       str   = "neutral"

    # RBI
    rbi_repo_rate:      float = 6.5     # updated manually or via scraping
    recent_rbi_action:  str   = "none"  # "hike", "cut", "hold", "none"

    # Overall macro score (-10 bearish to +10 bullish)
    macro_score:        float = 0.0
    summary:            str   = ""
    fetched_at:         Optional[datetime] = None


class MacroDataCollector:
    """
    Collects and caches macro data.

    Usage:
        macro = macro_collector.get_snapshot()
        print(macro.macro_score)   # -5 to +5
        print(macro.summary)       # human-readable summary
    """

    def __init__(self):
        self._cache: Optional[MacroSnapshot] = None
        self._cached_at: Optional[datetime]  = None

    def get_snapshot(self, force_refresh: bool = False) -> MacroSnapshot:
        """Returns current macro snapshot, using cache if fresh."""
        now = datetime.now(tz=IST)
        if (
            not force_refresh
            and self._cache is not None
            and self._cached_at is not None
            and (now - self._cached_at) < timedelta(minutes=CACHE_MINUTES)
        ):
            return self._cache

        snapshot = MacroSnapshot(fetched_at=now)

        # Fetch each data point — failures are non-fatal
        self._fetch_vix(snapshot)
        self._fetch_fii_flows(snapshot)
        self._fetch_global_markets(snapshot)
        self._fetch_currency_commodities(snapshot)

        # Compute overall macro score
        self._compute_score(snapshot)

        self._cache     = snapshot
        self._cached_at = now

        logger.info(
            f"[Macro] Score: {snapshot.macro_score:+.1f} | "
            f"VIX: {snapshot.nifty_vix:.1f} | "
            f"FII: ₹{snapshot.fii_net_flow:+,.0f}Cr | "
            f"SPX: {snapshot.sp500_change_pct:+.1f}% | "
            f"Crude: ${snapshot.crude_oil_usd:.1f}"
        )
        return snapshot

    # ─────────────────────────────────────────────────────────────
    # DATA FETCHERS
    # ─────────────────────────────────────────────────────────────

    def _fetch_vix(self, snap: MacroSnapshot) -> None:
        """Fetch Nifty VIX from NSE."""
        try:
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp = session.get(
                "https://www.nseindia.com/api/allIndices",
                headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            for index in data.get("data", []):
                if index.get("index") == "INDIA VIX":
                    snap.nifty_vix = float(index.get("last", 0))
                    break

            if snap.nifty_vix > 25:
                snap.vix_signal = "panic"
            elif snap.nifty_vix > 20:
                snap.vix_signal = "fearful"
            elif snap.nifty_vix > 15:
                snap.vix_signal = "elevated"
            else:
                snap.vix_signal = "calm"

        except Exception as e:
            logger.debug(f"[Macro] VIX fetch failed: {e}")

    def _fetch_fii_flows(self, snap: MacroSnapshot) -> None:
        """Fetch FII/DII net flows from NSE."""
        try:
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp = session.get(
                "https://www.nseindia.com/api/fiidiiTradeReact",
                headers=HEADERS, timeout=REQUEST_TIMEOUT
            )
            data = resp.json()
            if data:
                latest = data[0]   # most recent entry
                fii_buy  = float(latest.get("fiiBuyValue",  0) or 0)
                fii_sell = float(latest.get("fiiSellValue", 0) or 0)
                dii_buy  = float(latest.get("diiBuyValue",  0) or 0)
                dii_sell = float(latest.get("diiSellValue", 0) or 0)

                snap.fii_net_flow = fii_buy - fii_sell
                snap.dii_net_flow = dii_buy - dii_sell

            if snap.fii_net_flow > 2000:
                snap.fii_signal = "strongly_bullish"
            elif snap.fii_net_flow > 500:
                snap.fii_signal = "bullish"
            elif snap.fii_net_flow < -2000:
                snap.fii_signal = "strongly_bearish"
            elif snap.fii_net_flow < -500:
                snap.fii_signal = "bearish"
            else:
                snap.fii_signal = "neutral"

        except Exception as e:
            logger.debug(f"[Macro] FII flows fetch failed: {e}")

    def _fetch_global_markets(self, snap: MacroSnapshot) -> None:
        """Fetch S&P 500 and Dow via Yahoo Finance (no API key needed)."""
        symbols = {"^GSPC": "sp500", "^DJI": "dow"}
        try:
            for yahoo_sym, attr in symbols.items():
                url  = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?interval=1d&range=2d"
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                data = resp.json()
                closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                closes = [c for c in closes if c is not None]
                if len(closes) >= 2:
                    change_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
                    if attr == "sp500":
                        snap.sp500_change_pct = round(change_pct, 2)
                    else:
                        snap.dow_change_pct = round(change_pct, 2)

            avg_global = (snap.sp500_change_pct + snap.dow_change_pct) / 2
            if avg_global > 1.0:
                snap.global_signal = "bullish"
            elif avg_global > 0.3:
                snap.global_signal = "mildly_bullish"
            elif avg_global < -1.0:
                snap.global_signal = "bearish"
            elif avg_global < -0.3:
                snap.global_signal = "mildly_bearish"
            else:
                snap.global_signal = "flat"

        except Exception as e:
            logger.debug(f"[Macro] Global markets fetch failed: {e}")

    def _fetch_currency_commodities(self, snap: MacroSnapshot) -> None:
        """Fetch USD/INR and Crude Oil via Yahoo Finance."""
        try:
            # USD/INR
            url  = "https://query1.finance.yahoo.com/v8/finance/chart/USDINR=X?interval=1d&range=1d"
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if closes:
                snap.usdinr = round(closes[-1], 2)
        except Exception as e:
            logger.debug(f"[Macro] USD/INR fetch failed: {e}")

        try:
            # Crude oil (WTI)
            url  = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1d&range=2d"
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                snap.crude_oil_usd = round(closes[-1], 2)
                crude_chg = (closes[-1] - closes[-2]) / closes[-2] * 100
                if crude_chg > 2:
                    snap.crude_signal = "rising_sharply"   # bad for India (import cost)
                elif crude_chg > 0.5:
                    snap.crude_signal = "rising"
                elif crude_chg < -2:
                    snap.crude_signal = "falling_sharply"  # good for India
                elif crude_chg < -0.5:
                    snap.crude_signal = "falling"
                else:
                    snap.crude_signal = "stable"
        except Exception as e:
            logger.debug(f"[Macro] Crude oil fetch failed: {e}")

    def _compute_score(self, snap: MacroSnapshot) -> None:
        """
        Compute overall macro score from -10 (very bearish) to +10 (very bullish).
        Weights reflect importance to Indian equity market.
        """
        score = 0.0

        # VIX (weight: 25%)
        if snap.vix_signal == "calm":
            score += 2.5
        elif snap.vix_signal == "elevated":
            score += 0.0
        elif snap.vix_signal == "fearful":
            score -= 2.0
        elif snap.vix_signal == "panic":
            score -= 4.0

        # FII flows (weight: 35% — biggest driver of Indian markets)
        if snap.fii_signal == "strongly_bullish":
            score += 3.5
        elif snap.fii_signal == "bullish":
            score += 2.0
        elif snap.fii_signal == "neutral":
            score += 0.0
        elif snap.fii_signal == "bearish":
            score -= 2.0
        elif snap.fii_signal == "strongly_bearish":
            score -= 3.5

        # Global markets (weight: 25%)
        if snap.global_signal == "bullish":
            score += 2.5
        elif snap.global_signal == "mildly_bullish":
            score += 1.0
        elif snap.global_signal == "flat":
            score += 0.0
        elif snap.global_signal == "mildly_bearish":
            score -= 1.0
        elif snap.global_signal == "bearish":
            score -= 2.5

        # Crude oil (weight: 15%)
        if snap.crude_signal == "falling_sharply":
            score += 1.5
        elif snap.crude_signal == "falling":
            score += 0.5
        elif snap.crude_signal == "rising":
            score -= 0.5
        elif snap.crude_signal == "rising_sharply":
            score -= 1.5

        snap.macro_score = round(max(-10, min(10, score)), 1)

        # Human-readable summary
        parts = []
        parts.append(f"VIX {snap.nifty_vix:.1f} ({snap.vix_signal})")
        parts.append(f"FII ₹{snap.fii_net_flow:+,.0f}Cr ({snap.fii_signal})")
        parts.append(f"SPX {snap.sp500_change_pct:+.1f}%")
        parts.append(f"Crude ${snap.crude_oil_usd:.0f} ({snap.crude_signal})")
        if snap.usdinr:
            parts.append(f"USD/INR {snap.usdinr:.2f}")
        snap.summary = " | ".join(parts)


# ── Module-level singleton ────────────────────────────────────────
macro_collector = MacroDataCollector()
