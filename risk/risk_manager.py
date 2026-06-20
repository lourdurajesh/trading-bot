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

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.settings import (
    DAILY_LOSS_LIMIT_PCT,
    MAX_OPEN_POSITIONS,
    MAX_OPTIONS_ALLOCATION_PCT,
    MAX_PORTFOLIO_HEAT,
    MIN_RISK_REWARD,
    MIN_RISK_REWARD_OPTIONS,
    RISK_PER_TRADE_PCT,
    TOTAL_CAPITAL,
)
from risk.options_risk import options_risk_gate
from strategies.base_strategy import Direction, Signal, SignalType

logger = logging.getLogger(__name__)

from config.settings import DB_PATH as _DB_PATH
_RISK_STATE_PATH = os.path.join(os.path.dirname(_DB_PATH) or "db", "risk_state.json")


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
        self._daily_reset_date    = datetime.now(tz=IST).date()
        self._load_state()

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
        rr      = signal.calculate_rr()
        min_rr  = MIN_RISK_REWARD_OPTIONS if signal.signal_type == SignalType.OPTIONS else MIN_RISK_REWARD
        if rr < min_rr:
            return RiskDecision(False, f"R:R {rr:.1f} below minimum {min_rr}")

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

            # ── 8b. Options-specific safety gate ─────────────────
            opt_approved, opt_reason, approved_lots = options_risk_gate.check(signal, capital)
            if not opt_approved:
                return RiskDecision(False, f"[OptionsGate] {opt_reason}")
            # Store approved lots back on signal so position sizing uses it
            signal._approved_lots = approved_lots

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

    def update_daily_pnl(self, pnl_change: float, signal_type: str = "EQUITY") -> None:
        """
        Called by portfolio_tracker whenever a trade closes.
        Accumulates daily realised P&L and checks kill switch threshold.
        Also forwards options P&L to options_risk_gate for its separate kill switch.
        """
        self._check_daily_reset()
        self._daily_realised_pnl += pnl_change
        self._save_state()

        # Forward options P&L to options-specific gate
        if signal_type == "OPTIONS":
            try:
                options_risk_gate.update_daily_pnl(pnl_change, TOTAL_CAPITAL)
            except Exception:
                pass

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
        self._save_state()
        logger.warning("[RiskManager] Kill switch manually reset.")

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def daily_pnl(self) -> float:
        return self._daily_realised_pnl

    def status(self) -> dict:
        """Returns current risk state for dashboard."""
        d = {
            "kill_switch_active":  self._kill_switch_active,
            "kill_switch_reason":  self._kill_switch_reason,
            "daily_realised_pnl":  round(self._daily_realised_pnl, 2),
            "daily_loss_limit":    DAILY_LOSS_LIMIT_PCT,
            "max_portfolio_heat":  MAX_PORTFOLIO_HEAT,
            "risk_per_trade_pct":  RISK_PER_TRADE_PCT,
        }
        try:
            d["options"] = options_risk_gate.status()
        except Exception:
            pass
        return d

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _calculate_size(self, signal: Signal, capital: float) -> tuple[int, float]:
        """
        Position sizing — equity vs options routed separately.

        Equity: shares = risk_budget / risk_per_share
        Options: lots already computed by options_risk_gate.check() and stored
                 on signal._approved_lots; position_size = lots × lot_size
        """
        if signal.signal_type == SignalType.OPTIONS:
            return self._calculate_options_size(signal)

        # ── Equity / futures sizing ───────────────────────────────
        # Fixed-fractional sizing + tight-stop guard live in the shared sizer so the
        # rule is identical wherever equity is sized.
        from execution.sizing import shares_to_fit
        risk_amount        = capital * (RISK_PER_TRADE_PCT / 100)
        shares, actual_risk, reason = shares_to_fit(signal.entry, signal.stop_loss, risk_amount)
        if reason:
            logger.warning(f"[RiskManager] {signal.symbol}: {reason} — rejecting")
            return 0, 0.0
        return shares, actual_risk

    def _calculate_options_size(self, signal: Signal) -> tuple[int, float]:
        """
        Options position sizing.
        Units = lots × lot_size (e.g., 2 lots × 75 = 150 units for NIFTY).
        capital_at_risk = premium × units  (entire premium is at risk for debit spreads)
        """
        lots     = getattr(signal, "_approved_lots", 1)
        lot_size = int((signal.options_meta or {}).get("lot_size", 1))
        if lot_size <= 0:
            lot_size = 1

        units           = lots * lot_size
        capital_at_risk = round(signal.entry * units, 2)
        return units, capital_at_risk

    def _trigger_kill_switch(self, reason: str) -> None:
        if not self._kill_switch_active:
            self._kill_switch_active = True
            self._kill_switch_reason = reason
            self._save_state()
            logger.critical(f"[RiskManager] KILL SWITCH TRIGGERED: {reason}")
            try:
                from audit_log import audit_log
                audit_log.kill_switch(activated=True, reason=reason)
            except Exception:
                pass

    def _check_daily_reset(self) -> None:
        """Reset daily P&L counter at the start of each new trading day."""
        today = datetime.now(tz=IST).date()
        if today != self._daily_reset_date:
            logger.info(
                f"[RiskManager] New trading day. "
                f"Resetting daily P&L from ₹{self._daily_realised_pnl:,.0f} to 0."
            )
            self._daily_realised_pnl = 0.0
            self._daily_reset_date   = today
            self._save_state()
            # Kill switch does NOT auto-reset — requires manual reset each morning

    def _load_state(self) -> None:
        """Restore kill switch and daily P&L from disk on startup."""
        try:
            if not os.path.exists(_RISK_STATE_PATH):
                return
            with open(_RISK_STATE_PATH) as f:
                data = json.load(f)
            today      = datetime.now(tz=IST).date()
            saved_date = date.fromisoformat(data.get("daily_reset_date", "2000-01-01"))
            if saved_date == today:
                self._daily_realised_pnl = float(data.get("daily_pnl", 0.0))
                self._daily_reset_date   = today
            if data.get("kill_switch_active", False):
                self._kill_switch_active = True
                self._kill_switch_reason = data.get("kill_switch_reason", "Restored on restart")
                logger.warning(
                    f"[RiskManager] Kill switch RESTORED after restart: {self._kill_switch_reason}"
                )
            if saved_date == today and self._daily_realised_pnl != 0.0:
                logger.info(
                    f"[RiskManager] Daily P&L restored: ₹{self._daily_realised_pnl:,.0f}"
                )
        except Exception as e:
            logger.warning(f"[RiskManager] Could not load state file: {e}")

    def _save_state(self) -> None:
        """Persist kill switch and daily P&L to disk."""
        try:
            os.makedirs(os.path.dirname(_RISK_STATE_PATH) or ".", exist_ok=True)
            with open(_RISK_STATE_PATH, "w") as f:
                json.dump({
                    "kill_switch_active": self._kill_switch_active,
                    "kill_switch_reason": self._kill_switch_reason,
                    "daily_pnl":          self._daily_realised_pnl,
                    "daily_reset_date":   self._daily_reset_date.isoformat(),
                }, f)
        except Exception as e:
            logger.warning(f"[RiskManager] Could not save state file: {e}")


# ── Module-level singleton ────────────────────────────────────────
risk_manager = RiskManager()
