"""
portfolio_analyser.py
─────────────────────
Professional portfolio risk analysis tool.

Capabilities:
  1. Correlation matrix — hidden correlations between positions
  2. Sector concentration — overexposure detection
  3. Stress test — simulates a 20% Nifty drop on your portfolio
  4. Hedging suggestions — what options/positions would protect you
  5. Claude synthesis — analyst-grade portfolio review narrative

Run manually:   python portfolio_analyser.py
Also called by: weekly_agent.py for Sunday deep review
                dashboard API /portfolio/analysis endpoint
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("portfolio_analyser")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
REPORTS_DIR       = "db/portfolio_reports"
os.makedirs(REPORTS_DIR, exist_ok=True)

# Sector classifications for NSE stocks
STOCK_SECTORS = {
    "RELIANCE":   "energy",
    "TCS":        "it",
    "HDFCBANK":   "banking",
    "INFY":       "it",
    "ICICIBANK":  "banking",
    "HINDUNILVR": "fmcg",
    "ITC":        "fmcg",
    "SBIN":       "banking",
    "BHARTIARTL": "telecom",
    "KOTAKBANK":  "banking",
    "LT":         "infra",
    "AXISBANK":   "banking",
    "WIPRO":      "it",
    "HCLTECH":    "it",
    "ASIANPAINT": "consumer",
    "MARUTI":     "auto",
    "BAJFINANCE": "nbfc",
    "TITAN":      "consumer",
    "ULTRACEMCO": "cement",
    "NESTLEIND":  "fmcg",
    "PERSISTENT": "it",
    "COFORGE":    "it",
}

# Nifty beta estimates (how much a stock moves per 1% Nifty move)
STOCK_BETAS = {
    "RELIANCE":   0.95,
    "TCS":        0.85,
    "HDFCBANK":   1.10,
    "INFY":       0.90,
    "ICICIBANK":  1.25,
    "HINDUNILVR": 0.60,
    "ITC":        0.65,
    "SBIN":       1.35,
    "BHARTIARTL": 0.85,
    "KOTAKBANK":  1.15,
    "LT":         1.10,
    "AXISBANK":   1.30,
    "WIPRO":      0.90,
    "HCLTECH":    0.85,
    "ASIANPAINT": 0.75,
    "MARUTI":     0.95,
    "BAJFINANCE": 1.40,
    "TITAN":      1.05,
    "ULTRACEMCO": 0.90,
    "NESTLEIND":  0.55,
}
DEFAULT_BETA = 1.0


@dataclass
class PositionAnalysis:
    symbol:       str
    company:      str
    sector:       str
    market_value: float
    pct_of_portfolio: float
    beta:         float
    contribution_to_risk: float   # beta × weight
    unrealised_pnl: float


@dataclass
class CorrelationResult:
    symbol_a:    str
    symbol_b:    str
    correlation: float
    risk_level:  str    # HIGH | MEDIUM | LOW
    note:        str


@dataclass
class StressTestResult:
    scenario:         str
    nifty_drop_pct:   float
    estimated_portfolio_loss: float
    estimated_portfolio_loss_pct: float
    worst_position:   str
    best_position:    str
    positions_detail: list = field(default_factory=list)


@dataclass
class HedgeSuggestion:
    instrument:   str
    strategy:     str
    purpose:      str
    cost_estimate: str
    protection:   str


@dataclass
class PortfolioAnalysis:
    generated_at:      datetime
    total_value:       float
    total_pnl:         float
    positions:         list[PositionAnalysis]
    sector_exposure:   dict
    correlations:      list[CorrelationResult]
    stress_tests:      list[StressTestResult]
    hedge_suggestions: list[HedgeSuggestion]
    portfolio_beta:    float
    concentration_score: float   # 0 = well diversified, 10 = highly concentrated
    risk_rating:       str       # LOW | MODERATE | HIGH | CRITICAL
    analyst_narrative: str
    action_items:      list[str]


class PortfolioAnalyser:

    def __init__(self):
        self._price_cache: dict[str, pd.Series] = {}

    def analyse(self, positions: list[dict] = None) -> PortfolioAnalysis:
        """
        Run full portfolio analysis.

        positions: list of dicts from portfolio_tracker.get_open_positions()
                   If None, fetches from portfolio_tracker directly.
        """
        if positions is None:
            from risk.portfolio_tracker import portfolio_tracker
            positions = portfolio_tracker.get_open_positions()

        if not positions:
            logger.info("[PortfolioAnalyser] No open positions to analyse")
            return self._empty_analysis()

        logger.info(f"[PortfolioAnalyser] Analysing {len(positions)} positions...")

        total_value = sum(
            p.get("entry_price", 0) * p.get("position_size", 0)
            for p in positions
        )

        # ── 1. Build position analysis ────────────────────────────
        position_analyses = self._analyse_positions(positions, total_value)

        # ── 2. Sector exposure ────────────────────────────────────
        sector_exposure = self._compute_sector_exposure(position_analyses)

        # ── 3. Correlation analysis ───────────────────────────────
        correlations = self._compute_correlations(positions)

        # ── 4. Portfolio beta ─────────────────────────────────────
        portfolio_beta = sum(p.contribution_to_risk for p in position_analyses)

        # ── 5. Stress tests ───────────────────────────────────────
        stress_tests = self._run_stress_tests(position_analyses, total_value)

        # ── 6. Hedge suggestions ──────────────────────────────────
        hedge_suggestions = self._generate_hedges(
            position_analyses, sector_exposure, portfolio_beta, stress_tests
        )

        # ── 7. Concentration score ────────────────────────────────
        concentration = self._concentration_score(position_analyses, sector_exposure)

        # ── 8. Risk rating ────────────────────────────────────────
        risk_rating = self._risk_rating(portfolio_beta, concentration, correlations)

        # ── 9. Claude narrative ───────────────────────────────────
        total_pnl = sum(p.get("unrealised_pnl", 0) for p in positions)
        narrative, action_items = self._generate_narrative(
            position_analyses, sector_exposure, correlations,
            stress_tests, portfolio_beta, risk_rating, total_value, total_pnl
        )

        analysis = PortfolioAnalysis(
            generated_at      = datetime.now(tz=timezone.utc),
            total_value       = total_value,
            total_pnl         = total_pnl,
            positions         = position_analyses,
            sector_exposure   = sector_exposure,
            correlations      = correlations,
            stress_tests      = stress_tests,
            hedge_suggestions = hedge_suggestions,
            portfolio_beta    = round(portfolio_beta, 2),
            concentration_score = concentration,
            risk_rating       = risk_rating,
            analyst_narrative = narrative,
            action_items      = action_items,
        )

        self._save_report(analysis)
        self._log_summary(analysis)
        return analysis

    # ─────────────────────────────────────────────────────────────
    # POSITION ANALYSIS
    # ─────────────────────────────────────────────────────────────

    def _analyse_positions(
        self, positions: list[dict], total_value: float
    ) -> list[PositionAnalysis]:
        analyses = []
        for p in positions:
            symbol      = p.get("symbol", "")
            ticker      = self._to_ticker(symbol)
            entry_price = p.get("entry_price", 0)
            size        = p.get("position_size", 0)
            market_val  = entry_price * size
            weight      = market_val / total_value if total_value > 0 else 0
            beta        = STOCK_BETAS.get(ticker, DEFAULT_BETA)

            analyses.append(PositionAnalysis(
                symbol              = symbol,
                company             = ticker,
                sector              = STOCK_SECTORS.get(ticker, "other"),
                market_value        = round(market_val, 2),
                pct_of_portfolio    = round(weight * 100, 1),
                beta                = beta,
                contribution_to_risk = round(beta * weight, 3),
                unrealised_pnl      = p.get("unrealised_pnl", 0),
            ))
        return sorted(analyses, key=lambda x: x.market_value, reverse=True)

    # ─────────────────────────────────────────────────────────────
    # SECTOR EXPOSURE
    # ─────────────────────────────────────────────────────────────

    def _compute_sector_exposure(
        self, positions: list[PositionAnalysis]
    ) -> dict:
        """Compute % allocation per sector and flag overexposure."""
        sector_totals: dict[str, float] = {}
        for p in positions:
            sector = p.sector
            sector_totals[sector] = sector_totals.get(sector, 0) + p.pct_of_portfolio

        result = {}
        for sector, pct in sorted(sector_totals.items(), key=lambda x: x[1], reverse=True):
            status = "OK"
            if pct > 40:
                status = "CRITICAL"
            elif pct > 25:
                status = "HIGH"
            elif pct > 15:
                status = "ELEVATED"
            result[sector] = {
                "pct":    round(pct, 1),
                "status": status,
            }
        return result

    # ─────────────────────────────────────────────────────────────
    # CORRELATION ANALYSIS
    # ─────────────────────────────────────────────────────────────

    def _compute_correlations(self, positions: list[dict]) -> list[CorrelationResult]:
        """
        Compute return correlations between all position pairs.
        Uses 90-day historical daily returns from Yahoo Finance.
        High correlation (>0.7) = hidden concentration risk.
        """
        symbols     = [p.get("symbol", "") for p in positions]
        price_data  = {}

        for symbol in symbols:
            prices = self._fetch_prices(symbol, days=90)
            if prices is not None and len(prices) > 20:
                price_data[symbol] = prices

        if len(price_data) < 2:
            logger.info("[PortfolioAnalyser] Insufficient price data for correlation")
            return self._sector_based_correlations(positions)

        # Build returns DataFrame
        returns_df = pd.DataFrame()
        for symbol, prices in price_data.items():
            returns_df[symbol] = prices.pct_change().dropna()

        returns_df = returns_df.dropna()
        if len(returns_df) < 10:
            return self._sector_based_correlations(positions)

        corr_matrix = returns_df.corr()
        results     = []

        syms = list(price_data.keys())
        for i in range(len(syms)):
            for j in range(i + 1, len(syms)):
                a, b = syms[i], syms[j]
                if a in corr_matrix.index and b in corr_matrix.columns:
                    corr_val = corr_matrix.loc[a, b]
                    if pd.isna(corr_val):
                        continue
                    if corr_val > 0.75:
                        risk_level = "HIGH"
                        note = f"These positions move together {corr_val:.0%} of the time — effectively the same bet"
                    elif corr_val > 0.50:
                        risk_level = "MEDIUM"
                        note = f"Moderate correlation — partial diversification benefit"
                    else:
                        risk_level = "LOW"
                        note = "Good diversification — these positions are largely independent"

                    results.append(CorrelationResult(
                        symbol_a    = a,
                        symbol_b    = b,
                        correlation = round(float(corr_val), 2),
                        risk_level  = risk_level,
                        note        = note,
                    ))

        return sorted(results, key=lambda x: abs(x.correlation), reverse=True)

    def _sector_based_correlations(self, positions: list[dict]) -> list[CorrelationResult]:
        """Fallback: estimate correlations from sector membership."""
        results = []
        for i, pa in enumerate(positions):
            for pb in positions[i+1:]:
                sa = STOCK_SECTORS.get(self._to_ticker(pa.get("symbol", "")), "other")
                sb = STOCK_SECTORS.get(self._to_ticker(pb.get("symbol", "")), "other")
                if sa == sb:
                    results.append(CorrelationResult(
                        symbol_a    = pa.get("symbol", ""),
                        symbol_b    = pb.get("symbol", ""),
                        correlation = 0.75,
                        risk_level  = "HIGH",
                        note        = f"Both in {sa} sector — likely highly correlated",
                    ))
        return results

    # ─────────────────────────────────────────────────────────────
    # STRESS TESTS
    # ─────────────────────────────────────────────────────────────

    def _run_stress_tests(
        self, positions: list[PositionAnalysis], total_value: float
    ) -> list[StressTestResult]:
        """
        Simulate portfolio impact under different market crash scenarios.
        Uses each stock's beta to estimate individual position losses.
        """
        scenarios = [
            ("Mild correction",        -10),
            ("Standard bear market",   -20),
            ("Severe crash (2008)",    -40),
            ("Flash crash (single day)", -5),
        ]

        results = []
        for scenario_name, nifty_drop in scenarios:
            position_impacts = []
            total_loss       = 0.0
            worst_loss       = 0.0
            best_loss        = 0.0
            worst_pos        = ""
            best_pos         = ""

            for pos in positions:
                # Estimated stock move = beta × Nifty move
                # High beta stocks fall more; low beta (FMCG) fall less
                estimated_move = pos.beta * (nifty_drop / 100)

                # Defensive stocks cap losses in crashes
                if pos.sector in ("fmcg", "pharma") and nifty_drop < -15:
                    estimated_move *= 0.6   # defensive stocks fall less

                position_loss = pos.market_value * estimated_move
                total_loss   += position_loss

                if position_loss < worst_loss:
                    worst_loss = position_loss
                    worst_pos  = pos.symbol

                if position_loss > best_loss or not best_pos:
                    best_loss = position_loss
                    best_pos  = pos.symbol

                position_impacts.append({
                    "symbol":         pos.symbol,
                    "beta":           pos.beta,
                    "estimated_move": round(estimated_move * 100, 1),
                    "loss_inr":       round(position_loss, 0),
                })

            results.append(StressTestResult(
                scenario                  = scenario_name,
                nifty_drop_pct            = nifty_drop,
                estimated_portfolio_loss  = round(total_loss, 0),
                estimated_portfolio_loss_pct = round(
                    total_loss / total_value * 100 if total_value > 0 else 0, 1
                ),
                worst_position            = worst_pos,
                best_position             = best_pos,
                positions_detail          = sorted(
                    position_impacts, key=lambda x: x["loss_inr"]
                ),
            ))

        return results

    # ─────────────────────────────────────────────────────────────
    # HEDGE SUGGESTIONS
    # ─────────────────────────────────────────────────────────────

    def _generate_hedges(
        self,
        positions:        list[PositionAnalysis],
        sector_exposure:  dict,
        portfolio_beta:   float,
        stress_tests:     list[StressTestResult],
    ) -> list[HedgeSuggestion]:
        """
        Generate specific hedging suggestions based on portfolio composition.
        """
        suggestions = []

        # Get 20% crash loss estimate
        crash_test = next(
            (s for s in stress_tests if "20" in s.scenario or "Standard" in s.scenario),
            None
        )
        crash_loss_pct = abs(crash_test.estimated_portfolio_loss_pct) if crash_test else 0

        # ── Hedge 1: Nifty Put Options (portfolio-level hedge) ────
        if portfolio_beta > 0.8:
            hedge_pct = min(crash_loss_pct / 2, 5)   # hedge half the estimated loss
            suggestions.append(HedgeSuggestion(
                instrument  = "Nifty Put Options",
                strategy    = f"Buy Nifty {int(5)}% OTM Put, 30-45 DTE",
                purpose     = f"Portfolio-level hedge — covers {hedge_pct:.0f}% of crash risk",
                cost_estimate = "~0.5-1.0% of portfolio value per month",
                protection  = f"Offsets ~{crash_loss_pct/2:.0f}% of estimated 20% crash loss",
            ))

        # ── Hedge 2: Sector-specific hedge ────────────────────────
        overexposed = [
            s for s, d in sector_exposure.items()
            if d["status"] in ("HIGH", "CRITICAL")
        ]
        for sector in overexposed[:2]:
            sector_hedge_map = {
                "banking":  ("Bank Nifty Put",   "Buy BankNifty ATM Put, 2-week expiry"),
                "it":       ("IT sector ETF Short", "Buy NIFTY IT Put or reduce position"),
                "energy":   ("Crude Oil Futures", "Short crude if energy overexposed"),
                "fmcg":     ("No hedge needed",   "FMCG is defensive — already a hedge"),
                "auto":     ("Nifty Auto Put",    "Buy sector Put if auto > 25%"),
            }
            instrument, strategy = sector_hedge_map.get(
                sector, ("Sector ETF Put", f"Reduce {sector} exposure or buy sector Put")
            )
            suggestions.append(HedgeSuggestion(
                instrument    = instrument,
                strategy      = strategy,
                purpose       = f"{sector.upper()} overexposure at {sector_exposure[sector]['pct']:.0f}%",
                cost_estimate = "~0.3-0.5% of sector exposure per month",
                protection    = f"Reduces {sector} concentration risk",
            ))

        # ── Hedge 3: High beta hedge ──────────────────────────────
        high_beta_positions = [p for p in positions if p.beta > 1.3]
        if high_beta_positions:
            names = ", ".join(p.company for p in high_beta_positions[:3])
            suggestions.append(HedgeSuggestion(
                instrument    = "Stock-specific Puts",
                strategy      = f"Buy ATM Puts on {names}",
                purpose       = "High-beta positions amplify losses in corrections",
                cost_estimate = "~1-2% of position value per month",
                protection    = "Caps downside on most volatile positions",
            ))

        # ── Hedge 4: Gold as macro hedge ──────────────────────────
        if crash_loss_pct > 15:
            suggestions.append(HedgeSuggestion(
                instrument    = "Gold ETF (GOLDBEES)",
                strategy      = "Allocate 5-10% of portfolio to Gold ETF",
                purpose       = "Macro uncertainty hedge — gold rises when markets panic",
                cost_estimate = "No carry cost — just opportunity cost",
                protection    = "Negative correlation to equities in crisis scenarios",
            ))

        return suggestions

    # ─────────────────────────────────────────────────────────────
    # SCORING & RATING
    # ─────────────────────────────────────────────────────────────

    def _concentration_score(
        self,
        positions:       list[PositionAnalysis],
        sector_exposure: dict,
    ) -> float:
        """
        Herfindahl-Hirschman Index style concentration score.
        0 = perfectly diversified, 10 = single position.
        """
        # Position concentration (HHI)
        weights     = [p.pct_of_portfolio / 100 for p in positions]
        hhi_pos     = sum(w ** 2 for w in weights)

        # Sector concentration
        sect_weights = [d["pct"] / 100 for d in sector_exposure.values()]
        hhi_sect     = sum(w ** 2 for w in sect_weights)

        # Combine and scale to 0-10
        combined = (hhi_pos + hhi_sect) / 2
        return round(min(combined * 10 * 2, 10), 1)

    def _risk_rating(
        self,
        beta:            float,
        concentration:   float,
        correlations:    list[CorrelationResult],
    ) -> str:
        high_corr_count = sum(1 for c in correlations if c.risk_level == "HIGH")
        risk_score      = 0

        if beta > 1.3:      risk_score += 3
        elif beta > 1.0:    risk_score += 2
        elif beta > 0.8:    risk_score += 1

        if concentration > 7:   risk_score += 3
        elif concentration > 5: risk_score += 2
        elif concentration > 3: risk_score += 1

        if high_corr_count > 3: risk_score += 2
        elif high_corr_count > 1: risk_score += 1

        if risk_score >= 7:   return "CRITICAL"
        elif risk_score >= 5: return "HIGH"
        elif risk_score >= 3: return "MODERATE"
        else:                 return "LOW"

    # ─────────────────────────────────────────────────────────────
    # CLAUDE NARRATIVE
    # ─────────────────────────────────────────────────────────────

    def _generate_narrative(
        self, positions, sector_exposure, correlations,
        stress_tests, portfolio_beta, risk_rating,
        total_value, total_pnl
    ) -> tuple[str, list[str]]:
        """Generate analyst narrative using Claude or rule-based fallback."""

        if ANTHROPIC_API_KEY:
            return self._claude_narrative(
                positions, sector_exposure, correlations,
                stress_tests, portfolio_beta, risk_rating,
                total_value, total_pnl
            )
        return self._rule_narrative(
            positions, sector_exposure, stress_tests,
            portfolio_beta, risk_rating, total_pnl
        )

    def _claude_narrative(
        self, positions, sector_exposure, correlations,
        stress_tests, portfolio_beta, risk_rating,
        total_value, total_pnl
    ) -> tuple[str, list[str]]:

        # Build context
        pos_text = "\n".join([
            f"  {p.company} ({p.sector}): ₹{p.market_value:,.0f} "
            f"({p.pct_of_portfolio:.0f}%), beta={p.beta}, "
            f"P&L=₹{p.unrealised_pnl:+,.0f}"
            for p in positions
        ])

        sector_text = "\n".join([
            f"  {s}: {d['pct']:.0f}% ({d['status']})"
            for s, d in sector_exposure.items()
        ])

        high_corr = [c for c in correlations if c.risk_level == "HIGH"]
        corr_text = "\n".join([
            f"  {c.symbol_a} ↔ {c.symbol_b}: {c.correlation:.2f} correlation"
            for c in high_corr[:5]
        ]) or "  No high correlations detected"

        crash_test = next(
            (s for s in stress_tests if "20" in s.scenario or "Standard" in s.scenario),
            stress_tests[0] if stress_tests else None
        )
        crash_text = (
            f"  20% Nifty drop → portfolio loses ₹{abs(crash_test.estimated_portfolio_loss):,.0f} "
            f"({abs(crash_test.estimated_portfolio_loss_pct):.0f}%)"
        ) if crash_test else ""

        prompt = f"""You are a senior portfolio risk manager at a Mumbai-based hedge fund.
Analyse this NSE equity portfolio and provide professional risk assessment.

PORTFOLIO OVERVIEW:
  Total value:    ₹{total_value:,.0f}
  Unrealised P&L: ₹{total_pnl:+,.0f}
  Portfolio beta: {portfolio_beta:.2f}
  Risk rating:    {risk_rating}

POSITIONS:
{pos_text}

SECTOR EXPOSURE:
{sector_text}

HIGH CORRELATIONS (hidden concentration risks):
{corr_text}

STRESS TEST:
{crash_text}

Write a concise but professional portfolio review (150-200 words) covering:
1. Overall assessment — is this portfolio well-constructed?
2. The most critical risk you see
3. What's working well
4. Specific rebalancing recommendation

Then provide exactly 3 action items as a JSON list at the end:
NARRATIVE: <your narrative here>
ACTIONS: ["action 1", "action 2", "action 3"]"""

        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 600,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            content = resp.json()["content"][0]["text"]

            # Parse narrative and actions
            narrative = content
            actions   = []
            if "ACTIONS:" in content:
                parts     = content.split("ACTIONS:")
                narrative = parts[0].replace("NARRATIVE:", "").strip()
                import json as jsonlib
                try:
                    actions = jsonlib.loads(parts[1].strip())
                except Exception:
                    actions = [parts[1].strip()]

            return narrative, actions

        except Exception as e:
            logger.error(f"[PortfolioAnalyser] Claude narrative failed: {e}")
            return self._rule_narrative(
                positions, sector_exposure, stress_tests,
                portfolio_beta, risk_rating, total_pnl
            )

    def _rule_narrative(
        self, positions, sector_exposure, stress_tests,
        portfolio_beta, risk_rating, total_pnl
    ) -> tuple[str, list[str]]:
        """Rule-based narrative when Claude API not available."""
        n_pos       = len(positions)
        overexposed = [s for s, d in sector_exposure.items() if d["status"] in ("HIGH", "CRITICAL")]
        crash       = next((s for s in stress_tests if "20" in s.scenario or "Standard" in s.scenario), None)
        crash_pct   = abs(crash.estimated_portfolio_loss_pct) if crash else 0

        narrative_parts = [
            f"[SIMULATION MODE — Add ANTHROPIC_API_KEY for full analyst narrative]",
            f"",
            f"Portfolio holds {n_pos} positions with a weighted beta of {portfolio_beta:.2f}x "
            f"against the Nifty. Overall risk rating: {risk_rating}.",
        ]

        if overexposed:
            narrative_parts.append(
                f"Sector concentration risk detected in: {', '.join(overexposed).upper()}. "
                f"Consider reducing exposure to achieve better diversification."
            )

        if crash_pct > 20:
            narrative_parts.append(
                f"A 20% Nifty correction would result in an estimated {crash_pct:.0f}% portfolio loss. "
                f"This is above the recommended 15% maximum drawdown threshold — hedging is advised."
            )
        elif crash_pct > 10:
            narrative_parts.append(
                f"A 20% Nifty correction would result in an estimated {crash_pct:.0f}% portfolio loss. "
                f"This is within acceptable range but consider adding a Nifty Put hedge."
            )

        if total_pnl > 0:
            narrative_parts.append(f"Portfolio is currently profitable at ₹{total_pnl:+,.0f}. "
                                   f"Consider booking partial profits on top winners.")

        actions = []
        if overexposed:
            actions.append(f"Reduce {overexposed[0].upper()} exposure — currently overweight")
        if crash_pct > 15:
            actions.append("Buy Nifty Put options for portfolio-level protection")
        if portfolio_beta > 1.2:
            actions.append("Add 1-2 defensive positions (FMCG/Pharma) to reduce portfolio beta")
        if len(actions) < 3:
            actions.append("Review stop losses — ensure all positions have defined exit levels")

        return "\n".join(narrative_parts), actions[:3]

    # ─────────────────────────────────────────────────────────────
    # PRICE DATA
    # ─────────────────────────────────────────────────────────────

    def _fetch_prices(self, symbol: str, days: int = 90) -> Optional[pd.Series]:
        """Fetch daily closing prices for correlation calculation."""
        if symbol in self._price_cache:
            return self._price_cache[symbol]

        ticker = self._to_ticker(symbol) + ".NS"
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?interval=1d&range={days}d"
            )
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            data   = resp.json()
            result = data["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            times  = result["timestamp"]
            series = pd.Series(
                closes,
                index=pd.to_datetime(times, unit="s"),
                name=symbol
            ).dropna()
            self._price_cache[symbol] = series
            return series
        except Exception as e:
            logger.debug(f"[PortfolioAnalyser] Price fetch failed for {symbol}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _to_ticker(self, symbol: str) -> str:
        return symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "")

    def _empty_analysis(self) -> PortfolioAnalysis:
        return PortfolioAnalysis(
            generated_at      = datetime.now(tz=timezone.utc),
            total_value       = 0,
            total_pnl         = 0,
            positions         = [],
            sector_exposure   = {},
            correlations      = [],
            stress_tests      = [],
            hedge_suggestions = [],
            portfolio_beta    = 0,
            concentration_score = 0,
            risk_rating       = "LOW",
            analyst_narrative = "No open positions to analyse.",
            action_items      = [],
        )

    def _save_report(self, analysis: PortfolioAnalysis) -> None:
        """Save analysis to JSON file."""
        timestamp = analysis.generated_at.strftime("%Y%m%d_%H%M")
        path      = os.path.join(REPORTS_DIR, f"portfolio_{timestamp}.json")
        report    = {
            "generated_at":      analysis.generated_at.isoformat(),
            "total_value":       analysis.total_value,
            "total_pnl":         analysis.total_pnl,
            "portfolio_beta":    analysis.portfolio_beta,
            "risk_rating":       analysis.risk_rating,
            "concentration":     analysis.concentration_score,
            "sector_exposure":   analysis.sector_exposure,
            "correlations":      [
                {"a": c.symbol_a, "b": c.symbol_b,
                 "corr": c.correlation, "risk": c.risk_level}
                for c in analysis.correlations[:10]
            ],
            "stress_tests":      [
                {"scenario": s.scenario,
                 "nifty_drop": s.nifty_drop_pct,
                 "loss_pct": s.estimated_portfolio_loss_pct}
                for s in analysis.stress_tests
            ],
            "hedge_suggestions": [
                {"instrument": h.instrument, "strategy": h.strategy,
                 "purpose": h.purpose, "cost": h.cost_estimate}
                for h in analysis.hedge_suggestions
            ],
            "analyst_narrative": analysis.analyst_narrative,
            "action_items":      analysis.action_items,
        }
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info(f"[PortfolioAnalyser] Report saved: {path}")

    def _log_summary(self, analysis: PortfolioAnalysis) -> None:
        logger.info("─" * 60)
        logger.info(f"PORTFOLIO ANALYSIS SUMMARY")
        logger.info(f"  Total value:    ₹{analysis.total_value:,.0f}")
        logger.info(f"  P&L:            ₹{analysis.total_pnl:+,.0f}")
        logger.info(f"  Beta:           {analysis.portfolio_beta:.2f}x")
        logger.info(f"  Risk rating:    {analysis.risk_rating}")
        logger.info(f"  Concentration:  {analysis.concentration_score:.1f}/10")
        logger.info(f"  Sector flags:   "
                    f"{[s for s, d in analysis.sector_exposure.items() if d['status'] != 'OK']}")

        # Stress test summary
        for st in analysis.stress_tests:
            logger.info(
                f"  Stress [{st.nifty_drop_pct:+.0f}%]: "
                f"₹{st.estimated_portfolio_loss:,.0f} "
                f"({st.estimated_portfolio_loss_pct:.1f}%)"
            )

        logger.info(f"  Action items:")
        for i, item in enumerate(analysis.action_items, 1):
            logger.info(f"    {i}. {item}")
        logger.info("─" * 60)


def print_full_report(analysis: PortfolioAnalysis) -> None:
    """Print full human-readable report to console."""
    print("\n" + "=" * 70)
    print(f"PORTFOLIO RISK ANALYSIS — {analysis.generated_at.strftime('%d %b %Y %H:%M')}")
    print("=" * 70)
    print(f"\nTotal value:    ₹{analysis.total_value:,.0f}")
    print(f"Unrealised P&L: ₹{analysis.total_pnl:+,.0f}")
    print(f"Portfolio beta: {analysis.portfolio_beta:.2f}x Nifty")
    print(f"Risk rating:    {analysis.risk_rating}")
    print(f"Concentration:  {analysis.concentration_score:.1f}/10")

    print("\n── POSITIONS ─────────────────────────────────────────")
    for p in analysis.positions:
        print(f"  {p.company:<15} {p.sector:<12} "
              f"₹{p.market_value:>10,.0f}  {p.pct_of_portfolio:>5.1f}%  "
              f"β={p.beta:.2f}  P&L=₹{p.unrealised_pnl:+,.0f}")

    print("\n── SECTOR EXPOSURE ───────────────────────────────────")
    for sector, data in analysis.sector_exposure.items():
        flag = " ⚠" if data["status"] in ("HIGH", "CRITICAL") else ""
        print(f"  {sector:<15} {data['pct']:>5.1f}%  {data['status']}{flag}")

    print("\n── HIGH CORRELATIONS ─────────────────────────────────")
    high_corr = [c for c in analysis.correlations if c.risk_level == "HIGH"]
    if high_corr:
        for c in high_corr[:5]:
            print(f"  {c.symbol_a} ↔ {c.symbol_b}: {c.correlation:.2f} — {c.note}")
    else:
        print("  No high correlations — good diversification")

    print("\n── STRESS TESTS ──────────────────────────────────────")
    for st in analysis.stress_tests:
        print(f"  Nifty {st.nifty_drop_pct:+.0f}%  →  "
              f"₹{st.estimated_portfolio_loss:>12,.0f}  "
              f"({st.estimated_portfolio_loss_pct:.1f}%)  "
              f"Worst: {st.worst_position.replace('NSE:','').replace('-EQ','')}")

    print("\n── HEDGE SUGGESTIONS ─────────────────────────────────")
    for i, h in enumerate(analysis.hedge_suggestions, 1):
        print(f"  {i}. {h.instrument}")
        print(f"     Strategy: {h.strategy}")
        print(f"     Purpose:  {h.purpose}")
        print(f"     Cost:     {h.cost_estimate}")

    print("\n── ANALYST NARRATIVE ─────────────────────────────────")
    print(f"  {analysis.analyst_narrative}")

    print("\n── ACTION ITEMS ──────────────────────────────────────")
    for i, item in enumerate(analysis.action_items, 1):
        print(f"  {i}. {item}")

    print("\n" + "=" * 70)


# ── Module-level singleton ────────────────────────────────────────
portfolio_analyser = PortfolioAnalyser()


# ── Standalone execution ──────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Running portfolio analysis...")

    # Check if real positions exist, else use demo data
    try:
        from risk.portfolio_tracker import portfolio_tracker
        positions = portfolio_tracker.get_open_positions()
    except Exception:
        positions = []

    if not positions:
        logger.info("No live positions — running with demo portfolio...")
        positions = [
            {"symbol": "NSE:HDFCBANK-EQ",  "entry_price": 1650, "position_size": 100,
             "unrealised_pnl": 3200,  "strategy": "TrendFollow"},
            {"symbol": "NSE:ICICIBANK-EQ", "entry_price": 1120, "position_size": 150,
             "unrealised_pnl": -1800, "strategy": "TrendFollow"},
            {"symbol": "NSE:TCS-EQ",       "entry_price": 3800, "position_size": 40,
             "unrealised_pnl": 6400,  "strategy": "TrendFollow"},
            {"symbol": "NSE:INFY-EQ",      "entry_price": 1750, "position_size": 80,
             "unrealised_pnl": 2100,  "strategy": "MeanReversion"},
            {"symbol": "NSE:SBIN-EQ",      "entry_price": 780,  "position_size": 200,
             "unrealised_pnl": -4200, "strategy": "TrendFollow"},
            {"symbol": "NSE:RELIANCE-EQ",  "entry_price": 2850, "position_size": 50,
             "unrealised_pnl": 7500,  "strategy": "TrendFollow"},
        ]

    analysis = portfolio_analyser.analyse(positions)
    print_full_report(analysis)