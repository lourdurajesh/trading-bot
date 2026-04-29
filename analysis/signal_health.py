"""
signal_health.py
────────────────
Tracks WHY signals are not firing and surfaces action points.

Two components:

1. SkipCollector  — strategies call record() from log_skip(); collects
   per-symbol skip reasons each cycle.

2. SignalHealthMonitor — reads the collector each cycle, aggregates
   blocking patterns, tracks drought length, and emits structured
   INFO logs + a JSON snapshot the dashboard can poll.

Usage (automatic — wired into base_strategy.log_skip and
       strategy_selector.run_cycle):

   # in strategy_selector after run_cycle:
   health_monitor.update(skip_collector.flush(), signals_fired=len(signals))

   # from dashboard:
   health_monitor.snapshot()   → dict
"""

import logging
import threading
from collections import Counter, defaultdict
from datetime import datetime, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# SKIP COLLECTOR  (singleton written to by every strategy)
# ─────────────────────────────────────────────────────────────────

class SkipCollector:
    """
    Thread-safe bucket that strategies drop skip reasons into each cycle.
    strategy_selector flushes it after every run_cycle() call.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._reasons: list[dict] = []

    def record(self, symbol: str, strategy: str, reason: str) -> None:
        with self._lock:
            self._reasons.append({
                "symbol":   symbol,
                "strategy": strategy,
                "reason":   reason,
                "ts":       datetime.now(tz=IST).isoformat(),
            })

    def flush(self) -> list[dict]:
        """Returns accumulated reasons and clears the buffer."""
        with self._lock:
            data, self._reasons = self._reasons, []
        return data


skip_collector = SkipCollector()


# ─────────────────────────────────────────────────────────────────
# REASON BUCKETING  (maps verbose reason strings → category codes)
# ─────────────────────────────────────────────────────────────────

def _categorise(reason: str) -> str:
    r = reason.lower()
    if "ema" in r and ("align" in r or "bullish" in r or "bearish" in r or "bias" in r):
        return "ema_misalign"
    if "regime" in r:
        return "wrong_regime"
    if "rsi" in r and ("oversold" not in r and "overbought" not in r):
        return "rsi_neutral"
    if "oversold" in r or "overbought" in r:
        return "rsi_extreme_needed"
    if "bollinger" in r or "bb" in r or "reversion" in r or "near" in r:
        return "no_bb_touch"
    if "iv rank" in r or "iv_rank" in r:
        return "iv_rank_block"
    if "breakout" in r or "close" in r and "high" in r:
        return "no_breakout"
    if "volume" in r or "rvol" in r:
        return "low_volume"
    if "adx" in r:
        return "weak_trend"
    if "confidence" in r:
        return "low_confidence"
    if "r:r" in r or "risk" in r and "reward" in r:
        return "poor_rr"
    if "market" in r and "hour" in r:
        return "outside_hours"
    if "blackout" in r:
        return "opening_blackout"
    if "data" in r or "insufficient" in r:
        return "insufficient_data"
    if "debit" in r:
        return "bad_debit_cost"
    return "other"


# ─────────────────────────────────────────────────────────────────
# SIGNAL HEALTH MONITOR  (singleton — dashboard reads from this)
# ─────────────────────────────────────────────────────────────────

# Human-readable action points for each category
_ACTION_HINTS: dict[str, str] = {
    "ema_misalign":       "EMA stack misaligned — market in transition after a trend change. "
                          "Wait for 9/21/50 to re-stack or widen the entry gate.",
    "wrong_regime":       "Regime doesn't match strategy — TrendFollow won't fire in RANGING, "
                          "MeanReversion won't fire in TRENDING.",
    "rsi_neutral":        "RSI is neutral (40-60) — market consolidating, no extreme readings "
                          "for mean reversion entries.",
    "rsi_extreme_needed": "RSI threshold not reached — price not at Bollinger Band extremes.",
    "no_bb_touch":        "Price not near Bollinger Bands — market in the middle of the range, "
                          "not at mean-reversion extremes.",
    "iv_rank_block":      "IV rank outside strategy's required window — check VIX proxy history "
                          "and consider building real IV history.",
    "no_breakout":        "No price breakout above recent high — momentum not confirmed.",
    "low_volume":         "Relative volume below threshold — moves not backed by participation.",
    "weak_trend":         "ADX too low — no strong directional move in place.",
    "low_confidence":     "Signal confidence below minimum threshold — setup quality too low.",
    "poor_rr":            "Risk:Reward ratio too low — stop too tight or target too close.",
    "outside_hours":      "Options strategy outside market hours (9:15–15:30 IST).",
    "opening_blackout":   "Opening blackout active (9:15–9:44) — waiting for range to form.",
    "insufficient_data":  "Not enough historical bars — data warmup still in progress.",
    "bad_debit_cost":     "Options debit cost invalid — check chain fetch / pricing data.",
    "other":              "Uncategorised skip — enable DEBUG logging to see the exact reason.",
}


class SignalHealthMonitor:
    """
    Aggregates skip reasons across cycles and tracks trading drought.
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self._cycle_count   = 0
        self._last_trade_ts: datetime | None = None
        self._last_trade_date: date | None   = None
        self._drought_days  = 0
        self._drought_cycles = 0

        # Rolling window: last 30 cycles of category counts
        self._recent_categories: list[Counter] = []
        self._window = 30

        # Per-symbol last skip reasons (for detailed view)
        self._symbol_last_skip: dict[str, dict] = {}  # symbol → {reason, category, ts}

        # Persistent totals (reset on midnight)
        self._today_date        = date.today()
        self._today_skip_cats   = Counter()
        self._today_signals_fired = 0

    # ── called by strategy_selector every cycle ───────────────────

    def update(self, skip_records: list[dict], signals_fired: int = 0) -> None:
        with self._lock:
            self._cycle_count += 1
            now = datetime.now(tz=IST)

            # Day rollover
            today = date.today()
            if today != self._today_date:
                self._today_date        = today
                self._today_skip_cats   = Counter()
                self._today_signals_fired = 0

            if signals_fired > 0:
                self._last_trade_ts   = now
                self._last_trade_date = today
                self._drought_cycles  = 0
            else:
                self._drought_cycles += 1

            self._today_signals_fired += signals_fired

            # Calculate drought in calendar days
            if self._last_trade_date:
                self._drought_days = (today - self._last_trade_date).days
            else:
                self._drought_days = 0

            # Categorise and record skip reasons
            cycle_cats = Counter()
            for rec in skip_records:
                cat = _categorise(rec["reason"])
                cycle_cats[cat] += 1
                self._today_skip_cats[cat] += 1
                sym = rec["symbol"]
                self._symbol_last_skip[sym] = {
                    "reason":   rec["reason"],
                    "category": cat,
                    "strategy": rec["strategy"],
                    "ts":       rec["ts"],
                }

            self._recent_categories.append(cycle_cats)
            if len(self._recent_categories) > self._window:
                self._recent_categories.pop(0)

            # Emit structured log every 30 cycles (≈ 30 min)
            if self._cycle_count % 30 == 0 or (
                self._drought_days >= 3 and self._cycle_count % 5 == 0
            ):
                self._emit_health_log()

    def record_trade(self) -> None:
        """Call when a trade is actually executed (not just signalled)."""
        with self._lock:
            self._last_trade_ts   = datetime.now(tz=IST)
            self._last_trade_date = date.today()
            self._drought_days    = 0
            self._drought_cycles  = 0

    # ── called by dashboard API ───────────────────────────────────

    def snapshot(self) -> dict:
        """Return current health state as a JSON-serialisable dict."""
        with self._lock:
            # Aggregate rolling window
            agg = Counter()
            for c in self._recent_categories:
                agg.update(c)

            top_blockers = agg.most_common(5)
            action_points = [
                {
                    "category": cat,
                    "count":    cnt,
                    "hint":     _ACTION_HINTS.get(cat, ""),
                }
                for cat, cnt in top_blockers
            ]

            drought_status = "OK"
            if self._drought_days >= 10:
                drought_status = "CRITICAL"
            elif self._drought_days >= 5:
                drought_status = "WARNING"
            elif self._drought_days >= 2:
                drought_status = "CAUTION"

            today_top = [
                {"category": cat, "count": cnt, "hint": _ACTION_HINTS.get(cat, "")}
                for cat, cnt in self._today_skip_cats.most_common(5)
            ]

            return {
                "drought_days":        self._drought_days,
                "drought_cycles":      self._drought_cycles,
                "drought_status":      drought_status,
                "last_trade":          self._last_trade_ts.isoformat() if self._last_trade_ts else None,
                "signals_today":       self._today_signals_fired,
                "cycle_count":         self._cycle_count,
                "top_blockers_30min":  action_points,
                "top_blockers_today":  today_top,
                "symbol_detail":       dict(self._symbol_last_skip),
                "all_categories_today": dict(self._today_skip_cats),
            }

    # ── internal ─────────────────────────────────────────────────

    def _emit_health_log(self) -> None:
        agg = Counter()
        for c in self._recent_categories:
            agg.update(c)

        parts = ", ".join(f"{cat}={cnt}" for cat, cnt in agg.most_common(5))

        if self._drought_days >= 3:
            logger.warning(
                f"[SignalHealth] ⚠ DROUGHT {self._drought_days}d / "
                f"{self._drought_cycles} cycles without a trade. "
                f"Top blockers (last 30 cycles): {parts or 'none recorded'}"
            )
            for cat, cnt in agg.most_common(3):
                hint = _ACTION_HINTS.get(cat, "")
                if hint:
                    logger.warning(f"[SignalHealth]   → {cat}: {hint}")
        else:
            logger.info(
                f"[SignalHealth] Cycle {self._cycle_count} | "
                f"drought={self._drought_days}d | "
                f"top blockers (30 cycles): {parts or 'none'}"
            )


health_monitor = SignalHealthMonitor()
