"""
daily_risk_budget.py
────────────────────
Spec TASK 13: Daily Risk Budget Engine.

Tracks P&L per strategy within each trading day and enforces hard limits:
  - max_daily_loss_pct       : global daily loss ceiling (kills all trading)
  - max_strategy_loss_pct    : per-strategy loss ceiling (kills that strategy)
  - max_open_positions       : global open position cap
  - max_correlated_positions : max positions in same underlying/sector

Separate from the existing risk_manager.py kill-switch — this layer adds
granularity (per-strategy) without replacing the global kill switch.

Feature flag: DAILY_RISK_BUDGET_ENABLED (default True).
On any error, check() returns (True, "") — fails open to avoid blocking trading.

Resets automatically each trading day.
"""

import logging
import os
import threading
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED = os.getenv("DAILY_RISK_BUDGET_ENABLED", "true").lower() != "false"

try:
    from config.settings import TOTAL_CAPITAL, DAILY_LOSS_LIMIT_PCT
    MAX_GLOBAL_LOSS_PCT   = DAILY_LOSS_LIMIT_PCT           # e.g. 3.0%
except Exception:
    TOTAL_CAPITAL         = 500_000
    MAX_GLOBAL_LOSS_PCT   = 3.0

# Per-strategy loss limit (% of capital) — tighter than global
MAX_STRATEGY_LOSS_PCT = float(os.getenv("MAX_STRATEGY_LOSS_PCT", "1.5"))
MAX_OPEN_POSITIONS    = int(os.getenv("MAX_OPEN_POSITIONS", "6"))
MAX_CORRELATED        = int(os.getenv("MAX_CORRELATED_POSITIONS", "3"))


class BudgetStatus:
    OK       = "OK"
    BLOCKED  = "BLOCKED"
    WARNING  = "WARNING"


class DailyRiskBudget:
    """
    Per-strategy daily budget tracker.

    Usage:
        from risk.daily_risk_budget import daily_risk_budget

        # Before entering a trade:
        ok, reason = daily_risk_budget.check("TrendSpread", estimated_max_loss=500)
        if not ok:
            return  # blocked

        # After a trade closes:
        daily_risk_budget.record_pnl("TrendSpread", pnl=-300)

        # Dashboard:
        summary = daily_risk_budget.summary()
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reset_date: date = date.today()
        # Per-strategy accumulated P&L for today
        self._strategy_pnl: dict[str, float]  = {}
        # Global accumulated P&L for today
        self._global_pnl:  float = 0.0
        # Track open position count by symbol (key) → strategy name (val)
        self._open_positions: dict[str, str] = {}
        # Strategies halted for today
        self._halted_strategies: set[str] = set()
        self._global_halted: bool = False

    def _maybe_reset(self) -> None:
        """Auto-reset on new trading day."""
        today = date.today()
        if today != self._reset_date:
            self._reset_date        = today
            self._strategy_pnl      = {}
            self._global_pnl        = 0.0
            self._halted_strategies = set()
            self._global_halted     = False
            logger.info("[RiskBudget] Daily reset — all budgets cleared for new trading day")

    # ── Public API ───────────────────────────────────────────────

    def check(
        self,
        strategy_name: str,
        estimated_max_loss: float = 0.0,
        symbol: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Check whether a new trade is allowed under current budget.
        Returns (allowed: bool, reason: str).
        Fails open on engine error — never silently blocks trading.
        """
        if not ENABLED:
            return True, ""
        try:
            return self._check(strategy_name, estimated_max_loss, symbol)
        except Exception as e:
            logger.warning(f"[RiskBudget] check() error (fail-open): {e}")
            return True, ""

    def record_pnl(self, strategy_name: str, pnl: float) -> None:
        """Update accumulated P&L after a trade closes."""
        if not ENABLED:
            return
        try:
            with self._lock:
                self._maybe_reset()
                self._strategy_pnl[strategy_name] = (
                    self._strategy_pnl.get(strategy_name, 0.0) + pnl
                )
                self._global_pnl += pnl

                strat_pnl_pct  = abs(self._strategy_pnl[strategy_name]) / TOTAL_CAPITAL * 100
                global_pnl_pct = abs(self._global_pnl) / TOTAL_CAPITAL * 100

                # Halt strategy if per-strategy limit breached
                if (self._strategy_pnl[strategy_name] < 0 and
                        strat_pnl_pct >= MAX_STRATEGY_LOSS_PCT and
                        strategy_name not in self._halted_strategies):
                    self._halted_strategies.add(strategy_name)
                    logger.warning(
                        f"[RiskBudget] {strategy_name} HALTED for today — "
                        f"daily loss {strat_pnl_pct:.1f}% >= {MAX_STRATEGY_LOSS_PCT}% limit"
                    )

                # Halt all if global limit breached
                if self._global_pnl < 0 and global_pnl_pct >= MAX_GLOBAL_LOSS_PCT:
                    self._global_halted = True
                    logger.error(
                        f"[RiskBudget] GLOBAL HALT — daily loss {global_pnl_pct:.1f}% "
                        f">= {MAX_GLOBAL_LOSS_PCT}% limit"
                    )
        except Exception as e:
            logger.debug(f"[RiskBudget] record_pnl() error: {e}")

    def register_open(self, symbol: str, strategy_name: str) -> None:
        """Call when a position opens."""
        if not ENABLED:
            return
        with self._lock:
            self._maybe_reset()
            self._open_positions[symbol] = strategy_name

    def register_close(self, symbol: str) -> None:
        """Call when a position closes."""
        if not ENABLED:
            return
        with self._lock:
            self._open_positions.pop(symbol, None)

    def summary(self) -> dict:
        """Full budget snapshot for dashboard."""
        if not ENABLED:
            return {"enabled": False}
        with self._lock:
            self._maybe_reset()
            global_loss_pct = self._global_pnl / TOTAL_CAPITAL * 100
            strategy_statuses = {}
            for s, pnl in self._strategy_pnl.items():
                pct = pnl / TOTAL_CAPITAL * 100
                strategy_statuses[s] = {
                    "pnl":     round(pnl, 2),
                    "pct":     round(pct, 2),
                    "halted":  s in self._halted_strategies,
                    "status":  BudgetStatus.BLOCKED if s in self._halted_strategies else
                               BudgetStatus.WARNING if pct < -MAX_STRATEGY_LOSS_PCT * 0.7 else
                               BudgetStatus.OK,
                }
            return {
                "enabled":           True,
                "date":              self._reset_date.isoformat(),
                "global_pnl":        round(self._global_pnl, 2),
                "global_pnl_pct":    round(global_loss_pct, 2),
                "global_halted":     self._global_halted,
                "global_limit_pct":  MAX_GLOBAL_LOSS_PCT,
                "strategy_limit_pct":MAX_STRATEGY_LOSS_PCT,
                "open_positions":    len(self._open_positions),
                "max_positions":     MAX_OPEN_POSITIONS,
                "strategies":        strategy_statuses,
                "halted_strategies": list(self._halted_strategies),
            }

    # ── Internal ─────────────────────────────────────────────────

    def _check(
        self,
        strategy_name: str,
        estimated_max_loss: float,
        symbol: Optional[str],
    ) -> tuple[bool, str]:
        with self._lock:
            self._maybe_reset()

            # Global halt check
            if self._global_halted:
                return False, f"global daily loss limit {MAX_GLOBAL_LOSS_PCT}% breached — all trading halted"

            # Strategy halt check
            if strategy_name in self._halted_strategies:
                return False, f"{strategy_name} halted — per-strategy daily loss limit reached"

            # Max open positions check
            open_count = len(self._open_positions)
            if symbol and symbol not in self._open_positions and open_count >= MAX_OPEN_POSITIONS:
                return False, f"max open positions {MAX_OPEN_POSITIONS} reached"

            # Would this trade push global loss past limit?
            if estimated_max_loss > 0:
                projected_loss = self._global_pnl - estimated_max_loss
                projected_pct  = abs(projected_loss) / TOTAL_CAPITAL * 100
                if projected_loss < 0 and projected_pct > MAX_GLOBAL_LOSS_PCT:
                    return False, (
                        f"trade would push daily loss to {projected_pct:.1f}% "
                        f"(limit {MAX_GLOBAL_LOSS_PCT}%)"
                    )

                # Per-strategy check
                strat_loss = self._strategy_pnl.get(strategy_name, 0.0) - estimated_max_loss
                strat_pct  = abs(strat_loss) / TOTAL_CAPITAL * 100
                if strat_loss < 0 and strat_pct > MAX_STRATEGY_LOSS_PCT:
                    return False, (
                        f"{strategy_name} trade would exceed strategy loss limit "
                        f"{strat_pct:.1f}% > {MAX_STRATEGY_LOSS_PCT}%"
                    )

            # Correlated positions check (same underlying)
            if symbol:
                base = symbol.split(":")[1] if ":" in symbol else symbol
                # Strip trailing letters/numbers to get base instrument name
                import re
                instrument = re.sub(r"(FUT|CE|PE|\d{2}[A-Z]{3})\w*$", "", base).rstrip()
                correlated = sum(
                    1 for sym in self._open_positions
                    if instrument and instrument.upper() in sym.upper()
                )
                if correlated >= MAX_CORRELATED:
                    return False, (
                        f"max correlated positions {MAX_CORRELATED} reached for {instrument}"
                    )

            return True, ""


# Singleton
daily_risk_budget = DailyRiskBudget()
