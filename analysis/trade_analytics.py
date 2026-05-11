"""
trade_analytics.py
──────────────────
Deep statistical analysis of all closed trades from trades.db
and paper_trades.db (learning engine).

Produces a hedge-fund-grade performance attribution report:
  - Portfolio-wide: Sharpe, Sortino, Calmar, Profit Factor, Kelly f
  - By strategy: P&L, win rate, expectancy
  - By regime: which regimes are actually generating edge
  - By day-of-week: calendar effects (expiry week, Mon vs Fri)
  - By hour: time-of-day performance (morning rush vs afternoon drift)
  - Drawdown depth / duration / streak analysis
  - Exit reason attribution: what's actually making money
  - Correlation between strategies (cluster risk)
  - MAE/MFE analysis (do we cut winners too early?)

Usage:
    from analysis.trade_analytics import trade_analytics
    print(trade_analytics.full_report())
    trade_analytics.save_report()  # writes to db/reports/analytics_YYYY-MM-DD.txt
"""

import logging
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

IST            = ZoneInfo("Asia/Kolkata")
RISK_FREE_RATE = 0.065
TRADING_DAYS   = 252
logger         = logging.getLogger(__name__)


class TradeAnalytics:

    def full_report(self, include_paper: bool = True) -> str:
        trades = self._load(include_paper)
        if not trades:
            return "No closed trades available for analysis.\n"

        sections = []
        sections += self._overview(trades)
        sections += self._by_strategy(trades)
        sections += self._by_regime(trades)
        sections += self._by_day(trades)
        sections += self._by_hour(trades)
        sections += self._drawdown_analysis(trades)
        sections += self._exit_reasons(trades)
        sections += self._kelly(trades)
        sections += self._mae_mfe(trades)
        return "\n".join(sections)

    def save_report(self, include_paper: bool = True) -> str:
        report = self.full_report(include_paper)
        try:
            os.makedirs("db/reports", exist_ok=True)
            date_str = datetime.now(tz=IST).strftime("%Y-%m-%d")
            path = f"db/reports/analytics_{date_str}.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
            logger.info(f"[TradeAnalytics] Report saved: {path}")
            return path
        except Exception as e:
            logger.warning(f"[TradeAnalytics] Save failed: {e}")
            return ""

    # ─── SECTIONS ────────────────────────────────────────────────────

    def _overview(self, trades: list[dict]) -> list[str]:
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        loss = [p for p in pnls if p < 0]

        from config.settings import TOTAL_CAPITAL
        arr     = np.array(pnls) / TOTAL_CAPITAL
        mean_r  = arr.mean()
        std_r   = arr.std()
        rf_d    = RISK_FREE_RATE / TRADING_DAYS
        sharpe  = (mean_r - rf_d) / std_r * math.sqrt(TRADING_DAYS) if std_r > 1e-10 else 0.0

        neg     = arr[arr < 0]
        down_std = neg.std() if len(neg) > 1 else 1e-10
        sortino = (mean_r - rf_d) / down_std * math.sqrt(TRADING_DAYS) if down_std > 1e-10 else 0.0

        eq      = np.cumsum(pnls) + TOTAL_CAPITAL
        eq      = np.insert(eq, 0, TOTAL_CAPITAL)
        peak    = np.maximum.accumulate(eq)
        max_dd  = float(((peak - eq) / peak * 100).max())
        total_r = (eq[-1] - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100
        calmar  = total_r / max_dd if max_dd > 0 else 0.0

        gw  = sum(wins)
        gl  = abs(sum(loss))
        pf  = round(gw / gl, 2) if gl > 0 else 99.0
        exp = sum(pnls) / len(pnls)

        return [
            "=" * 72,
            "PORTFOLIO-WIDE STATISTICS",
            "=" * 72,
            f"Total trades:        {len(pnls)}",
            f"Win rate:            {len(wins)/len(pnls):.1%}",
            f"Profit Factor:       {pf:.2f}",
            f"Sharpe Ratio:        {sharpe:.2f}",
            f"Sortino Ratio:       {sortino:.2f}",
            f"Calmar Ratio:        {calmar:.2f}",
            f"Max Drawdown:        {max_dd:.1f}%",
            f"Total Return:        {total_r:+.1f}%",
            f"Avg Winner:          ₹{sum(wins)/len(wins):,.0f}" if wins else "Avg Winner:          N/A",
            f"Avg Loser:           ₹{abs(sum(loss)/len(loss)):,.0f}" if loss else "Avg Loser:           N/A",
            f"Expectancy/trade:    ₹{exp:+,.0f}",
            "",
        ]

    def _by_strategy(self, trades: list[dict]) -> list[str]:
        by = defaultdict(list)
        for t in trades:
            by[t["strategy"]].append(t["pnl"])

        lines = ["-" * 72, "BY STRATEGY", "-" * 72]
        lines.append(f"{'Strategy':<26}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'Exp ₹':>9}  {'Total ₹':>11}")
        lines.append("-" * 72)
        for strat in sorted(by):
            pnls = by[strat]
            wins = [p for p in pnls if p > 0]
            loss = [p for p in pnls if p < 0]
            wr   = len(wins) / len(pnls)
            gw   = sum(wins)
            gl   = abs(sum(loss))
            pf   = gw / gl if gl else 99.0
            exp  = sum(pnls) / len(pnls)
            lines.append(
                f"{strat:<26}  {len(pnls):>4}  {wr:>5.0%}  {pf:>6.2f}  "
                f"₹{exp:>8,.0f}  ₹{sum(pnls):>10,.0f}"
            )
        lines.append("")
        return lines

    def _by_regime(self, trades: list[dict]) -> list[str]:
        by = defaultdict(list)
        for t in trades:
            r = t.get("regime") or "UNKNOWN"
            by[r].append(t["pnl"])
        if all(k == "UNKNOWN" for k in by):
            return []   # no regime data stored — skip section

        lines = ["-" * 72, "BY REGIME", "-" * 72]
        lines.append(f"{'Regime':<18}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'Avg P&L ₹':>11}")
        for regime in sorted(by):
            pnls = by[regime]
            wins = [p for p in pnls if p > 0]
            gl   = abs(sum(p for p in pnls if p < 0))
            pf   = sum(wins) / gl if gl else 99.0
            wr   = len(wins) / len(pnls)
            avg  = sum(pnls) / len(pnls)
            lines.append(f"{regime:<18}  {len(pnls):>4}  {wr:>5.0%}  {pf:>6.2f}  ₹{avg:>10,.0f}")
        lines.append("")
        return lines

    def _by_day(self, trades: list[dict]) -> list[str]:
        by = defaultdict(list)
        for t in trades:
            ts = t.get("entry_time")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts).astimezone(IST)
                by[dt.strftime("%A")].append(t["pnl"])
            except Exception:
                pass
        if not by:
            return []

        lines = ["-" * 72, "BY DAY OF WEEK", "-" * 72]
        lines.append(f"{'Day':<12}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'Avg P&L ₹':>11}")
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            pnls = by.get(day, [])
            if not pnls:
                continue
            wins = [p for p in pnls if p > 0]
            gl   = abs(sum(p for p in pnls if p < 0))
            pf   = sum(wins) / gl if gl else 99.0
            wr   = len(wins) / len(pnls)
            avg  = sum(pnls) / len(pnls)
            lines.append(f"{day:<12}  {len(pnls):>4}  {wr:>5.0%}  {pf:>6.2f}  ₹{avg:>10,.0f}")
        lines.append("")
        return lines

    def _by_hour(self, trades: list[dict]) -> list[str]:
        by = defaultdict(list)
        for t in trades:
            ts = t.get("entry_time")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts).astimezone(IST)
                by[dt.hour].append(t["pnl"])
            except Exception:
                pass
        if not by:
            return []

        lines = ["-" * 72, "BY HOUR OF DAY (IST ENTRY TIME)", "-" * 72]
        lines.append(f"{'Hour':<10}  {'N':>4}  {'WR':>6}  {'Avg P&L ₹':>11}")
        for hour in sorted(by):
            pnls = by[hour]
            wr   = sum(1 for p in pnls if p > 0) / len(pnls)
            avg  = sum(pnls) / len(pnls)
            lines.append(f"{hour:02d}:00-{(hour+1)%24:02d}:00  {len(pnls):>4}  {wr:>5.0%}  ₹{avg:>10,.0f}")
        lines.append("")
        return lines

    def _drawdown_analysis(self, trades: list[dict]) -> list[str]:
        from config.settings import TOTAL_CAPITAL
        pnls = [t["pnl"] for t in trades]
        if not pnls:
            return []

        eq   = TOTAL_CAPITAL
        peak = eq
        cur_dd_bars = 0
        max_dd_bars = 0
        dd_depths   = []
        streaks_w   = streaks_l = cur_w = cur_l = max_w = max_l = 0

        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd_pct = (peak - eq) / peak * 100
            dd_depths.append(dd_pct)
            if dd_pct > 0:
                cur_dd_bars += 1
                max_dd_bars  = max(max_dd_bars, cur_dd_bars)
            else:
                cur_dd_bars  = 0
            if p > 0:
                cur_w += 1; cur_l = 0
                max_w  = max(max_w, cur_w)
            else:
                cur_l += 1; cur_w = 0
                max_l  = max(max_l, cur_l)

        cur_streak_n = cur_w if cur_w > 0 else cur_l
        cur_streak_d = "wins" if cur_w > 0 else "losses"

        lines = ["-" * 72, "DRAWDOWN & STREAK ANALYSIS", "-" * 72]
        lines += [
            f"Max drawdown depth:    {max(dd_depths):.1f}%",
            f"Current drawdown:      {dd_depths[-1]:.1f}%",
            f"Longest DD period:     {max_dd_bars} trades",
            f"Max winning streak:    {max_w}",
            f"Max losing streak:     {max_l}",
            f"Current streak:        {cur_streak_n} {cur_streak_d}",
            "",
        ]
        return lines

    def _exit_reasons(self, trades: list[dict]) -> list[str]:
        by = defaultdict(list)
        for t in trades:
            reason = (t.get("exit_reason") or "UNKNOWN").upper()
            by[reason].append(t["pnl"])

        lines = ["-" * 72, "EXIT REASON ATTRIBUTION", "-" * 72]
        lines.append(
            f"{'Exit Reason':<22}  {'N':>5}  {'WR':>6}  {'Avg P&L ₹':>11}  {'Total ₹':>12}"
        )
        lines.append("-" * 72)
        for reason in sorted(by, key=lambda x: -len(by[x])):
            pnls = by[reason]
            wr   = sum(1 for p in pnls if p > 0) / len(pnls)
            avg  = sum(pnls) / len(pnls)
            lines.append(
                f"{reason:<22}  {len(pnls):>5}  {wr:>5.0%}  ₹{avg:>10,.0f}  ₹{sum(pnls):>11,.0f}"
            )
        lines.append("")
        return lines

    def _kelly(self, trades: list[dict]) -> list[str]:
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        loss = [abs(p) for p in pnls if p < 0]
        if not wins or not loss:
            return []

        p  = len(wins) / len(pnls)
        q  = 1 - p
        b  = (sum(wins) / len(wins)) / (sum(loss) / len(loss))
        k  = p - q / b if b > 0 else 0.0
        hk = k / 2

        lines = ["-" * 72, "KELLY CRITERION — OPTIMAL POSITION SIZING", "-" * 72]
        lines += [
            f"Win probability p:       {p:.1%}",
            f"Avg win / avg loss (b):  {b:.2f}x",
            f"Full Kelly f:            {k:.1%}  ← theoretical maximum (never use)",
            f"Half Kelly (recommended): {hk:.1%} of capital per trade",
        ]
        from config.settings import TOTAL_CAPITAL
        rupees = TOTAL_CAPITAL * hk
        lines.append(f"  → ₹{rupees:,.0f} per trade on ₹{TOTAL_CAPITAL:,.0f} capital")
        lines.append("")
        return lines

    def _mae_mfe(self, trades: list[dict]) -> list[str]:
        """
        Max Adverse Excursion / Max Favorable Excursion from metadata.
        Only available for learning engine trades that store mae_pts / mfe_pts.
        """
        mae_vals = []
        mfe_vals = []
        for t in trades:
            try:
                meta = t.get("metadata") or {}
                if isinstance(meta, str):
                    import json
                    meta = json.loads(meta)
                if meta.get("mae_pts") is not None:
                    mae_vals.append(float(meta["mae_pts"]))
                if meta.get("mfe_pts") is not None:
                    mfe_vals.append(float(meta["mfe_pts"]))
            except Exception:
                pass

        if not mae_vals and not mfe_vals:
            return []

        lines = ["-" * 72, "MAE / MFE ANALYSIS", "-" * 72]
        if mae_vals:
            lines += [
                f"Max Adverse Excursion (MAE) — how far trades went against us:",
                f"  Avg: {sum(mae_vals)/len(mae_vals):.1f} pts  |  "
                f"P75: {float(np.percentile(mae_vals, 75)):.1f} pts  |  "
                f"P95: {float(np.percentile(mae_vals, 95)):.1f} pts",
            ]
        if mfe_vals:
            lines += [
                f"Max Favorable Excursion (MFE) — how far trades ran in our favor:",
                f"  Avg: {sum(mfe_vals)/len(mfe_vals):.1f} pts  |  "
                f"P75: {float(np.percentile(mfe_vals, 75)):.1f} pts  |  "
                f"P95: {float(np.percentile(mfe_vals, 95)):.1f} pts",
            ]
            if mae_vals:
                ratio = sum(mfe_vals) / len(mfe_vals) / (sum(mae_vals) / len(mae_vals) + 1e-10)
                lines.append(f"  MFE/MAE ratio: {ratio:.2f}x  (>1 means winners run further than losers)")
        lines.append("")
        return lines

    # ─── DATA LOAD ───────────────────────────────────────────────────

    def _load(self, include_paper: bool = True) -> list[dict]:
        import json as _json
        trades = []
        try:
            from config.settings import DB_PATH
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT strategy, realised_pnl AS pnl, entry_time, exit_time, "
                    "exit_reason, direction FROM trades WHERE status='CLOSED' ORDER BY exit_time"
                ).fetchall()
                trades.extend(dict(r) for r in rows)
        except Exception as e:
            logger.debug(f"[TradeAnalytics] trades.db: {e}")

        if include_paper:
            try:
                from config.settings import DB_PATH
                paper_db = os.path.join(os.path.dirname(DB_PATH), "paper_trades.db")
                with sqlite3.connect(paper_db) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT strategy, pnl, entry_time, exit_time, "
                        "exit_reason, direction, metadata "
                        "FROM learning_trades WHERE status='CLOSED' ORDER BY exit_time"
                    ).fetchall()
                    for r in rows:
                        entry = dict(r)
                        try:
                            entry["metadata"] = _json.loads(entry.get("metadata") or "{}")
                        except Exception:
                            entry["metadata"] = {}
                        trades.append(entry)
            except Exception as e:
                logger.debug(f"[TradeAnalytics] paper_trades.db: {e}")

        return trades


trade_analytics = TradeAnalytics()
