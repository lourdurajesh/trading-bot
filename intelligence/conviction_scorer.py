"""
conviction_scorer.py
────────────────────
Pre-market conviction score. Runs at 9:00 AM IST daily.
Combines 5 inputs into a single score (-10 to +10).
Only fires signal if abs(score) >= CONVICTION_THRESHOLD (default: 7).

Scoring model:
  FII F&O net change:           +3 bullish / -3 bearish  (MODULE 1)
  OI signal (PCR + gamma wall): +2 bullish / -2 bearish  (MODULE 2)
  India VIX direction:          +1 bullish / -1 bearish
  Gift Nifty overnight:         +1 bullish / -1 bearish
  BankNifty leadership:         +1 bullish / -1 bearish

Output: ConvictionScore(score, direction, reasons, capital_pct)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

_VIX_HISTORY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "vix_history.json")


@dataclass
class ConvictionScore:
    score: int                       # -10 to +10
    direction: str                   # BULLISH / BEARISH / NEUTRAL
    reasons: list[str]               # one line per signal explaining the score
    capital_pct: int                 # 35 (score 7-8) or 50 (score 9-10)
    tradeable: bool                  # abs(score) >= threshold
    timestamp: str = ""
    fii_score: int = 0
    oi_score: int = 0
    vix_score: int = 0
    gift_score: int = 0
    rs_score: int = 0


class ConvictionScorer:
    """
    Aggregates pre-market intelligence into a conviction score.

    Usage:
        scorer = ConvictionScorer()
        result = scorer.score()       # call at 09:00 AM IST
        if result.tradeable:
            # fire institutional_momentum strategy
    """

    def __init__(self):
        self._vix_history: list[dict] = []
        self._load_vix_history()
        self._last_score: Optional[ConvictionScore] = None

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def score(self, symbol: str = "BANKNIFTY") -> ConvictionScore:
        """
        Compute today's pre-market conviction score.
        Call at 9:00 AM IST (before market open) using previous day's data.
        """
        from config.settings import CONVICTION_THRESHOLD, MAX_FO_CAPITAL_PCT

        now = datetime.now(tz=IST)
        reasons: list[str] = []
        total = 0

        # ── Signal 1: FII F&O net change (+3/-3) ─────────────────
        fii_score, fii_reason = self._score_fii(symbol)
        total += fii_score
        reasons.append(f"FII [{fii_score:+d}]: {fii_reason}")

        # ── Signal 2: OI analysis PCR+gamma (+2/-2) ──────────────
        oi_score, oi_reason = self._score_oi(symbol)
        total += oi_score
        reasons.append(f"OI  [{oi_score:+d}]: {oi_reason}")

        # ── Signal 3: India VIX direction (+1/-1) ─────────────────
        vix_score, vix_reason = self._score_vix()
        total += vix_score
        reasons.append(f"VIX [{vix_score:+d}]: {vix_reason}")

        # ── Signal 4: Gift Nifty overnight (+1/-1) ────────────────
        gift_score, gift_reason = self._score_gift_nifty()
        total += gift_score
        reasons.append(f"GIFT[{gift_score:+d}]: {gift_reason}")

        # ── Signal 5: BankNifty leadership (+1/-1) ────────────────
        rs_score, rs_reason = self._score_relative_strength()
        total += rs_score
        reasons.append(f"RS  [{rs_score:+d}]: {rs_reason}")

        # ── Combine ───────────────────────────────────────────────
        total = max(-10, min(10, total))
        direction = "BULLISH" if total > 0 else ("BEARISH" if total < 0 else "NEUTRAL")
        tradeable = abs(total) >= CONVICTION_THRESHOLD

        capital_pct = 0
        if tradeable:
            capital_pct = MAX_FO_CAPITAL_PCT if abs(total) >= 9 else 35

        result = ConvictionScore(
            score       = total,
            direction   = direction,
            reasons     = reasons,
            capital_pct = capital_pct,
            tradeable   = tradeable,
            timestamp   = now.strftime("%Y-%m-%d %H:%M"),
            fii_score   = fii_score,
            oi_score    = oi_score,
            vix_score   = vix_score,
            gift_score  = gift_score,
            rs_score    = rs_score,
        )

        self._last_score = result
        self._log_score(result)
        return result

    def get_last_score(self) -> Optional[ConvictionScore]:
        """Return the most recently computed score (cached)."""
        return self._last_score

    def record_vix(self, vix: float) -> None:
        """Store today's VIX close. Call at market close daily."""
        now = datetime.now(tz=IST)
        self._vix_history.append({"date": now.date().isoformat(), "vix": vix})
        self._vix_history = self._vix_history[-252:]   # keep 1 year
        self._save_vix_history()

    # ─────────────────────────────────────────────────────────────
    # SIGNAL SCORERS
    # ─────────────────────────────────────────────────────────────

    def _score_fii(self, symbol: str) -> tuple[int, str]:
        """FII F&O net change signal from NSE participant collector."""
        try:
            from intelligence.nse_participant_collector import nse_participant_collector
            return nse_participant_collector.get_fii_signal("INDEX")
        except Exception as e:
            logger.warning(f"[ConvictionScorer] FII signal error: {e}")
            return 0, "FII data unavailable"

    def _score_oi(self, symbol: str) -> tuple[int, str]:
        """OI signal from previous-day close snapshot."""
        try:
            from analysis.oi_analyzer import oi_analyzer
            score, reason = oi_analyzer.get_oi_signal(symbol)
            # Clamp to +2/-2 max
            return max(-2, min(2, score)), reason
        except Exception as e:
            logger.warning(f"[ConvictionScorer] OI signal error: {e}")
            return 0, "OI data unavailable"

    def _score_vix(self) -> tuple[int, str]:
        """
        India VIX direction signal.
        VIX falling = fear decreasing = bullish (+1)
        VIX rising  = fear increasing = bearish (-1)
        Uses 5-day moving average to filter noise.
        """
        try:
            vix_data = self._vix_history[-6:] if len(self._vix_history) >= 6 else self._vix_history
            if len(vix_data) < 2:
                current_vix = self._fetch_live_vix()
                if current_vix:
                    if current_vix < 15:
                        return 1, f"VIX={current_vix:.1f} (low fear, bullish)"
                    elif current_vix > 22:
                        return -1, f"VIX={current_vix:.1f} (elevated fear, bearish)"
                    return 0, f"VIX={current_vix:.1f} (neutral)"
                return 0, "VIX history insufficient"

            recent = [d["vix"] for d in vix_data]
            current = recent[-1]
            avg_prev = sum(recent[:-1]) / len(recent[:-1])

            if current < avg_prev * 0.97:     # VIX down >3%
                return 1, f"VIX falling {avg_prev:.1f}→{current:.1f} (fear declining, bullish)"
            elif current > avg_prev * 1.03:   # VIX up >3%
                return -1, f"VIX rising {avg_prev:.1f}→{current:.1f} (fear rising, bearish)"
            else:
                return 0, f"VIX stable {current:.1f} (neutral)"
        except Exception as e:
            logger.warning(f"[ConvictionScorer] VIX score error: {e}")
            return 0, "VIX unavailable"

    def _score_gift_nifty(self) -> tuple[int, str]:
        """
        Gift Nifty overnight premium signal.
        Compares SGX/GIFT Nifty futures price to Nifty prev close.
        Uses sp500_change_pct from macro as a US overnight proxy when Gift Nifty unavailable.
        """
        try:
            from intelligence.macro_data import macro_collector
            macro = macro_collector.get_snapshot()

            # Primary: use sp500_change_pct as US overnight proxy.
            # Gift Nifty tracks US markets overnight closely; S&P 500 +0.5% ≈ Nifty gap-up.
            sp500_chg = getattr(macro, "sp500_change_pct", 0.0)
            if sp500_chg != 0.0:
                if sp500_chg > 0.5:
                    return 1, f"US S&P500 +{sp500_chg:.1f}% overnight (positive gap-up proxy)"
                elif sp500_chg < -0.5:
                    return -1, f"US S&P500 {sp500_chg:.1f}% overnight (negative gap-down proxy)"
                else:
                    return 0, f"US overnight {sp500_chg:+.1f}% (neutral)"

            return 0, "Overnight signal unavailable"
        except Exception as e:
            logger.warning(f"[ConvictionScorer] Gift Nifty score error: {e}")
            return 0, "Overnight signal unavailable"

    def _score_relative_strength(self) -> tuple[int, str]:
        """
        BankNifty vs Nifty 5-day relative strength.
        BankNifty outperforming = financials leading = bullish (+1).
        Falls back to historical CSVs when live data is unavailable.
        """
        try:
            from data.data_store import store
            bn_df = store.get_ohlcv("NSE:NIFTYBANK-INDEX", "1D", n=10)
            nf_df = store.get_ohlcv("NSE:NIFTY50-INDEX",   "1D", n=10)

            # Fallback: read from local historical CSVs
            if bn_df is None or len(bn_df) < 5:
                bn_df = self._load_csv_ohlcv("NSE_NIFTYBANK_INDEX_1D.csv")
            if nf_df is None or len(nf_df) < 5:
                nf_df = self._load_csv_ohlcv("NSE_NIFTY50_INDEX_1D.csv")

            if bn_df is None or nf_df is None or len(bn_df) < 5 or len(nf_df) < 5:
                return 0, "Index data unavailable for relative strength"

            bn_ret = (bn_df["close"].iloc[-1] / bn_df["close"].iloc[-5] - 1) * 100
            nf_ret = (nf_df["close"].iloc[-1] / nf_df["close"].iloc[-5] - 1) * 100
            rs = bn_ret - nf_ret

            if rs > 0.5:
                return 1, f"BankNifty +{rs:.1f}% vs Nifty 5d (financials leading, bullish)"
            elif rs < -0.5:
                return -1, f"BankNifty {rs:.1f}% vs Nifty 5d (financials lagging, bearish)"
            else:
                return 0, f"BankNifty RS={rs:+.1f}% vs Nifty (neutral)"
        except Exception as e:
            logger.warning(f"[ConvictionScorer] RS score error: {e}")
            return 0, "Relative strength unavailable"

    def _load_csv_ohlcv(self, filename: str):
        """Load OHLCV from local historical CSV as fallback for data_store."""
        import pandas as pd
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "historical", filename)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_csv(path)
            df.columns = [c.lower() for c in df.columns]
            if "close" not in df.columns:
                return None
            return df.tail(20)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _fetch_live_vix(self) -> Optional[float]:
        """Fetch India VIX from macro_collector."""
        try:
            from intelligence.macro_data import macro_collector
            macro = macro_collector.get_snapshot()
            vix = getattr(macro, "nifty_vix", 0)
            return vix if vix > 0 else None
        except Exception:
            return None

    def _log_score(self, result: ConvictionScore) -> None:
        status = "TRADEABLE" if result.tradeable else "below threshold"
        logger.info(
            f"[ConvictionScorer] Score={result.score:+d} | {result.direction} | {status} "
            f"(capital={result.capital_pct}%)"
        )
        for reason in result.reasons:
            logger.info(f"[ConvictionScorer]   {reason}")

    def _load_vix_history(self) -> None:
        try:
            if os.path.exists(_VIX_HISTORY_PATH):
                with open(_VIX_HISTORY_PATH) as f:
                    self._vix_history = json.load(f)
                logger.info(f"[ConvictionScorer] Loaded {len(self._vix_history)} VIX history rows")
        except Exception as e:
            logger.warning(f"[ConvictionScorer] Could not load VIX history: {e}")
            self._vix_history = []

    def _save_vix_history(self) -> None:
        try:
            os.makedirs(os.path.dirname(_VIX_HISTORY_PATH), exist_ok=True)
            with open(_VIX_HISTORY_PATH, "w") as f:
                json.dump(self._vix_history, f, indent=2)
        except Exception as e:
            logger.warning(f"[ConvictionScorer] Could not save VIX history: {e}")


# ── Module-level singleton ────────────────────────────────────────
conviction_scorer = ConvictionScorer()
