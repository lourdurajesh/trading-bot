"""
alert_service.py
────────────────
Central Telegram notification hub. Every significant bot event flows
through here — one place to read, one place to change message formats.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HOW TO ADD A NEW EVENT — TWO STEPS ONLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Step 1 — Add a formatter to _FORMATTERS at the bottom of this file:

      "your_event": lambda param1, param2: (
          f"🔔 *YOUR EVENT TITLE*\\n"
          f"Param1: `{param1}`\\n"
          f"Param2: `{param2}`"
      ),

  Step 2 — Add a one-line public method:

      def your_event(self, param1: str, param2: str) -> None:
          self.emit("your_event", param1=param1, param2=param2)

  Done. The formatter handles the message; the method provides the API.
  All existing callers are unaffected.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Setup:
  1. Create a bot via @BotFather → get TELEGRAM_BOT_TOKEN
  2. Send any message to your bot, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates  → copy chat_id
  3. Add both to .env:
       TELEGRAM_BOT_TOKEN=...
       TELEGRAM_CHAT_ID=...
"""

import logging
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger  = logging.getLogger(__name__)
IST     = ZoneInfo("Asia/Kolkata")
_TS     = lambda: datetime.now(tz=IST).strftime("%d %b %H:%M IST")


# ─────────────────────────────────────────────────────────────────────────────
# EVENT FORMATTERS
# Add new events here — one lambda per event, kwargs must match emit() call.
# ─────────────────────────────────────────────────────────────────────────────

_FORMATTERS: dict[str, Any] = {

    # ── Bot lifecycle ─────────────────────────────────────────────────────────
    "bot_started": lambda mode, paper_trading: (
        f"🟢 *BOT STARTED*\n"
        f"Mode:    `{mode}`\n"
        f"Trading: `{'PAPER' if paper_trading else 'LIVE'}`\n"
        f"Time:    `{_TS()}`"
    ),

    "bot_stopped": lambda reason="Shutdown signal": (
        f"🔴 *BOT STOPPED*\n"
        f"Reason: `{reason}`\n"
        f"Time:   `{_TS()}`"
    ),

    # ── Conviction score (pre-market, 09:00–09:10 IST) ───────────────────────
    "conviction_score": lambda score, direction, tradeable, reasons,
                               capital_pct, iep_score, label="": (
        f"{'📈' if score > 0 else '📉' if score < 0 else '➖'} "
        f"*CONVICTION SCORE{' · ' + label if label else ''}*\n"
        f"Score:   `{score:+d}/10 — {direction}`  "
        f"{'✅ TRADE DAY' if tradeable else '⏸ below threshold'}\n"
        + (f"IEP gap: `{iep_score:+d}`\n" if iep_score != 0 else "")
        + (f"Capital: `{capital_pct}%`\n" if tradeable else "")
        + (("_Signals:_\n" + "\n".join(f"  • {r}" for r in reasons[:6])) if reasons else "")
    ),

    # ── NSE equity trades ─────────────────────────────────────────────────────
    "trade_opened": lambda symbol, direction, strategy, entry, stop,
                           target, rr, size, risk, confidence, order_id: (
        f"{'🟢' if direction == 'LONG' else '🔴'} *NSE TRADE OPENED*\n"
        f"Symbol:    `{symbol}`\n"
        f"Strategy:  `{strategy}`\n"
        f"Direction: `{direction}`\n"
        f"Entry:     `₹{entry:,.2f}`\n"
        f"Stop:      `₹{stop:,.2f}`\n"
        f"Target 1:  `₹{target:,.2f}`\n"
        f"R:R:       `{rr:.1f}`\n"
        f"Size:      `{size} shares`\n"
        f"Risk:      `₹{risk:,.0f}`\n"
        f"Confidence:`{confidence:.0%}`\n"
        f"Order ID:  `{order_id}`"
    ),

    "trade_closed": lambda symbol, pnl, reason="": (
        f"{'✅' if pnl >= 0 else '❌'} *NSE TRADE CLOSED*\n"
        f"Symbol: `{symbol}`\n"
        f"P&L:    `₹{pnl:+,.0f}`\n"
        f"Reason: `{reason}`"
    ),

    "signal_pending": lambda symbol, direction, entry, stop, target,
                             rr, confidence, reason, signal_id: (
        f"🟡 *SIGNAL PENDING CONFIRMATION*\n"
        f"ID:        `{signal_id}`\n"
        f"Symbol:    `{symbol}`\n"
        f"Direction: `{direction}`\n"
        f"Entry:     `₹{entry:,.2f}`\n"
        f"Stop:      `₹{stop:,.2f}`\n"
        f"Target:    `₹{target:,.2f}`\n"
        f"R:R:       `{rr:.1f}`\n"
        f"Confidence:`{confidence:.0%}`\n"
        f"Reason:    `{reason}`\n\n"
        f"_Confirm or reject in dashboard._"
    ),

    # ── MCX commodity trades ──────────────────────────────────────────────────
    "mcx_trade_opened": lambda instrument, direction, strategy, spot,
                                net_debit, lots, sl_price, target_pct, mode="PAPER": (
        f"{'🟢' if direction == 'LONG' else '🔴'} *MCX TRADE OPENED* [{mode}]\n"
        f"{'📈 LONG' if direction == 'LONG' else '📉 SHORT'}  `{instrument}` · `{strategy}`\n"
        f"Spot:    `₹{spot:,.2f}`\n"
        f"Debit:   `₹{net_debit:.2f}` / lot · Lots: `{lots}`\n"
        f"SL Spot: `₹{sl_price:,.2f}`\n"
        f"Target:  `{target_pct:.0%}` of debit"
    ),

    "mcx_trade_closed": lambda instrument, direction, pnl_approx, pnl_r,
                                exit_reason, entry_spot, exit_spot, mode="PAPER": (
        f"{'✅' if pnl_approx >= 0 else '❌'} *MCX TRADE CLOSED* [{mode}]\n"
        f"{'📈 LONG' if direction == 'LONG' else '📉 SHORT'}  `{instrument}`\n"
        f"Entry:  `₹{entry_spot:,.2f}` → Exit: `₹{exit_spot:,.2f}`\n"
        f"P&L:    `₹{pnl_approx:+,.0f}` ({pnl_r:+.1f}R)\n"
        f"Reason: `{exit_reason}`"
    ),

    # ── Risk / safety ─────────────────────────────────────────────────────────
    "kill_switch": lambda reason: (
        f"🚨 *KILL SWITCH TRIGGERED*\n"
        f"Reason: `{reason}`\n"
        f"All trading halted until manually reset."
    ),

    # ── System health ─────────────────────────────────────────────────────────
    "system_alert": lambda component, message, severity="error": (
        f"{'🚨' if severity == 'critical' else '⚠️'} "
        f"*SYSTEM ALERT — {severity.upper()}*\n"
        f"Component: `{component}`\n"
        f"Detail:    {message}"
    ),

    # ── Daily summary ─────────────────────────────────────────────────────────
    "daily_summary": lambda pnl, pnl_pct, trades, win_rate, drawdown_pct: (
        f"{'📈' if pnl >= 0 else '📉'} *DAILY SUMMARY*\n"
        f"P&L:      `₹{pnl:+,.0f} ({pnl_pct:+.1f}%)`\n"
        f"Trades:   `{trades}`\n"
        f"Win Rate: `{win_rate:.0f}%`\n"
        f"Max DD:   `{drawdown_pct:.1f}%`"
    ),

    # ── Generic info ──────────────────────────────────────────────────────────
    "info": lambda message: f"ℹ️ {message}",
}


# ─────────────────────────────────────────────────────────────────────────────
# SERVICE
# ─────────────────────────────────────────────────────────────────────────────

class AlertService:
    """
    Central Telegram notification hub.

    Every event has a typed method and a registered formatter.
    Add a new event by following the two-step guide at the top of this file.
    """

    def __init__(self) -> None:
        self._enabled  = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        self._api_url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        if not self._enabled:
            logger.info("[Alerts] Telegram not configured — notifications disabled.")

    # ── Core dispatcher ───────────────────────────────────────────────────────

    def emit(self, event: str, **kwargs) -> None:
        """
        Send a typed event notification.
        Looks up the formatter in _FORMATTERS, formats the message, and sends it.
        Silently skips unknown event names (logs a debug warning).
        """
        fmt = _FORMATTERS.get(event)
        if fmt is None:
            logger.debug(f"[Alerts] Unknown event '{event}' — add it to _FORMATTERS")
            return
        try:
            msg = fmt(**kwargs)
            self._send(msg)
        except Exception as exc:
            logger.debug(f"[Alerts] Formatter error for '{event}': {exc}")

    # ── Typed public methods ──────────────────────────────────────────────────
    # Each method is a thin wrapper around emit(). The method signature
    # documents what data the event expects; the formatter in _FORMATTERS
    # defines the Telegram message format.

    # Bot lifecycle
    def bot_started(self, mode: str, paper_trading: bool) -> None:
        self.emit("bot_started", mode=mode, paper_trading=paper_trading)

    def bot_stopped(self, reason: str = "Shutdown signal") -> None:
        self.emit("bot_stopped", reason=reason)

    # Conviction score
    def conviction_score(
        self, score: int, direction: str, tradeable: bool,
        reasons: list, capital_pct: int, iep_score: int = 0, label: str = ""
    ) -> None:
        self.emit(
            "conviction_score", score=score, direction=direction,
            tradeable=tradeable, reasons=reasons, capital_pct=capital_pct,
            iep_score=iep_score, label=label,
        )

    # NSE equity trades (backward-compatible signatures kept)
    def trade_opened(self, signal, order_id: str) -> None:
        self.emit(
            "trade_opened",
            symbol=signal.symbol, direction=signal.direction.value,
            strategy=signal.strategy, entry=signal.entry,
            stop=signal.stop_loss, target=signal.target_1,
            rr=signal.risk_reward, size=signal.position_size,
            risk=signal.capital_at_risk, confidence=signal.confidence,
            order_id=order_id,
        )

    def trade_closed(self, symbol: str, pnl: float, reason: str = "") -> None:
        self.emit("trade_closed", symbol=symbol, pnl=pnl, reason=reason)

    def signal_pending(self, signal, signal_id: str) -> None:
        self.emit(
            "signal_pending",
            symbol=signal.symbol, direction=signal.direction.value,
            entry=signal.entry, stop=signal.stop_loss, target=signal.target_1,
            rr=signal.risk_reward, confidence=signal.confidence,
            reason=signal.reason, signal_id=signal_id,
        )

    # MCX commodity trades
    def mcx_trade_opened(
        self, instrument: str, direction: str, strategy: str,
        spot: float, net_debit: float, lots: int,
        sl_price: float, target_pct: float, mode: str = "PAPER",
    ) -> None:
        self.emit(
            "mcx_trade_opened", instrument=instrument, direction=direction,
            strategy=strategy, spot=spot, net_debit=net_debit, lots=lots,
            sl_price=sl_price, target_pct=target_pct, mode=mode,
        )

    def mcx_trade_closed(
        self, instrument: str, direction: str, pnl_approx: float,
        pnl_r: float, exit_reason: str, entry_spot: float,
        exit_spot: float, mode: str = "PAPER",
    ) -> None:
        self.emit(
            "mcx_trade_closed", instrument=instrument, direction=direction,
            pnl_approx=pnl_approx, pnl_r=pnl_r, exit_reason=exit_reason,
            entry_spot=entry_spot, exit_spot=exit_spot, mode=mode,
        )

    # Risk / safety
    def kill_switch(self, reason: str) -> None:
        self.emit("kill_switch", reason=reason)

    # System health
    def system_alert(self, component: str, message: str, severity: str = "error") -> None:
        self.emit("system_alert", component=component, message=message, severity=severity)

    # Daily summary
    def daily_summary(self, stats: dict) -> None:
        self.emit(
            "daily_summary",
            pnl=stats.get("total_realised_pnl", 0),
            pnl_pct=stats.get("total_pnl_pct", 0),
            trades=stats.get("total_trades", 0),
            win_rate=stats.get("win_rate", 0),
            drawdown_pct=stats.get("drawdown_pct", 0),
        )

    # Generic info (for ad-hoc messages from other modules)
    def info(self, message: str) -> None:
        self.emit("info", message=message)

    # ── Transport ─────────────────────────────────────────────────────────────

    def _send(self, message: str) -> None:
        """Queue message for background delivery. Returns immediately."""
        if not self._enabled:
            logger.debug(f"[Alerts] (disabled) {message[:100]}")
            return
        threading.Thread(target=self._post, args=(message,), daemon=True).start()

    @staticmethod
    def _redact(text: str) -> str:
        """Strip the bot token before logging — it appears verbatim in the request URL,
        so a raw requests exception (e.g. ConnectionError) would otherwise leak it."""
        return text.replace(TELEGRAM_BOT_TOKEN, "***") if TELEGRAM_BOT_TOKEN else text

    def _post(self, message: str) -> None:
        try:
            import requests
            resp = requests.post(
                self._api_url,
                json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       message,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning(f"[Alerts] Telegram API error: {resp.status_code} {resp.text[:200]}")
        except Exception as exc:
            logger.warning(f"[Alerts] Telegram send error: {self._redact(str(exc))}")


# ── Module-level singleton ────────────────────────────────────────────────────
alert_service = AlertService()
