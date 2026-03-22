"""
alert_service.py
────────────────
Sends real-time alerts via Telegram.
All alerts are non-blocking — sent in background threads.

Setup:
  1. Create a bot via @BotFather → get TELEGRAM_BOT_TOKEN
  2. Send any message to your bot → get chat ID via:
     https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Add both to .env
"""

import logging
import threading
import requests
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


class AlertService:

    def __init__(self):
        self._enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        if not self._enabled:
            logger.info("[Alerts] Telegram not configured — alerts disabled.")

    def trade_opened(self, signal, order_id: str) -> None:
        emoji = "🟢" if signal.direction.value == "LONG" else "🔴"
        msg = (
            f"{emoji} *TRADE OPENED*\n"
            f"Symbol:    `{signal.symbol}`\n"
            f"Strategy:  `{signal.strategy}`\n"
            f"Direction: `{signal.direction.value}`\n"
            f"Entry:     `₹{signal.entry:,.2f}`\n"
            f"Stop:      `₹{signal.stop_loss:,.2f}`\n"
            f"Target 1:  `₹{signal.target_1:,.2f}`\n"
            f"R:R:       `{signal.risk_reward:.1f}`\n"
            f"Size:      `{signal.position_size} shares`\n"
            f"Risk:      `₹{signal.capital_at_risk:,.0f}`\n"
            f"Confidence:`{signal.confidence:.0%}`\n"
            f"Order ID:  `{order_id}`"
        )
        self._send(msg)

    def trade_closed(self, symbol: str, pnl: float, reason: str = "") -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} *TRADE CLOSED*\n"
            f"Symbol: `{symbol}`\n"
            f"P&L:    `₹{pnl:+,.0f}`\n"
            f"Reason: `{reason}`"
        )
        self._send(msg)

    def signal_pending(self, signal, signal_id: str) -> None:
        msg = (
            f"🟡 *SIGNAL AWAITING CONFIRMATION*\n"
            f"ID:        `{signal_id}`\n"
            f"Symbol:    `{signal.symbol}`\n"
            f"Direction: `{signal.direction.value}`\n"
            f"Entry:     `₹{signal.entry:,.2f}`\n"
            f"Stop:      `₹{signal.stop_loss:,.2f}`\n"
            f"Target 1:  `₹{signal.target_1:,.2f}`\n"
            f"R:R:       `{signal.risk_reward:.1f}`\n"
            f"Confidence:`{signal.confidence:.0%}`\n"
            f"Reason:    `{signal.reason}`\n\n"
            f"Confirm or reject in dashboard."
        )
        self._send(msg)

    def kill_switch(self, reason: str) -> None:
        self._send(f"🚨 *KILL SWITCH TRIGGERED*\nReason: `{reason}`\nAll trading halted.")

    def daily_summary(self, stats: dict) -> None:
        pnl = stats.get("total_realised_pnl", 0)
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} *DAILY SUMMARY*\n"
            f"P&L:      `₹{pnl:+,.0f} ({stats.get('total_pnl_pct', 0):+.1f}%)`\n"
            f"Trades:   `{stats.get('total_trades', 0)}`\n"
            f"Win Rate: `{stats.get('win_rate', 0):.0f}%`\n"
            f"Max DD:   `{stats.get('drawdown_pct', 0):.1f}%`"
        )
        self._send(msg)

    def info(self, message: str) -> None:
        self._send(f"ℹ️ {message}")

    def _send(self, message: str) -> None:
        if not self._enabled:
            logger.debug(f"[Alerts] (disabled) {message[:80]}")
            return
        threading.Thread(target=self._post, args=(message,), daemon=True).start()

    def _post(self, message: str) -> None:
        try:
            resp = requests.post(TELEGRAM_API, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "Markdown",
            }, timeout=10)
            if not resp.ok:
                logger.warning(f"[Alerts] Telegram failed: {resp.text}")
        except Exception as e:
            logger.warning(f"[Alerts] Telegram error: {e}")


alert_service = AlertService()
