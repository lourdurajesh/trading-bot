"""
analyst_agent.py
────────────────
The brain of the intelligence layer.
Calls Claude API to analyse a trade signal like a senior analyst.

When ANTHROPIC_API_KEY is set   → calls Claude API for real analysis
When key is not set             → runs in simulation mode (logs what it would do)

Claude receives:
  - Technical signal details
  - Recent news (from news_scraper)
  - Macro snapshot (from macro_data)
  - Fundamental risk (from fundamental_guard)

Claude returns a structured JSON verdict:
  {
    "conviction":    0-10,
    "verdict":       "APPROVE" | "REJECT" | "REDUCE_SIZE",
    "bull_case":     "...",
    "bear_case":     "...",
    "key_risks":     ["...", "..."],
    "analyst_notes": "..."
  }
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from config.settings import TOTAL_CAPITAL

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = "claude-sonnet-4-20250514"
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MAX_TOKENS        = 1000


@dataclass
class AnalystVerdict:
    conviction:    float = 5.0          # 0-10
    verdict:       str   = "APPROVE"    # APPROVE | REJECT | REDUCE_SIZE
    bull_case:     str   = ""
    bear_case:     str   = ""
    key_risks:     list  = field(default_factory=list)
    analyst_notes: str   = ""
    simulated:     bool  = False        # True if API key not set
    raw_response:  str   = ""


class AnalystAgent:
    """
    LLM-powered trade analyst.

    Usage:
        verdict = analyst_agent.analyse(signal, news_items, macro, fundamental_risk)
        if verdict.verdict == "REJECT":
            # block the trade
    """

    def __init__(self):
        self._enabled = bool(ANTHROPIC_API_KEY)
        if not self._enabled:
            logger.info(
                "[Analyst] ANTHROPIC_API_KEY not set — running in simulation mode. "
                "Add key to .env to activate real analysis."
            )

    def analyse(
        self,
        signal,
        news_items:    list,
        macro,
        fundamental:   object,
    ) -> AnalystVerdict:
        """
        Run full analyst evaluation on a trade signal.
        Returns AnalystVerdict with conviction score and verdict.
        """
        if not self._enabled:
            return self._simulate(signal, news_items, macro, fundamental)

        prompt = self._build_prompt(signal, news_items, macro, fundamental)

        try:
            response = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      ANTHROPIC_MODEL,
                    "max_tokens": MAX_TOKENS,
                    "system":     self._system_prompt(),
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            response.raise_for_status()
            data    = response.json()
            content = data["content"][0]["text"]
            verdict = self._parse_response(content)
            logger.info(
                f"[Analyst] {signal.symbol} → {verdict.verdict} "
                f"(conviction: {verdict.conviction:.1f}/10)"
            )
            return verdict

        except Exception as e:
            logger.error(f"[Analyst] API call failed: {e} — defaulting to APPROVE")
            return AnalystVerdict(
                conviction    = 5.0,
                verdict       = "APPROVE",
                analyst_notes = f"API error — technical signal used without analyst confirmation: {e}",
            )

    # ─────────────────────────────────────────────────────────────
    # PROMPT BUILDING
    # ─────────────────────────────────────────────────────────────

    def _system_prompt(self) -> str:
        return """You are a senior equity analyst at a top Mumbai hedge fund with 20 years of experience 
trading Indian markets. You specialise in NSE large-cap equities and derivatives.

Your job is to evaluate a trade signal and give a structured verdict.

ALWAYS respond with ONLY valid JSON in exactly this format — no markdown, no explanation outside the JSON:
{
  "conviction": <0-10 float>,
  "verdict": "<APPROVE|REJECT|REDUCE_SIZE>",
  "bull_case": "<one sentence>",
  "bear_case": "<one sentence>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "analyst_notes": "<2-3 sentence summary of your reasoning>"
}

Verdict rules:
- APPROVE if conviction >= 6 and no critical red flags
- REDUCE_SIZE if conviction 4-6 or minor concerns (position size will be halved)
- REJECT if conviction < 4 or critical red flags (earnings, news shock, macro headwind)"""

    def _build_prompt(self, signal, news_items, macro, fundamental) -> str:
        # Format news items
        news_text = ""
        if news_items:
            news_text = "\n".join([
                f"  [{item.source}] {item.headline} ({item.published.strftime('%d %b %H:%M')})"
                for item in news_items[:8]
            ])
        else:
            news_text = "  No recent news found"

        # Format macro
        macro_text = (
            f"  VIX: {macro.nifty_vix:.1f} ({macro.vix_signal})\n"
            f"  FII net flow: ₹{macro.fii_net_flow:+,.0f} Cr ({macro.fii_signal})\n"
            f"  S&P 500: {macro.sp500_change_pct:+.1f}% ({macro.global_signal})\n"
            f"  Crude oil: ${macro.crude_oil_usd:.1f} ({macro.crude_signal})\n"
            f"  USD/INR: {macro.usdinr:.2f}\n"
            f"  Overall macro score: {macro.macro_score:+.1f}/10"
        )

        # Format fundamental
        fund_text = (
            f"  Days to earnings: {fundamental.days_to_earnings if fundamental.days_to_earnings < 999 else 'Not imminent'}\n"
            f"  Upcoming corporate actions: {', '.join(fundamental.upcoming_actions) or 'None'}\n"
            f"  Fundamental score: {fundamental.fundamental_score:.1f}/10\n"
            f"  Notes: {fundamental.notes}"
        )

        return f"""Evaluate this trade signal for an Indian equity swing trade:

TRADE SIGNAL:
  Symbol:     {signal.symbol}
  Direction:  {signal.direction.value}
  Strategy:   {signal.strategy}
  Entry:      ₹{signal.entry:,.2f}
  Stop Loss:  ₹{signal.stop_loss:,.2f}
  Target 1:   ₹{signal.target_1:,.2f}
  Target 2:   ₹{signal.target_2:,.2f}
  R:R Ratio:  {signal.risk_reward:.1f}
  Timeframe:  {signal.timeframe}
  Regime:     {signal.regime}
  Technical confidence: {signal.confidence:.0%}
  Signal reason: {signal.reason}

RECENT NEWS & SENTIMENT:
{news_text}

MACRO ENVIRONMENT:
{macro_text}

FUNDAMENTAL CHECK:
{fund_text}

Based on all of the above, should this trade be taken?
Respond ONLY with the JSON verdict."""

    # ─────────────────────────────────────────────────────────────
    # RESPONSE PARSING
    # ─────────────────────────────────────────────────────────────

    def _parse_response(self, content: str) -> AnalystVerdict:
        """Parse Claude's JSON response into AnalystVerdict."""
        try:
            # Strip any accidental markdown
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            data = json.loads(content)
            return AnalystVerdict(
                conviction    = float(data.get("conviction", 5)),
                verdict       = data.get("verdict", "APPROVE").upper(),
                bull_case     = data.get("bull_case", ""),
                bear_case     = data.get("bear_case", ""),
                key_risks     = data.get("key_risks", []),
                analyst_notes = data.get("analyst_notes", ""),
                raw_response  = content,
            )
        except Exception as e:
            logger.error(f"[Analyst] Failed to parse response: {e}\nContent: {content[:200]}")
            return AnalystVerdict(
                conviction    = 5.0,
                verdict       = "APPROVE",
                analyst_notes = "Parse error — defaulting to approve",
            )

    # ─────────────────────────────────────────────────────────────
    # SIMULATION MODE
    # ─────────────────────────────────────────────────────────────

    def _simulate(self, signal, news_items, macro, fundamental) -> AnalystVerdict:
        """
        Simulation mode — applies simple rules to estimate what Claude would say.
        Useful for testing the pipeline without an API key.
        """
        conviction = signal.confidence * 10   # start from technical confidence

        # Macro adjustment
        conviction += macro.macro_score * 0.3

        # Fundamental adjustment
        if fundamental.veto:
            return AnalystVerdict(
                conviction    = 1.0,
                verdict       = "REJECT",
                bull_case     = "Technical setup is valid",
                bear_case     = fundamental.veto_reason,
                key_risks     = [fundamental.veto_reason],
                analyst_notes = f"[SIMULATION] Auto-rejected: {fundamental.veto_reason}",
                simulated     = True,
            )

        conviction += (fundamental.fundamental_score - 5) * 0.2

        # News adjustment (simple keyword scan)
        negative_keywords = ["fraud", "probe", "sebi", "loss", "resign", "raid", "default"]
        positive_keywords = ["profit", "order", "acquisition", "revenue", "beat", "growth"]

        for item in news_items[:5]:
            text = (item.headline + " " + item.summary).lower()
            for kw in negative_keywords:
                if kw in text:
                    conviction -= 1.5
            for kw in positive_keywords:
                if kw in text:
                    conviction += 0.5

        conviction = round(max(0, min(10, conviction)), 1)

        if conviction >= 6:
            verdict = "APPROVE"
        elif conviction >= 4:
            verdict = "REDUCE_SIZE"
        else:
            verdict = "REJECT"

        return AnalystVerdict(
            conviction    = conviction,
            verdict       = verdict,
            bull_case     = f"Technical setup valid: {signal.reason[:100]}",
            bear_case     = f"Macro score {macro.macro_score:+.1f}, monitor closely",
            key_risks     = [
                f"VIX at {macro.nifty_vix:.1f} ({macro.vix_signal})",
                f"FII flow: ₹{macro.fii_net_flow:+,.0f}Cr",
            ],
            analyst_notes = (
                f"[SIMULATION MODE] Conviction {conviction:.1f}/10. "
                f"Add ANTHROPIC_API_KEY to .env for real analyst intelligence. "
                f"Macro: {macro.summary}"
            ),
            simulated     = True,
        )


# ── Module-level singleton ────────────────────────────────────────
analyst_agent = AnalystAgent()
