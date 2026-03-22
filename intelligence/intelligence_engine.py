"""
intelligence_engine.py
──────────────────────
Orchestrates all 4 intelligence layers in parallel.
Called by strategy_selector for every signal before order submission.

Flow:
  Signal received
    → Layer 1: News scraper       (parallel)
    → Layer 2: Macro data         (parallel, cached)
    → Layer 3: Fundamental guard  (parallel)
    → Layer 4: Claude analyst     (uses output of 1-3)
  → IntelligenceResult returned
  → strategy_selector uses result to approve/reject/resize signal
"""

import logging
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from intelligence.news_scraper import get_news_for_symbol, NewsItem
from intelligence.macro_data import macro_collector, MacroSnapshot
from intelligence.fundamental_guard import fundamental_guard, FundamentalRisk
from intelligence.analyst_agent import analyst_agent, AnalystVerdict

logger = logging.getLogger(__name__)

# Max time to wait for all intelligence layers (seconds)
INTELLIGENCE_TIMEOUT = 20


@dataclass
class IntelligenceResult:
    symbol:       str
    approved:     bool
    verdict:      str              # APPROVE | REJECT | REDUCE_SIZE
    conviction:   float            # 0-10
    size_factor:  float = 1.0      # 1.0 = full size, 0.5 = half size, 0.0 = rejected

    # Layer outputs
    news_items:   list  = field(default_factory=list)
    macro:        Optional[MacroSnapshot]  = None
    fundamental:  Optional[FundamentalRisk] = None
    analyst:      Optional[AnalystVerdict]  = None

    # Summary for dashboard and alerts
    summary:      str   = ""
    duration_ms:  int   = 0


class IntelligenceEngine:
    """
    Runs all intelligence layers in parallel and returns a combined verdict.

    Usage:
        result = intelligence_engine.evaluate(signal)
        if not result.approved:
            return   # trade blocked
        signal.position_size = int(signal.position_size * result.size_factor)
    """

    def evaluate(self, signal) -> IntelligenceResult:
        """
        Run all 4 intelligence layers and return combined verdict.
        Designed to complete in under 20 seconds.
        """
        start = datetime.now(tz=timezone.utc)
        logger.info(f"[Intelligence] Evaluating {signal.symbol}...")

        # ── Run layers 1-3 in parallel ────────────────────────────
        news_items   = []
        macro        = None
        fundamental  = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(get_news_for_symbol, signal.symbol):    "news",
                executor.submit(macro_collector.get_snapshot):           "macro",
                executor.submit(fundamental_guard.check, signal.symbol): "fundamental",
            }
            for future in concurrent.futures.as_completed(futures, timeout=INTELLIGENCE_TIMEOUT):
                layer = futures[future]
                try:
                    result = future.result()
                    if layer == "news":
                        news_items = result
                    elif layer == "macro":
                        macro = result
                    elif layer == "fundamental":
                        fundamental = result
                except Exception as e:
                    logger.warning(f"[Intelligence] Layer '{layer}' failed: {e}")

        # Use defaults if any layer failed
        if macro is None:
            from intelligence.macro_data import MacroSnapshot
            macro = MacroSnapshot()
            logger.warning("[Intelligence] Macro data unavailable — using neutral defaults")

        if fundamental is None:
            from intelligence.fundamental_guard import FundamentalRisk
            fundamental = FundamentalRisk(symbol=signal.symbol)
            logger.warning("[Intelligence] Fundamental check unavailable — proceeding")

        # ── Hard veto from fundamental guard ─────────────────────
        if fundamental.veto:
            duration = int((datetime.now(tz=timezone.utc) - start).total_seconds() * 1000)
            result = IntelligenceResult(
                symbol      = signal.symbol,
                approved    = False,
                verdict     = "REJECT",
                conviction  = 0.0,
                size_factor = 0.0,
                news_items  = news_items,
                macro       = macro,
                fundamental = fundamental,
                summary     = f"VETOED: {fundamental.veto_reason}",
                duration_ms = duration,
            )
            logger.info(f"[Intelligence] {signal.symbol} → VETOED by fundamental guard")
            return result

        # ── Macro hard brake ─────────────────────────────────────
        # If macro is very bearish AND VIX is panicking, block ALL new trades
        if macro.macro_score < -6 and macro.vix_signal == "panic":
            duration = int((datetime.now(tz=timezone.utc) - start).total_seconds() * 1000)
            result = IntelligenceResult(
                symbol      = signal.symbol,
                approved    = False,
                verdict     = "REJECT",
                conviction  = 1.0,
                size_factor = 0.0,
                news_items  = news_items,
                macro       = macro,
                fundamental = fundamental,
                summary     = f"VETOED: Macro panic — VIX {macro.nifty_vix:.1f}, score {macro.macro_score}",
                duration_ms = duration,
            )
            logger.info(f"[Intelligence] {signal.symbol} → VETOED by macro panic brake")
            return result

        # ── Layer 4: Claude analyst ───────────────────────────────
        analyst_verdict = analyst_agent.analyse(signal, news_items, macro, fundamental)

        # ── Combine into final verdict ────────────────────────────
        duration = int((datetime.now(tz=timezone.utc) - start).total_seconds() * 1000)

        approved    = analyst_verdict.verdict in ("APPROVE", "REDUCE_SIZE")
        size_factor = 1.0
        if analyst_verdict.verdict == "REDUCE_SIZE":
            size_factor = 0.5
        elif analyst_verdict.verdict == "REJECT":
            size_factor = 0.0
            approved    = False

        summary = self._build_summary(signal, analyst_verdict, macro, fundamental, news_items)

        result = IntelligenceResult(
            symbol      = signal.symbol,
            approved    = approved,
            verdict     = analyst_verdict.verdict,
            conviction  = analyst_verdict.conviction,
            size_factor = size_factor,
            news_items  = news_items,
            macro       = macro,
            fundamental = fundamental,
            analyst     = analyst_verdict,
            summary     = summary,
            duration_ms = duration,
        )

        logger.info(
            f"[Intelligence] {signal.symbol} → {analyst_verdict.verdict} "
            f"(conviction: {analyst_verdict.conviction:.1f}/10, "
            f"size: {size_factor:.0%}, {duration}ms)"
        )
        return result

    def _build_summary(self, signal, analyst, macro, fundamental, news_items) -> str:
        """Build human-readable summary for dashboard and Telegram alerts."""
        parts = [
            f"Verdict: {analyst.verdict} (conviction {analyst.conviction:.1f}/10)",
            f"Macro: {macro.summary}",
        ]
        if fundamental.notes and fundamental.notes != "No fundamental concerns":
            parts.append(f"Fundamentals: {fundamental.notes}")
        if news_items:
            parts.append(f"News: {len(news_items)} items — {news_items[0].headline[:60]}...")
        if analyst.analyst_notes:
            parts.append(f"Analyst: {analyst.analyst_notes[:150]}")
        return "\n".join(parts)


# ── Module-level singleton ────────────────────────────────────────
intelligence_engine = IntelligenceEngine()
