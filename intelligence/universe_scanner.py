"""
universe_scanner.py
───────────────────
Scans the full NSE universe (~1800 stocks) and returns a ranked
shortlist of stocks to monitor based on:
  1. Active market themes (from theme_detector)
  2. Minimum liquidity (avg daily volume × price)
  3. Price momentum and relative strength
  4. Regime suitability

This replaces the static watchlist with a dynamic one that
changes based on what's actually moving in the market.

Output: ranked list of stock candidates for tomorrow's watchlist.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Optional

import pandas as pd
import requests

from intelligence.theme_detector import Theme, theme_detector

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Minimum average daily turnover (₹ Crores) — filters out illiquid stocks
MIN_DAILY_TURNOVER_CR = 5.0    # ₹5 Crore minimum avg daily volume

# Sector → NSE industry classification mapping
# Company-name substring keywords per theme.
# Used when EQUITY_L.csv has no INDUSTRY column (sector_match always fails).
# These words appear in actual NSE company names, unlike headline-trigger words.
THEME_COMPANY_KEYWORDS: dict[str, list[str]] = {
    "infra_push":          ["infra", "construction", "cement", "steel", "rail", "road",
                            "highway", "bridge", "tunnel", "nbcc", "ircon", "rvnl",
                            "pnc", "knr", "dilip", "ashoka", "hg infra", "megastar",
                            "ultratech", "acc", "ambuja", "shree cement", "jsw",
                            "tata steel", "sail", "jindal", "concrete"],
    "infra_spending":      ["infra", "construction", "cement", "steel", "rail", "road"],
    "defence_spending":    ["defence", "aerospace", "bharat electronics", "bel", "hal",
                            "bharat forge", "mtar", "paras", "astra", "bharat dynamic",
                            "garden reach", "cochin shipyard", "mazagon"],
    "ev_push":             ["electric", "battery", "tata motors", "mahindra electric",
                            "exide", "amara raja", "greaves", "ampere", "olectra",
                            "tata power", "charge"],
    "pharma_opportunity":  ["pharma", "lab", "biotech", "drug", "health", "hospital",
                            "diagnostic", "cipla", "sun pharma", "dr reddy",
                            "lupin", "aurobindo", "divi", "alkem"],
    "healthcare_push":     ["pharma", "hospital", "health", "diagnostic", "medical",
                            "apollo", "fortis", "max health", "narayana"],
    "banking_stress":      ["bank", "finance", "capital", "credit", "nbfc"],
    "rate_cut":            ["bank", "finance", "housing", "realty", "estate",
                            "hdfc", "lichfl", "can fin", "pnb housing"],
    "it_weakness":         ["tech", "infotech", "software", "digital", "data",
                            "systems", "infosys", "wipro", "hcl", "tcs", "mphasis"],
    "digital_push":        ["tech", "digital", "fintech", "payment", "data", "cloud",
                            "software", "telecom", "broadband"],
    "china_plus_one":      ["electronic", "chemical", "textile", "manufacturing",
                            "export", "plastic", "polymer"],
    "crude_rise":          ["airline", "airway", "aviation", "tyre", "paint", "rubber",
                            "indigo", "spicejet", "berger", "asian paints", "apollo tyre"],
    "crude_fall":          ["airline", "airway", "aviation", "tyre", "paint",
                            "oil marketing", "bpcl", "iocl", "hpcl"],
    "rupee_fall":          ["tech", "infotech", "software", "pharma", "export",
                            "textile", "garment"],
    "fii_selling":         ["bank", "tech", "infotech", "hdfc", "reliance", "tcs"],
    "tariff_fears":        ["tech", "pharma", "textile", "auto", "motor", "export"],
    "global_risk_off":     ["fmcg", "pharma", "consumer", "hul", "itc", "nestle",
                            "britannia", "dabur"],
    "monsoon_failure":     ["agro", "pesticide", "fertilizer", "chemical", "seed",
                            "ubi", "iffco", "gsfc", "coromandel", "rallis"],
    "monsoon_good":        ["fertilizer", "tractor", "agri", "seed", "fmcg",
                            "mahindra", "escorts", "vstagro", "kaveri"],
    "lpg_shortage":        ["appliance", "electric", "kitchen", "prestige", "hawkins",
                            "ttk", "butterfly", "elgi"],
    "fii_buying":          ["bank", "it", "large cap", "hdfc", "reliance", "tcs",
                            "infosys", "icici", "kotak"],
}

SECTOR_INDUSTRY_MAP = {
    "consumer_durables":   ["CONSUMER DURABLES"],
    "electric_appliances": ["CONSUMER DURABLES", "CAPITAL GOODS"],
    "auto":                ["AUTOMOBILES", "AUTO COMPONENTS"],
    "battery":             ["ELECTRICAL EQUIPMENT", "CONSUMER DURABLES"],
    "pharma":              ["PHARMACEUTICALS", "HEALTHCARE"],
    "banking":             ["BANKS"],
    "nbfc":                ["FINANCE"],
    "housing_finance":     ["FINANCE"],
    "real_estate":         ["REALTY"],
    "it":                  ["IT", "COMPUTERS"],
    "cement":              ["CEMENT"],
    "steel":               ["METALS", "IRON & STEEL"],
    "fmcg":                ["FMCG", "FOOD"],
    "agrochemicals":       ["PESTICIDES", "FERTILISERS"],
    "fertilizers":         ["FERTILISERS"],
    "defence":             ["DEFENCE"],
    "power":               ["POWER"],
    "oil_gas":             ["OIL & GAS"],
    "oil_marketing":       ["OIL & GAS"],
    "airlines":            ["TRANSPORT"],
    "telecom":             ["TELECOM"],
    "infra":               ["CONSTRUCTION", "INFRASTRUCTURE"],
    "construction":        ["CONSTRUCTION"],
    "paints":              ["PAINTS"],
    "tyres":               ["TYRES"],
    "chemicals":           ["CHEMICALS"],
    "textiles":            ["TEXTILES"],
    "electronics_mfg":     ["ELECTRONICS"],
    "logistics":           ["LOGISTICS", "TRANSPORT"],
    "diagnostics":         ["HEALTHCARE"],
    "hospitals":           ["HEALTHCARE"],
}


@dataclass
class StockCandidate:
    symbol:         str          # Fyers format: NSE:SYMBOL-EQ
    company_name:   str
    sector:         str
    price:          float
    avg_turnover_cr: float       # avg daily turnover in ₹ Crores
    momentum_score: float        # 0-10
    theme_match:    list = field(default_factory=list)  # matching theme names
    theme_conviction: float = 0.0
    overall_score:  float = 0.0
    reason:         str   = ""


class UniverseScanner:
    """
    Scans NSE universe and returns theme-matched stock candidates.

    Usage:
        candidates = universe_scanner.scan(themes, max_stocks=50)
        # Returns ranked list of StockCandidate
    """

    def __init__(self):
        self._nse_universe: Optional[pd.DataFrame] = None
        self._universe_fetched_at: Optional[datetime] = None

    def scan(
        self,
        themes:     list[Theme],
        max_stocks: int = 50,
    ) -> list[StockCandidate]:
        """
        Scan NSE universe for stocks matching active themes.
        When no themes are active, falls back to scoring top liquid stocks
        from the NSE most-active list so the nightly agent always produces output.
        Returns top `max_stocks` candidates ranked by overall score.
        """
        logger.info(f"[UniverseScanner] Scanning for {len(themes)} themes...")

        # Fetch NSE equity list
        universe_df = self._get_nse_universe()
        if universe_df is None or len(universe_df) == 0:
            logger.warning("[UniverseScanner] Could not fetch NSE universe")
            return self._fallback_candidates(themes)

        # Get sectors to focus on from active themes
        target_sectors = set()
        for theme in themes:
            for sector in theme.sectors:
                industries = SECTOR_INDUSTRY_MAP.get(sector, [])
                target_sectors.update(industries)

        # Filter to relevant sectors — if no themes, use most-active stocks
        if target_sectors:
            mask = universe_df["industry"].str.upper().apply(
                lambda x: any(s in str(x).upper() for s in target_sectors)
            )
            filtered_df = universe_df[mask].copy()
            logger.info(f"[UniverseScanner] {len(filtered_df)} stocks in theme sectors")

            # If sector filter yields nothing (e.g. EQUITY_L.csv has no industry column),
            # fall back to full universe and rely on keyword matching in _score_candidate
            if len(filtered_df) == 0 and themes:
                logger.info("[UniverseScanner] Sector filter empty — falling back to keyword scan on full universe")
                filtered_df = universe_df.copy()
        else:
            # No active themes — score top liquid stocks for momentum opportunities
            logger.info("[UniverseScanner] No active themes — scanning top liquid stocks")
            filtered_df = universe_df.copy()

        # Apply liquidity filter
        if "turnover" in filtered_df.columns:
            filtered_df = filtered_df[filtered_df["turnover"] >= MIN_DAILY_TURNOVER_CR]

        # Score and rank candidates
        no_theme_mode = len(themes) == 0
        candidates = []
        for _, row in filtered_df.iterrows():
            candidate = self._score_candidate(row, themes, no_theme_mode=no_theme_mode)
            if candidate:
                candidates.append(candidate)

        # Sort by overall score
        candidates.sort(key=lambda x: x.overall_score, reverse=True)
        result = candidates[:max_stocks]

        logger.info(
            f"[UniverseScanner] {len(result)} candidates from "
            f"{len(universe_df)} total NSE stocks"
        )
        return result

    def get_dynamic_watchlist(
        self,
        themes:         list[Theme],
        base_watchlist: list[str],
        max_add:        int = 20,
    ) -> list[str]:
        """
        Returns expanded watchlist = base_watchlist + theme-matched additions.
        Never removes base watchlist stocks.
        """
        candidates = self.scan(themes, max_stocks=max_add * 2)

        # Add top candidates not already in base watchlist
        additions = []
        for c in candidates:
            if c.symbol not in base_watchlist and len(additions) < max_add:
                additions.append(c.symbol)

        final = base_watchlist + additions
        logger.info(
            f"[UniverseScanner] Watchlist: {len(base_watchlist)} base + "
            f"{len(additions)} theme additions = {len(final)} total"
        )
        return final

    # ─────────────────────────────────────────────────────────────
    # NSE UNIVERSE FETCHING
    # ─────────────────────────────────────────────────────────────

    def _get_nse_universe(self) -> Optional[pd.DataFrame]:
        """
        Fetch complete NSE equity list with sector/industry tags.
        Cached for 24 hours — refreshed once per day.
        """
        now = datetime.now(tz=IST)
        if (
            self._nse_universe is not None
            and self._universe_fetched_at is not None
            and (now - self._universe_fetched_at).total_seconds() < 86400  # 24 * 3600
        ):
            return self._nse_universe

        # Try NSE equity master CSV
        df = self._fetch_nse_equity_list()

        # Fallback: NSE most active stocks
        if df is None:
            df = self._fetch_nse_most_active()

        if df is not None:
            self._nse_universe     = df
            self._universe_fetched_at = now

        return df

    def _fetch_nse_equity_list(self) -> Optional[pd.DataFrame]:
        """Fetch NSE equity master file."""
        try:
            url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
            resp = session.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()

            from io import StringIO
            df = pd.read_csv(StringIO(resp.text))
            df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")

            # Standardise columns
            rename_map = {}
            for col in df.columns:
                if "symbol" in col:     rename_map[col] = "symbol"
                if "name" in col:       rename_map[col] = "company_name"
                if "industry" in col:   rename_map[col] = "industry"
                if "series" in col:     rename_map[col] = "series"

            df = df.rename(columns=rename_map)

            # Keep only EQ series
            if "series" in df.columns:
                df = df[df["series"] == "EQ"]

            # Add Fyers-format symbol
            df["fyers_symbol"] = "NSE:" + df["symbol"].astype(str) + "-EQ"

            if "industry" not in df.columns:
                df["industry"] = "UNKNOWN"

            if "turnover" not in df.columns:
                df["turnover"] = 0.0

            logger.info(f"[UniverseScanner] NSE equity list: {len(df)} stocks")
            return df

        except Exception as e:
            logger.warning(f"[UniverseScanner] NSE equity list failed: {e}")
            return None

    def _fetch_nse_most_active(self) -> Optional[pd.DataFrame]:
        """Fetch NSE most-active stocks as fallback universe."""
        try:
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
            resp = session.get(
                "https://www.nseindia.com/api/live-analysis-most-active-securities?index=equities&limit=100",
                headers=HEADERS, timeout=10
            )
            data = resp.json().get("data", [])
            rows = []
            for item in data:
                rows.append({
                    "symbol":       item.get("symbol", ""),
                    "company_name": item.get("companyName", ""),
                    "industry":     item.get("industry", "UNKNOWN"),
                    "fyers_symbol": f"NSE:{item.get('symbol', '')}-EQ",
                    "turnover":     float(item.get("turnover", 0) or 0) / 1e7,  # to Cr
                    "price":        float(item.get("lastPrice", 0) or 0),
                })
            return pd.DataFrame(rows) if rows else None

        except Exception as e:
            logger.warning(f"[UniverseScanner] NSE most-active failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # SCORING
    # ─────────────────────────────────────────────────────────────

    def _score_candidate(self, row: pd.Series, themes: list[Theme], no_theme_mode: bool = False) -> Optional[StockCandidate]:
        """Score a stock against active themes."""
        symbol       = str(row.get("fyers_symbol", ""))
        company_name = str(row.get("company_name", ""))
        industry     = str(row.get("industry", "")).upper()
        price        = float(row.get("price", 0) or 0)
        turnover     = float(row.get("turnover", 0) or 0)

        if not symbol or not company_name:
            return None

        # Theme matching
        matching_themes   = []
        theme_conviction  = 0.0
        company_lower     = company_name.lower()
        industry_lower    = industry.lower()

        for theme in themes:
            # Check sector match
            theme_industries = []
            for sector in theme.sectors:
                theme_industries.extend(SECTOR_INDUSTRY_MAP.get(sector, []))

            sector_match  = any(ind.upper() in industry for ind in theme_industries)
            # Use headline-trigger keywords AND company-name-specific keywords.
            # EQUITY_L.csv has no industry column so sector_match is always False;
            # company keywords ensure meaningful candidates are still returned.
            company_keywords = THEME_COMPANY_KEYWORDS.get(theme.name, [])
            keyword_match = (
                any(kw.lower() in company_lower for kw in theme.keywords) or
                any(kw.lower() in company_lower for kw in company_keywords)
            )

            if sector_match or keyword_match:
                matching_themes.append(theme.name)
                theme_conviction = max(theme_conviction, theme.conviction)

        if not matching_themes and not no_theme_mode:
            return None

        # Skip very low-liquidity stocks when in no-theme mode
        if no_theme_mode and turnover < MIN_DAILY_TURNOVER_CR * 2:
            return None

        # Overall score
        score = 0.0
        if matching_themes:
            score += theme_conviction * 5.0                      # theme match (0-5)
            score += 1.0 if len(matching_themes) > 1 else 0.0   # multi-theme bonus
        else:
            score += 1.0                                         # no-theme base score

        score += min(turnover / 50, 2.0)                         # liquidity (0-2)
        score += 2.0                                             # base for passing filter

        reason = (
            f"Themes: {', '.join(matching_themes)}" if matching_themes
            else f"High-liquidity stock (no active theme) — turnover: Rs.{turnover:.0f}Cr"
        )

        return StockCandidate(
            symbol           = symbol,
            company_name     = company_name,
            sector           = industry,
            price            = price,
            avg_turnover_cr  = turnover,
            momentum_score   = 0.0,   # filled later by backtest
            theme_match      = matching_themes,
            theme_conviction = theme_conviction,
            overall_score    = round(score, 2),
            reason           = reason,
        )

    def _fallback_candidates(self, themes: list[Theme]) -> list[StockCandidate]:
        """Return empty list if universe fetch fails."""
        logger.warning("[UniverseScanner] Using empty candidate list — universe unavailable")
        return []


# ── Module-level singleton ────────────────────────────────────────
universe_scanner = UniverseScanner()
