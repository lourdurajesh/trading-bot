"""
commodity_options_learning.py
─────────────────────────────
Standalone paper-trading engine for MCX commodity options.

Completely separate from:
  - Production strategy system
  - NSE learning_engine.py
  - NSE options_executor.py

What it does:
  Every cycle during MCX hours (09:00–23:30 IST):
    1. Checks the 1H commodity futures trend (EMA20 + RSI)
    2. If bullish: tries to buy an ATM call debit spread
       If bearish: tries to buy an ATM put debit spread
    3. Fetches live MCX options chain via Fyers (falls back to
       Black-Scholes simulation when chain is unavailable)
    4. Logs everything to commodity_learning_trades table

Review via API:
  GET /commodity/trades   — all paper trades
  GET /commodity/stats    — win rate, avg R, per-instrument breakdown
  GET /commodity/chain/{symbol}  — live chain snapshot for a symbol

MCX trading hours:
  Metals  (GOLD, SILVER, COPPER): 09:00–23:30 IST
  Energy  (CRUDEOIL, NATGAS):     09:00–23:30 IST
  Agri    (not included here):    09:00–17:00 IST
"""

import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, date, timedelta, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

IST     = ZoneInfo("Asia/Kolkata")
DB_PATH = "db/trades.db"

logger = logging.getLogger(__name__)

# ── Enhancement modules (fail-safe imports) ───────────────────────
try:
    from analysis.spread_quality import spread_quality_engine, LegData
    _SPREAD_QUALITY_AVAILABLE = True
except Exception:
    _SPREAD_QUALITY_AVAILABLE = False

try:
    from analysis.trade_decision_audit import trade_decision_audit, AuditEntry, Decision
    _AUDIT_AVAILABLE = True
except Exception:
    _AUDIT_AVAILABLE = False

try:
    from risk.daily_risk_budget import daily_risk_budget
    _RISK_BUDGET_AVAILABLE = True
except Exception:
    _RISK_BUDGET_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────
# MCX COMMODITY METADATA
# ─────────────────────────────────────────────────────────────────

MCX_MARKET_OPEN  = dtime(9, 0)
MCX_MARKET_CLOSE = dtime(23, 30)
# Stop taking new entries 30 min before close (spread widens)
MCX_ENTRY_CUTOFF = dtime(20, 0)   # stop new entries at 8 PM — spreads widen after that

# Populated at startup from the commodity_instruments DB table.
# Code throughout this module reads these dicts; they are refreshed whenever
# an instrument is added, updated, or removed via the API.
MCX_CONTRACTS:    dict[str, dict] = {}
_ROLLOVER_BUFFER: dict[str, int]  = {}
_VALID_MONTHS:    dict[str, list] = {}

# Built-in seed rows — written to DB with INSERT OR IGNORE so user edits are never lost.
# Columns: name, lot_size, strike_step, typical_iv, price_unit,
#          min_price, max_price, rollover_buffer, valid_months (JSON list)
#
# Columns: name, display_name, lot_size, strike_step, typical_iv, price_unit,
#          min_price, max_price, rollover_buffer, valid_months (JSON list)
# valid_months: [] = All Months traded  |  specific list = restricted expiry schedule
# min/max_price: wide ranges — tighten per instrument via dashboard if needed
_INSTRUMENT_SEEDS = [
    # ── Precious Metals ─────────────────────────────────────────────
    ("GOLD",        "Gold",             1,    500, 0.18, "INR/10gm",   80000, 300000,  7, [2,4,6,8,10,12]),
    ("GOLDM",       "Gold Mini",      100,    500, 0.18, "INR/10gm",   80000, 300000,  7, [2,4,6,8,10,12]),
    ("GOLDGUINEA",  "Gold Guinea",      8,    500, 0.18, "INR/8gm",    64000, 240000,  7, [2,4,6,8,10,12]),
    ("GOLDPETAL",   "Gold Petal",       1,    100, 0.20, "INR/1gm",     8000,  40000,  5, []),
    # min/max updated May-2026: silver spot ~₹2.65L/kg (was 40k–200k, now 150k–400k)
    # valid_months corrected to match MCX silver delivery schedule (no May delivery)
    ("SILVER",      "Silver",          30,   1000, 0.28, "INR/kg",    150000, 400000,  7, [3,7,9,12]),
    ("SILVERM",     "Silver Mini",      5,   1000, 0.30, "INR/kg",     60000, 500000,  7, [3,5,7,9,12]),
    ("SILVERMIC",   "Silver Micro",     1,    500, 0.32, "INR/kg",     60000, 500000,  5, []),
    # ── Energy ──────────────────────────────────────────────────────
    # rollover_buffer fixed May-2026: CRUDEOILM/NATGASMINI expire ~19th and last-Wed
    # respectively — old buffer of 3 days kept them on stale MAY contracts until the 28th.
    ("CRUDEOIL",    "Crude Oil",      100,    100, 0.35, "INR/bbl",     2000,  20000, 14, []),
    ("CRUDEOILM",   "Crude Oil Mini",  10,    100, 0.38, "INR/bbl",     2000,  20000, 12, []),
    ("NATURALGAS",  "Natural Gas",   1250,      5, 0.45, "INR/mmBtu",    100,   2000,  5, []),
    ("NATGASMINI",  "Natural Gas Mini", 250,    5, 0.48, "INR/mmBtu",    100,   2000,  5, []),
    # ── Base Metals ─────────────────────────────────────────────────
    ("COPPER",      "Copper",           1,     10, 0.25, "INR/kg",       500,   5000,  5, []),
    ("COPPERM",     "Copper Mini",      1,     10, 0.28, "INR/kg",       500,   5000,  5, []),
    ("ZINC",        "Zinc",             5,      5, 0.22, "INR/kg",       100,   1000,  5, []),
    ("ZINCMINI",    "Zinc Mini",        1,      5, 0.24, "INR/kg",       100,   1000,  5, []),
    ("ALUMINIUM",   "Aluminium",        5,      2, 0.20, "INR/kg",       100,    600,  5, []),
    ("ALUMINI",     "Aluminium Mini",   1,      2, 0.22, "INR/kg",       100,    600,  5, []),
    ("LEAD",        "Lead",             5,      5, 0.24, "INR/kg",        80,    500,  5, []),
    ("LEADMINI",    "Lead Mini",        1,      5, 0.26, "INR/kg",        80,    500,  5, []),
    ("NICKEL",      "Nickel",         250,     20, 0.35, "INR/kg",       500,   8000,  5, []),
    ("NICKELM",     "Nickel Mini",     50,     20, 0.38, "INR/kg",       500,   8000,  5, []),
    # ── Agricultural ────────────────────────────────────────────────
    ("COTTON",      "Cotton",          25,    200, 0.30, "INR/bale",   30000, 120000,  7, []),
    ("MENTHAOIL",   "Mentha Oil",     360,     10, 0.40, "INR/kg",       300,   3000,  5, []),
]

# ─────────────────────────────────────────────────────────────────
# MCX STRATEGY CONFIGURATION — risk profiles and cooldown settings
# ─────────────────────────────────────────────────────────────────
# Strategies are evaluated in priority order (1 = first / most preferred).
# cooldown_enabled / cooldown_hours are editable at runtime via API.
MCX_STRATEGY_CONFIG: dict = {
    "TrendSpread": {
        "priority":             1,
        "risk":                 "MEDIUM",
        "risk_label":           "Trend-following EMA crossover — well-defined conditions, lower failure rate",
        "risk_color":           "#f59e0b",
        "cooldown_enabled":     True,
        "cooldown_hours":       2,
        # SL / trailing / target — all expressed as % of net_debit so the spot
        # distance scales with what was actually paid (cheap spread = tight SL;
        # expensive spread = more room). Configurable at runtime via /commodity/config.
        "sl_debit_pct":         0.40,   # exit when spread loses 40% of debit in spot-equivalent terms
        "trail_debit_pct":      0.30,   # trailing SL distance = 30% of debit below/above peak
        "trail_trigger_pct":    0.50,   # trailing activates once gain = 50% of debit in spot-equivalent terms
        "target_pct":           0.65,   # initial exit target = 65% of max profit (was 50% — left too much on table)
        "target_upgraded_pct":  0.80,   # target raised to 80% when strong momentum (was 75%)
        "target_upgrade_mult":  2.0,    # upgrade after favorable move ≥ 2× SL distance
    },
    "RSIReversalSpread": {
        "priority":             2,
        "risk":                 "HIGH",
        "risk_label":           "Counter-trend reversal — may catch a falling knife if trend continues",
        "risk_color":           "#f97316",
        "cooldown_enabled":     True,
        "cooldown_hours":       3,
        "sl_debit_pct":         0.35,   # tighter SL — counter-trend trades fail faster
        "trail_debit_pct":      0.25,
        "trail_trigger_pct":    0.45,
        "target_pct":           0.65,   # was 0.50
        "target_upgraded_pct":  0.78,   # lower ceiling than trend — reversal momentum less sustained
        "target_upgrade_mult":  2.0,
    },
    "BreakoutSpread": {
        "priority":             3,
        "risk":                 "HIGH",
        "risk_label":           "Price breakout — frequent false signals, high failure rate",
        "risk_color":           "#ef4444",
        "cooldown_enabled":     True,
        "cooldown_hours":       3,
        "sl_debit_pct":         0.40,   # FALSE_BREAKOUT fires first; STOP_SPOT is the fallback
        "trail_debit_pct":      0.30,
        "trail_trigger_pct":    0.50,
        "target_pct":           0.65,   # was 0.50
        "target_upgraded_pct":  0.80,
        "target_upgrade_mult":  2.0,
    },
}

# ─────────────────────────────────────────────────────────────────
# STRATEGY CONFIG PERSISTENCE
# Editable params are saved here so they survive bot restarts.
# Read-only fields (priority, risk, risk_label, risk_color) are
# never written to the file.
# ─────────────────────────────────────────────────────────────────
_STRATEGY_CONFIG_PATH = "config/commodity_strategy_config.json"
_READONLY_FIELDS = frozenset({"priority", "risk", "risk_label", "risk_color"})


def _load_strategy_config() -> None:
    """Overlay persisted user edits onto MCX_STRATEGY_CONFIG at startup."""
    try:
        with open(_STRATEGY_CONFIG_PATH, "r") as f:
            saved = json.load(f)
        for strat, params in saved.items():
            if strat in MCX_STRATEGY_CONFIG:
                for k, v in params.items():
                    if k not in _READONLY_FIELDS:
                        MCX_STRATEGY_CONFIG[strat][k] = v
        logger.info(f"[CommOpts] Strategy config loaded from {_STRATEGY_CONFIG_PATH}")
    except FileNotFoundError:
        pass  # first run — file written on first update
    except Exception as exc:
        logger.warning(f"[CommOpts] Could not load strategy config: {exc}")


def _save_strategy_config() -> None:
    """Persist editable fields of MCX_STRATEGY_CONFIG to disk."""
    import os
    saveable = {
        strat: {k: v for k, v in cfg.items() if k not in _READONLY_FIELDS}
        for strat, cfg in MCX_STRATEGY_CONFIG.items()
    }
    try:
        os.makedirs(os.path.dirname(_STRATEGY_CONFIG_PATH), exist_ok=True)
        with open(_STRATEGY_CONFIG_PATH, "w") as f:
            json.dump(saveable, f, indent=2)
    except Exception as exc:
        logger.error(f"[CommOpts] Could not save strategy config: {exc}")


# Apply any previously saved overrides now (module-level, runs on import)
_load_strategy_config()

_MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

# Days-before-month-end trigger for rolling to next contract.
# Rule: roll when calendar day >= (days_in_month - buffer).
# NATURALGAS uses 32 (> any month) so it always resolves to next month —
# MCX NG expires ~25th of the month PRIOR to the contract month.
# _ROLLOVER_BUFFER and _VALID_MONTHS are populated from DB — see _load_contracts_from_db().


def _bootstrap_contracts() -> None:
    """
    Populate MCX_CONTRACTS, _ROLLOVER_BUFFER, _VALID_MONTHS at module import time.

    Priority:
      1. commodity_instruments DB table  (user edits + dynamic adds)
      2. _INSTRUMENT_SEEDS fallback      (first run / DB missing)

    Called once at module load so that fyers_stream.py and other early importers
    see a fully-populated contract list before CommodityOptionsLearning is instantiated.
    """
    import os
    loaded = False
    if os.path.exists(DB_PATH):
        try:
            with sqlite3.connect(DB_PATH) as _conn:
                _conn.row_factory = sqlite3.Row
                _rows = _conn.execute(
                    "SELECT * FROM commodity_instruments"
                ).fetchall()
            if _rows:
                for _r in _rows:
                    _n = _r["name"]
                    MCX_CONTRACTS[_n] = {
                        "lot_size":    _r["lot_size"],
                        "strike_step": _r["strike_step"],
                        "typical_iv":  _r["typical_iv"],
                        "price_unit":  _r["price_unit"],
                        "min_price":   _r["min_price"],
                        "max_price":   _r["max_price"],
                    }
                    _ROLLOVER_BUFFER[_n] = _r["rollover_buffer"]
                    _months = json.loads(_r["valid_months"] or "[]")
                    if _months:
                        _VALID_MONTHS[_n] = _months
                loaded = True
        except Exception:
            pass

    if not loaded:
        # First run or DB not yet created — use seed defaults
        for _n, _ls, _ss, _iv, _pu, _mn, _mx, _rb, _vm in _INSTRUMENT_SEEDS:
            MCX_CONTRACTS[_n] = {
                "lot_size": _ls, "strike_step": _ss, "typical_iv": _iv,
                "price_unit": _pu, "min_price": _mn, "max_price": _mx,
            }
            _ROLLOVER_BUFFER[_n] = _rb
            if _vm:
                _VALID_MONTHS[_n] = _vm


_bootstrap_contracts()

# ALL_MCX_SHORTS: simple list, rebuilt whenever _load_contracts_from_db() runs.
# Imported by fyers_stream.py — must be a real list, not a lazy proxy,
# because fyers_stream captures it by reference at subscription time.
ALL_MCX_SHORTS: list = list(MCX_CONTRACTS.keys())


def _fyers_sym(short: str) -> str:
    """Return the active front-month MCX futures symbol for today.

    Example (called on 7-May-2026):
        _fyers_sym("CRUDEOIL")   → "MCX:CRUDEOIL26MAYFUT"
        _fyers_sym("NATURALGAS") → "MCX:NATURALGAS26JUNFUT"
        _fyers_sym("SILVER")     → "MCX:SILVER26JULFUT"  (skips June — invalid)
    """
    import calendar as _cal
    now   = datetime.now(tz=IST)
    year  = now.year
    month = now.month
    days_in_month = _cal.monthrange(year, month)[1]
    buf   = _ROLLOVER_BUFFER.get(short, 5)

    if now.day >= max(days_in_month - buf, 1):
        month += 1
        if month > 12:
            month, year = 1, year + 1

    # Advance to next valid expiry month for commodities with restricted schedules
    valid = _VALID_MONTHS.get(short)
    if valid:
        while month not in valid:
            month += 1
            if month > 12:
                month, year = 1, year + 1

    return f"MCX:{short}{str(year)[-2:]}{_MONTHS[month - 1]}FUT"


# ─────────────────────────────────────────────────────────────────
# BLACK-SCHOLES HELPERS (simulation fallback)
# ─────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_price(spot, strike, iv, dte_days, opt_type="call", r=0.065) -> float:
    """Black-Scholes option price. r = India risk-free rate ~6.5%."""
    if dte_days <= 0 or iv <= 0 or spot <= 0:
        return 0.0
    T = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        if opt_type == "call":
            return spot * _norm_cdf(d1) - strike * math.exp(-r * T) * _norm_cdf(d2)
        else:
            return strike * math.exp(-r * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    except Exception:
        return 0.0


def _atm_strike(spot: float, step: int) -> int:
    """Round spot to nearest strike_step."""
    return int(round(spot / step) * step)


# ─────────────────────────────────────────────────────────────────
# COMMODITY OPTIONS LEARNING ENGINE
_SPREAD_DELTA = 0.35  # debit spread net-delta estimate used for debit→spot conversion

# Real-trade execution constants
_MCX_PRODUCT        = "INTRADAY"  # MCX options — broker auto-squares at session end
_MAX_LOTS_PER_TRADE = 3           # hard cap per spread trade
_MAX_CAPITAL_PCT    = 0.10        # max 10% of free capital per new trade
_FILL_POLL_SECS     = 30          # order fill confirmation timeout (seconds)


_SL_OVERRIDE_FIELDS = (
    "sl_debit_pct", "trail_debit_pct", "trail_trigger_pct",
    "target_pct", "target_upgraded_pct", "target_upgrade_mult",
)


def _merged_strat_cfg(instrument: str, strategy: str) -> dict:
    """Return strategy config overlaid with any per-instrument overrides.

    Instrument overrides (stored in MCX_CONTRACTS) take precedence over the
    strategy-level defaults for any field that has been explicitly set (non-None).
    This lets Silver use a wider SL while Copper uses a tighter one — without
    touching the shared strategy config.
    """
    base = dict(MCX_STRATEGY_CONFIG.get(strategy, {}))
    instr_cfg = MCX_CONTRACTS.get(instrument, {})
    for field in _SL_OVERRIDE_FIELDS:
        val = instr_cfg.get(field)
        if val is not None:
            base[field] = val
    return base


def _debit_sl_price(spot: float, direction: str, net_debit: float, strat_cfg: dict) -> float:
    """Compute initial SL spot level from debit cost.

    sl_spot_dist = (sl_debit_pct × net_debit) / spread_delta
    High-IV spreads (large debit) get more spot room; cheap spreads get tighter SL.
    """
    sl_pct = strat_cfg.get("sl_debit_pct", 0.40)
    dist   = round((sl_pct * net_debit) / _SPREAD_DELTA, 2) if net_debit > 0 else round(spot * 0.015, 2)
    return round(spot - dist, 2) if direction == "LONG" else round(spot + dist, 2)


# ─────────────────────────────────────────────────────────────────

class CommodityOptionsLearning:
    """
    Standalone paper-trade engine for MCX commodity options.
    Run learning_cycle() every 60 seconds during MCX hours.
    """

    MAX_DAILY_ENTRIES_PER_INSTRUMENT = 2  # cap re-entries to avoid compounding on choppy instruments

    def __init__(self):
        self._open_positions: dict[str, dict] = {}  # key → trade
        self._chain_cache:    dict[str, tuple] = {}  # symbol → (data, fetched_at)
        # Cooldown after a bad exit — blocks all re-entry on that instrument
        self._entry_cooldown: dict[str, datetime] = {}  # instrument → resume_at
        self._cooldown_cleared: set[str] = set()        # instruments manually force-cleared via API
        self._trade_mode: str = "PAPER"                 # "PAPER" or "REAL"
        self._enabled_commodities: set[str] = set()
        # Per-instrument daily entry counter — reset each calendar day
        self._daily_entries: dict[str, int] = {}        # instrument → entries taken today
        self._daily_entries_date: date = date.today()
        # Wired by main.py to trigger stream resubscription when a rollover symbol changes
        self._on_instrument_update = None
        self._init_db()
        self._load_trade_mode()
        self._load_enabled_commodities()
        self._reload_open_positions()
        self._reconcile_positions()

    def _reload_open_positions(self) -> None:
        """Re-populate _open_positions from DB on startup so SL/TP monitoring
        survives a process restart."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM commodity_learning_trades WHERE status='OPEN'"
                ).fetchall()
            count = 0
            for r in rows:
                trade = dict(r)
                try:
                    trade["metadata"] = json.loads(trade.get("metadata") or "{}")
                except Exception:
                    trade["metadata"] = {}
                # Restore SL state from DB metadata; recompute if missing
                meta_d    = trade["metadata"]
                direction = trade.get("direction", "LONG")
                ep        = trade.get("spot_at_entry", 0)
                if "sl_price" not in meta_d:
                    net_debit = trade.get("net_debit", 0)
                    strategy  = trade.get("strategy", "")
                    strat_cfg = MCX_STRATEGY_CONFIG.get(strategy, {})
                    meta_d["sl_price"]    = _debit_sl_price(ep, direction, net_debit, strat_cfg)
                    meta_d["peak_spot"]   = ep
                    meta_d["target_pct"]  = strat_cfg.get("target_pct", 0.50)
                    meta_d["trail_active"] = False

                key = f"{trade['instrument']}:{trade['direction']}"
                if key not in self._open_positions:
                    self._open_positions[key] = trade
                    count += 1
            if count:
                logger.info(f"[CommOpts] Reloaded {count} open position(s) from DB — SL monitoring resumed")
        except Exception as exc:
            logger.error(f"[CommOpts] Failed to reload open positions: {exc}")

    # ─────────────────────────────────────────────────────────────
    # TRADE MODE (PAPER ↔ REAL)
    # ─────────────────────────────────────────────────────────────

    def _load_contracts_from_db(self) -> None:
        """Reload MCX_CONTRACTS, _ROLLOVER_BUFFER, _VALID_MONTHS, ALL_MCX_SHORTS from DB."""
        global ALL_MCX_SHORTS
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM commodity_instruments").fetchall()
        MCX_CONTRACTS.clear()
        _ROLLOVER_BUFFER.clear()
        _VALID_MONTHS.clear()
        for r in rows:
            name = r["name"]
            instr = {
                "lot_size":    r["lot_size"],
                "strike_step": r["strike_step"],
                "typical_iv":  r["typical_iv"],
                "price_unit":  r["price_unit"],
                "min_price":   r["min_price"],
                "max_price":   r["max_price"],
            }
            # Per-instrument SL/target overrides — None means "inherit strategy default"
            for _ov in ("sl_debit_pct", "trail_debit_pct", "trail_trigger_pct",
                        "target_pct", "target_upgraded_pct", "target_upgrade_mult"):
                try:
                    instr[_ov] = r[_ov]  # will be None if column is NULL
                except IndexError:
                    instr[_ov] = None
            MCX_CONTRACTS[name] = instr
            _ROLLOVER_BUFFER[name] = r["rollover_buffer"]
            months = json.loads(r["valid_months"] or "[]")
            if months:
                _VALID_MONTHS[name] = months
        # Keep ALL_MCX_SHORTS list in sync (fyers_stream re-reads it on reconnect)
        ALL_MCX_SHORTS[:] = list(MCX_CONTRACTS.keys())
        # Refresh enabled set from DB enabled column
        self._enabled_commodities = {
            r["name"] for r in rows if r["enabled"]
        }

    def _load_trade_mode(self) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT value FROM commodity_settings WHERE key='trade_mode'"
                ).fetchone()
            self._trade_mode = (row[0] if row else "PAPER").upper()
        except Exception:
            self._trade_mode = "PAPER"
        logger.info(f"[CommOpts] Trade mode: {self._trade_mode}")

    def set_trade_mode(self, mode: str) -> None:
        mode = mode.upper()
        if mode not in ("PAPER", "REAL"):
            raise ValueError(f"Invalid mode '{mode}' — must be PAPER or REAL")
        if mode == "REAL" and self._open_positions:
            raise RuntimeError(
                "Cannot switch to REAL while paper positions are open. "
                "Close all open commodity trades first."
            )
        self._trade_mode = mode
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO commodity_settings (key, value) VALUES ('trade_mode', ?)",
                (mode,),
            )
        logger.info(f"[CommOpts] Trade mode → {mode}")

    def get_trade_mode(self) -> str:
        return self._trade_mode

    def _is_real(self) -> bool:
        return self._trade_mode == "REAL"

    # ─────────────────────────────────────────────────────────────
    # ENABLED COMMODITIES
    # ─────────────────────────────────────────────────────────────

    def _load_enabled_commodities(self) -> None:
        # Enabled state is already loaded by _load_contracts_from_db.
        # This is called separately to refresh just the enabled set.
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT name FROM commodity_instruments WHERE enabled=1"
                ).fetchall()
            self._enabled_commodities = {r["name"] for r in rows}
        except Exception:
            self._enabled_commodities = set(MCX_CONTRACTS.keys())
        logger.info(f"[CommOpts] Enabled: {sorted(self._enabled_commodities)}")

    def get_instruments(self) -> list[dict]:
        """Return all instruments from DB with enabled status and metadata."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM commodity_instruments ORDER BY name"
            ).fetchall()
        result = []
        for r in rows:
            entry = {
                "name":            r["name"],
                "display_name":    r["display_name"] or r["name"],
                "enabled":         bool(r["enabled"]),
                "lot_size":        r["lot_size"],
                "strike_step":     r["strike_step"],
                "typical_iv":      r["typical_iv"],
                "price_unit":      r["price_unit"],
                "min_price":       r["min_price"],
                "max_price":       r["max_price"],
                "rollover_buffer": r["rollover_buffer"],
                "valid_months":    json.loads(r["valid_months"] or "[]"),
                "symbol":          _fyers_sym(r["name"]),
            }
            for _ov in _SL_OVERRIDE_FIELDS:
                try:
                    entry[_ov] = r[_ov]   # None if not set
                except IndexError:
                    entry[_ov] = None
            result.append(entry)
        return result

    def enable_commodity(self, name: str) -> None:
        name = name.upper()
        if name not in MCX_CONTRACTS:
            raise ValueError(f"Unknown instrument '{name}'")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE commodity_instruments SET enabled=1 WHERE name=?", (name,)
            )
        self._enabled_commodities.add(name)
        logger.info(f"[CommOpts] {name} ENABLED")

    def disable_commodity(self, name: str) -> None:
        name = name.upper()
        if name not in MCX_CONTRACTS:
            raise ValueError(f"Unknown instrument '{name}'")
        if any(name in k for k in self._open_positions):
            raise RuntimeError(
                f"Cannot disable {name} — open position exists. Close it first."
            )
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE commodity_instruments SET enabled=0 WHERE name=?", (name,)
            )
        self._enabled_commodities.discard(name)
        logger.info(f"[CommOpts] {name} DISABLED")

    def add_instrument(self, name: str, lot_size: int, strike_step: float,
                       typical_iv: float, price_unit: str,
                       min_price: float, max_price: float,
                       rollover_buffer: int = 5,
                       valid_months: Optional[list] = None,
                       display_name: str = "") -> None:
        name = name.upper().strip()
        if not name.isalpha():
            raise ValueError("Name must contain letters only (e.g. CRUDEOILMINI)")
        if name in MCX_CONTRACTS:
            raise ValueError(f"'{name}' already exists — use update_instrument() to modify it")
        if lot_size <= 0 or strike_step <= 0 or min_price >= max_price:
            raise ValueError("Invalid numeric params: lot_size/strike_step must be >0; min_price < max_price")
        vm = valid_months or []
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO commodity_instruments
                (name, display_name, lot_size, strike_step, typical_iv, price_unit,
                 min_price, max_price, rollover_buffer, valid_months, enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,1)
            """, (name, display_name or name, lot_size, strike_step, typical_iv, price_unit,
                  min_price, max_price, rollover_buffer, json.dumps(vm)))
        self._load_contracts_from_db()
        logger.info(f"[CommOpts] {name} added (lot={lot_size}, step={strike_step})")

    def update_instrument(self, name: str, **fields) -> None:
        name = name.upper()
        if name not in MCX_CONTRACTS:
            raise ValueError(f"Unknown instrument '{name}'")
        allowed = {
            "display_name",
            "lot_size", "strike_step", "typical_iv", "price_unit",
            "min_price", "max_price", "rollover_buffer", "valid_months",
            # Per-instrument SL/target overrides (None clears back to strategy default)
            "sl_debit_pct", "trail_debit_pct", "trail_trigger_pct",
            "target_pct", "target_upgraded_pct", "target_upgrade_mult",
        }
        updates = {}
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"Unknown field '{k}'")
            updates[k] = json.dumps(v) if k == "valid_months" else v
        if not updates:
            return

        rollover_changing = any(k in updates for k in ("rollover_buffer", "valid_months"))
        old_symbol = _fyers_sym(name) if rollover_changing else None

        sets = ", ".join(f"{k}=?" for k in updates)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                f"UPDATE commodity_instruments SET {sets} WHERE name=?",
                list(updates.values()) + [name],
            )
        self._load_contracts_from_db()
        logger.info(f"[CommOpts] {name} updated: {list(updates)}")

        if rollover_changing and old_symbol != _fyers_sym(name):
            logger.info(f"[CommOpts] {name} contract changed: {old_symbol} → {_fyers_sym(name)}")
            if self._on_instrument_update:
                self._on_instrument_update()

    def remove_instrument(self, name: str) -> None:
        name = name.upper()
        if name not in MCX_CONTRACTS:
            raise ValueError(f"Unknown instrument '{name}'")
        if any(name in k for k in self._open_positions):
            raise RuntimeError(f"Cannot remove {name} — open position exists")
        with sqlite3.connect(DB_PATH) as conn:
            # Keep trade history — only remove the instrument definition
            conn.execute("DELETE FROM commodity_instruments WHERE name=?", (name,))
        self._load_contracts_from_db()
        logger.info(f"[CommOpts] {name} removed")

    # ─────────────────────────────────────────────────────────────
    # CAPITAL & LOTS
    # ─────────────────────────────────────────────────────────────

    def _get_available_funds(self) -> float:
        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised:
                return 0.0
            funds = fyers_broker.get_funds()
            # Fyers returns a list of fund buckets; find the available/equity balance
            if isinstance(funds, list):
                for item in funds:
                    if isinstance(item, dict):
                        val = item.get("equityAmount") or item.get("availableBalance") or 0
                        if val:
                            return float(val)
            if isinstance(funds, dict):
                return float(
                    funds.get("availableBalance")
                    or funds.get("equityAmount")
                    or 0
                )
        except Exception as exc:
            logger.warning(f"[CommOpts] Funds check error: {exc}")
        return 0.0

    def _compute_lots(self, instrument: str, net_debit: float) -> int:
        """
        How many lots to trade.
        Paper: always 1 lot (learning, not sizing).
        Real: scale up to _MAX_CAPITAL_PCT of free capital, hard-cap at _MAX_LOTS_PER_TRADE.
        Returns 0 if insufficient funds (blocks entry).
        """
        if not self._is_real():
            return 1
        lot_size = MCX_CONTRACTS[instrument]["lot_size"]
        cost_per_lot = net_debit * lot_size          # INR cost for 1 lot
        if cost_per_lot <= 0:
            return 1
        available = self._get_available_funds()
        if available <= 0:
            logger.warning(f"[CommOpts] {instrument} fund check returned 0 — blocking entry")
            return 0
        max_lots = int((available * _MAX_CAPITAL_PCT) / cost_per_lot)
        lots = max(1, min(max_lots, _MAX_LOTS_PER_TRADE))
        logger.info(
            f"[CommOpts] {instrument} lots={lots} "
            f"(₹{available:,.0f} free, ₹{cost_per_lot:,.0f}/lot, cap={_MAX_CAPITAL_PCT:.0%})"
        )
        return lots

    # ─────────────────────────────────────────────────────────────
    # REAL ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────

    def _confirm_fill(self, order_id: str, timeout_secs: int = _FILL_POLL_SECS) -> tuple[bool, float]:
        """
        Poll the broker orderbook until the order is filled or timed out.
        Returns (filled: bool, fill_price: float).
        In simulation mode always returns (True, 0.0).
        """
        import time
        from execution.fyers_broker import fyers_broker
        if not fyers_broker._initialised:
            return True, 0.0  # sim — assume immediate fill
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            try:
                for order in fyers_broker.get_orders():
                    if str(order.get("id")) == str(order_id):
                        status = order.get("status", 0)
                        if status == 2:  # fully filled
                            return True, float(order.get("tradedPrice", 0))
                        if status in (5, 6):  # rejected / cancelled
                            logger.error(f"[CommOpts] Order {order_id} rejected/cancelled")
                            return False, 0.0
            except Exception as exc:
                logger.debug(f"[CommOpts] Fill-poll error: {exc}")
            time.sleep(2)
        logger.error(f"[CommOpts] Order {order_id} fill timeout after {timeout_secs}s")
        return False, 0.0

    def _execute_real_entry(
        self, atm_sym: str, otm_sym: str, qty: int
    ) -> tuple[Optional[str], Optional[str], float, float]:
        """
        Place buy ATM + sell OTM legs atomically.
        Returns (atm_order_id, otm_order_id, atm_fill_price, otm_fill_price).
        On any failure: unwinds the filled leg and returns (None, None, 0, 0).
        """
        from execution.fyers_broker import fyers_broker
        if not atm_sym or not otm_sym:
            logger.error("[CommOpts] Real entry blocked — missing option symbols (need live chain)")
            return None, None, 0.0, 0.0

        # Leg 1 — buy ATM
        atm_oid = fyers_broker.place_order(
            symbol=atm_sym, direction="LONG", qty=qty,
            order_type="MARKET", product=_MCX_PRODUCT,
        )
        if not atm_oid:
            logger.error(f"[CommOpts] ATM entry order rejected: {atm_sym}")
            return None, None, 0.0, 0.0

        atm_filled, atm_fill = self._confirm_fill(atm_oid)
        if not atm_filled:
            fyers_broker.cancel_order(atm_oid)
            logger.error(f"[CommOpts] ATM fill timeout — cancelled: {atm_oid}")
            return None, None, 0.0, 0.0

        # Leg 2 — sell OTM
        otm_oid = fyers_broker.place_order(
            symbol=otm_sym, direction="SHORT", qty=qty,
            order_type="MARKET", product=_MCX_PRODUCT,
        )
        if not otm_oid:
            logger.critical(
                f"[CommOpts] OTM entry failed after ATM filled — UNWINDING ATM leg {atm_sym}"
            )
            self._send_alert(
                f"MCX SPREAD ENTRY PARTIAL — ATM filled ({atm_sym}), OTM rejected ({otm_sym}). "
                f"Unwinding ATM. Check broker."
            )
            fyers_broker.place_order(atm_sym, "SHORT", qty, "MARKET", product=_MCX_PRODUCT)
            return None, None, 0.0, 0.0

        otm_filled, otm_fill = self._confirm_fill(otm_oid)
        if not otm_filled:
            logger.critical(
                f"[CommOpts] OTM fill timeout after ATM filled — UNWINDING ATM leg {atm_sym}"
            )
            self._send_alert(
                f"MCX SPREAD ENTRY PARTIAL — ATM filled ({atm_sym}), OTM timed out ({otm_oid}). "
                f"Unwinding ATM. Check broker immediately."
            )
            fyers_broker.cancel_order(otm_oid)
            fyers_broker.place_order(atm_sym, "SHORT", qty, "MARKET", product=_MCX_PRODUCT)
            return None, None, 0.0, 0.0

        logger.info(
            f"[CommOpts] REAL entry confirmed — "
            f"ATM {atm_sym} filled@{atm_fill:.2f} | OTM {otm_sym} filled@{otm_fill:.2f}"
        )
        return atm_oid, otm_oid, atm_fill, otm_fill

    def _execute_real_exit(self, trade: dict, reason: str) -> None:
        """
        Close both legs of a real debit spread with market orders.
        Sends a CRITICAL alert if any leg fails — manual action required.
        """
        from execution.fyers_broker import fyers_broker
        meta      = trade.get("metadata", {})
        atm_sym   = meta.get("atm_symbol", "")
        otm_sym   = meta.get("otm_symbol", "")
        lots      = trade.get("lots", 1)
        lot_size  = trade.get("lot_size", 1)
        qty       = lots * lot_size
        direction = trade.get("direction", "LONG")

        # Closing directions are the reverse of entry directions
        atm_close_dir = "SHORT" if direction == "LONG" else "LONG"
        otm_close_dir = "LONG"  if direction == "LONG" else "SHORT"

        all_ok = True
        for sym, close_dir, label in [
            (atm_sym, atm_close_dir, "ATM"),
            (otm_sym, otm_close_dir, "OTM"),
        ]:
            if not sym:
                continue
            oid = fyers_broker.place_order(
                symbol=sym, direction=close_dir, qty=qty,
                order_type="MARKET", product=_MCX_PRODUCT,
            )
            if oid:
                logger.info(
                    f"[CommOpts] {label} exit order {oid} | "
                    f"{close_dir} {qty} × {sym} ({reason})"
                )
            else:
                all_ok = False
                logger.critical(
                    f"[CommOpts] {label} EXIT FAILED for trade {trade['id']} "
                    f"({sym}) reason={reason} — MANUAL ACTION REQUIRED"
                )
        if not all_ok:
            self._send_alert(
                f"MCX EXIT FAILED — Trade {trade['id']} {trade['instrument']} "
                f"({reason}). One or more legs could not be closed. "
                f"CHECK BROKER AND CLOSE MANUALLY."
            )

    def _send_alert(self, message: str) -> None:
        try:
            from notifications.alert_service import alert_service
            alert_service.info(f"[MCX] {message}")
        except Exception:
            logger.critical(f"[CommOpts] ALERT (unsent): {message}")

    # ─────────────────────────────────────────────────────────────
    # STARTUP RECONCILIATION
    # ─────────────────────────────────────────────────────────────

    def _reconcile_positions(self) -> None:
        """
        On startup in REAL mode: compare DB open trades against broker positions.
        Any trade whose option symbols are absent from the broker is marked
        RECONCILE_CLOSED so the bot doesn't ghost-manage vanished positions.
        """
        if not self._is_real():
            return
        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised:
                logger.warning("[CommOpts] Reconcile skipped — broker not initialised")
                return
            broker_pos = fyers_broker.get_positions()
            # Build set of symbols with non-zero net qty at broker
            live_syms = {
                p.get("symbol", "")
                for p in broker_pos
                if float(p.get("netQty", 0)) != 0
            }
            stale = []
            for key, trade in list(self._open_positions.items()):
                meta    = trade.get("metadata", {})
                atm_sym = meta.get("atm_symbol", "")
                otm_sym = meta.get("otm_symbol", "")
                # If neither leg exists at broker the position is gone
                if atm_sym and otm_sym and atm_sym not in live_syms and otm_sym not in live_syms:
                    stale.append((key, trade))
            for key, trade in stale:
                logger.warning(
                    f"[CommOpts] Reconcile: {trade['id']} not found in broker — "
                    f"marking RECONCILE_CLOSED"
                )
                self._db_close(trade["id"], trade.get("spot_at_entry", 0),
                               "RECONCILE_CLOSED", 0.0, 0.0)
                del self._open_positions[key]
            if stale:
                self._send_alert(
                    f"MCX reconcile on startup: {len(stale)} position(s) not found in broker "
                    f"and marked closed. Review trade history."
                )
                logger.warning(f"[CommOpts] Reconciled {len(stale)} stale position(s)")
            else:
                logger.info(f"[CommOpts] Reconcile OK — {len(self._open_positions)} open position(s) confirmed")
        except Exception as exc:
            logger.error(f"[CommOpts] Reconcile error: {exc}")

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def get_strategy_config(self) -> dict:
        """Return MCX_STRATEGY_CONFIG (risk profiles + cooldown settings)."""
        return MCX_STRATEGY_CONFIG

    def get_cooldown_status(self) -> dict:
        """Return currently active cooldowns with remaining time."""
        now = datetime.now(tz=IST)
        return {
            instr: {
                "resume_at":     resume_at.isoformat(),
                "remaining_min": max(0, int((resume_at - now).total_seconds() / 60)),
            }
            for instr, resume_at in self._entry_cooldown.items()
            if resume_at > now
        }

    def update_strategy_config(self, strategy: str, param: str, value) -> tuple[bool, str]:
        """Update a single MCX strategy config param at runtime (cooldown_enabled, cooldown_hours)."""
        if strategy not in MCX_STRATEGY_CONFIG:
            return False, f"Unknown MCX strategy: {strategy}"
        cfg = MCX_STRATEGY_CONFIG[strategy]
        if param not in cfg:
            return False, f"Unknown param '{param}' for strategy '{strategy}'"
        existing = cfg[param]
        try:
            if isinstance(existing, bool):
                if isinstance(value, str):
                    value = value.lower() in ("true", "1", "yes")
                else:
                    value = bool(value)
            elif isinstance(existing, int):
                value = int(value)
            elif isinstance(existing, float):
                value = float(value)
        except (ValueError, TypeError) as e:
            return False, f"Type error: {e}"
        MCX_STRATEGY_CONFIG[strategy][param] = value
        _save_strategy_config()
        logger.info(f"[CommOpts] Strategy config updated: {strategy}.{param} = {value}")
        return True, ""

    def run_cycle(self) -> None:
        """Entry evaluation once per minute during MCX hours.
        Exit monitoring is handled by check_exits() on the 5-second fast loop."""
        now = datetime.now(tz=IST)
        if not (MCX_MARKET_OPEN <= now.time() <= MCX_MARKET_CLOSE):
            return

        # Reset per-instrument daily entry counters at the start of each new day
        today = now.date()
        if today != self._daily_entries_date:
            self._daily_entries.clear()
            self._daily_entries_date = today
            logger.info("[CommOpts] Daily entry counters reset for new trading day")

        from data.data_store import store

        open_count = len(self._open_positions)
        logger.info(f"[CommOpts] Cycle @ {now.strftime('%H:%M')} IST | open={open_count}")

        # New entry scan (stop at cutoff — enabled commodities only)
        if now.time() <= MCX_ENTRY_CUTOFF:
            for short in list(MCX_CONTRACTS.keys()):
                if short not in self._enabled_commodities:
                    continue
                self._evaluate(short, store, now)

    # ─────────────────────────────────────────────────────────────
    # STRATEGY LOGIC
    # ─────────────────────────────────────────────────────────────

    def _evaluate(self, short: str, store, now: datetime) -> None:
        meta   = MCX_CONTRACTS[short]
        symbol = _fyers_sym(short)   # e.g. MCX:CRUDEOIL26MAYFUT

        # Already have an open position for this commodity?
        if any(short in k for k in self._open_positions):
            return

        # Daily entry cap — prevents compounding losses on choppy instruments (e.g. Silver May-14)
        entries_today = self._daily_entries.get(short, 0)
        if entries_today >= self.MAX_DAILY_ENTRIES_PER_INSTRUMENT:
            logger.info(
                f"[CommOpts] {short} daily entry cap reached "
                f"({entries_today}/{self.MAX_DAILY_ENTRIES_PER_INSTRUMENT}) — skipping"
            )
            return

        # Post-exit cooldown — in-memory fast path
        resume_at = self._entry_cooldown.get(short)
        if resume_at and now < resume_at:
            remaining = int((resume_at - now).total_seconds() / 60)
            logger.info(f"[CommOpts] {short} entry blocked — cooldown {remaining}min remaining")
            return

        # DB-based cooldown check — survives restarts and catches manual closes
        # that may have bypassed the in-memory dict (race condition / process restart)
        try:
            with sqlite3.connect(DB_PATH) as _conn:
                _row = _conn.execute(
                    "SELECT exit_reason, exit_time, strategy FROM commodity_learning_trades "
                    "WHERE instrument=? AND status='CLOSED' "
                    "AND exit_reason IN "
                    "  ('FALSE_BREAKOUT','STOP_SPOT','STOP_60PCT','MANUAL_CLOSE','TARGET_PCT') "
                    "ORDER BY exit_time DESC LIMIT 1",
                    (short,),
                ).fetchone()
            if _row:
                _exit_reason, _exit_time_str, _strategy = _row
                if _exit_time_str:
                    _exit_time = datetime.fromisoformat(_exit_time_str)
                    if _exit_time.tzinfo is None:
                        _exit_time = _exit_time.replace(tzinfo=IST)
                    _strat_cfg = MCX_STRATEGY_CONFIG.get(_strategy or "", {})
                    # MANUAL_CLOSE always applies cooldown; others respect cooldown_enabled flag
                    _cd_enabled = (_exit_reason == "MANUAL_CLOSE") or _strat_cfg.get("cooldown_enabled", True)
                    _base_hours = _strat_cfg.get("cooldown_hours", 3)
                    # TARGET_PCT uses half the stop cooldown (min 1h) — matches live logic
                    if _exit_reason == "TARGET_PCT":
                        _cd_hours = max(1.0, _base_hours * 0.5)
                    else:
                        _cd_hours = _base_hours
                    if _cd_enabled:
                        _resume = _exit_time + timedelta(hours=_cd_hours)
                        if now < _resume:
                            if short in self._cooldown_cleared:
                                # User manually force-cleared — skip DB re-application
                                logger.info(f"[CommOpts] {short} cooldown was manually cleared — allowing entry")
                            else:
                                _remaining = int((_resume - now).total_seconds() / 60)
                                logger.info(
                                    f"[CommOpts] {short} entry blocked (DB cooldown after {_exit_reason}) "
                                    f"— {_remaining}min remaining (resumes {_resume.strftime('%H:%M')} IST)"
                                )
                                # Re-populate in-memory dict so next cycle skips the DB query
                                self._entry_cooldown[short] = _resume
                                return
        except Exception as _e:
            logger.debug(f"[CommOpts] DB cooldown check error for {short}: {_e}")

        # Get futures price
        spot = store.get_ltp(symbol)
        if not spot or spot < meta["min_price"] or spot > meta["max_price"]:
            logger.info(f"[CommOpts] {short} no valid spot (got {spot}, sym={symbol})")
            return

        # 1H data
        df = store.get_ohlcv(symbol, "1H", n=50)
        if df is None or len(df) < 20:
            logger.info(f"[CommOpts] {short} insufficient 1H bars (got {len(df) if df is not None else 0})")
            return

        # Try strategies in priority order — first match wins
        result = (
            self._check_trend_spread(df, spot) or
            self._check_reversal_spread(df, spot) or
            self._check_breakout_spread(df, spot)
        )
        if not result:
            logger.info(f"[CommOpts] {short} no strategy signal — spot={spot:.2f}")
            return

        # BreakoutSpread returns a 7th element (breakout level); others return 6
        if len(result) == 7:
            direction, strategy_name, signal_reason, rsi_val, ema5_val, ema20_val, breakout_level = result
        else:
            direction, strategy_name, signal_reason, rsi_val, ema5_val, ema20_val = result
            breakout_level = None
        logger.info(f"[CommOpts] {short} → {strategy_name} {direction} | {signal_reason}")

        chain    = self._get_chain(symbol)
        opt_type = "call" if direction == "LONG" else "put"

        trade = self._build_trade(
            symbol=symbol, short=short, meta=meta, spot=spot,
            direction=direction, strategy_name=strategy_name, signal_reason=signal_reason,
            opt_type=opt_type, chain=chain,
            rsi_val=rsi_val, ema5_val=ema5_val, ema20_val=ema20_val, now=now,
            breakout_level=breakout_level,
        )
        if trade:
            # ── Risk budget check ─────────────────────────────────
            if _RISK_BUDGET_AVAILABLE:
                try:
                    risk_est = trade.get("risk_per_lot", 0) * trade.get("lots", 1)
                    ok, reason = daily_risk_budget.check(strategy_name, risk_est, symbol=symbol)
                    if not ok:
                        logger.info(f"[CommOpts] {short} blocked by risk budget: {reason}")
                        if _AUDIT_AVAILABLE:
                            trade_decision_audit.log(AuditEntry(
                                symbol=short, strategy=strategy_name,
                                decision=Decision.REJECTED_BY_FILTER,
                                rejection_reason=f"risk_budget: {reason}",
                                data_snapshot={"risk_est": risk_est},
                            ))
                        return
                    daily_risk_budget.register_open(symbol, strategy_name)
                except Exception as _rbe:
                    logger.debug(f"[CommOpts] risk budget check error (skipped): {_rbe}")

            self._open_trade(trade)
            self._daily_entries[short] = self._daily_entries.get(short, 0) + 1
            logger.info(
                f"[CommOpts] {short} daily entries: "
                f"{self._daily_entries[short]}/{self.MAX_DAILY_ENTRIES_PER_INSTRUMENT}"
            )

            # ── Audit: TRADE taken ────────────────────────────────
            if _AUDIT_AVAILABLE:
                try:
                    trade_decision_audit.log(AuditEntry(
                        symbol=short, strategy=strategy_name,
                        decision=Decision.TRADE,
                        iv=round(trade.get("iv_used", 0) * 100, 1),
                        data_snapshot={
                            "net_debit":   trade.get("net_debit"),
                            "max_profit":  trade.get("max_profit"),
                            "rr":          trade.get("rr"),
                            "spot":        trade.get("spot_at_entry"),
                            "sq_score":    trade.get("spread_quality_score", 0),
                            "trade_id":    trade.get("id"),
                        },
                    ))
                    # Seed outcome metrics row at entry
                    from analysis.trade_decision_audit import OutcomeEntry
                    trade_decision_audit.log_outcome(OutcomeEntry(
                        trade_id   = trade["id"],
                        entry_time = trade["entry_time"],
                        strategy   = strategy_name,
                        symbol     = short,
                        net_debit  = trade.get("net_debit", 0),
                        max_profit = trade.get("max_profit", 0),
                        spread_quality_score = trade.get("spread_quality_score", 0),
                        iv_change  = trade.get("iv_used", 0),   # will be updated at close
                    ))
                except Exception as _ae:
                    logger.debug(f"[CommOpts] audit log error (skipped): {_ae}")
        else:
            # ── Audit: NO_TRADE ───────────────────────────────────
            if _AUDIT_AVAILABLE:
                try:
                    trade_decision_audit.log(AuditEntry(
                        symbol=short, strategy=strategy_name or "unknown",
                        decision=Decision.NO_TRADE,
                        rejection_reason="build_trade returned None",
                        data_snapshot={"spot": spot},
                    ))
                except Exception:
                    pass

    # ─────────────────────────────────────────────────────────────
    # STRATEGY EVALUATORS
    # ─────────────────────────────────────────────────────────────

    def _check_trend_spread(self, df, spot: float):
        """TrendSpread: EMA5/EMA20 crossover with RSI 55-68 and minimum EMA gap."""
        try:
            from analysis.indicators import rsi as calc_rsi, ema as calc_ema
            close   = df["close"]
            rsi_val = calc_rsi(close).iloc[-1]
            ema20   = calc_ema(close, 20).iloc[-1]
            ema5    = calc_ema(close, 5).iloc[-1]

            # Require at least 0.3% gap between EMAs — pure crossover noise is excluded
            ema_gap_pct = abs(ema5 - ema20) / ema20 * 100

            if (ema5 > ema20 and spot > ema20
                    and 55 < rsi_val < 68        # tightened: avoid weak(51) and near-OB(74) entries
                    and ema_gap_pct >= 0.3):      # confirmed trend, not a flat crossover
                return ("LONG", "TrendSpread",
                        f"EMA5={ema5:.0f}>EMA20={ema20:.0f}(+{ema_gap_pct:.1f}%), RSI={rsi_val:.1f}",
                        round(rsi_val, 1), round(ema5, 2), round(ema20, 2))
            if (ema5 < ema20 and spot < ema20
                    and 32 < rsi_val < 45        # tightened: avoid weak(49) and near-OS(26) entries
                    and ema_gap_pct >= 0.3):
                return ("SHORT", "TrendSpread",
                        f"EMA5={ema5:.0f}<EMA20={ema20:.0f}(-{ema_gap_pct:.1f}%), RSI={rsi_val:.1f}",
                        round(rsi_val, 1), round(ema5, 2), round(ema20, 2))
        except Exception as exc:
            logger.debug(f"[CommOpts] TrendSpread error: {exc}")
        return None

    def _check_reversal_spread(self, df, spot: float):
        """RSIReversalSpread: Buy oversold dip in uptrend; sell overbought high in downtrend."""
        try:
            from analysis.indicators import rsi as calc_rsi, ema as calc_ema
            close   = df["close"]
            rsi_val = calc_rsi(close).iloc[-1]
            ema20   = calc_ema(close, 20).iloc[-1]
            ema5    = calc_ema(close, 5).iloc[-1]

            # Oversold in higher-timeframe uptrend → reversal long
            if rsi_val < 32 and ema5 >= ema20 * 0.995:
                return ("LONG", "RSIReversalSpread",
                        f"RSI={rsi_val:.1f} oversold, EMA20={ema20:.0f}",
                        round(rsi_val, 1), round(ema5, 2), round(ema20, 2))
            # Overbought in higher-timeframe downtrend → reversal short
            if rsi_val > 68 and ema5 <= ema20 * 1.005:
                return ("SHORT", "RSIReversalSpread",
                        f"RSI={rsi_val:.1f} overbought, EMA20={ema20:.0f}",
                        round(rsi_val, 1), round(ema5, 2), round(ema20, 2))
        except Exception as exc:
            logger.debug(f"[CommOpts] RSIReversalSpread error: {exc}")
        return None

    def _check_breakout_spread(self, df, spot: float):
        """BreakoutSpread: 12-bar price breakout above/below recent range with RSI confirmation.

        Uses a 12-bar lookback (was 5) to define a meaningful range — 5 bars on 1H is only
        5 hours which is intraday noise.  Requires spot to clear the range high/low by at
        least 20% of ATR (buffer) to avoid 1-tick false triggers.

        Skips the first 30 min of MCX session (09:00–09:29) — the opening bar is incomplete
        and opening gaps frequently reverse before 09:30, generating false breakdown signals.

        Returns a 7-tuple; 7th element is the breakout level used for false-breakout exit.
        """
        now = datetime.now(tz=IST)
        if now.hour == 9 and now.minute < 30:
            return None
        try:
            from analysis.indicators import rsi as calc_rsi, ema as calc_ema, atr as calc_atr
            close   = df["close"]
            rsi_val = calc_rsi(close).iloc[-1]
            ema20   = calc_ema(close, 20).iloc[-1]
            ema5    = calc_ema(close, 5).iloc[-1]
            atr_val = calc_atr(df).iloc[-1]

            if len(close) < 14:
                return None

            # 12-bar range (excluding current bar) — defines a meaningful consolidation zone
            prev_high = close.iloc[-13:-1].max()
            prev_low  = close.iloc[-13:-1].min()

            # ATR buffer: spot must clear the range by 20% of ATR to filter noise
            buffer = round(atr_val * 0.20, 2)

            if spot > prev_high + buffer and rsi_val > 52:
                return ("LONG", "BreakoutSpread",
                        f"Breakout above {prev_high:.0f}+{buffer:.0f}buf, RSI={rsi_val:.1f}",
                        round(rsi_val, 1), round(ema5, 2), round(ema20, 2),
                        round(prev_high, 2))
            if spot < prev_low - buffer and rsi_val < 48:
                return ("SHORT", "BreakoutSpread",
                        f"Breakdown below {prev_low:.0f}-{buffer:.0f}buf, RSI={rsi_val:.1f}",
                        round(rsi_val, 1), round(ema5, 2), round(ema20, 2),
                        round(prev_low, 2))
        except Exception as exc:
            logger.debug(f"[CommOpts] BreakoutSpread error: {exc}")
        return None

    def _build_trade(
        self, symbol, short, meta, spot, direction, strategy_name, signal_reason,
        opt_type, chain, rsi_val, ema5_val, ema20_val, now, breakout_level=None,
    ) -> Optional[dict]:
        """Build a debit spread trade (buy ATM + sell OTM)."""
        step    = meta["strike_step"]
        iv      = meta["typical_iv"]
        lot     = meta["lot_size"]
        dte     = 21   # target ~3 weeks to expiry

        atm  = _atm_strike(spot, step)

        # OTM leg is 2 strikes away (defines spread width)
        otm  = (atm + 2 * step) if opt_type == "call" else (atm - 2 * step)

        # Try to read real prices from chain; fall back to BS
        atm_premium = otm_premium = None

        atm_sym = otm_sym = ""
        if chain:
            atm_premium, real_iv, nfo_sym = self._chain_lookup(
                chain, atm, opt_type, dte
            )
            if real_iv and real_iv > 0:
                iv = real_iv
            if nfo_sym:
                atm_sym = nfo_sym
            otm_premium, _, otm_nfo = self._chain_lookup(chain, otm, opt_type, dte)
            if otm_nfo:
                otm_sym = otm_nfo

        if atm_premium is None or atm_premium <= 0:
            atm_premium = _bs_price(spot, atm, iv, dte, opt_type)
        if otm_premium is None or otm_premium <= 0:
            otm_premium = _bs_price(spot, otm, iv, dte, opt_type)

        # Net debit = buy ATM − sell OTM credit
        net_debit  = round(atm_premium - otm_premium, 2)
        spread_width = abs(atm - otm)
        max_profit   = round(spread_width - net_debit, 2)

        if net_debit <= 0 or max_profit <= 0:
            logger.debug(f"[CommOpts] {short} invalid spread pricing: debit={net_debit}")
            return None

        rr = round(max_profit / net_debit, 2)
        if rr < 1.0:
            logger.debug(f"[CommOpts] {short} R:R {rr:.2f} too low")
            return None

        # ── Spread quality gate ───────────────────────────────────
        sq_score = 5.0   # default if engine unavailable
        if _SPREAD_QUALITY_AVAILABLE:
            try:
                # Build leg data from chain or BS estimates
                # OI from chain if available; else use conservative estimate
                long_oi  = 1000 if chain else 0
                short_oi = 800  if chain else 0
                if chain:
                    for exp in chain.get("expiryData", []):
                        for opt in exp.get("optionData", []):
                            if opt.get("strikePrice") == atm:
                                long_oi  = opt.get(f"{opt_type}OI", long_oi)
                            if opt.get("strikePrice") == otm:
                                short_oi = opt.get(f"{opt_type}OI", short_oi)
                            if long_oi != 1000 and short_oi != 800:
                                break

                long_leg  = LegData(bid=atm_premium * 0.97, ask=atm_premium * 1.03,
                                    oi=long_oi, theta=-0.02 * atm_premium)
                short_leg = LegData(bid=otm_premium * 0.97, ask=otm_premium * 1.03,
                                    oi=short_oi, theta=-0.02 * otm_premium)
                sq = spread_quality_engine.evaluate(long_leg, short_leg, net_debit, max_profit)
                sq_score = sq.quality_score

                if not sq.approved:
                    logger.info(
                        f"[CommOpts] {short} spread quality REJECTED "
                        f"(score={sq_score:.1f}): {sq.rejection_reason}"
                    )
                    if _AUDIT_AVAILABLE:
                        trade_decision_audit.log(AuditEntry(
                            symbol=short, strategy=strategy_name,
                            decision=Decision.REJECTED_BY_FILTER,
                            rejection_reason=f"spread_quality: {sq.rejection_reason}",
                            iv=round(iv * 100, 1),
                            data_snapshot={"sq_score": sq_score, "rr": rr,
                                           "net_debit": net_debit, "max_profit": max_profit},
                        ))
                    return None
            except Exception as _sqe:
                logger.debug(f"[CommOpts] spread quality check error (skipped): {_sqe}")

        # Notional risk per lot
        risk_per_lot = round(net_debit * lot, 2)

        trade_id = f"COM-{uuid.uuid4().hex[:8].upper()}"
        return {
            "id":           trade_id,
            "symbol":       symbol,
            "instrument":   short,
            "direction":    direction,
            "opt_type":     opt_type,
            "strategy":     strategy_name,
            "entry_time":   now.isoformat(),
            "status":       "OPEN",
            "spot_at_entry": round(spot, 2),
            "atm_strike":   atm,
            "otm_strike":   otm,
            "net_debit":    net_debit,
            "max_profit":   max_profit,
            "spread_width": spread_width,
            "rr":           rr,
            "iv_used":      round(iv, 4),
            "lot_size":     lot,
            "risk_per_lot": risk_per_lot,
            "dte":          dte,
            "data_source":  "live_chain" if chain else "bs_estimate",
            "metadata": {
                "rsi":           rsi_val,
                "ema5":          ema5_val,
                "ema20":         ema20_val,
                "spot":          round(spot, 2),
                "iv_pct":        round(iv * 100, 1),
                "atm_prem":      round(atm_premium, 2),
                "otm_prem":      round(otm_premium, 2),
                "price_unit":    meta["price_unit"],
                "signal_reason":   signal_reason,
                "atm_symbol":      atm_sym,
                "otm_symbol":      otm_sym,
                "breakout_level":  breakout_level,
                # SL / trailing state — updated by _check_exits as trade evolves.
                # sl_price is derived from debit cost so expensive spreads (high IV)
                # get more room and cheap spreads get tighter protection.
                "sl_price":    _debit_sl_price(spot, direction, net_debit,
                                               _merged_strat_cfg(short, strategy_name)),
                "peak_spot":   round(spot, 2),
                "target_pct":  _merged_strat_cfg(short, strategy_name).get("target_pct", 0.50),
                "trail_active": False,
            },
            "spread_quality_score": sq_score,
        }

    # ─────────────────────────────────────────────────────────────
    # EXIT MONITORING
    # ─────────────────────────────────────────────────────────────

    def check_exits(self) -> None:
        """Public entry point — called from the 5-second fast loop in main.py."""
        if not self._open_positions:
            return
        now = datetime.now(tz=IST)
        if not (MCX_MARKET_OPEN <= now.time() <= MCX_MARKET_CLOSE):
            return
        from data.data_store import store
        self._check_exits(store, now)

    def _check_exits(self, store, now: datetime) -> None:
        closed = []
        for key, trade in list(self._open_positions.items()):
            instrument = trade["instrument"]
            stored_sym = trade["symbol"]
            symbol = _fyers_sym(instrument)
            spot   = store.get_ltp(symbol)
            if not spot and symbol != stored_sym:
                spot = store.get_ltp(stored_sym)
            if not spot:
                continue

            cfg   = MCX_CONTRACTS.get(instrument, {})
            min_p = cfg.get("min_price", 0)
            max_p = cfg.get("max_price", float("inf"))
            if spot < min_p or spot > max_p:
                logger.warning(
                    f"[CommOpts] Spot {spot:.1f} out of range [{min_p}-{max_p}] "
                    f"for {instrument} — skipping exit check"
                )
                continue

            direction  = trade["direction"]
            entry_spot = trade["spot_at_entry"]
            net_debit  = trade["net_debit"]
            max_profit = trade["max_profit"]
            strategy   = trade.get("strategy", "")
            trade_meta = trade.get("metadata", {})
            exit_reason = None
            pnl_approx  = None

            # Delta-based P&L estimate (per unit, used for reporting only)
            spot_move = spot - entry_spot if direction == "LONG" else entry_spot - spot
            est_pnl   = round(spot_move * 0.35, 2)

            # ── Trailing SL & Dynamic Target ──────────────────────────────────────
            # Merge strategy defaults with per-instrument overrides so Silver/Copper
            # can each have appropriate SL widths without touching shared strategy config.
            strat_cfg       = _merged_strat_cfg(instrument, strategy)
            sl_debit_pct    = strat_cfg.get("sl_debit_pct",        0.40)
            trail_debit_pct = strat_cfg.get("trail_debit_pct",     0.30)
            trail_trig_pct  = strat_cfg.get("trail_trigger_pct",   0.50)
            tgt_up_pct      = strat_cfg.get("target_upgraded_pct", 0.75)
            tgt_up_mult     = strat_cfg.get("target_upgrade_mult", 2.0)

            # Convert debit-% params to spot-point distances for this trade
            sl_dist    = round((sl_debit_pct    * net_debit) / _SPREAD_DELTA, 2) if net_debit > 0 else round(entry_spot * 0.015, 2)
            trail_dist = round((trail_debit_pct * net_debit) / _SPREAD_DELTA, 2) if net_debit > 0 else round(entry_spot * 0.011, 2)
            # Trail trigger as a fraction of SL distance — e.g. 0.50 means "activate
            # trail once spot has moved half the SL distance in our favour".
            # Previously used (pct × debit / delta) which gave 135 pts vs SL of 108 pts,
            # meaning trail only kicked in 16 pts before target — effectively useless.
            trail_trig = round(trail_trig_pct * sl_dist, 2)

            sl_price   = trade_meta.get("sl_price")
            peak_spot  = trade_meta.get("peak_spot", entry_spot)
            target_pct = trade_meta.get("target_pct", strat_cfg.get("target_pct", 0.50))
            sl_updated = False

            # First-time setup for trades opened before this feature
            if sl_price is None:
                sl_price   = _debit_sl_price(entry_spot, direction, net_debit, strat_cfg)
                sl_updated = True
            # Recalculate trail_trig_spot; rewrite if stale so formula changes take effect
            _trig_spot_new = round(
                (entry_spot + trail_trig) if direction == "LONG" else (entry_spot - trail_trig), 2
            )
            if trade_meta.get("trail_trig_spot") != _trig_spot_new:
                sl_updated = True

            # Update peak (best spot seen in the trade's favor)
            peak_updated = False
            if direction == "LONG":
                if spot > peak_spot:
                    peak_spot    = spot
                    peak_updated = True
            else:
                if spot < peak_spot:
                    peak_spot    = spot
                    peak_updated = True

            # Advance trailing SL once favorable move ≥ trail_trig
            favorable_move = (peak_spot - entry_spot) if direction == "LONG" else (entry_spot - peak_spot)
            if favorable_move >= trail_trig:
                if direction == "LONG":
                    new_sl = round(peak_spot - trail_dist, 2)   # fixed distance below peak
                    if new_sl > sl_price:
                        sl_price = new_sl
                        trade_meta["trail_active"] = True
                        sl_updated = True
                else:
                    new_sl = round(peak_spot + trail_dist, 2)   # fixed distance above peak (trough)
                    if new_sl < sl_price:
                        sl_price = new_sl
                        trade_meta["trail_active"] = True
                        sl_updated = True

            # Upgrade target once momentum is ≥ tgt_up_mult × SL distance,
            # capped at 90% of T1 distance so the upgrade always fires before
            # the initial target exit — without this cap, low-R:R spreads (e.g.
            # Crude Oil R:R ≈ 1.5) have 2×SL > T1_dist, meaning T1 exits first
            # and the dynamic target never triggers.
            t1_dist_pts = round((max_profit * target_pct) / _SPREAD_DELTA, 2)
            upgrade_trigger = min(
                round(tgt_up_mult * sl_dist, 2),
                round(t1_dist_pts * 0.90, 2),   # hard cap: never exceed 90% of T1
            )
            if favorable_move >= upgrade_trigger and target_pct < tgt_up_pct:
                old_target_pct = target_pct
                target_pct     = tgt_up_pct
                sl_updated     = True
                # T1 spot level: the price at which est_pnl would equal old target
                t1_spot_dist = round((max_profit * old_target_pct) / _SPREAD_DELTA, 2)
                # Small buffer (half a trail_dist) so a 1-tick wick at T1 doesn't stop us
                t1_sl_buffer = round(trail_dist * 0.25, 2)
                if direction == "LONG":
                    t1_sl = round(entry_spot + t1_spot_dist - t1_sl_buffer, 2)
                    if t1_sl > sl_price:
                        sl_price = t1_sl
                else:
                    t1_sl = round(entry_spot - t1_spot_dist + t1_sl_buffer, 2)
                    if t1_sl < sl_price:
                        sl_price = t1_sl
                logger.info(
                    f"[CommOpts] {instrument} target upgraded to {int(tgt_up_pct*100)}% — "
                    f"SL floor raised to {sl_price:.2f} (T1 lock)"
                )

            # Persist SL state to DB whenever anything relevant changed.
            # peak_spot is persisted on every new high/low so a restart doesn't
            # lose the trailing reference point.
            if sl_updated or peak_updated:
                trade_meta["sl_price"]        = sl_price
                trade_meta["peak_spot"]       = round(peak_spot, 2)
                trade_meta["target_pct"]      = target_pct
                trade_meta["trail_trig_spot"] = round(
                    (entry_spot + trail_trig) if direction == "LONG" else (entry_spot - trail_trig), 2
                )
                trade["metadata"] = trade_meta
                self._db_update_sl(trade["id"], trade_meta)

            # ── False-breakout exit (BreakoutSpread only) ────────────────────────
            if strategy == "BreakoutSpread":
                breakout_level = trade_meta.get("breakout_level") or entry_spot
                if direction == "LONG" and spot < breakout_level:
                    exit_reason = "FALSE_BREAKOUT"
                    pnl_approx  = round(est_pnl, 2)
                    logger.info(
                        f"[CommOpts] {instrument} LONG breakout invalidated — "
                        f"spot {spot:.2f} < breakout {breakout_level:.2f}"
                    )
                elif direction == "SHORT" and spot > breakout_level:
                    exit_reason = "FALSE_BREAKOUT"
                    pnl_approx  = round(est_pnl, 2)
                    logger.info(
                        f"[CommOpts] {instrument} SHORT breakdown invalidated — "
                        f"spot {spot:.2f} > breakout {breakout_level:.2f}"
                    )

            # ── Spot-based SL ────────────────────────────────────────────────────
            if exit_reason is None:
                sl_type = "trail" if trade_meta.get("trail_active") else "initial"
                if direction == "LONG" and spot <= sl_price:
                    exit_reason = "STOP_SPOT"
                    pnl_approx  = round(est_pnl, 2)
                    logger.info(
                        f"[CommOpts] {instrument} SL hit: spot {spot:.2f} ≤ sl {sl_price:.2f} ({sl_type})"
                    )
                elif direction == "SHORT" and spot >= sl_price:
                    exit_reason = "STOP_SPOT"
                    pnl_approx  = round(est_pnl, 2)
                    logger.info(
                        f"[CommOpts] {instrument} SL hit: spot {spot:.2f} ≥ sl {sl_price:.2f} ({sl_type})"
                    )

            # ── Dynamic target ───────────────────────────────────────────────────
            if exit_reason is None:
                if est_pnl >= max_profit * target_pct:
                    exit_reason = "TARGET_PCT"
                    pnl_approx  = round(max_profit * target_pct, 2)

            # ── EOD ─────────────────────────────────────────────────────────────
            if exit_reason is None and now.time() >= dtime(23, 15):
                exit_reason = "EOD_MCX"
                pnl_approx  = round(est_pnl, 2)

            if exit_reason:
                lot_size   = trade.get("lot_size", 1)
                lots       = trade.get("lots", 1)
                pnl_r      = round(pnl_approx / net_debit, 2) if net_debit > 0 else 0
                # Scale PnL by actual lots traded
                pnl_approx = round(pnl_approx * lot_size * lots, 2)

                # ── Real exit: place broker close orders ──────────
                if self._is_real():
                    self._execute_real_exit(trade, exit_reason)
                    self._send_alert(
                        f"MCX REAL CLOSE — {instrument} {exit_reason} "
                        f"spot={spot:.2f} pnl=₹{pnl_approx:+.0f} ({pnl_r:+.1f}R)"
                    )

                self._db_close(trade["id"], spot, exit_reason, pnl_approx, pnl_r)
                closed.append(key)
                logger.info(
                    f"[CommOpts] {'REAL' if self._is_real() else 'PAPER'} CLOSE "
                    f"{trade['id']} | {instrument} "
                    f"{exit_reason} spot={spot:.2f} pnl=₹{pnl_approx:+.0f} "
                    f"({pnl_r:+.1f}R)"
                )

                # ── Outcome metrics logging ───────────────────────
                if _AUDIT_AVAILABLE:
                    try:
                        from analysis.trade_decision_audit import OutcomeEntry
                        # Estimate IV change from spot move heuristic
                        # (real IV change needs live chain; use proxy for paper mode)
                        spot_move_pct = abs(spot - entry_spot) / entry_spot if entry_spot > 0 else 0
                        iv_change_est = round(-spot_move_pct * 0.1, 4)
                        trade_decision_audit.log_outcome(OutcomeEntry(
                            trade_id        = trade["id"],
                            entry_time      = trade["entry_time"],
                            exit_time       = now.isoformat(),
                            pnl             = pnl_approx,
                            pnl_r_multiple  = pnl_r,
                            theta_impact    = round(-net_debit * 0.02 *
                                              (now - datetime.fromisoformat(trade["entry_time"])
                                               .replace(tzinfo=IST)).days, 2),
                            iv_change       = iv_change_est,
                            spread_slippage = 0.0,
                            exit_reason     = exit_reason,
                            strategy        = strategy,
                            symbol          = instrument,
                            net_debit       = net_debit,
                            max_profit      = max_profit,
                            spread_quality_score = trade.get("spread_quality_score", 0),
                        ))
                    except Exception as _oe:
                        logger.debug(f"[CommOpts] outcome log error (skipped): {_oe}")

                # ── Risk budget update ────────────────────────────
                if _RISK_BUDGET_AVAILABLE:
                    try:
                        daily_risk_budget.record_pnl(strategy, pnl_approx)
                        daily_risk_budget.register_close(trade["symbol"])
                    except Exception as _rbe:
                        logger.debug(f"[CommOpts] risk budget record error (skipped): {_rbe}")
                strat_cfg = MCX_STRATEGY_CONFIG.get(strategy, {})
                if strat_cfg.get("cooldown_enabled", True):
                    base_hours = strat_cfg.get("cooldown_hours", 3)
                    if exit_reason in ("FALSE_BREAKOUT", "STOP_SPOT", "STOP_60PCT"):
                        cd_hours = base_hours
                    elif exit_reason == "TARGET_PCT":
                        # After a winning exit don't re-enter immediately — the move
                        # is likely extended and the next signal will chase. Half the
                        # stop cooldown, minimum 1 hour.
                        cd_hours = max(1.0, base_hours * 0.5)
                    else:
                        cd_hours = 0
                    if cd_hours > 0:
                        self._entry_cooldown[instrument] = now + timedelta(hours=cd_hours)
                        logger.info(
                            f"[CommOpts] {instrument} entry cooldown {cd_hours:.1f}h "
                            f"after {exit_reason} — "
                            f"no new trades until {self._entry_cooldown[instrument].strftime('%H:%M')}"
                        )

        for k in closed:
            del self._open_positions[k]

    def _open_trade(self, trade: dict) -> None:
        instrument = trade["instrument"]
        meta       = trade.get("metadata", {})

        # ── Lot sizing ────────────────────────────────────────────
        lots = self._compute_lots(instrument, trade["net_debit"])
        if lots == 0:
            logger.warning(f"[CommOpts] {instrument} entry blocked — insufficient funds")
            return
        trade["lots"]       = lots
        trade["trade_mode"] = self._trade_mode

        # ── Real execution ────────────────────────────────────────
        if self._is_real():
            atm_sym  = meta.get("atm_symbol", "")
            otm_sym  = meta.get("otm_symbol", "")
            qty      = lots * trade["lot_size"]
            atm_oid, otm_oid, atm_fill, otm_fill = self._execute_real_entry(
                atm_sym, otm_sym, qty
            )
            if atm_oid is None:
                # Entry failed — do not record the trade
                logger.error(f"[CommOpts] {instrument} real entry aborted")
                return
            # Update debit with actual fill prices (real > estimated when IV spikes)
            if atm_fill > 0 and otm_fill > 0:
                actual_debit  = round(atm_fill - otm_fill, 2)
                actual_profit = round(trade["spread_width"] - actual_debit, 2)
                if actual_debit > 0 and actual_profit > 0:
                    trade["net_debit"]  = actual_debit
                    trade["max_profit"] = actual_profit
                    trade["rr"]         = round(actual_profit / actual_debit, 2)
                    # Recompute SL with actual debit
                    strat_cfg = MCX_STRATEGY_CONFIG.get(trade.get("strategy", ""), {})
                    meta["sl_price"]   = _debit_sl_price(
                        trade["spot_at_entry"], trade["direction"], actual_debit, strat_cfg
                    )
                    meta["atm_fill"]   = atm_fill
                    meta["otm_fill"]   = otm_fill
            meta["atm_order_id"] = atm_oid
            meta["otm_order_id"] = otm_oid
            trade["metadata"]    = meta
            self._send_alert(
                f"MCX REAL ENTRY — {instrument} {trade['direction']} debit spread | "
                f"spot={trade['spot_at_entry']:.2f} ATM={trade['atm_strike']} "
                f"OTM={trade['otm_strike']} debit={trade['net_debit']:.2f} "
                f"lots={lots} SL@{meta.get('sl_price','?')}"
            )

        key = f"{instrument}:{trade['direction']}"
        self._open_positions[key] = trade
        self._db_insert(trade)
        logger.info(
            f"[CommOpts] {'REAL' if self._is_real() else 'PAPER'} OPEN "
            f"{trade['id']} | {instrument} {trade['direction']} debit spread | "
            f"spot={trade['spot_at_entry']:.2f} "
            f"ATM={trade['atm_strike']} OTM={trade['otm_strike']} "
            f"debit={trade['net_debit']:.2f} maxP={trade['max_profit']:.2f} "
            f"R:R={trade['rr']:.2f} lots={lots} SL@{meta.get('sl_price','?')} | "
            f"src={trade['data_source']}"
        )

    def _db_update_sl(self, trade_id: str, metadata: dict) -> None:
        """Persist trailing SL / dynamic target state to DB metadata column."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE commodity_learning_trades SET metadata=? WHERE id=?",
                    (json.dumps(metadata), trade_id),
                )
        except Exception as exc:
            logger.debug(f"[CommOpts] SL state persist error for {trade_id}: {exc}")

    # ─────────────────────────────────────────────────────────────
    # FYERS CHAIN FETCH
    # ─────────────────────────────────────────────────────────────

    def _get_chain(self, symbol: str) -> Optional[dict]:
        """Try to fetch MCX options chain from Fyers. Returns None on failure."""
        now = datetime.now(tz=IST)
        if symbol in self._chain_cache:
            cached, fetched_at = self._chain_cache[symbol]
            if (now - fetched_at).total_seconds() < 120:
                return cached

        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised or not fyers_broker._client:
                return None

            resp = fyers_broker._client.optionchain(data={
                "symbol":      symbol,
                "strikecount": 10,
                "timestamp":   "",
            })

            if resp.get("s") != "ok":
                logger.debug(f"[CommOpts] Chain fetch failed for {symbol}: {resp.get('message')}")
                return None

            data = resp.get("data", {})
            self._chain_cache[symbol] = (data, now)
            logger.info(f"[CommOpts] Live chain fetched for {symbol}")
            return data

        except Exception as e:
            logger.debug(f"[CommOpts] Chain exception for {symbol}: {e}")
            return None

    def _chain_lookup(
        self, chain: dict, strike: int, opt_type: str, target_dte: int
    ) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Find a specific strike/type in chain data.
        Returns (ltp, iv, nfo_symbol) or (None, None, None).
        """
        try:
            expiries = chain.get("expiryData", [])
            if not expiries:
                return None, None, None

            # Pick expiry closest to target_dte
            from datetime import datetime as dt
            today = dt.now(tz=IST).date()
            best_expiry = None
            best_diff   = 9999

            for exp in expiries:
                expiry_str = exp.get("expiry", "")
                try:
                    exp_date = dt.strptime(expiry_str, "%Y-%m-%d").date()
                    diff = abs((exp_date - today).days - target_dte)
                    if diff < best_diff:
                        best_diff, best_expiry = diff, exp
                except Exception:
                    continue

            if not best_expiry:
                return None, None, None

            field = "CE" if opt_type == "call" else "PE"
            for row in best_expiry.get("optionsChain", []):
                if abs(row.get("strikePrice", 0) - strike) < 1:
                    leg = row.get(field, {})
                    ltp = leg.get("ltp") or leg.get("close_price")
                    iv  = leg.get("iv", 0)
                    sym = leg.get("symbol", "")
                    if ltp and ltp > 0:
                        return float(ltp), float(iv) if iv else None, sym
        except Exception as exc:
            logger.debug(f"[CommOpts] Chain lookup error: {exc}")
        return None, None, None

    # ─────────────────────────────────────────────────────────────
    # DATABASE
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commodity_learning_trades (
                    id              TEXT PRIMARY KEY,
                    symbol          TEXT,
                    instrument      TEXT,
                    direction       TEXT,
                    opt_type        TEXT,
                    strategy        TEXT,
                    spot_at_entry   REAL,
                    spot_at_exit    REAL DEFAULT 0,
                    atm_strike      INTEGER,
                    otm_strike      INTEGER,
                    net_debit       REAL,
                    max_profit      REAL,
                    spread_width    REAL,
                    rr              REAL,
                    iv_used         REAL,
                    lot_size        INTEGER,
                    lots            INTEGER DEFAULT 1,
                    trade_mode      TEXT    DEFAULT 'PAPER',
                    risk_per_lot    REAL,
                    dte             INTEGER,
                    pnl_approx      REAL DEFAULT 0,
                    pnl_r           REAL DEFAULT 0,
                    status          TEXT DEFAULT 'OPEN',
                    exit_reason     TEXT DEFAULT '',
                    data_source     TEXT DEFAULT 'bs_estimate',
                    entry_time      TEXT,
                    exit_time       TEXT DEFAULT '',
                    metadata        TEXT DEFAULT '{}'
                )
            """)
            for col_sql in [
                "ALTER TABLE commodity_learning_trades ADD COLUMN lots       INTEGER DEFAULT 1",
                "ALTER TABLE commodity_learning_trades ADD COLUMN trade_mode TEXT    DEFAULT 'PAPER'",
            ]:
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS commodity_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # Instruments table — source of truth for all MCX contracts
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commodity_instruments (
                    name             TEXT PRIMARY KEY,
                    display_name     TEXT    NOT NULL DEFAULT '',
                    lot_size         INTEGER NOT NULL,
                    strike_step      REAL    NOT NULL,
                    typical_iv       REAL    NOT NULL DEFAULT 0.30,
                    price_unit       TEXT    NOT NULL DEFAULT 'INR/unit',
                    min_price        REAL    NOT NULL DEFAULT 100,
                    max_price        REAL    NOT NULL DEFAULT 999999,
                    rollover_buffer  INTEGER NOT NULL DEFAULT 5,
                    valid_months     TEXT    NOT NULL DEFAULT '[]',
                    enabled          INTEGER NOT NULL DEFAULT 1
                )
            """)
            # Add any columns missing from older DB versions
            for _col_sql in [
                "ALTER TABLE commodity_instruments ADD COLUMN display_name       TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE commodity_instruments ADD COLUMN sl_debit_pct       REAL",
                "ALTER TABLE commodity_instruments ADD COLUMN trail_debit_pct    REAL",
                "ALTER TABLE commodity_instruments ADD COLUMN trail_trigger_pct  REAL",
                "ALTER TABLE commodity_instruments ADD COLUMN target_pct         REAL",
                "ALTER TABLE commodity_instruments ADD COLUMN target_upgraded_pct REAL",
                "ALTER TABLE commodity_instruments ADD COLUMN target_upgrade_mult REAL",
            ]:
                try:
                    conn.execute(_col_sql)
                except Exception:
                    pass  # column already exists

            # Seed built-in instruments — INSERT OR IGNORE preserves user edits
            conn.executemany("""
                INSERT OR IGNORE INTO commodity_instruments
                (name, display_name, lot_size, strike_step, typical_iv, price_unit,
                 min_price, max_price, rollover_buffer, valid_months, enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,1)
            """, [
                (n, dn, ls, ss, iv, pu, mn, mx, rb, json.dumps(vm))
                for n, dn, ls, ss, iv, pu, mn, mx, rb, vm in _INSTRUMENT_SEEDS
            ])

        self._load_contracts_from_db()
        logger.info(f"[CommOpts] DB ready — {len(MCX_CONTRACTS)} instruments loaded")

    def _db_insert(self, t: dict) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO commodity_learning_trades
                (id, symbol, instrument, direction, opt_type, strategy,
                 spot_at_entry, atm_strike, otm_strike, net_debit, max_profit,
                 spread_width, rr, iv_used, lot_size, lots, trade_mode,
                 risk_per_lot, dte, status, data_source, entry_time, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t["id"], t["symbol"], t["instrument"], t["direction"],
                t["opt_type"], t["strategy"], t["spot_at_entry"],
                t["atm_strike"], t["otm_strike"], t["net_debit"],
                t["max_profit"], t["spread_width"], t["rr"], t["iv_used"],
                t["lot_size"], t.get("lots", 1), t.get("trade_mode", "PAPER"),
                t["risk_per_lot"], t["dte"],
                t["status"], t["data_source"], t["entry_time"],
                json.dumps(t.get("metadata", {})),
            ))

    def _db_close(
        self, trade_id: str, exit_spot: float,
        reason: str, pnl: float, pnl_r: float,
    ) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                UPDATE commodity_learning_trades
                SET spot_at_exit=?, exit_reason=?, pnl_approx=?, pnl_r=?,
                    status='CLOSED', exit_time=?
                WHERE id=?
            """, (
                exit_spot, reason,
                round(pnl, 2), round(pnl_r, 2),
                datetime.now(tz=IST).isoformat(),
                trade_id,
            ))

    # ─────────────────────────────────────────────────────────────
    # READ API
    # ─────────────────────────────────────────────────────────────

    def get_trades(self, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM commodity_learning_trades "
                    "WHERE status=? ORDER BY entry_time DESC LIMIT ?",
                    (status.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM commodity_learning_trades "
                    "ORDER BY entry_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            result.append(d)
        return result

    def get_stats(self) -> dict:
        trades = self.get_trades(status="CLOSED", limit=1000)
        open_t = self.get_trades(status="OPEN")
        if not trades:
            return {
                "total_closed": 0,
                "total_open":   len(open_t),
                "message": "No closed commodity option trades yet.",
            }

        all_r  = [t["pnl_r"] for t in trades]
        wins   = [r for r in all_r if r > 0]

        by_instrument: dict = {}
        for t in trades:
            ins = t["instrument"]
            if ins not in by_instrument:
                by_instrument[ins] = {"total": 0, "wins": 0, "total_r": 0.0}
            by_instrument[ins]["total"]   += 1
            by_instrument[ins]["total_r"] += t["pnl_r"]
            if t["pnl_r"] > 0:
                by_instrument[ins]["wins"] += 1

        for ins, d in by_instrument.items():
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1)
            d["avg_r"]    = round(d["total_r"] / d["total"], 2)

        by_strategy: dict = {}
        for t in trades:
            strat = t.get("strategy") or "Unknown"
            if strat not in by_strategy:
                by_strategy[strat] = {
                    "total": 0, "wins": 0, "total_r": 0.0,
                    "best_r": None, "worst_r": None,
                }
            by_strategy[strat]["total"]   += 1
            by_strategy[strat]["total_r"] += t["pnl_r"]
            if t["pnl_r"] > 0:
                by_strategy[strat]["wins"] += 1
            r = t["pnl_r"]
            d = by_strategy[strat]
            if d["best_r"] is None or r > d["best_r"]:
                d["best_r"] = r
            if d["worst_r"] is None or r < d["worst_r"]:
                d["worst_r"] = r

        for strat, d in by_strategy.items():
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1)
            d["avg_r"]    = round(d["total_r"] / d["total"], 2)
            d["total_r"]  = round(d["total_r"], 2)
            d["best_r"]   = round(d["best_r"] or 0, 2)
            d["worst_r"]  = round(d["worst_r"] or 0, 2)

        return {
            "total_closed":   len(trades),
            "total_open":     len(open_t),
            "win_rate_pct":   round(len(wins) / len(trades) * 100, 1),
            "avg_r":          round(sum(all_r) / len(all_r), 2),
            "total_r":        round(sum(all_r), 2),
            "best_trade_r":   round(max(all_r), 2),
            "worst_trade_r":  round(min(all_r), 2),
            "by_instrument":  by_instrument,
            "by_strategy":    by_strategy,
            "data_sources":   _count_field(trades, "data_source"),
            "exit_reasons":   _count_field(trades, "exit_reason"),
            "open_positions": [
                {k: v for k, v in t.items() if k != "metadata"}
                for t in open_t
            ],
        }

    def get_chain_snapshot(self, symbol: str) -> dict:
        """Return a human-readable chain snapshot for the dashboard."""
        chain = self._get_chain(symbol)
        spot  = None
        try:
            from data.data_store import store
            spot = store.get_ltp(symbol)
        except Exception:
            pass

        if not chain:
            meta = MCX_CONTRACTS.get(symbol, {})
            iv   = meta.get("typical_iv", 0.35)
            step = meta.get("strike_step", 100)
            atm  = _atm_strike(spot or 0, step) if spot else 0
            return {
                "source":   "bs_estimate",
                "symbol":   symbol,
                "spot":     spot,
                "atm":      atm,
                "note":     "Live chain unavailable — showing Black-Scholes estimates",
                "strikes": [
                    {
                        "strike": atm + (i - 3) * step,
                        "call":   round(_bs_price(spot or atm, atm + (i - 3) * step, iv, 21, "call"), 2),
                        "put":    round(_bs_price(spot or atm, atm + (i - 3) * step, iv, 21, "put"),  2),
                    }
                    for i in range(7)
                ] if spot else [],
            }

        return {
            "source":            "live_chain",
            "symbol":            symbol,
            "spot":              chain.get("underlyingValue"),
            "expiry_count":      len(chain.get("expiryData", [])),
            "nearest_expiry":    chain.get("expiryData", [{}])[0].get("expiry") if chain.get("expiryData") else None,
            "strikes_available": len(chain.get("expiryData", [{}])[0].get("optionsChain", [])) if chain.get("expiryData") else 0,
        }


    def _to_short(self, symbol: str) -> str:
        """Map 'CRUDEOIL' or 'MCX:CRUDEOIL26MAYFUT' → short name key in MCX_CONTRACTS."""
        sym_up = symbol.upper().replace("MCX:", "")
        if sym_up in MCX_CONTRACTS:
            return sym_up
        for short in MCX_CONTRACTS:
            if sym_up.startswith(short):
                return short
        return sym_up

    def get_full_chain(self, symbol: str, expiry_idx: int = 0) -> dict:
        """Return full options chain with Greeks for dashboard display."""
        from analysis.options_engine import OptionsEngine
        engine = OptionsEngine()
        now    = datetime.now(tz=IST)
        today  = now.date()

        short      = self._to_short(symbol)
        full_sym   = _fyers_sym(short)
        meta       = MCX_CONTRACTS.get(short, {})
        step       = meta.get("strike_step", 100)
        typical_iv = meta.get("typical_iv", 0.35)

        spot = None
        try:
            from data.data_store import store
            spot = store.get_ltp(full_sym)
        except Exception:
            pass

        chain = self._get_chain(full_sym)

        if chain:
            expiries_raw = chain.get("expiryData", [])
            expiry_list  = [e.get("expiry", "") for e in expiries_raw]
            underlying   = chain.get("underlyingValue")
            if spot is None:
                spot = underlying

            if not expiries_raw:
                return self._bs_chain(full_sym, spot, step, typical_iv, meta)

            idx          = min(expiry_idx, len(expiries_raw) - 1)
            expiry_entry = expiries_raw[idx]
            expiry_str   = expiry_entry.get("expiry", "")

            try:
                from datetime import datetime as _dt
                exp_date = _dt.strptime(expiry_str, "%Y-%m-%d").date()
                dte = max((exp_date - today).days, 1)
            except Exception:
                dte = 21

            T   = dte / 365.0
            atm = _atm_strike(spot or 0, step) if spot else 0

            strikes_out = []
            for row in expiry_entry.get("optionsChain", []):
                k  = float(row.get("strikePrice", 0))
                ce = row.get("CE", {}) or {}
                pe = row.get("PE", {}) or {}

                ce_ltp = float(ce.get("ltp") or ce.get("close_price") or 0)
                pe_ltp = float(pe.get("ltp") or pe.get("close_price") or 0)
                ce_iv  = float(ce.get("iv") or 0)
                pe_iv  = float(pe.get("iv") or 0)
                ce_oi  = int(ce.get("oi") or ce.get("openInterest") or 0)
                pe_oi  = int(pe.get("oi") or pe.get("openInterest") or 0)
                ce_vol = int(ce.get("volume") or ce.get("vol") or 0)
                pe_vol = int(pe.get("volume") or pe.get("vol") or 0)

                sigma_c = ce_iv if ce_iv > 0 else typical_iv
                sigma_p = pe_iv if pe_iv > 0 else typical_iv

                g_ce = engine.black_scholes(spot, k, T, 0.065, sigma_c, "call") if spot and T > 0 else None
                g_pe = engine.black_scholes(spot, k, T, 0.065, sigma_p, "put")  if spot and T > 0 else None

                strikes_out.append({
                    "strike":    k,
                    "is_atm":    abs(k - atm) < step * 0.5,
                    "ce_ltp":    round(ce_ltp, 2),
                    "ce_iv":     round(ce_iv * 100, 1),
                    "ce_oi":     ce_oi,
                    "ce_vol":    ce_vol,
                    "ce_delta":  round(g_ce.delta, 3) if g_ce else 0,
                    "ce_theta":  round(g_ce.theta, 2) if g_ce else 0,
                    "ce_symbol": ce.get("symbol", ""),
                    "pe_ltp":    round(pe_ltp, 2),
                    "pe_iv":     round(pe_iv * 100, 1),
                    "pe_oi":     pe_oi,
                    "pe_vol":    pe_vol,
                    "pe_delta":  round(g_pe.delta, 3) if g_pe else 0,
                    "pe_theta":  round(g_pe.theta, 2) if g_pe else 0,
                    "pe_symbol": pe.get("symbol", ""),
                })

            strikes_out.sort(key=lambda x: x["strike"])

            return {
                "source":     "live_chain",
                "symbol":     full_sym,
                "short":      short,
                "spot":       spot,
                "atm":        atm,
                "dte":        dte,
                "expiry":     expiry_str,
                "expiries":   expiry_list,
                "strikes":    strikes_out,
                "fetched_at": now.isoformat(),
            }

        return self._bs_chain(full_sym, short, spot, step, typical_iv, meta)

    def _bs_chain(self, symbol: str, short: str, spot, step: int, iv: float, meta: dict = None) -> dict:
        """Generate Black-Scholes chain estimates when live chain is unavailable."""
        from analysis.options_engine import OptionsEngine
        engine = OptionsEngine()
        now    = datetime.now(tz=IST)
        if meta is None:
            meta = MCX_CONTRACTS.get(short, {})

        if not spot:
            return {
                "source":     "bs_estimate",
                "symbol":     symbol,
                "short":      short,
                "spot":       None,
                "atm":        None,
                "dte":        21,
                "expiry":     None,
                "expiries":   [],
                "strikes":    [],
                "note":       "No spot price — start bot during MCX hours (09:00–23:30 IST)",
                "fetched_at": now.isoformat(),
            }

        atm = _atm_strike(spot, step)
        T   = 21 / 365.0
        strikes_out = []

        for i in range(-5, 6):
            k    = atm + i * step
            g_ce = engine.black_scholes(spot, k, T, 0.065, iv, "call")
            g_pe = engine.black_scholes(spot, k, T, 0.065, iv, "put")
            strikes_out.append({
                "strike":    k,
                "is_atm":    i == 0,
                "ce_ltp":    g_ce.price,
                "ce_iv":     round(iv * 100, 1),
                "ce_oi":     0,
                "ce_vol":    0,
                "ce_delta":  g_ce.delta,
                "ce_theta":  g_ce.theta,
                "ce_symbol": "",
                "pe_ltp":    g_pe.price,
                "pe_iv":     round(iv * 100, 1),
                "pe_oi":     0,
                "pe_vol":    0,
                "pe_delta":  g_pe.delta,
                "pe_theta":  g_pe.theta,
                "pe_symbol": "",
            })

        return {
            "source":     "bs_estimate",
            "symbol":     symbol,
            "short":      short,
            "spot":       spot,
            "atm":        atm,
            "dte":        21,
            "expiry":     None,
            "expiries":   [],
            "strikes":    strikes_out,
            "note":       "Live chain unavailable — Black-Scholes estimates (DTE=21, typical IV)",
            "fetched_at": now.isoformat(),
        }


def _count_field(trades: list[dict], field: str) -> dict:
    c: dict = {}
    for t in trades:
        v = t.get(field, "unknown")
        c[v] = c.get(v, 0) + 1
    return c


# Module-level singleton
commodity_options = CommodityOptionsLearning()
