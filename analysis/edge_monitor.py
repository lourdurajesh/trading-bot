"""
edge_monitor.py
───────────────
Rolling edge decay detection across all live strategies.

Compares recent performance (last RECENT_WINDOW trades) against the
lifetime baseline. Fires Telegram alerts when a strategy's edge has
materially degraded — before the kill switch is needed.

Checks every Monday morning (wired into main.py) or on-demand.

Decay thresholds:
  ALERT:  recent profit factor < 1.0 when lifetime PF >= 1.2
          (edge has reversed — stop trading this strategy)
  WARN:   recent win rate dropped > 15pp vs lifetime
          OR recent PF < 60% of lifetime PF
  OK:     within normal variance

Tuning hints (B3):
  Per-strategy rules covering stop-hit rate, target sizing, entry quality,
  and regime fit. Hints include a confidence score (0–1) so the weekly
  Telegram summary can show only high-confidence suggestions.

Usage:
    from analysis.edge_monitor import edge_monitor
    report = edge_monitor.run()
    print(report.text())
"""

import json
import logging
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

RECENT_WINDOW = 20    # compare last N trades vs lifetime
MIN_TRADES_FOR_HINT = 10   # don't produce hints with fewer trades


@dataclass
class TuningHint:
    rule:       str    # machine-readable rule ID
    text:       str    # human-readable suggestion
    confidence: float  # 0.0–1.0


@dataclass
class StrategyEdge:
    strategy:        str
    lifetime_trades: int   = 0
    lifetime_wr:     float = 0.0
    lifetime_pf:     float = 0.0
    recent_trades:   int   = 0
    recent_wr:       float = 0.0
    recent_pf:       float = 0.0
    wr_decay:        float = 0.0   # lifetime_wr - recent_wr (positive = bad)
    pf_ratio:        float = 0.0   # recent_pf / lifetime_pf (1.0 = healthy)
    status:          str   = "OK"  # OK | WARN | ALERT
    detail:          str   = ""
    hints:           list  = field(default_factory=list)   # list[TuningHint]

    @property
    def hint(self) -> str:
        """Backwards-compatible single-hint string for text() output."""
        return self.hints[0].text if self.hints else ""


@dataclass
class EdgeReport:
    generated_at:    str
    overall_status:  str                = "OK"
    strategies:      list[StrategyEdge] = field(default_factory=list)
    summary_lines:   list[str]          = field(default_factory=list)

    def text(self) -> str:
        lines = [
            f"EDGE MONITOR  —  {self.generated_at[:10]}",
            f"Status: {self.overall_status}",
            "=" * 72,
            f"{'Strategy':<24}  {'N(all)':>6}  {'WR(all)':>7}  {'PF(all)':>7}  "
            f"{'N(rec)':>6}  {'WR(rec)':>7}  {'PF(rec)':>7}  {'Status':>6}",
            "-" * 72,
        ]
        for s in self.strategies:
            lines.append(
                f"{s.strategy:<24}  {s.lifetime_trades:>6}  {s.lifetime_wr:>6.0%}  "
                f"{s.lifetime_pf:>7.2f}  {s.recent_trades:>6}  {s.recent_wr:>6.0%}  "
                f"{s.recent_pf:>7.2f}  {s.status:>6}"
            )
            if s.detail:
                lines.append(f"  {'↳':>2} {s.detail}")
            for h in s.hints:
                conf_bar = "▓" * round(h.confidence * 5)
                lines.append(f"  💡 [{conf_bar:<5}] {h.text}")
        lines.append("-" * 72)
        lines.extend(self.summary_lines)
        return "\n".join(lines)


class EdgeMonitor:

    _STRATEGIES = [
        "TrendFollow",
        "ShortTrend",
        "MeanReversion",
        "MomentumReversal",
        "GapFade",
        "SimpleRSI",
        "SimpleMomentum",
        "InstitutionalMomentum",
        "DirectionalOptions",
        "OptionsIncome",
    ]

    def run(self, send_alert: bool = True) -> EdgeReport:
        report = EdgeReport(generated_at=datetime.now(tz=IST).isoformat())

        for strat in self._STRATEGIES:
            edge = self._check_strategy(strat)
            report.strategies.append(edge)

        alerts = [e for e in report.strategies if e.status == "ALERT"]
        warns  = [e for e in report.strategies if e.status == "WARN"]

        if alerts:
            report.overall_status = "ALERT"
        elif warns:
            report.overall_status = "WARN"
        else:
            report.overall_status = "OK"

        if alerts or warns:
            report.summary_lines = (
                ["DEGRADED STRATEGIES:"]
                + [f"  • [{e.status}] {e.strategy}: {e.detail}" for e in alerts + warns]
            )
            if send_alert:
                self._send_alert(report)
        else:
            report.summary_lines = ["All strategies within normal performance range."]

        self._save(report)
        logger.info(
            f"[EdgeMonitor] {report.overall_status}  "
            f"alerts={len(alerts)}  warns={len(warns)}"
        )
        return report

    # ─── STRATEGY CHECK ──────────────────────────────────────────────

    def _check_strategy(self, strategy: str) -> StrategyEdge:
        edge = StrategyEdge(strategy=strategy)
        trades = self._load_trades(strategy)

        if not trades:
            edge.detail = "No trades yet"
            return edge

        pnl_all    = [t["pnl"] for t in trades]
        pnl_recent = pnl_all[-RECENT_WINDOW:]

        edge.lifetime_trades = len(pnl_all)
        edge.recent_trades   = len(pnl_recent)
        edge.lifetime_wr     = _win_rate(pnl_all)
        edge.recent_wr       = _win_rate(pnl_recent) if pnl_recent else 0.0
        edge.lifetime_pf     = _profit_factor(pnl_all)
        edge.recent_pf       = _profit_factor(pnl_recent) if pnl_recent else 0.0
        edge.wr_decay        = round(edge.lifetime_wr - edge.recent_wr, 3)
        edge.pf_ratio        = round(edge.recent_pf / edge.lifetime_pf, 2) if edge.lifetime_pf > 0 else 0.0

        if edge.lifetime_trades < 10:
            edge.status = "OK"
            edge.detail = f"Insufficient history ({edge.lifetime_trades} trades)"
        elif edge.recent_trades < 5:
            edge.status = "OK"
            edge.detail = f"Only {edge.recent_trades} recent trades — monitoring"
        elif edge.recent_pf < 1.0 and edge.lifetime_pf >= 1.2:
            edge.status = "ALERT"
            edge.detail = (
                f"PF collapsed: {edge.lifetime_pf:.2f} → {edge.recent_pf:.2f} "
                f"(last {edge.recent_trades} trades). Stop trading this strategy."
            )
        elif edge.wr_decay > 0.15 and edge.recent_trades >= 10:
            edge.status = "WARN"
            edge.detail = (
                f"Win rate decayed {edge.wr_decay:.0%}: "
                f"{edge.lifetime_wr:.0%} → {edge.recent_wr:.0%}"
            )
        elif edge.pf_ratio < 0.60 and edge.recent_trades >= 5:
            edge.status = "WARN"
            edge.detail = (
                f"PF at {edge.pf_ratio:.0%} of lifetime: "
                f"{edge.lifetime_pf:.2f} → {edge.recent_pf:.2f}"
            )

        edge.hints = self._adaptation_hints(strategy, trades)
        return edge

    # ─── TUNING HINTS (B3) ───────────────────────────────────────────

    def _adaptation_hints(self, strategy: str, trades: list) -> list:
        """
        Returns a list of TuningHint for the strategy.
        Covers all strategies — not just SimpleRSI.
        """
        if len(trades) < MIN_TRADES_FOR_HINT:
            return []

        recent = trades[-RECENT_WINDOW:]
        if len(recent) < 5:
            return []

        hints: list[TuningHint] = []

        # ── Common metrics ────────────────────────────────────────────
        pnl_r         = [t["pnl"] for t in recent]
        recent_wr     = _win_rate(pnl_r)
        stop_exits    = sum(1 for t in recent
                            if (t.get("exit_reason") or "").upper()
                            in ("STOP_LOSS", "SL_TRIGGERED", "STOP", "STOPPED"))
        stop_hit_rate = stop_exits / len(recent)
        meta_trades   = [t for t in recent if t.get("metadata")]

        # MFE/MAE from metadata when available (paper trade engine records these)
        mfe_list = [t["metadata"].get("mfe_r")
                    for t in meta_trades if t["metadata"].get("mfe_r") is not None]
        mae_list = [t["metadata"].get("mae_r")
                    for t in meta_trades if t["metadata"].get("mae_r") is not None]
        avg_mfe  = sum(mfe_list) / len(mfe_list) if mfe_list else None
        avg_mae  = sum(mae_list) / len(mae_list)  if mae_list else None

        # Regime breakdown from metadata
        regime_trades  = [t for t in meta_trades if t["metadata"].get("regime")]
        ranging_trades = [t for t in regime_trades if "RANG" in str(t["metadata"]["regime"]).upper()]
        ranging_wr     = _win_rate([t["pnl"] for t in ranging_trades]) if len(ranging_trades) >= 5 else None

        # ── Rule 1 (all equity strategies): stop-hit rate > 60% ───────
        if stop_hit_rate > 0.60 and stop_exits >= 5:
            conf = min(0.9, 0.5 + (stop_hit_rate - 0.60) * 2.0)
            param = _stop_param_for(strategy)
            hints.append(TuningHint(
                rule="STOP_TOO_TIGHT",
                text=(
                    f"Stop hit {stop_hit_rate:.0%} of last {len(recent)} trades "
                    f"({stop_exits} stops). Consider widening {param}."
                ),
                confidence=round(conf, 2),
            ))

        # ── Rule 2: MAE ≈ stop distance (stops triggered then reversal) ─
        if avg_mae is not None and avg_mae > 0.80 and stop_hit_rate > 0.40:
            # Avg MAE is close to 1R — price regularly touches the stop before reversing
            hints.append(TuningHint(
                rule="STOP_TRIGGERS_BEFORE_REVERSAL",
                text=(
                    f"Avg MAE {avg_mae:.2f}R with {stop_hit_rate:.0%} stop-hit — "
                    f"price tests stops then reverses. Stop may be too tight."
                ),
                confidence=0.70,
            ))

        # ── Rule 3: MFE >> current target (leaving money on table) ────
        if avg_mfe is not None:
            target_r = _target_r_for(strategy)
            if target_r and avg_mfe > target_r * 1.4:
                conf = min(0.85, 0.55 + (avg_mfe - target_r) / target_r * 0.3)
                hints.append(TuningHint(
                    rule="TARGET_TOO_LOW",
                    text=(
                        f"Avg MFE {avg_mfe:.1f}R >> current target {target_r}R — "
                        f"trades regularly reach {avg_mfe:.1f}R. Consider raising TARGET_1_R."
                    ),
                    confidence=round(conf, 2),
                ))

        # ── Rule 4: Win rate collapses only in RANGING regime ─────────
        if ranging_wr is not None and ranging_wr < recent_wr - 0.20 and len(ranging_trades) >= 5:
            hints.append(TuningHint(
                rule="POOR_IN_RANGING",
                text=(
                    f"Win rate in RANGING regime: {ranging_wr:.0%} "
                    f"vs overall {recent_wr:.0%} ({len(ranging_trades)} ranging trades). "
                    f"Consider disabling this strategy in ranging markets."
                ),
                confidence=0.75,
            ))

        # ── Strategy-specific rules ────────────────────────────────────
        hints.extend(self._strategy_specific_hints(strategy, recent, meta_trades, recent_wr))

        # Sort by confidence descending
        hints.sort(key=lambda h: h.confidence, reverse=True)
        return hints

    def _strategy_specific_hints(self, strategy: str, recent: list, meta_trades: list,
                                  recent_wr: float) -> list:
        hints: list[TuningHint] = []

        if strategy in ("SimpleRSI", "MeanReversion"):
            # Tighter RSI threshold → better win rate?
            tight_trades = [
                t for t in meta_trades
                if (
                    (t.get("direction") == "LONG"  and float(t["metadata"].get("rsi", 50)) < 30) or
                    (t.get("direction") == "SHORT" and float(t["metadata"].get("rsi", 50)) > 70)
                )
            ]
            loose_trades = [t for t in meta_trades if t not in tight_trades]
            if len(tight_trades) >= 3 and len(loose_trades) >= 3:
                tight_wr  = _win_rate([t["pnl"] for t in tight_trades])
                loose_wr  = _win_rate([t["pnl"] for t in loose_trades])
                if tight_wr > loose_wr + 0.15:
                    param = "RSI_OVERSOLD/RSI_OVERBOUGHT" if strategy == "MeanReversion" else "threshold"
                    hints.append(TuningHint(
                        rule="RSI_THRESHOLD_TOO_LOOSE",
                        text=(
                            f"RSI<30/RSI>70 entries: WR={tight_wr:.0%} ({len(tight_trades)} trades) "
                            f"vs relaxed entries: WR={loose_wr:.0%} — "
                            f"consider tightening {param}."
                        ),
                        confidence=0.72,
                    ))

        elif strategy == "InstitutionalMomentum":
            # High win rate → can lower threshold to catch more setups
            if recent_wr > 0.65 and len(recent) >= 10:
                hints.append(TuningHint(
                    rule="CONVICTION_CAN_LOWER",
                    text=(
                        f"Win rate {recent_wr:.0%} is strong — "
                        f"consider lowering CONVICTION_THRESHOLD by 1 to get more trades."
                    ),
                    confidence=0.60,
                ))
            elif recent_wr < 0.40 and len(recent) >= 10:
                hints.append(TuningHint(
                    rule="CONVICTION_RAISE",
                    text=(
                        f"Win rate {recent_wr:.0%} is weak — "
                        f"consider raising CONVICTION_THRESHOLD to require stronger setups."
                    ),
                    confidence=0.70,
                ))

        elif strategy == "DirectionalOptions":
            # Low win rate: IV may be too high at entry
            if recent_wr < 0.45 and len(recent) >= 8:
                hints.append(TuningHint(
                    rule="IV_RANK_TOO_HIGH",
                    text=(
                        f"Win rate {recent_wr:.0%} on debit spreads — "
                        f"paying too much for premium. Consider lowering MAX_IV_RANK."
                    ),
                    confidence=0.65,
                ))

        elif strategy == "GapFade":
            # Check if only large gaps or only small gaps are working
            gap_meta = [t for t in meta_trades if t["metadata"].get("gap_pct") is not None]
            if len(gap_meta) >= 6:
                small_gaps = [t for t in gap_meta if abs(float(t["metadata"]["gap_pct"])) < 2.5]
                large_gaps = [t for t in gap_meta if abs(float(t["metadata"]["gap_pct"])) >= 2.5]
                if len(small_gaps) >= 3 and len(large_gaps) >= 3:
                    small_wr = _win_rate([t["pnl"] for t in small_gaps])
                    large_wr = _win_rate([t["pnl"] for t in large_gaps])
                    if large_wr > small_wr + 0.20:
                        hints.append(TuningHint(
                            rule="GAP_SIZE_TOO_SMALL",
                            text=(
                                f"Large gaps (>2.5%) WR={large_wr:.0%} "
                                f"vs small gaps WR={small_wr:.0%} — "
                                f"consider raising MIN_GAP_PCT."
                            ),
                            confidence=0.68,
                        ))

        return hints

    # ─── DATA ────────────────────────────────────────────────────────

    def _load_trades(self, strategy: str) -> list[dict]:
        trades = []
        try:
            from config.settings import DB_PATH
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT realised_pnl AS pnl, direction, exit_time, exit_reason "
                    "FROM trades WHERE strategy=? AND status='CLOSED' ORDER BY exit_time",
                    (strategy,)
                ).fetchall()
                trades.extend(dict(r) for r in rows)
        except Exception:
            pass

        try:
            from config.settings import DB_PATH
            paper_db = os.path.join(os.path.dirname(DB_PATH), "paper_trades.db")
            with sqlite3.connect(paper_db) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT pnl, direction, exit_time, exit_reason, metadata "
                    "FROM learning_trades WHERE strategy=? AND status='CLOSED' ORDER BY exit_time",
                    (strategy,)
                ).fetchall()
                for r in rows:
                    entry = dict(r)
                    try:
                        entry["metadata"] = json.loads(entry.get("metadata") or "{}")
                    except Exception:
                        entry["metadata"] = {}
                    trades.append(entry)
        except Exception:
            pass

        trades.sort(key=lambda x: x.get("exit_time") or "")
        return trades

    def _get_last_param_changes(self, strategy: str) -> list[dict]:
        """Load the 3 most recent param changes for a strategy from the audit table."""
        try:
            from config.settings import DB_PATH
            # Map edge monitor name → config key used in param_changes table
            cfg_key = _strategy_config_key(strategy)
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT ts, param, old_value, new_value, reason "
                    "FROM param_changes WHERE strategy=? ORDER BY id DESC LIMIT 3",
                    (cfg_key,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _trades_since_last_change(self, strategy: str, last_change_ts: str) -> int:
        """Count trades closed after last_change_ts for this strategy."""
        try:
            from config.settings import DB_PATH
            with sqlite3.connect(DB_PATH) as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE strategy=? AND status='CLOSED' AND exit_time>?",
                    (strategy, last_change_ts),
                ).fetchone()[0]
            return n
        except Exception:
            return 0

    # ─── I/O ─────────────────────────────────────────────────────────

    def _save(self, report: EdgeReport) -> None:
        try:
            path = "db/edge_reports"
            os.makedirs(path, exist_ok=True)
            fname = os.path.join(path, f"edge_{report.generated_at[:10]}.json")
            with open(fname, "w") as f:
                json.dump({
                    "generated_at":   report.generated_at,
                    "overall_status": report.overall_status,
                    "strategies": [
                        {
                            **{k: v for k, v in s.__dict__.items() if k != "hints"},
                            "hints": [h.__dict__ for h in s.hints],
                        }
                        for s in report.strategies
                    ],
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"[EdgeMonitor] Save failed: {e}")

    def _send_alert(self, report: EdgeReport) -> None:
        """
        Send Monday edge report via Telegram (B4).
        Includes: decay summary, tuning hints with confidence, last param change info.
        """
        try:
            from notifications.alert_service import alert_service

            lines = [
                f"📊 *EDGE MONITOR — {report.generated_at[:10]}*",
                f"Status: *{report.overall_status}*",
                "",
            ]

            degraded = [s for s in report.strategies if s.status in ("WARN", "ALERT")]
            healthy  = [s for s in report.strategies if s.status == "OK" and s.lifetime_trades >= 10]

            if degraded:
                lines.append("*⚠️ Degraded strategies:*")
                for s in degraded:
                    icon = "🔴" if s.status == "ALERT" else "🟡"
                    lines.append(
                        f"{icon} `{s.strategy}` — {s.detail}\n"
                        f"    WR: {s.lifetime_wr:.0%}→{s.recent_wr:.0%}  "
                        f"PF: {s.lifetime_pf:.2f}→{s.recent_pf:.2f}  "
                        f"N={s.recent_trades}"
                    )
                lines.append("")

            # Tuning hints with confidence (B4)
            strategies_with_hints = [s for s in report.strategies if s.hints]
            if strategies_with_hints:
                lines.append("*💡 Tuning suggestions:*")
                for s in strategies_with_hints:
                    # Only surface hints with confidence >= 0.60
                    good_hints = [h for h in s.hints if h.confidence >= 0.60]
                    if not good_hints:
                        continue

                    # Param change context
                    last_changes = self._get_last_param_changes(s.strategy)
                    change_note  = ""
                    if last_changes:
                        lc = last_changes[0]
                        lc_dt = datetime.fromisoformat(lc["ts"]) if lc.get("ts") else None
                        if lc_dt:
                            days_ago = (datetime.now(tz=IST) - lc_dt.replace(tzinfo=IST)).days
                            trades_since = self._trades_since_last_change(s.strategy, lc["ts"])
                            if days_ago < 30 and trades_since < 30:
                                change_note = (
                                    f"⏳ Last change: `{lc['param']}={lc['new_value']}` "
                                    f"{days_ago}d ago, only {trades_since} trades since — "
                                    f"*wait for more data*"
                                )
                            else:
                                change_note = (
                                    f"Last change: `{lc['param']}={lc['new_value']}` "
                                    f"{days_ago}d ago ({trades_since} trades since)"
                                )

                    lines.append(f"\n*{s.strategy}*")
                    for h in good_hints:
                        conf_pct = round(h.confidence * 100)
                        lines.append(f"  • [{conf_pct}% conf] {h.text}")
                    if change_note:
                        lines.append(f"  {change_note}")
                lines.append("")

            if healthy:
                lines.append(
                    f"✅ Healthy ({len(healthy)}): "
                    + ", ".join(f"`{s.strategy}`" for s in healthy)
                )

            lines.append("\n_Review at 9 AM — max 1 param change per strategy this week._")

            alert_service.info("\n".join(lines))

        except Exception as e:
            logger.warning(f"[EdgeMonitor] Alert send failed: {e}")


# ── Helpers ───────────────────────────────────────────────────────────

def _win_rate(pnl: list) -> float:
    if not pnl:
        return 0.0
    return sum(1 for p in pnl if p > 0) / len(pnl)


def _profit_factor(pnl: list) -> float:
    gw = sum(p for p in pnl if p > 0)
    gl = abs(sum(p for p in pnl if p < 0))
    return round(gw / gl, 2) if gl > 0 else 99.0


def _stop_param_for(strategy: str) -> str:
    """Return the stop-loss parameter name for a strategy."""
    mapping = {
        "TrendFollow":           "ATR_STOP_MULTIPLIER",
        "ShortTrend":            "ATR_STOP_BUFFER",
        "MeanReversion":         "ATR_STOP_BUFFER",
        "MomentumReversal":      "ATR_STOP_BUFFER",
        "GapFade":               "ATR_STOP_BUFFER",
        "SimpleRSI":             "stop threshold",
        "SimpleMomentum":        "stop threshold",
        "InstitutionalMomentum": "stop multiplier",
        "DirectionalOptions":    "stop_pct",
        "OptionsIncome":         "stop_mult",
    }
    return mapping.get(strategy, "stop parameter")


def _target_r_for(strategy: str) -> float | None:
    """Return the current TARGET_1_R value for a strategy, or None if not applicable."""
    try:
        from config.strategy_config import STRATEGY_CONFIGS
        key_map = {
            "TrendFollow":      "trend_follow",
            "ShortTrend":       "short_trend",
            "MeanReversion":    "mean_reversion",
            "MomentumReversal": "momentum_reversal",
            "GapFade":          "gap_fade",
        }
        cfg_key = key_map.get(strategy)
        if cfg_key and cfg_key in STRATEGY_CONFIGS:
            return STRATEGY_CONFIGS[cfg_key].get("TARGET_1_R", {}).get("value")
    except Exception:
        pass
    return None


def _strategy_config_key(strategy: str) -> str:
    """Map edge monitor strategy name → strategy_config.py key."""
    mapping = {
        "TrendFollow":           "trend_follow",
        "ShortTrend":            "short_trend",
        "MeanReversion":         "mean_reversion",
        "MomentumReversal":      "momentum_reversal",
        "GapFade":               "gap_fade",
        "SimpleRSI":             "mean_reversion",  # closest config owner
        "SimpleMomentum":        "trend_follow",    # closest config owner
        "InstitutionalMomentum": "institutional_momentum",
        "DirectionalOptions":    "directional_options",
        "OptionsIncome":         "options_income",
    }
    return mapping.get(strategy, strategy.lower())


edge_monitor = EdgeMonitor()
