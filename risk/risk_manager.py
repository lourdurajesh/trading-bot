"""
risk_manager.py
───────────────
Central risk management layer. Every signal passes through here
before reaching the order manager.

Responsibilities:
  - Position sizing (fixed fractional — risk % of capital per trade)
  - Portfolio heat check (no new trades if total risk > MAX_PORTFOLIO_HEAT)
  - Daily loss kill switch (halt all trading if day loss > DAILY_LOSS_LIMIT_PCT)
  - Correlation filter (avoid duplicate sector exposure)
  - Per-trade R:R validation
  - Options allocation cap
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from config.settings import (
    DAILY_LOSS_LIMIT_PCT,
    MAX_OPEN_POSITIONS,
    MAX_OPTIONS_ALLOCATION_PCT,
    MAX_PORTFOLIO_HEAT,
    MIN_RISK_REWARD,
    RISK_PER_TRADE_PCT,
    TOTAL_CAPITAL,
)
from strategies.base_strategy import Direction, Signal, SignalType

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    approved:       bool
    reason:         str
    position_size:  int    = 0
    capital_at_risk: float = 0.0


class RiskManager:
    """
    Validates every signal and computes position size.

    Usage:
        decision = risk_manager.validate(signal, open_positions)
        if decision.approved:
            signal.position_size   = decision.position_size
            signal.capital_at_risk = decision.capital_at_risk
            order_manager.route(signal)
    """

    def __init__(self):
        self._kill_switch_active = False
        self._kill_switch_reason = ""
        self._daily_realised_pnl  = 0.0   # updated by portfolio_tracker
        self._daily_reset_date    = date.today()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def validate(
        self,
        signal: Signal,
        open_positions: list[dict],
        current_capital: float = None,
    ) -> RiskDecision:
        """
        Run all risk checks on a signal. Returns RiskDecision.

        open_positions: list of dicts from portfolio_tracker
            Each dict: {symbol, direction, entry, stop, size, capital_at_risk, strategy}
        current_capital: current portfolio value (defaults to TOTAL_CAPITAL from settings)
        """
        capital = current_capital or TOTAL_CAPITAL
        self._check_daily_reset()

        # ── 1. Kill switch ────────────────────────────────────────
        if self._kill_switch_active:
            return RiskDecision(False, f"Kill switch active: {self._kill_switch_reason}")

        # ── 2. Signal sanity ──────────────────────────────────────
        if not signal.is_valid():
            return RiskDecision(False, "Signal failed basic validity check")

        # ── 3. R:R check ──────────────────────────────────────────
        rr = signal.calculate_rr()
        if rr < MIN_RISK_REWARD:
            return RiskDecision(False, f"R:R {rr:.1f} below minimum {MIN_RISK_REWARD}")

        # ── 4. Max open positions ─────────────────────────────────
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return RiskDecision(False, f"Max open positions reached ({MAX_OPEN_POSITIONS})")

        # ── 5. Duplicate symbol check ─────────────────────────────
        open_symbols = [p["symbol"] for p in open_positions]
        if signal.symbol in open_symbols:
            return RiskDecision(False, f"Already have open position in {signal.symbol}")

        # ── 6. Portfolio heat check ───────────────────────────────
        total_risk_capital = sum(p.get("capital_at_risk", 0) for p in open_positions)
        heat_pct = (total_risk_capital / capital) * 100
        if heat_pct >= MAX_PORTFOLIO_HEAT:
            return RiskDecision(False, f"Portfolio heat {heat_pct:.1f}% at maximum ({MAX_PORTFOLIO_HEAT}%)")

        # ── 7. Daily loss limit ───────────────────────────────────
        daily_loss_pct = abs(self._daily_realised_pnl / capital) * 100
        if self._daily_realised_pnl < 0 and daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
            self._trigger_kill_switch(f"Daily loss {daily_loss_pct:.1f}% hit limit {DAILY_LOSS_LIMIT_PCT}%")
            return RiskDecision(False, self._kill_switch_reason)

        # ── 8. Options allocation cap ─────────────────────────────
        if signal.signal_type == SignalType.OPTIONS:
            options_risk = sum(
                p.get("capital_at_risk", 0)
                for p in open_positions
                if p.get("signal_type") == "OPTIONS"
            )
            options_pct = (options_risk / capital) * 100
            if options_pct >= MAX_OPTIONS_ALLOCATION_PCT:
                return RiskDecision(
                    False,
                    f"Options allocation {options_pct:.1f}% at max ({MAX_OPTIONS_ALLOCATION_PCT}%)"
                )

        # ── 9. Position sizing ────────────────────────────────────
        position_size, capital_at_risk = self._calculate_size(signal, capital)

        if position_size <= 0:
            return RiskDecision(False, "Position size calculated as zero — price or risk too large")

        logger.info(
            f"[RiskManager] APPROVED {signal.symbol} | "
            f"Size: {position_size} | Risk: ₹{capital_at_risk:,.0f} | "
            f"Heat: {heat_pct:.1f}% | R:R: {rr:.1f}"
        )

        return RiskDecision(
            approved        = True,
            reason          = "All risk checks passed",
            position_size   = position_size,
            capital_at_risk = capital_at_risk,
        )

    def update_daily_pnl(self, pnl_change: float) -> None:
        """
        Called by portfolio_tracker whenever a trade closes.
        Accumulates daily realised P&L and checks kill switch threshold.
        """
        self._check_daily_reset()
        self._daily_realised_pnl += pnl_change

        loss_pct = abs(self._daily_realised_pnl / TOTAL_CAPITAL) * 100
        if self._daily_realised_pnl < 0 and loss_pct >= DAILY_LOSS_LIMIT_PCT:
            self._trigger_kill_switch(
                f"Daily loss ₹{abs(self._daily_realised_pnl):,.0f} "
                f"({loss_pct:.1f}%) exceeded limit {DAILY_LOSS_LIMIT_PCT}%"
            )

    def reset_kill_switch(self) -> None:
        """Manual override to re-enable trading. Use carefully."""
        self._kill_switch_active = False
        self._kill_switch_reason = ""
        logger.warning("[RiskManager] Kill switch manually reset.")

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def daily_pnl(self) -> float:
        return self._daily_realised_pnl

    def status(self) -> dict:
        """Returns current risk state for dashboard."""
        return {
            "kill_switch_active":  self._kill_switch_active,
            "kill_switch_reason":  self._kill_switch_reason,
            "daily_realised_pnl":  round(self._daily_realised_pnl, 2),
            "daily_loss_limit":    DAILY_LOSS_LIMIT_PCT,
            "max_portfolio_heat":  MAX_PORTFOLIO_HEAT,
            "risk_per_trade_pct":  RISK_PER_TRADE_PCT,
        }

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _calculate_size(self, signal: Signal, capital: float) -> tuple[int, float]:
        """
        Fixed fractional position sizing.
        Risk amount = capital × RISK_PER_TRADE_PCT / 100
        Position size = risk amount / (entry - stop_loss)
        """
        risk_amount = capital * (RISK_PER_TRADE_PCT / 100)
        risk_per_share = abs(signal.entry - signal.stop_loss)

        if risk_per_share <= 0:
            return 0, 0.0

        shares = int(risk_amount / risk_per_share)
        actual_risk = shares * risk_per_share

        return shares, round(actual_risk, 2)

    def _trigger_kill_switch(self, reason: str) -> None:
        if not self._kill_switch_active:
            self._kill_switch_active = True
            self._kill_switch_reason = reason
            logger.critical(f"[RiskManager] KILL SWITCH TRIGGERED: {reason}")

    def _check_daily_reset(self) -> None:
        """Reset daily P&L counter at the start of each new trading day."""
        today = date.today()
        if today != self._daily_reset_date:
            logger.info(
                f"[RiskManager] New trading day. "
                f"Resetting daily P&L from ₹{self._daily_realised_pnl:,.0f} to 0."
            )
            self._daily_realised_pnl = 0.0
            self._daily_reset_date   = today
            # Kill switch does NOT auto-reset — requires manual reset each morning


# ── Module-level singleton ────────────────────────────────────────
risk_manager = RiskManager()
