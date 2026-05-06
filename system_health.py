"""
system_health.py
─────────────────
Singleton that tracks live component health alerts.

Any module can call system_health.set_alert() when it detects a failure
that requires human attention or auto-recovery.  The dashboard reads this
and shows a visible "ACTION REQUIRED" banner.

Alerts are persisted to DB so they survive bot restarts — a token error
that was present before restart will still show after restart, not silently
disappear only to fail again on the next cycle.

Severity levels:
  warning  — degraded but trading continues (e.g. OI data unavailable)
  error    — feature broken, needs fix soon (e.g. token expired)
  critical — trading impossible until resolved (e.g. broker down)
"""

import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DB_PATH = "db/trades.db"

logger = logging.getLogger(__name__)


class SystemHealth:

    def __init__(self):
        # Internal storage keeps ISO datetimes for accurate DB persistence.
        # get_alerts() formats them as HH:MM:SS for the dashboard.
        self._alerts: dict[str, dict] = {}
        self._init_db()
        self._load_alerts()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def set_alert(
        self,
        component: str,
        message:   str,
        severity:  str = "warning",
    ) -> None:
        existing = self._alerts.get(component, {})
        now = datetime.now(tz=IST)
        alert = {
            "component":   component,
            "message":     message,
            "severity":    severity,
            "since_iso":   existing.get("since_iso") or now.isoformat(),
            "updated_iso": now.isoformat(),
        }
        self._alerts[component] = alert
        self._db_upsert(alert)

    def clear_alert(self, component: str) -> None:
        self._alerts.pop(component, None)
        self._db_delete(component)

    def get_alerts(self) -> list[dict]:
        """Return alerts formatted for the dashboard API."""
        result = []
        for a in self._alerts.values():
            try:
                since_fmt = datetime.fromisoformat(a["since_iso"]).strftime("%H:%M:%S")
            except Exception:
                since_fmt = a.get("since_iso", "")
            try:
                updated_fmt = datetime.fromisoformat(a["updated_iso"]).strftime("%H:%M:%S")
            except Exception:
                updated_fmt = a.get("updated_iso", "")
            result.append({
                "component": a["component"],
                "message":   a["message"],
                "severity":  a["severity"],
                "since":     since_fmt,
                "updated":   updated_fmt,
            })
        return result

    def has_alerts(self) -> bool:
        return bool(self._alerts)

    def has_critical(self) -> bool:
        return any(a["severity"] == "critical" for a in self._alerts.values())

    # ─────────────────────────────────────────────────────────────
    # DB PERSISTENCE
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS system_health_alerts (
                        component   TEXT PRIMARY KEY,
                        message     TEXT,
                        severity    TEXT,
                        since_iso   TEXT,
                        updated_iso TEXT
                    )
                """)
        except Exception as e:
            logger.warning(f"[SystemHealth] DB init failed: {e}")

    def _load_alerts(self) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT component, message, severity, since_iso, updated_iso "
                    "FROM system_health_alerts"
                ).fetchall()
            for component, message, severity, since_iso, updated_iso in rows:
                self._alerts[component] = {
                    "component":   component,
                    "message":     message,
                    "severity":    severity,
                    "since_iso":   since_iso,
                    "updated_iso": updated_iso,
                }
            if self._alerts:
                logger.info(
                    f"[SystemHealth] Restored {len(self._alerts)} alert(s) from DB: "
                    f"{list(self._alerts.keys())}"
                )
        except Exception as e:
            logger.warning(f"[SystemHealth] Could not load alerts from DB: {e}")

    def _db_upsert(self, alert: dict) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_health_alerts "
                    "(component, message, severity, since_iso, updated_iso) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        alert["component"], alert["message"], alert["severity"],
                        alert["since_iso"], alert["updated_iso"],
                    ),
                )
        except Exception as e:
            logger.debug(f"[SystemHealth] Could not persist alert for {alert['component']}: {e}")

    def _db_delete(self, component: str) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "DELETE FROM system_health_alerts WHERE component=?", (component,)
                )
        except Exception as e:
            logger.debug(f"[SystemHealth] Could not delete alert for {component}: {e}")


# Module-level singleton
system_health = SystemHealth()
