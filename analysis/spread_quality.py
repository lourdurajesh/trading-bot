"""
spread_quality.py
─────────────────
Spec TASK 9: Spread Quality Engine.

Scores a debit spread on 5 dimensions before entry:
  1. bid_ask_score       — penalises wide bid-ask (slippage risk)
  2. liquidity_score     — OI + volume (can we exit cleanly?)
  3. rr_score            — risk/reward vs minimum threshold
  4. theta_efficiency    — daily theta relative to debit paid
  5. premium_efficiency  — potential return on risk

Overall spread_quality_score: 0.0 – 10.0.
Spreads scoring below QUALITY_THRESHOLD are rejected with a reason.

Feature flag: SPREAD_QUALITY_ENABLED (default True).
Fails open (approves) if calculation errors — never blocks a trade silently.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

ENABLED           = os.getenv("SPREAD_QUALITY_ENABLED", "true").lower() != "false"
QUALITY_THRESHOLD = float(os.getenv("SPREAD_QUALITY_THRESHOLD", "4.5"))   # reject below this

# Minimum liquidity requirements
MIN_OI_LONG   = 500    # minimum OI on the long (ATM) leg
MIN_OI_SHORT  = 300    # minimum OI on the short (OTM) leg
MIN_RR        = 0.8    # minimum risk/reward (max_profit / net_debit)
MAX_SPREAD_PCT = 0.15  # max bid-ask spread as % of mid-price (15%) on either leg


@dataclass
class LegData:
    """Data for one option leg."""
    bid:    float = 0.0
    ask:    float = 0.0
    oi:     int   = 0
    volume: int   = 0
    delta:  float = 0.0
    theta:  float = 0.0     # daily theta (negative for long options)
    vega:   float = 0.0
    gamma:  float = 0.0


@dataclass
class SpreadQuality:
    quality_score:       float = 0.0      # 0–10; >= QUALITY_THRESHOLD = approved
    approved:            bool  = True
    rejection_reason:    str   = ""

    # Individual dimension scores (0–10 each)
    bid_ask_score:       float = 0.0
    liquidity_score:     float = 0.0
    rr_score:            float = 0.0
    theta_score:         float = 0.0
    premium_score:       float = 0.0

    # Derived metrics for audit
    long_spread_pct:     float = 0.0      # bid-ask spread as % of mid for long leg
    short_spread_pct:    float = 0.0
    net_delta:           float = 0.0
    net_theta:           float = 0.0
    net_vega:            float = 0.0
    theta_per_day_pct:   float = 0.0      # daily theta as % of debit
    max_return_pct:      float = 0.0      # max_profit / net_debit × 100

    def to_dict(self) -> dict:
        return {
            "quality_score":     round(self.quality_score, 2),
            "approved":          self.approved,
            "rejection_reason":  self.rejection_reason,
            "bid_ask_score":     round(self.bid_ask_score, 1),
            "liquidity_score":   round(self.liquidity_score, 1),
            "rr_score":          round(self.rr_score, 1),
            "theta_score":       round(self.theta_score, 1),
            "premium_score":     round(self.premium_score, 1),
            "net_delta":         round(self.net_delta, 3),
            "net_theta":         round(self.net_theta, 3),
            "net_vega":          round(self.net_vega, 3),
            "theta_per_day_pct": round(self.theta_per_day_pct, 2),
            "max_return_pct":    round(self.max_return_pct, 1),
        }


class SpreadQualityEngine:
    """
    Evaluates spread quality before entry.

    Usage:
        from analysis.spread_quality import spread_quality_engine, LegData

        long_leg  = LegData(bid=50, ask=55, oi=8000, volume=500, delta=0.45, theta=-2.5, vega=15)
        short_leg = LegData(bid=20, ask=23, oi=5000, volume=200, delta=0.25, theta=-1.2, vega=8)
        result = spread_quality_engine.evaluate(long_leg, short_leg, net_debit=32.5, max_profit=17.5)

        if not result.approved:
            logger.info(f"Spread rejected: {result.rejection_reason}")
    """

    def evaluate(
        self,
        long_leg:   LegData,
        short_leg:  LegData,
        net_debit:  float,
        max_profit: float,
    ) -> SpreadQuality:
        """
        Score the spread. Returns SpreadQuality with approved=True/False.
        On calculation error, returns approved=True (fail-open) to avoid
        silently blocking all trades if the engine has a bug.
        """
        if not ENABLED:
            return SpreadQuality(quality_score=7.0, approved=True,
                                 rejection_reason="spread quality engine disabled")
        try:
            return self._score(long_leg, short_leg, net_debit, max_profit)
        except Exception as e:
            logger.warning(f"[SpreadQuality] Scoring failed (fail-open): {e}")
            return SpreadQuality(quality_score=5.0, approved=True,
                                 rejection_reason=f"scoring_error: {e}")

    # ── Scoring ──────────────────────────────────────────────────

    def _score(
        self,
        long_leg:   LegData,
        short_leg:  LegData,
        net_debit:  float,
        max_profit: float,
    ) -> SpreadQuality:
        result = SpreadQuality()

        # ── 1. Bid-Ask quality ────────────────────────────────────
        long_mid  = (long_leg.bid  + long_leg.ask)  / 2  if long_leg.ask  > 0 else 1
        short_mid = (short_leg.bid + short_leg.ask) / 2  if short_leg.ask > 0 else 1
        long_spd_pct  = (long_leg.ask  - long_leg.bid)  / long_mid  if long_mid  > 0 else 1
        short_spd_pct = (short_leg.ask - short_leg.bid) / short_mid if short_mid > 0 else 1
        result.long_spread_pct  = round(long_spd_pct * 100, 2)
        result.short_spread_pct = round(short_spd_pct * 100, 2)

        worst_spread = max(long_spd_pct, short_spd_pct)
        # Score: 10 at 0% spread, 0 at MAX_SPREAD_PCT or beyond
        result.bid_ask_score = max(0.0, 10.0 * (1 - worst_spread / MAX_SPREAD_PCT))

        # Hard reject: extremely wide spread on either leg
        if worst_spread > MAX_SPREAD_PCT * 2:
            result.approved = False
            result.rejection_reason = (
                f"bid-ask too wide: long={result.long_spread_pct:.1f}% "
                f"short={result.short_spread_pct:.1f}% (max {MAX_SPREAD_PCT*100:.0f}%)"
            )
            result.quality_score = result.bid_ask_score
            return result

        # ── 2. Liquidity (OI) ─────────────────────────────────────
        long_oi_score  = min(10.0, long_leg.oi  / MIN_OI_LONG  * 5)
        short_oi_score = min(10.0, short_leg.oi / MIN_OI_SHORT * 5)
        result.liquidity_score = (long_oi_score + short_oi_score) / 2

        if long_leg.oi < MIN_OI_LONG or short_leg.oi < MIN_OI_SHORT:
            result.approved = False
            result.rejection_reason = (
                f"insufficient OI: long={long_leg.oi} (min {MIN_OI_LONG}), "
                f"short={short_leg.oi} (min {MIN_OI_SHORT})"
            )
            result.quality_score = (
                result.bid_ask_score * 0.4 + result.liquidity_score * 0.6
            ) / 2
            return result

        # ── 3. Risk-Reward score ──────────────────────────────────
        rr = max_profit / net_debit if net_debit > 0 else 0
        result.max_return_pct = round(rr * 100, 1)
        # Score: 10 at 2:1 RR, 5 at 1:1, 0 below MIN_RR
        result.rr_score = max(0.0, min(10.0, (rr - MIN_RR) / (2.0 - MIN_RR) * 10))

        if rr < MIN_RR:
            result.approved = False
            result.rejection_reason = f"R:R {rr:.2f} below minimum {MIN_RR}"
            result.quality_score = (result.bid_ask_score + result.liquidity_score) / 2
            return result

        # ── 4. Theta efficiency ───────────────────────────────────
        # Net theta of the spread (long pays theta, short receives it)
        # For a debit spread: net_theta = short_leg.theta - long_leg.theta
        # Theta values are typically negative for long options.
        net_theta = short_leg.theta - long_leg.theta   # net daily theta
        result.net_theta = round(net_theta, 3)
        # theta_per_day_pct: what % of the debit is lost to theta per day
        theta_per_day = abs(net_theta) / net_debit * 100 if net_debit > 0 else 0
        result.theta_per_day_pct = round(theta_per_day, 2)
        # Score: 10 if theta decay < 1% per day; 0 if > 5% per day
        result.theta_score = max(0.0, 10.0 * (1 - theta_per_day / 5.0))

        # ── 5. Premium efficiency ─────────────────────────────────
        result.net_delta = round(long_leg.delta - short_leg.delta, 3)
        result.net_vega  = round(long_leg.vega  - short_leg.vega,  3)
        # Premium efficiency = max_return as % of debit vs target (50% gain = good)
        # Score: 10 at >=80% max return, 5 at 50%, 0 at 0%
        result.premium_score = min(10.0, rr * 10 / 2)

        # ── Weighted overall score ─────────────────────────────────
        result.quality_score = round(
            result.bid_ask_score   * 0.25 +
            result.liquidity_score * 0.25 +
            result.rr_score        * 0.25 +
            result.theta_score     * 0.15 +
            result.premium_score   * 0.10,
            2
        )

        if result.quality_score < QUALITY_THRESHOLD:
            result.approved = False
            result.rejection_reason = (
                f"quality score {result.quality_score:.1f} below threshold {QUALITY_THRESHOLD} "
                f"(bid-ask={result.bid_ask_score:.1f} liq={result.liquidity_score:.1f} "
                f"rr={result.rr_score:.1f} theta={result.theta_score:.1f})"
            )
        else:
            result.approved = True

        return result


# Singleton
spread_quality_engine = SpreadQualityEngine()
