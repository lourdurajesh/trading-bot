"""
theme_detector.py
─────────────────
Identifies active market themes from news headlines.
Uses Claude to understand what macro/sector themes are driving markets.

Example themes detected:
  - "LPG shortage → kitchen appliance demand"
  - "EV push → battery, charging infrastructure stocks"
  - "Monsoon failure → agrochemicals, irrigation"
  - "Defence spending surge → defence PSUs"
  - "China+1 → electronics manufacturing"
  - "Rate cut cycle → NBFCs, real estate"

Each theme maps to a list of NSE sector/stock tags to scan.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-sonnet-4-6"


@dataclass
class Theme:
    name:           str
    description:    str
    catalyst:       str          # what news triggered this theme
    direction:      str          # BULLISH / BEARISH
    duration:       str          # SHORT (1-5d) / MEDIUM (1-4w) / LONG (1-3m)
    sectors:        list = field(default_factory=list)   # NSE sectors to scan
    keywords:       list = field(default_factory=list)   # stock screening keywords
    conviction:     float = 0.5  # 0-1
    detected_at:    Optional[datetime] = None
    expires_at:     Optional[datetime] = None


# Hardcoded theme → sector mappings (expanded by Claude dynamically)
THEME_SECTOR_MAP = {
    "lpg_shortage":          ["consumer_durables", "electric_appliances"],
    "ev_push":               ["auto", "battery", "power", "charging"],
    "monsoon_failure":       ["agrochemicals", "irrigation", "fmcg"],
    "monsoon_good":          ["fertilizers", "seeds", "tractors", "fmcg"],
    "defence_spending":      ["defence", "aerospace", "electronics"],
    "china_plus_one":        ["electronics_mfg", "chemicals", "textiles"],
    "rate_cut":              ["nbfc", "housing_finance", "real_estate", "banks"],
    "rate_hike":             ["it_services", "fmcg", "pharma"],
    "crude_oil_rise":        ["oil_gas", "paints", "airlines", "tyres"],
    "crude_oil_fall":        ["airlines", "paints", "logistics", "fmcg"],
    "rupee_depreciation":    ["it_exports", "pharma_exports", "textiles"],
    "rupee_appreciation":    ["importers", "airlines", "oil_marketing"],
    "infra_spending":        ["cement", "steel", "construction", "logistics"],
    "healthcare_push":       ["pharma", "hospitals", "diagnostics", "medtech"],
    "digital_push":          ["it", "fintech", "telecom", "cloud"],
    "fii_buying":            ["large_cap", "banking", "it"],
    "fii_selling":           ["mid_cap", "small_cap"],
}


class ThemeDetector:
    """
    Detects active market themes from aggregated news.

    Usage:
        themes = theme_detector.detect(news_headlines)
        for theme in themes:
            print(theme.name, theme.sectors)
    """

    def __init__(self):
        self._active_themes: list[Theme] = []
        self._last_detection: Optional[datetime] = None

    def detect(self, headlines: list[str]) -> list[Theme]:
        """
        Detect themes from a list of news headlines.
        Uses Claude if API key available, else rule-based detection.
        """
        if not headlines:
            return self._active_themes

        if ANTHROPIC_API_KEY:
            themes = self._detect_with_claude(headlines)
        else:
            themes = self._detect_with_rules(headlines)

        # Merge with existing active themes
        self._merge_themes(themes)
        self._last_detection = datetime.now(tz=timezone.utc)

        logger.info(f"[ThemeDetector] {len(self._active_themes)} active themes: "
                    f"{[t.name for t in self._active_themes]}")
        return self._active_themes

    def get_active_themes(self) -> list[Theme]:
        """Return currently active themes (not expired)."""
        now = datetime.now(tz=timezone.utc)
        self._active_themes = [
            t for t in self._active_themes
            if t.expires_at is None or t.expires_at > now
        ]
        return self._active_themes

    def get_sectors_to_scan(self) -> list[str]:
        """Return all sectors currently flagged by active themes."""
        sectors = set()
        for theme in self.get_active_themes():
            sectors.update(theme.sectors)
        return list(sectors)

    # ─────────────────────────────────────────────────────────────
    # CLAUDE DETECTION
    # ─────────────────────────────────────────────────────────────

    def _detect_with_claude(self, headlines: list[str]) -> list[Theme]:
        """Ask Claude to identify themes from headlines."""
        headlines_text = "\n".join(f"- {h}" for h in headlines[:30])

        prompt = f"""You are a senior Indian equity market analyst.

Read these market news headlines from today and identify 2-5 key investment themes 
that could drive specific NSE stocks or sectors over the next 1-30 days.

Headlines:
{headlines_text}

For each theme, respond ONLY with a JSON array. No markdown, no explanation:
[
  {{
    "name": "short_snake_case_name",
    "description": "one sentence description",
    "catalyst": "the specific news that triggered this",
    "direction": "BULLISH or BEARISH",
    "duration": "SHORT (1-5d) or MEDIUM (1-4w) or LONG (1-3m)",
    "sectors": ["sector1", "sector2"],
    "keywords": ["keyword1", "keyword2"],
    "conviction": 0.0-1.0
  }}
]

Focus on actionable themes specific to Indian markets. Include sector names like:
pharma, banking, it, auto, cement, fmcg, steel, power, defence, real_estate,
consumer_durables, agrochemicals, oil_gas, telecom, infra, nbfc, textiles."""

        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      ANTHROPIC_MODEL,
                    "max_tokens": 1500,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"].strip()

            # Strip markdown if present
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            theme_data = json.loads(content)
            themes     = []
            now        = datetime.now(tz=timezone.utc)

            duration_days = {"SHORT": 5, "MEDIUM": 21, "LONG": 90}

            for td in theme_data:
                days   = duration_days.get(td.get("duration", "MEDIUM"), 21)
                theme  = Theme(
                    name        = td.get("name", "unknown"),
                    description = td.get("description", ""),
                    catalyst    = td.get("catalyst", ""),
                    direction   = td.get("direction", "BULLISH"),
                    duration    = td.get("duration", "MEDIUM"),
                    sectors     = td.get("sectors", []),
                    keywords    = td.get("keywords", []),
                    conviction  = float(td.get("conviction", 0.5)),
                    detected_at = now,
                    expires_at  = now + timedelta(days=days),
                )
                themes.append(theme)

            return themes

        except Exception as e:
            logger.error(f"[ThemeDetector] Claude detection failed: {e}")
            return self._detect_with_rules(headlines)

    # ─────────────────────────────────────────────────────────────
    # RULE-BASED DETECTION (no API key)
    # ─────────────────────────────────────────────────────────────

    def _detect_with_rules(self, headlines: list[str]) -> list[Theme]:
        """Simple keyword-based theme detection."""
        text  = " ".join(headlines).lower()
        now   = datetime.now(tz=timezone.utc)
        found = []

        rules = [
            ("lpg_shortage",     ["lpg", "cooking gas", "gas shortage", "cylinder"],
             "LPG/cooking gas supply issue drives kitchen appliance demand",
             "BULLISH", "MEDIUM", ["consumer_durables", "electric_appliances"]),

            ("ev_push",          ["electric vehicle", "ev policy", "ev subsidy", "charging"],
             "EV adoption driving auto and battery sector",
             "BULLISH", "LONG", ["auto", "battery", "power"]),

            ("monsoon_failure",  ["drought", "monsoon deficit", "rainfall below", "el nino"],
             "Poor monsoon impacts agri stocks",
             "BEARISH", "MEDIUM", ["agrochemicals", "fertilizers", "fmcg"]),

            ("monsoon_good",     ["good monsoon", "above normal rainfall", "monsoon surplus"],
             "Good monsoon boosts rural consumption",
             "BULLISH", "MEDIUM", ["fmcg", "tractors", "fertilizers"]),

            ("defence_spending", ["defence budget", "defence order", "indigenisation", "drdo"],
             "Defence capex push benefits defence sector",
             "BULLISH", "LONG", ["defence", "aerospace"]),

            ("infra_push",       ["infrastructure", "capex", "road project", "highway", "rail"],
             "Government infra spending benefits construction sector",
             "BULLISH", "LONG", ["cement", "steel", "construction"]),

            ("rate_cut",         ["rate cut", "repo cut", "rbi cuts", "dovish"],
             "Rate cut cycle beneficial for rate-sensitive sectors",
             "BULLISH", "LONG", ["nbfc", "housing_finance", "real_estate"]),

            ("crude_rise",       ["crude oil rises", "oil price up", "opec cut", "brent surge"],
             "Rising crude hurts India as major oil importer",
             "BEARISH", "SHORT", ["airlines", "paints", "tyres"]),

            ("crude_fall",       ["crude oil falls", "oil price drop", "brent falls"],
             "Falling crude positive for India",
             "BULLISH", "SHORT", ["airlines", "oil_marketing", "paints"]),

            ("china_plus_one",   ["china+1", "china plus one", "supply chain shift", "manufacturing india"],
             "Global supply chain diversification benefits Indian manufacturers",
             "BULLISH", "LONG", ["electronics_mfg", "chemicals", "textiles"]),

            ("tariff_fears",     ["tariff", "trade war", "import duty", "protectionism", "reciprocal tax"],
             "Global tariff escalation hurts export-oriented sectors",
             "BEARISH", "MEDIUM", ["it", "pharma", "textiles", "auto"]),

            ("global_risk_off",  ["global selloff", "risk off", "market rout", "global sell-off",
                                  "dow falls", "nasdaq falls", "fear index", "flight to safety"],
             "Global risk-off benefits defensives, hurts cyclicals",
             "BEARISH", "SHORT", ["fmcg", "pharma", "it"]),

            ("fii_selling",      ["fii selling", "foreign outflow", "fii exits", "foreign selling",
                                  "capital flight", "fpi selling", "fpi outflow"],
             "FII/FPI selling pressure weighing on large-caps",
             "BEARISH", "SHORT", ["large_cap", "banking", "it"]),

            ("rupee_fall",       ["rupee falls", "rupee weakens", "dollar strengthens", "inr drops",
                                  "currency pressure", "rupee at"],
             "Rupee weakness benefits IT exporters and pharma exporters",
             "BULLISH", "SHORT", ["it", "pharma_exports", "textiles"]),

            ("crude_rise",       ["crude rises", "crude oil up", "oil prices rise", "brent rises",
                                  "crude higher", "oil surges", "opec", "crude at $"],
             "Rising crude hurts India as major oil importer",
             "BEARISH", "SHORT", ["airlines", "paints", "tyres"]),

            ("crude_fall",       ["crude falls", "crude oil down", "oil prices drop", "brent falls",
                                  "crude lower", "oil tumbles"],
             "Falling crude positive for India",
             "BULLISH", "SHORT", ["airlines", "oil_marketing", "paints"]),

            ("banking_stress",   ["bank crisis", "npa rise", "credit risk", "banking sector stress",
                                  "loan default", "financial stress"],
             "Banking stress weighs on financials",
             "BEARISH", "MEDIUM", ["banking", "nbfc"]),

            ("it_weakness",      ["it sector", "tech layoffs", "us slowdown", "visa restrictions",
                                  "software demand", "tech spending cuts"],
             "IT sector under pressure from US slowdown or visa issues",
             "BEARISH", "MEDIUM", ["it"]),

            ("pharma_opportunity", ["usfda approval", "drug approval", "generic launch", "pharma export",
                                    "health policy", "drug pricing"],
             "Pharma sector catalyst from regulatory or policy development",
             "BULLISH", "MEDIUM", ["pharma", "hospitals"]),
        ]

        for name, keywords, desc, direction, duration, sectors in rules:
            if any(kw in text for kw in keywords):
                days  = {"SHORT": 5, "MEDIUM": 21, "LONG": 90}[duration]
                found.append(Theme(
                    name        = name,
                    description = desc,
                    catalyst    = next((h for h in headlines if any(kw in h.lower() for kw in keywords)), ""),
                    direction   = direction,
                    duration    = duration,
                    sectors     = sectors,
                    keywords    = keywords,
                    conviction  = 0.6,
                    detected_at = now,
                    expires_at  = now + timedelta(days=days),
                ))

        return found

    def _merge_themes(self, new_themes: list[Theme]) -> None:
        """Merge new themes with active themes, update conviction."""
        existing_names = {t.name for t in self._active_themes}
        for theme in new_themes:
            if theme.name in existing_names:
                # Update existing theme conviction
                for existing in self._active_themes:
                    if existing.name == theme.name:
                        existing.conviction = min(1.0, existing.conviction + 0.1)
                        existing.expires_at = theme.expires_at
            else:
                self._active_themes.append(theme)


# ── Module-level singleton ────────────────────────────────────────
theme_detector = ThemeDetector()
