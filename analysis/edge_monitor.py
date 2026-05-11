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

Also produces parameter adaptation hints:
  If recent entries with tighter thresholds (e.g. RSI < 30 vs < 35)
  have higher win rates, flags it for manual review.

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
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

RECENT_WINDOW = 20    # compare last N trades vs lifetime


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
    hint:            str   = ""    # parameter adaptation suggestion


@dataclass
class EdgeReport:
    generated_at:    str
    overall_status:  str              = "OK"
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
            if s.hint:
                lines.append(f"  {'💡':>2} HINT: {s.hint}")
        lines.append("-" * 72)
        lines.extend(self.summary_lines)
        return "\n".join(lines)


class EdgeMonitor:

    _STRATEGIES = [
        "TrendFollow",
        "MeanReversion",
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

        # Parameter adaptation hint
        edge.hint = self._adaptation_hint(strategy, trades)
        return edge

    def _adaptation_hint(self, strategy: str, trades: list) -> str:
        """
        Checks if tighter entry thresholds would have improved recent performance.
        Only fires when we have enough data to compare.
        """
        if strategy != "SimpleRSI" or len(trades) < 20:
            return ""

        recent = trades[-20:]
        meta_trades = [t for t in recent if t.get("metadata")]
        if len(meta_trades) < 10:
            return ""

        try:
            tight_wins = sum(
                1 for t in meta_trades
                if t["pnl"] > 0
                and (
                    (t.get("direction") == "LONG"  and float(t["metadata"].get("rsi", 50)) < 30) or
                    (t.get("direction") == "SHORT" and float(t["metadata"].get("rsi", 50)) > 70)
                )
            )
            tight_n = sum(
                1 for t in meta_trades
                if (
                    (t.get("direction") == "LONG"  and float(t["metadata"].get("rsi", 50)) < 30) or
                    (t.get("direction") == "SHORT" and float(t["metadata"].get("rsi", 50)) > 70)
                )
            )
            normal_wins = sum(
                1 for t in meta_trades if t["pnl"] > 0
            ) - tight_wins
            normal_n = len(meta_trades) - tight_n

            if tight_n >= 3 and normal_n >= 3:
                tight_wr  = tight_wins / tight_n
                normal_wr = normal_wins / normal_n
                if tight_wr > normal_wr + 0.15:
                    return (
                        f"RSI<30/RSI>70 entries: WR={tight_wr:.0%} ({tight_n} trades) vs "
                        f"all entries: WR={normal_wr:.0%} — consider tightening threshold"
                    )
        except Exception:
            pass
        return ""

    # ─── DATA ────────────────────────────────────────────────────────

    def _load_trades(self, strategy: str) -> list[dict]:
        trades = []
        try:
            from config.settings import DB_PATH
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT realised_pnl AS pnl, direction, exit_time "
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
                    "SELECT pnl, direction, exit_time, metadata "
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
                        {k: v for k, v in s.__dict__.items()}
                        for s in report.strategies
                    ],
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"[EdgeMonitor] Save failed: {e}")

    def _send_alert(self, report: EdgeReport) -> None:
        try:
            from notifications.alert_service import alert_service
            alert_service.info(
                f"EDGE MONITOR {report.overall_status}\n\n"
                + "\n".join(report.summary_lines)
            )
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


edge_monitor = EdgeMonitor()
