"""
journal_analyser.py
───────────────────
Reads your complete trade history from SQLite and uses Claude
to identify behavioural patterns, recurring mistakes, and
missed opportunities.

What it analyses:
  1. Win/loss patterns by time of day, day of week, market regime
  2. Holding period analysis — do you exit winners too early?
  3. Loss patterns — do you hold losers too long?
  4. Strategy performance — which strategy works best for you?
  5. Emotional patterns — do you revenge trade after losses?
  6. Missed opportunities — signals you rejected that would have worked
  7. Streak analysis — behaviour during winning/losing streaks

Output:
  - 3 personalised trading rules based on YOUR data
  - Detailed behavioural bias report
  - Saved to db/journal_reports/

Run manually:   python journal_analyser.py
Also runs:      weekly_agent.py (every Sunday)
API endpoint:   GET /journal/analysis
"""

import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("journal_analyser")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
DB_PATH           = os.getenv("DB_PATH", "db/trades.db")
REPORTS_DIR       = "db/journal_reports"
os.makedirs(REPORTS_DIR, exist_ok=True)

MIN_TRADES_FOR_ANALYSIS = 5   # minimum trades needed for meaningful analysis


@dataclass
class TradeRecord:
    id:            str
    symbol:        str
    strategy:      str
    direction:     str
    entry_price:   float
    exit_price:    float
    stop_loss:     float
    target_1:      float
    position_size: int
    realised_pnl:  float
    status:        str
    exit_reason:   str
    entry_time:    Optional[datetime]
    exit_time:     Optional[datetime]

    @property
    def holding_days(self) -> float:
        if self.entry_time and self.exit_time:
            return (self.exit_time - self.entry_time).total_seconds() / 86400
        return 0

    @property
    def entry_hour(self) -> int:
        return self.entry_time.hour if self.entry_time else 0

    @property
    def exit_hour(self) -> int:
        return self.exit_time.hour if self.exit_time else 0

    @property
    def entry_weekday(self) -> str:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        if self.entry_time:
            return days[min(self.entry_time.weekday(), 4)]
        return "Unknown"

    @property
    def is_winner(self) -> bool:
        return self.realised_pnl > 0

    @property
    def rr_achieved(self) -> float:
        """Actual R:R achieved vs planned."""
        risk = abs(self.entry_price - self.stop_loss)
        if risk <= 0:
            return 0
        return abs(self.exit_price - self.entry_price) / risk

    @property
    def hit_target(self) -> bool:
        return self.exit_reason in ("TARGET1", "TARGET2")

    @property
    def hit_stop(self) -> bool:
        return self.exit_reason == "STOP"


@dataclass
class BehaviouralBias:
    name:        str
    detected:    bool
    severity:    str    # LOW | MEDIUM | HIGH
    evidence:    str
    impact:      str
    fix:         str


@dataclass
class JournalAnalysis:
    generated_at:     str
    total_trades:     int
    date_range:       str

    # Core metrics
    win_rate:         float
    avg_winner_pnl:   float
    avg_loser_pnl:    float
    profit_factor:    float
    avg_holding_days: float
    total_pnl:        float

    # Pattern findings
    best_day:         str
    worst_day:        str
    best_hour:        str
    worst_hour:       str
    best_strategy:    str
    worst_strategy:   str
    avg_rr_achieved:  float
    exit_too_early:   bool
    hold_losers_long: bool

    # Behavioural biases detected
    biases:           list[BehaviouralBias]

    # Claude output
    personalised_rules: list[str]
    narrative:          str
    missed_opportunities: list[str]
    strengths:          list[str]
    improvement_areas:  list[str]


class JournalAnalyser:

    def analyse(self, min_trades: int = MIN_TRADES_FOR_ANALYSIS) -> Optional[JournalAnalysis]:
        """
        Run full journal analysis on trade history.
        Returns None if insufficient trade history.
        """
        trades = self._load_trades()

        if len(trades) < min_trades:
            logger.info(
                f"[JournalAnalyser] Only {len(trades)} trades — "
                f"need {min_trades} for meaningful analysis. Keep trading!"
            )
            return self._insufficient_data_result(len(trades), min_trades)

        logger.info(f"[JournalAnalyser] Analysing {len(trades)} trades...")

        # ── Core metrics ──────────────────────────────────────────
        winners  = [t for t in trades if t.is_winner]
        losers   = [t for t in trades if not t.is_winner]
        win_rate = len(winners) / len(trades)

        avg_winner  = sum(t.realised_pnl for t in winners) / len(winners) if winners else 0
        avg_loser   = abs(sum(t.realised_pnl for t in losers) / len(losers)) if losers else 1
        pf          = sum(t.realised_pnl for t in winners) / abs(sum(t.realised_pnl for t in losers)) \
                      if losers else 999.0
        avg_holding = sum(t.holding_days for t in trades) / len(trades)
        total_pnl   = sum(t.realised_pnl for t in trades)

        # ── Time patterns ─────────────────────────────────────────
        best_day, worst_day   = self._day_analysis(trades)
        best_hour, worst_hour = self._hour_analysis(trades)

        # ── Strategy analysis ─────────────────────────────────────
        best_strat, worst_strat = self._strategy_analysis(trades)

        # ── R:R analysis ──────────────────────────────────────────
        avg_rr_achieved = sum(t.rr_achieved for t in trades) / len(trades)
        exit_too_early  = self._detect_early_exits(winners)
        hold_losers     = self._detect_holding_losers(losers)

        # ── Behavioural biases ────────────────────────────────────
        biases = self._detect_biases(trades, winners, losers)

        # ── Claude analysis ───────────────────────────────────────
        rules, narrative, missed, strengths, improvements = self._generate_insights(
            trades, winners, losers, biases,
            win_rate, avg_winner, avg_loser, pf,
            best_day, worst_day, best_hour, best_strat,
            avg_rr_achieved, exit_too_early, hold_losers, total_pnl
        )

        # Date range
        sorted_trades = sorted(trades, key=lambda t: t.entry_time or datetime.min)
        date_range = (
            f"{sorted_trades[0].entry_time.strftime('%d %b %Y')} – "
            f"{sorted_trades[-1].entry_time.strftime('%d %b %Y')}"
            if sorted_trades and sorted_trades[0].entry_time else "Unknown"
        )

        analysis = JournalAnalysis(
            generated_at      = datetime.now(tz=timezone.utc).isoformat(),
            total_trades      = len(trades),
            date_range        = date_range,
            win_rate          = round(win_rate * 100, 1),
            avg_winner_pnl    = round(avg_winner, 2),
            avg_loser_pnl     = round(avg_loser, 2),
            profit_factor     = round(pf, 2),
            avg_holding_days  = round(avg_holding, 1),
            total_pnl         = round(total_pnl, 2),
            best_day          = best_day,
            worst_day         = worst_day,
            best_hour         = best_hour,
            worst_hour        = worst_hour,
            best_strategy     = best_strat,
            worst_strategy    = worst_strat,
            avg_rr_achieved   = round(avg_rr_achieved, 2),
            exit_too_early    = exit_too_early,
            hold_losers_long  = hold_losers,
            biases            = biases,
            personalised_rules = rules,
            narrative         = narrative,
            missed_opportunities = missed,
            strengths         = strengths,
            improvement_areas = improvements,
        )

        self._save_report(analysis)
        self._log_summary(analysis)
        return analysis

    # ─────────────────────────────────────────────────────────────
    # DATA LOADING
    # ─────────────────────────────────────────────────────────────

    def _load_trades(self) -> list[TradeRecord]:
        """Load all closed trades from SQLite."""
        if not os.path.exists(DB_PATH):
            logger.warning(f"[JournalAnalyser] Database not found: {DB_PATH}")
            return []
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status IN ('CLOSED','STOPPED') "
                    "ORDER BY entry_time ASC"
                ).fetchall()

            trades = []
            for row in rows:
                entry_time = None
                exit_time  = None
                try:
                    if row["entry_time"]:
                        entry_time = datetime.fromisoformat(row["entry_time"])
                    if row["exit_time"]:
                        exit_time = datetime.fromisoformat(row["exit_time"])
                except Exception:
                    pass

                trades.append(TradeRecord(
                    id            = row["id"],
                    symbol        = row["symbol"],
                    strategy      = row["strategy"],
                    direction     = row["direction"],
                    entry_price   = row["entry_price"] or 0,
                    exit_price    = row["exit_price"]  or 0,
                    stop_loss     = row["stop_loss"]   or 0,
                    target_1      = row["target_1"]    or 0,
                    position_size = row["position_size"] or 0,
                    realised_pnl  = row["realised_pnl"]  or 0,
                    status        = row["status"],
                    exit_reason   = row["exit_reason"] or "",
                    entry_time    = entry_time,
                    exit_time     = exit_time,
                ))

            logger.info(f"[JournalAnalyser] Loaded {len(trades)} closed trades")
            return trades

        except Exception as e:
            logger.error(f"[JournalAnalyser] DB load failed: {e}")
            return []

    # ─────────────────────────────────────────────────────────────
    # PATTERN ANALYSIS
    # ─────────────────────────────────────────────────────────────

    def _day_analysis(self, trades: list[TradeRecord]) -> tuple[str, str]:
        """Find best and worst trading days of the week."""
        day_pnl: dict[str, list[float]] = {}
        for t in trades:
            day = t.entry_weekday
            day_pnl.setdefault(day, []).append(t.realised_pnl)

        if not day_pnl:
            return "Unknown", "Unknown"

        day_avgs = {d: sum(pnls) / len(pnls) for d, pnls in day_pnl.items()}
        best     = max(day_avgs, key=day_avgs.get)
        worst    = min(day_avgs, key=day_avgs.get)
        return best, worst

    def _hour_analysis(self, trades: list[TradeRecord]) -> tuple[str, str]:
        """Find best and worst entry hours."""
        hour_pnl: dict[int, list[float]] = {}
        for t in trades:
            h = t.entry_hour
            hour_pnl.setdefault(h, []).append(t.realised_pnl)

        if not hour_pnl:
            return "Unknown", "Unknown"

        hour_avgs = {h: sum(pnls) / len(pnls) for h, pnls in hour_pnl.items()}
        best      = max(hour_avgs, key=hour_avgs.get)
        worst     = min(hour_avgs, key=hour_avgs.get)
        return f"{best:02d}:00", f"{worst:02d}:00"

    def _strategy_analysis(self, trades: list[TradeRecord]) -> tuple[str, str]:
        """Find best and worst performing strategies."""
        strat_pnl: dict[str, list[float]] = {}
        for t in trades:
            strat_pnl.setdefault(t.strategy, []).append(t.realised_pnl)

        if not strat_pnl:
            return "Unknown", "Unknown"

        strat_avgs = {s: sum(pnls) / len(pnls) for s, pnls in strat_pnl.items()}
        best   = max(strat_avgs, key=strat_avgs.get)
        worst  = min(strat_avgs, key=strat_avgs.get)
        return best, worst

    def _detect_early_exits(self, winners: list[TradeRecord]) -> bool:
        """
        Detect if winners are being exited before reaching target.
        Signal: avg winner holding < 3 days AND avg rr_achieved < 1.5
        """
        if len(winners) < 3:
            return False
        avg_rr = sum(t.rr_achieved for t in winners) / len(winners)
        hit_target_rate = sum(1 for t in winners if t.hit_target) / len(winners)
        return avg_rr < 1.5 and hit_target_rate < 0.4

    def _detect_holding_losers(self, losers: list[TradeRecord]) -> bool:
        """
        Detect if losers are held longer than winners.
        Classic behavioural bias — cutting winners, riding losers.
        """
        if len(losers) < 3:
            return False
        avg_loser_days  = sum(t.holding_days for t in losers) / len(losers)
        stop_hit_rate   = sum(1 for t in losers if t.hit_stop) / len(losers)
        # If losers held > 5 days on avg and stops not being hit = holding too long
        return avg_loser_days > 5 and stop_hit_rate < 0.5

    # ─────────────────────────────────────────────────────────────
    # BEHAVIOURAL BIAS DETECTION
    # ─────────────────────────────────────────────────────────────

    def _detect_biases(
        self,
        trades:  list[TradeRecord],
        winners: list[TradeRecord],
        losers:  list[TradeRecord],
    ) -> list[BehaviouralBias]:
        biases = []

        # ── 1. Revenge trading ────────────────────────────────────
        revenge = self._check_revenge_trading(trades)
        biases.append(BehaviouralBias(
            name     = "Revenge trading",
            detected = revenge["detected"],
            severity = revenge["severity"],
            evidence = revenge["evidence"],
            impact   = "Trades placed in emotional state after losses have lower win rate",
            fix      = "After any losing trade: mandatory 30-minute break before next entry",
        ))

        # ── 2. Cutting winners short ──────────────────────────────
        early_exit = self._detect_early_exits(winners)
        biases.append(BehaviouralBias(
            name     = "Cutting winners short",
            detected = early_exit,
            severity = "HIGH" if early_exit else "LOW",
            evidence = (
                f"Avg R:R achieved on winners: {sum(t.rr_achieved for t in winners)/len(winners):.1f}x "
                f"vs target of 2x+" if winners else "Insufficient data"
            ),
            impact   = "Leaving money on the table — profit factor suffers",
            fix      = "Never exit at breakeven or tiny profit. Let price reach T1 or trail the stop",
        ))

        # ── 3. Holding losers too long ────────────────────────────
        holding_losers = self._detect_holding_losers(losers)
        avg_loser_days = sum(t.holding_days for t in losers) / len(losers) if losers else 0
        biases.append(BehaviouralBias(
            name     = "Holding losers too long",
            detected = holding_losers,
            severity = "HIGH" if holding_losers else "LOW",
            evidence = f"Average losing trade held {avg_loser_days:.1f} days",
            impact   = "Small losses become large losses. Capital locked in dead positions",
            fix      = "Stop loss is non-negotiable. If price hits SL, exit immediately. No exceptions",
        ))

        # ── 4. Overtrading ────────────────────────────────────────
        overtrading = self._check_overtrading(trades)
        biases.append(BehaviouralBias(
            name     = "Overtrading",
            detected = overtrading["detected"],
            severity = overtrading["severity"],
            evidence = overtrading["evidence"],
            impact   = "Transaction costs compound. Quality setups diluted by quantity",
            fix      = "Max 2 new trades per day. Quality over quantity",
        ))

        # ── 5. Time-of-day bias ───────────────────────────────────
        tod_bias = self._check_time_bias(trades)
        biases.append(BehaviouralBias(
            name     = "Poor time-of-day selection",
            detected = tod_bias["detected"],
            severity = tod_bias["severity"],
            evidence = tod_bias["evidence"],
            impact   = "Trading at wrong market times reduces win rate significantly",
            fix      = tod_bias["fix"],
        ))

        # ── 6. Streak chasing / tilt ──────────────────────────────
        tilt = self._check_streak_tilt(trades)
        biases.append(BehaviouralBias(
            name     = "Tilt after losing streaks",
            detected = tilt["detected"],
            severity = tilt["severity"],
            evidence = tilt["evidence"],
            impact   = "Larger losses when already down — compounds drawdowns",
            fix      = "After 2 consecutive losses: reduce position size by 50% for next 3 trades",
        ))

        return biases

    def _check_revenge_trading(self, trades: list[TradeRecord]) -> dict:
        """
        Detect revenge trading: loss followed quickly by another trade
        that also loses — sign of emotional trading.
        """
        if len(trades) < 4:
            return {"detected": False, "severity": "LOW", "evidence": "Insufficient data"}

        revenge_count = 0
        sorted_trades = sorted(trades, key=lambda t: t.entry_time or datetime.min)

        for i in range(1, len(sorted_trades)):
            prev = sorted_trades[i-1]
            curr = sorted_trades[i]
            if (
                not prev.is_winner
                and prev.exit_time and curr.entry_time
                and (curr.entry_time - prev.exit_time).total_seconds() < 3600  # within 1 hour
                and not curr.is_winner
            ):
                revenge_count += 1

        rate = revenge_count / max(len([t for t in trades if not t.is_winner]), 1)

        return {
            "detected": rate > 0.2,
            "severity": "HIGH" if rate > 0.3 else "MEDIUM" if rate > 0.2 else "LOW",
            "evidence": f"Quick re-entry after losses: {revenge_count} occurrences ({rate:.0%} of losses)",
        }

    def _check_overtrading(self, trades: list[TradeRecord]) -> dict:
        """Detect days with unusually high trade count."""
        if len(trades) < 5:
            return {"detected": False, "severity": "LOW", "evidence": "Insufficient data"}

        day_counts: dict[str, int] = {}
        for t in trades:
            if t.entry_time:
                day = t.entry_time.strftime("%Y-%m-%d")
                day_counts[day] = day_counts.get(day, 0) + 1

        max_day_trades = max(day_counts.values()) if day_counts else 0
        avg_day_trades = sum(day_counts.values()) / len(day_counts) if day_counts else 0

        detected = max_day_trades >= 5 or avg_day_trades > 2.5

        return {
            "detected": detected,
            "severity": "HIGH" if max_day_trades >= 6 else "MEDIUM" if detected else "LOW",
            "evidence": (
                f"Max {max_day_trades} trades in one day. "
                f"Average {avg_day_trades:.1f} trades/day."
            ),
        }

    def _check_time_bias(self, trades: list[TradeRecord]) -> dict:
        """Check if certain hours consistently underperform."""
        hour_results: dict[int, list[bool]] = {}
        for t in trades:
            h = t.entry_hour
            hour_results.setdefault(h, []).append(t.is_winner)

        bad_hours = []
        for h, results in hour_results.items():
            if len(results) >= 3:
                wr = sum(results) / len(results)
                if wr < 0.35:
                    bad_hours.append((h, wr))

        detected = len(bad_hours) > 0
        fix      = (
            f"Avoid trading at {', '.join(f'{h:02d}:00' for h, _ in bad_hours)} — "
            f"your win rate drops significantly at these hours."
            if bad_hours else
            "No specific time bias detected."
        )

        return {
            "detected": detected,
            "severity": "MEDIUM" if detected else "LOW",
            "evidence": (
                f"Poor performance hours: {[(f'{h:02d}:00', f'{wr:.0%}') for h, wr in bad_hours]}"
                if bad_hours else "No consistent time bias"
            ),
            "fix": fix,
        }

    def _check_streak_tilt(self, trades: list[TradeRecord]) -> dict:
        """
        Detect if position sizes increase after losing streaks
        (a sign of tilt / emotional trading).
        Currently checks if loss rate after 2+ consecutive losses is higher.
        """
        if len(trades) < 6:
            return {"detected": False, "severity": "LOW", "evidence": "Insufficient data"}

        sorted_t         = sorted(trades, key=lambda t: t.entry_time or datetime.min)
        post_streak_loss = []
        streak           = 0

        for i, t in enumerate(sorted_t):
            if i > 0:
                if streak >= 2:
                    post_streak_loss.append(not t.is_winner)
            streak = streak + 1 if not t.is_winner else 0

        if not post_streak_loss:
            return {"detected": False, "severity": "LOW", "evidence": "No losing streaks found"}

        post_loss_rate = sum(post_streak_loss) / len(post_streak_loss)
        overall_loss   = 1 - (sum(1 for t in trades if t.is_winner) / len(trades))
        tilt_detected  = post_loss_rate > overall_loss + 0.15

        return {
            "detected": tilt_detected,
            "severity": "HIGH" if tilt_detected and post_loss_rate > 0.7 else "MEDIUM" if tilt_detected else "LOW",
            "evidence": (
                f"Win rate after 2+ consecutive losses: {1-post_loss_rate:.0%} "
                f"vs overall {1-overall_loss:.0%}"
            ),
        }

    # ─────────────────────────────────────────────────────────────
    # CLAUDE INSIGHTS
    # ─────────────────────────────────────────────────────────────

    def _generate_insights(
        self, trades, winners, losers, biases,
        win_rate, avg_winner, avg_loser, pf,
        best_day, worst_day, best_hour, best_strat,
        avg_rr, exit_too_early, hold_losers, total_pnl
    ) -> tuple[list, str, list, list, list]:
        """Generate personalised insights using Claude or rule-based."""
        if ANTHROPIC_API_KEY:
            return self._claude_insights(
                trades, winners, losers, biases,
                win_rate, avg_winner, avg_loser, pf,
                best_day, worst_day, best_hour, best_strat,
                avg_rr, exit_too_early, hold_losers, total_pnl
            )
        return self._rule_insights(
            biases, win_rate, avg_winner, avg_loser, pf,
            best_day, best_hour, best_strat, exit_too_early, hold_losers, total_pnl
        )

    def _claude_insights(
        self, trades, winners, losers, biases,
        win_rate, avg_winner, avg_loser, pf,
        best_day, worst_day, best_hour, best_strat,
        avg_rr, exit_too_early, hold_losers, total_pnl
    ) -> tuple[list, str, list, list, list]:

        # Build detailed trade table (last 20)
        recent = sorted(trades, key=lambda t: t.entry_time or datetime.min)[-20:]
        trade_table = "\n".join([
            f"  {t.symbol.replace('NSE:','').replace('-EQ','')} | "
            f"{t.direction} | {t.strategy} | "
            f"Entry ₹{t.entry_price:.0f} | Exit ₹{t.exit_price:.0f} | "
            f"P&L ₹{t.realised_pnl:+,.0f} | "
            f"{t.holding_days:.0f}d | {t.exit_reason}"
            for t in recent
        ])

        detected_biases = [b for b in biases if b.detected]
        bias_text = "\n".join([
            f"  {b.name} ({b.severity}): {b.evidence}"
            for b in detected_biases
        ]) or "  No significant biases detected"

        prompt = f"""You are a professional trading coach reviewing a trader's journal.
Analyse their performance data and provide personalised, specific advice.
Be direct and honest — like a coach, not a cheerleader.

PERFORMANCE SUMMARY:
  Total trades:     {len(trades)}
  Win rate:         {win_rate:.0%}
  Profit factor:    {pf:.2f}
  Avg winner:       ₹{avg_winner:,.0f}
  Avg loser:        ₹{avg_loser:,.0f}
  Total P&L:        ₹{total_pnl:+,.0f}
  Avg R:R achieved: {avg_rr:.2f}x
  Best day:         {best_day}
  Worst day:        {worst_day}
  Best hour:        {best_hour}
  Best strategy:    {best_strat}
  Exits winners early: {"YES" if exit_too_early else "NO"}
  Holds losers long:   {"YES" if hold_losers else "NO"}

BEHAVIOURAL BIASES DETECTED:
{bias_text}

LAST 20 TRADES:
{trade_table}

Provide your analysis in this EXACT format (JSON, no markdown):
{{
  "narrative": "3-4 sentence honest assessment of this trader's current state",
  "personalised_rules": [
    "Rule 1: specific, measurable rule based on THEIR data",
    "Rule 2: specific, measurable rule based on THEIR data",
    "Rule 3: specific, measurable rule based on THEIR data"
  ],
  "strengths": ["strength 1", "strength 2"],
  "improvement_areas": ["area 1", "area 2", "area 3"],
  "missed_opportunities": ["pattern 1 they keep missing", "pattern 2"]
}}

Rules must reference their actual numbers. 
Example: "Never trade on Fridays — your Friday win rate is 28% vs 61% other days"
NOT generic advice like "stick to your plan" or "manage your emotions"."""

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
                    "max_tokens": 800,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            content = resp.json()["content"][0]["text"].strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            data = json.loads(content)
            return (
                data.get("personalised_rules", []),
                data.get("narrative", ""),
                data.get("missed_opportunities", []),
                data.get("strengths", []),
                data.get("improvement_areas", []),
            )

        except Exception as e:
            logger.error(f"[JournalAnalyser] Claude failed: {e}")
            return self._rule_insights(
                biases, win_rate, avg_winner, avg_loser, pf,
                best_day, best_hour, best_strat, exit_too_early, hold_losers, total_pnl
            )

    def _rule_insights(
        self, biases, win_rate, avg_winner, avg_loser, pf,
        best_day, best_hour, best_strat, exit_too_early, hold_losers, total_pnl
    ) -> tuple[list, str, list, list, list]:
        """Rule-based insights when Claude API not available."""
        rules = []

        # Rule based on day pattern
        if best_day and best_day != "Unknown":
            rules.append(
                f"Prioritise trading on {best_day}s — your data shows this is your "
                f"best performing day. Be more selective on other days."
            )

        # Rule based on exit pattern
        if exit_too_early:
            rules.append(
                f"Stop exiting winners early. Your avg R:R achieved is below 1.5x. "
                f"Once T1 is hit, trail your stop — never manually close a running winner."
            )
        elif hold_losers:
            rules.append(
                f"You are holding losers too long. "
                f"From now: if price hits stop loss, close immediately — no exceptions, no hoping."
            )
        else:
            rules.append(
                f"Your exit discipline is reasonable. "
                f"Focus on improving entry quality — only take setups scoring > 7/10."
            )

        # Rule based on strategy
        if best_strat and best_strat != "Unknown":
            rules.append(
                f"Focus on {best_strat} — it's your best performing strategy. "
                f"When in doubt between strategies, always default to this one."
            )

        # Ensure 3 rules
        while len(rules) < 3:
            fallback_rules = [
                f"Profit factor is {pf:.2f}. Target > 2.0. Improve by letting winners run longer.",
                f"Win rate is {win_rate:.0%}. Focus on setup quality over trade quantity.",
                "Review every trade within 24 hours. Journal: what worked, what didn't, why.",
            ]
            for r in fallback_rules:
                if r not in rules and len(rules) < 3:
                    rules.append(r)

        detected = [b for b in biases if b.detected]
        narrative = (
            f"[SIMULATION MODE — Add ANTHROPIC_API_KEY for AI coaching]\n\n"
            f"Based on {sum(1 for b in biases if b.detected)} behavioural patterns detected: "
            f"win rate {win_rate:.0%}, profit factor {pf:.2f}, "
            f"total P&L ₹{total_pnl:+,.0f}. "
            + (f"Key concern: {detected[0].name} detected." if detected else "No critical biases detected.")
        )

        strengths = [
            f"Win rate of {win_rate:.0%}" + (" is strong" if win_rate > 0.55 else " — room to improve"),
            f"Best day is {best_day} — concentrate effort there",
        ]

        improvements = [b.name for b in detected[:3]] or [
            "Increase sample size — need more trades for reliable analysis"
        ]

        missed = [
            "Theme-driven setups that fired after signal rejection",
            "Breakouts during high-volume opening hour",
        ]

        return rules[:3], narrative, missed, strengths, improvements

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _save_report(self, analysis: JournalAnalysis) -> None:
        ts   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
        path = os.path.join(REPORTS_DIR, f"journal_{ts}.json")
        data = {
            "generated_at":      analysis.generated_at,
            "total_trades":      analysis.total_trades,
            "date_range":        analysis.date_range,
            "win_rate":          analysis.win_rate,
            "profit_factor":     analysis.profit_factor,
            "avg_holding_days":  analysis.avg_holding_days,
            "total_pnl":         analysis.total_pnl,
            "best_day":          analysis.best_day,
            "worst_day":         analysis.worst_day,
            "best_hour":         analysis.best_hour,
            "best_strategy":     analysis.best_strategy,
            "avg_rr_achieved":   analysis.avg_rr_achieved,
            "biases":            [
                {"name": b.name, "detected": b.detected,
                 "severity": b.severity, "evidence": b.evidence,
                 "fix": b.fix}
                for b in analysis.biases
            ],
            "personalised_rules":  analysis.personalised_rules,
            "narrative":           analysis.narrative,
            "strengths":           analysis.strengths,
            "improvement_areas":   analysis.improvement_areas,
            "missed_opportunities": analysis.missed_opportunities,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"[JournalAnalyser] Report saved: {path}")

    def _log_summary(self, analysis: JournalAnalysis) -> None:
        logger.info("─" * 60)
        logger.info(f"JOURNAL ANALYSIS — {analysis.date_range}")
        logger.info(f"  Trades:        {analysis.total_trades}")
        logger.info(f"  Win rate:      {analysis.win_rate:.0f}%")
        logger.info(f"  Profit factor: {analysis.profit_factor:.2f}")
        logger.info(f"  Total P&L:     ₹{analysis.total_pnl:+,.0f}")
        logger.info(f"  Best day:      {analysis.best_day}")
        logger.info(f"  Best strategy: {analysis.best_strategy}")
        biases = [b.name for b in analysis.biases if b.detected]
        if biases:
            logger.info(f"  Biases:        {', '.join(biases)}")
        logger.info(f"  Rules:")
        for i, r in enumerate(analysis.personalised_rules, 1):
            logger.info(f"    {i}. {r}")
        logger.info("─" * 60)

    def _insufficient_data_result(self, count: int, needed: int) -> JournalAnalysis:
        return JournalAnalysis(
            generated_at      = datetime.now(tz=timezone.utc).isoformat(),
            total_trades      = count,
            date_range        = "Insufficient data",
            win_rate          = 0, avg_winner_pnl=0, avg_loser_pnl=0,
            profit_factor     = 0, avg_holding_days=0, total_pnl=0,
            best_day          = "N/A", worst_day="N/A",
            best_hour         = "N/A", worst_hour="N/A",
            best_strategy     = "N/A", worst_strategy="N/A",
            avg_rr_achieved   = 0, exit_too_early=False, hold_losers_long=False,
            biases            = [],
            personalised_rules = [
                f"Complete at least {needed} trades before the journal analyser can identify patterns.",
                "Focus on executing your plan correctly — results come from consistent process.",
                "Record the reason for every trade entry and exit — this data feeds the analyser.",
            ],
            narrative = (
                f"Only {count} trades recorded — need {needed} minimum for meaningful analysis. "
                f"Keep trading and recording. The more data, the more precise the insights."
            ),
            missed_opportunities = [],
            strengths            = [],
            improvement_areas    = ["Build trade history — minimum 5 trades needed"],
        )


def print_full_report(analysis: JournalAnalysis) -> None:
    print(f"\n{'=' * 65}")
    print(f"TRADING JOURNAL ANALYSIS")
    print(f"Period: {analysis.date_range}")
    print(f"{'=' * 65}")

    if analysis.total_trades < MIN_TRADES_FOR_ANALYSIS:
        print(f"\n  Only {analysis.total_trades} trades recorded.")
        print(f"  Need {MIN_TRADES_FOR_ANALYSIS} trades for full analysis.")
        print(f"\n  Rules to follow now:")
        for i, r in enumerate(analysis.personalised_rules, 1):
            print(f"  {i}. {r}")
        print(f"\n{'=' * 65}\n")
        return

    print(f"\n── PERFORMANCE ────────────────────────────────────────")
    print(f"  Trades:        {analysis.total_trades}")
    print(f"  Win rate:      {analysis.win_rate:.0f}%")
    print(f"  Profit factor: {analysis.profit_factor:.2f}")
    print(f"  Avg winner:    ₹{analysis.avg_winner_pnl:,.0f}")
    print(f"  Avg loser:     ₹{analysis.avg_loser_pnl:,.0f}")
    print(f"  Total P&L:     ₹{analysis.total_pnl:+,.0f}")
    print(f"  Avg R:R:       {analysis.avg_rr_achieved:.2f}x")
    print(f"  Avg hold:      {analysis.avg_holding_days:.1f} days")

    print(f"\n── PATTERNS ────────────────────────────────────────────")
    print(f"  Best day:      {analysis.best_day}")
    print(f"  Worst day:     {analysis.worst_day}")
    print(f"  Best hour:     {analysis.best_hour}")
    print(f"  Best strategy: {analysis.best_strategy}")

    print(f"\n── BEHAVIOURAL BIASES ──────────────────────────────────")
    for b in analysis.biases:
        flag = "⚠ DETECTED" if b.detected else "✓ clean"
        print(f"  {b.name:<30} {flag} [{b.severity}]")
        if b.detected:
            print(f"    Evidence: {b.evidence}")
            print(f"    Fix:      {b.fix}")

    print(f"\n── STRENGTHS ───────────────────────────────────────────")
    for s in analysis.strengths:
        print(f"  + {s}")

    print(f"\n── IMPROVEMENT AREAS ───────────────────────────────────")
    for a in analysis.improvement_areas:
        print(f"  → {a}")

    print(f"\n── ANALYST NARRATIVE ───────────────────────────────────")
    print(f"  {analysis.narrative}")

    print(f"\n── YOUR 3 PERSONALISED RULES ───────────────────────────")
    for i, rule in enumerate(analysis.personalised_rules, 1):
        print(f"\n  Rule {i}:")
        print(f"  {rule}")

    print(f"\n{'=' * 65}\n")


# ── Module-level singleton ────────────────────────────────────────
journal_analyser = JournalAnalyser()


# ── Standalone execution ──────────────────────────────────────────
if __name__ == "__main__":
    analysis = journal_analyser.analyse(min_trades=1)   # min_trades=1 for demo
    if analysis:
        print_full_report(analysis)