"""
mcx_engine_settings.py
──────────────────────
Runtime-configurable engine parameters for the MCX trading engine.

All values persist to the commodity_settings SQLite table (prefixed
"engine.") and are editable via the dashboard:

  GET  /commodity/engine-settings           — list all with labels
  PUT  /commodity/engine-settings/{key}     — update a single value

Changes take effect on the NEXT engine cycle — no bot restart required.
Defaults are written to DB on first run; subsequent starts read from DB.

Groups
──────
  Session   — trading window timing (hours enforced by MCXSessionCalendar)
  Risk      — position sizing and daily entry caps
  Execution — broker order-fill behavior
  Pricing   — Black-Scholes fallback parameters
  Health    — data-quality escalation thresholds
  Premarket — pre-open signal scoring thresholds
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import time as dtime

logger = logging.getLogger(__name__)

DB_PATH = "db/trades.db"

# ─────────────────────────────────────────────────────────────────
# DEFAULTS  —  written to DB on first run; never overwrite existing
# ─────────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    # Session
    "mcx_open_wait_minutes":        30,       # opening blackout — false-breakout filter
    "mcx_entry_cutoff":             "20:00",  # HH:MM — global last-entry time
    "silver_entry_cutoff":          "19:15",  # HH:MM — SILVER / SILVERM / SILVERMIC
    "mcx_eod_exit_time":            "23:15",  # HH:MM — begin forced EOD close (15 min before close)
    # Risk
    "max_daily_entries":            2,        # max entries per instrument per calendar day
    "max_lots_per_trade":           3,        # hard lot cap per spread (REAL mode only)
    "max_capital_pct":              0.10,     # max fraction of free capital per new trade
    # Execution
    "fill_poll_secs":               30,       # order fill confirmation poll timeout
    # Pricing
    "spread_delta":                 0.35,     # debit spread net-delta estimate (spot→PnL conversion)
    "bs_risk_free_rate":            0.065,    # Black-Scholes risk-free rate (India ≈ 6.5%)
    "bs_default_dte":               21,       # DTE used when live chain is unavailable
    # Health
    "data_miss_error_minutes":      10,       # escalate warning → error after N continuous minutes
    "data_miss_startup_grace_minutes": 5,     # suppress data-miss alerts for N min after bot restart
    # Premarket
    "premarket_strong_thresh_pct":  0.50,     # IEP gap % that scores ±2
    "premarket_mild_thresh_pct":    0.15,     # IEP gap % that scores ±1
}

# ─────────────────────────────────────────────────────────────────
# UI METADATA  —  label, type, unit, group sent to dashboard
# ─────────────────────────────────────────────────────────────────
_METADATA: dict = {
    "mcx_open_wait_minutes":       {
        "label": "Opening Blackout", "type": "int", "unit": "minutes",
        "group": "Session",
        "note": "Entry scan is blocked for this many minutes after MCX session opens (false-breakout filter)",
    },
    "mcx_entry_cutoff":            {
        "label": "Entry Cutoff (global)", "type": "time", "unit": "HH:MM",
        "group": "Session",
        "note": "No new entries after this time. Applies to all instruments except those with a tighter cutoff below.",
    },
    "silver_entry_cutoff":         {
        "label": "Silver Entry Cutoff", "type": "time", "unit": "HH:MM",
        "group": "Session",
        "note": "Applies to SILVER, SILVERM, SILVERMIC. Spreads widen as Indian session participants exit.",
    },
    "mcx_eod_exit_time":           {
        "label": "EOD Close Trigger", "type": "time", "unit": "HH:MM",
        "group": "Session",
        "note": "All open positions are closed at this time. Must be before MCX closes at 23:30.",
    },
    "max_daily_entries":           {
        "label": "Max Daily Entries", "type": "int", "unit": "per instrument",
        "group": "Risk",
        "note": "Prevents compounding losses on choppy instruments by capping re-entries per day.",
    },
    "max_lots_per_trade":          {
        "label": "Max Lots per Trade", "type": "int", "unit": "lots",
        "group": "Risk",
        "note": "Hard cap on lots per debit spread (REAL mode only). Paper mode always uses 1 lot.",
    },
    "max_capital_pct":             {
        "label": "Max Capital per Trade", "type": "float", "unit": "%",
        "group": "Risk",
        "display_mult": 100,
        "note": "Maximum fraction of free capital to risk on a single spread (e.g. 0.10 = 10%).",
    },
    "fill_poll_secs":              {
        "label": "Fill Poll Timeout", "type": "int", "unit": "seconds",
        "group": "Execution",
        "note": "How long to wait for a broker fill confirmation before treating the order as failed.",
    },
    "spread_delta":                {
        "label": "Spread Delta Estimate", "type": "float", "unit": "",
        "group": "Pricing",
        "note": "Net delta of the debit spread, used to convert spot-point moves to estimated PnL.",
    },
    "bs_risk_free_rate":           {
        "label": "Risk-Free Rate (BS)", "type": "float", "unit": "%",
        "group": "Pricing",
        "display_mult": 100,
        "note": "Risk-free rate used in Black-Scholes fallback pricing (India repo rate ≈ 6.5%).",
    },
    "bs_default_dte":              {
        "label": "Default DTE for BS Pricing", "type": "int", "unit": "days",
        "group": "Pricing",
        "note": "Days-to-expiry assumed when live chain is unavailable and Black-Scholes estimates are used.",
    },
    "data_miss_error_minutes":     {
        "label": "Data Miss Escalation", "type": "int", "unit": "minutes",
        "group": "Health",
        "note": "Fyers data alerts start as 'warning'. After this many continuous minutes they become 'error'.",
    },
    "data_miss_startup_grace_minutes": {
        "label": "Startup Data Grace Period", "type": "int", "unit": "minutes",
        "group": "Health",
        "note": "Suppress MCX data-miss alerts for this many minutes after bot restart (WebSocket needs time to receive first tick). Set 0 to disable.",
    },
    "premarket_strong_thresh_pct": {
        "label": "Pre-Market Strong Threshold", "type": "float", "unit": "%",
        "group": "Premarket",
        "note": "IEP gap ≥ this % gives a ±2 conviction score. Default 0.50%.",
    },
    "premarket_mild_thresh_pct":   {
        "label": "Pre-Market Mild Threshold", "type": "float", "unit": "%",
        "group": "Premarket",
        "note": "IEP gap ≥ this % (but < strong threshold) gives a ±1 conviction score. Default 0.15%.",
    },
}

# Instruments that share the silver_entry_cutoff (earlier spread-quality deterioration)
SILVER_INSTRUMENTS: frozenset = frozenset({"SILVER", "SILVERM", "SILVERMIC"})

# DB key prefix — all engine settings are stored as "engine.<key>"
_PREFIX = "engine."


class MCXEngineSettings:
    """
    Singleton that exposes runtime-configurable engine parameters.
    Backed by the commodity_settings SQLite table.

    Read path:  in-memory dict (loaded from DB at startup)
    Write path: in-memory dict updated first, then persisted to DB
    Thread safety: single process; dict reads are GIL-protected
    """

    def __init__(self) -> None:
        self._cache: dict = {}
        self._ensure_table()
        self._seed_defaults()
        self._load()

    # ── Typed getters ───────────────────────────────────────────────

    def mcx_open_wait_minutes(self) -> int:
        return int(self._get("mcx_open_wait_minutes"))

    def mcx_entry_cutoff(self) -> dtime:
        return self._parse_time("mcx_entry_cutoff")

    def silver_entry_cutoff(self) -> dtime:
        return self._parse_time("silver_entry_cutoff")

    def mcx_eod_exit_time(self) -> dtime:
        return self._parse_time("mcx_eod_exit_time")

    def instrument_entry_cutoff(self, instrument: str) -> dtime:
        """Return the entry cutoff for an instrument.
        Silver-group instruments use the tighter silver_entry_cutoff;
        all others use the global mcx_entry_cutoff."""
        if instrument.upper() in SILVER_INSTRUMENTS:
            return self.silver_entry_cutoff()
        return self.mcx_entry_cutoff()

    def max_daily_entries(self) -> int:
        return int(self._get("max_daily_entries"))

    def max_lots_per_trade(self) -> int:
        return int(self._get("max_lots_per_trade"))

    def max_capital_pct(self) -> float:
        return float(self._get("max_capital_pct"))

    def fill_poll_secs(self) -> int:
        return int(self._get("fill_poll_secs"))

    def spread_delta(self) -> float:
        return float(self._get("spread_delta"))

    def bs_risk_free_rate(self) -> float:
        return float(self._get("bs_risk_free_rate"))

    def bs_default_dte(self) -> int:
        return int(self._get("bs_default_dte"))

    def data_miss_error_minutes(self) -> int:
        return int(self._get("data_miss_error_minutes"))

    def data_miss_startup_grace_minutes(self) -> int:
        return int(self._get("data_miss_startup_grace_minutes"))

    def premarket_strong_thresh_pct(self) -> float:
        return float(self._get("premarket_strong_thresh_pct"))

    def premarket_mild_thresh_pct(self) -> float:
        return float(self._get("premarket_mild_thresh_pct"))

    # ── Public setter  ──────────────────────────────────────────────

    def set(self, key: str, value) -> tuple[bool, str]:
        """
        Update a setting at runtime.
        Returns (ok: bool, error_message: str).
        Value is coerced to the correct type and range-validated before writing.
        """
        if key not in _DEFAULTS:
            return False, f"Unknown setting '{key}'. Valid keys: {sorted(_DEFAULTS)}"
        expected_type = type(_DEFAULTS[key])
        try:
            if expected_type is int:
                value = int(value)
            elif expected_type is float:
                value = float(value)
            else:
                value = str(value)
                if _METADATA.get(key, {}).get("type") == "time":
                    _parse_time_str(value)   # validate format early
        except (ValueError, TypeError) as exc:
            return False, f"Cannot convert '{value}' to {expected_type.__name__}: {exc}"

        ok, err = _validate(key, value)
        if not ok:
            return False, err

        self._cache[key] = value
        self._persist(key, value)
        logger.info(f"[EngineSettings] {key} = {value!r}")
        return True, ""

    def all_settings(self) -> list[dict]:
        """Return all settings with current values, defaults, and UI metadata."""
        result = []
        for key, default in _DEFAULTS.items():
            meta = _METADATA.get(key, {})
            result.append({
                "key":     key,
                "value":   self._cache.get(key, default),
                "default": default,
                **meta,
            })
        return result

    # ── Internal helpers ────────────────────────────────────────────

    def _get(self, key: str):
        return self._cache.get(key, _DEFAULTS[key])

    def _parse_time(self, key: str) -> dtime:
        return _parse_time_str(str(self._get(key)))

    def _ensure_table(self) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS commodity_settings (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
        except Exception as exc:
            logger.warning(f"[EngineSettings] DB table init failed: {exc}")

    def _seed_defaults(self) -> None:
        """Write defaults for any key not yet present in DB (INSERT OR IGNORE)."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO commodity_settings (key, value) VALUES (?, ?)",
                    [(f"{_PREFIX}{k}", str(v)) for k, v in _DEFAULTS.items()],
                )
        except Exception as exc:
            logger.warning(f"[EngineSettings] Seed defaults failed: {exc}")

    def _load(self) -> None:
        """Load all engine.* rows from DB into the in-memory cache."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM commodity_settings WHERE key LIKE ?",
                    (f"{_PREFIX}%",),
                ).fetchall()
            loaded = 0
            for db_key, db_val in rows:
                setting_key = db_key[len(_PREFIX):]
                if setting_key not in _DEFAULTS:
                    continue
                expected = type(_DEFAULTS[setting_key])
                try:
                    self._cache[setting_key] = expected(db_val)
                    loaded += 1
                except (ValueError, TypeError):
                    self._cache[setting_key] = _DEFAULTS[setting_key]
            logger.info(f"[EngineSettings] Loaded {loaded} settings from DB")
        except Exception as exc:
            logger.warning(f"[EngineSettings] Load failed — using defaults: {exc}")

    def _persist(self, key: str, value) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO commodity_settings (key, value) VALUES (?, ?)",
                    (f"{_PREFIX}{key}", str(value)),
                )
        except Exception as exc:
            logger.debug(f"[EngineSettings] Persist failed for {key}: {exc}")


# ── Module-level helpers (used by the class and by callers) ─────────

def _parse_time_str(s: str) -> dtime:
    """Parse "HH:MM" → dtime. Raises ValueError on bad format."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Time must be HH:MM, got '{s}'")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: {h:02d}:{m:02d}")
    return dtime(h, m)


def _validate(key: str, value) -> tuple[bool, str]:
    """Sanity-bound checks for each setting."""
    checks: dict = {
        "mcx_open_wait_minutes":        (lambda v: 1  <= v <= 120,  "must be 1–120 minutes"),
        "max_daily_entries":            (lambda v: 1  <= v <= 20,   "must be 1–20"),
        "max_lots_per_trade":           (lambda v: 1  <= v <= 100,  "must be 1–100"),
        "max_capital_pct":              (lambda v: 0  < v <= 1.0,   "must be 0–1.0 (e.g. 0.10 = 10%)"),
        "fill_poll_secs":               (lambda v: 5  <= v <= 300,  "must be 5–300 seconds"),
        "spread_delta":                 (lambda v: 0  < v <= 1.0,   "must be 0–1.0"),
        "bs_risk_free_rate":            (lambda v: 0  <= v <= 0.20, "must be 0–0.20 (e.g. 0.065 = 6.5%)"),
        "bs_default_dte":               (lambda v: 1  <= v <= 90,   "must be 1–90 days"),
        "data_miss_error_minutes":          (lambda v: 1  <= v <= 120,  "must be 1–120 minutes"),
        "data_miss_startup_grace_minutes":  (lambda v: 0  <= v <= 60,   "must be 0–60 minutes"),
        "premarket_strong_thresh_pct":  (lambda v: 0  < v <= 5.0,  "must be 0–5.0%"),
        "premarket_mild_thresh_pct":    (lambda v: 0  < v <= 2.0,  "must be 0–2.0%"),
    }
    if key in checks:
        fn, msg = checks[key]
        if not fn(value):
            return False, f"'{key}' {msg} (got {value!r})"
    return True, ""


# Module-level singleton — imported by all engine modules
engine_settings = MCXEngineSettings()
