"""
strategy_config.py
──────────────────
Runtime-mutable strategy parameters.
The dashboard reads the schema and can update any param live without restarting.
Updating a value here pushes it into the strategy module's global namespace so
the next evaluate() call picks it up automatically.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_OVERRIDES_PATH = os.path.join("db", "config_overrides.json")

# Schema: strategy_key → { param_name → {value, type, min?, max?, description} }
# "enabled" is a special bool — applied to the strategy instance, not a module global.
STRATEGY_CONFIGS: dict = {

    "trend_follow": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable TrendFollow (LONG breakout)"},
        "BREAKOUT_LOOKBACK":  {"value": 20,    "type": "int",   "min": 5,   "max": 100, "description": "Bars for N-bar high breakout"},
        "MIN_RVOL":           {"value": 1.4,   "type": "float", "min": 0.5, "max": 5.0, "description": "Min relative volume"},
        "ATR_STOP_MULTIPLIER":{"value": 1.5,   "type": "float", "min": 0.3, "max": 3.0, "description": "ATR multiplier for stop (below entry)"},
        "TARGET_1_R":         {"value": 2.0,   "type": "float", "min": 0.5, "max": 6.0, "description": "Target 1 in multiples of risk"},
        "TARGET_2_R":         {"value": 3.0,   "type": "float", "min": 1.0, "max": 10.0,"description": "Target 2 in multiples of risk"},
        "MIN_ADX":            {"value": 20,    "type": "int",   "min": 10,  "max": 50,  "description": "Min ADX for trend strength"},
        "MAX_RSI_ENTRY":      {"value": 75,    "type": "int",   "min": 50,  "max": 90,  "description": "Max RSI — avoid chasing overbought"},
    },

    "short_trend": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable ShortTrend (SHORT breakdown)"},
        "BREAKDOWN_LOOKBACK": {"value": 20,    "type": "int",   "min": 5,   "max": 100, "description": "Bars for N-bar low breakdown"},
        "SWING_HIGH_LOOKBACK":{"value": 15,    "type": "int",   "min": 5,   "max": 50,  "description": "Bars for stop-loss swing high"},
        "MIN_RVOL":           {"value": 1.2,   "type": "float", "min": 0.5, "max": 5.0, "description": "Min relative volume"},
        "ATR_STOP_BUFFER":    {"value": 0.5,   "type": "float", "min": 0.1, "max": 2.0, "description": "ATR buffer above swing high for stop"},
        "TARGET_1_R":         {"value": 1.5,   "type": "float", "min": 0.5, "max": 6.0, "description": "Target 1 in multiples of risk"},
        "TARGET_2_R":         {"value": 2.5,   "type": "float", "min": 1.0, "max": 10.0,"description": "Target 2 in multiples of risk"},
        "MIN_ADX":            {"value": 20,    "type": "int",   "min": 10,  "max": 50,  "description": "Min ADX for trend strength"},
        "RSI_MIN":            {"value": 30,    "type": "int",   "min": 10,  "max": 60,  "description": "Min RSI (floor — not exhausted)"},
        "RSI_MAX":            {"value": 55,    "type": "int",   "min": 40,  "max": 80,  "description": "Max RSI (not bouncing)"},
    },

    "mean_reversion": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable MeanReversion (range bounce)"},
        "RSI_OVERSOLD":       {"value": 40,    "type": "int",   "min": 10,  "max": 50,  "description": "RSI threshold for LONG signal"},
        "RSI_OVERBOUGHT":     {"value": 60,    "type": "int",   "min": 50,  "max": 90,  "description": "RSI threshold for SHORT signal"},
        "BB_PROXIMITY_PCT":   {"value": 0.01,  "type": "float", "min": 0.001,"max": 0.05,"description": "Band proximity % (0.01 = 1%)"},
        "MIN_RVOL":           {"value": 1.0,   "type": "float", "min": 0.3, "max": 3.0, "description": "Min relative volume"},
        "ATR_STOP_BUFFER":    {"value": 1.0,   "type": "float", "min": 0.2, "max": 3.0, "description": "ATR buffer for stop loss"},
    },

    "institutional_momentum": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable InstitutionalMomentum (ATM options on conviction days)"},
        "CONVICTION_THRESHOLD":{"value": 7,   "type": "int",   "min": 5,   "max": 10,  "description": "Min conviction score to trade (5–10 scale)"},
        "MAX_FO_CAPITAL_PCT": {"value": 50,   "type": "int",   "min": 10,  "max": 80,  "description": "Max capital % for score 9-10"},
    },

    "directional_options": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable DirectionalOptions (ATM debit spread)"},
        "MAX_IV_RANK":        {"value": 55,    "type": "int",   "min": 20,  "max": 100, "description": "Max IV rank to buy options (avoid expensive)"},
        "MIN_DTE":            {"value": 7,     "type": "int",   "min": 1,   "max": 30,  "description": "Minimum days to expiry"},
        "MAX_DTE":            {"value": 21,    "type": "int",   "min": 5,   "max": 60,  "description": "Maximum days to expiry"},
    },

    "options_income": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable OptionsIncome (short strangle/premium collection)"},
    },

    "iron_condor": {
        "enabled":            {"value": False, "type": "bool",  "description": "Enable IronCondor (disabled — poor backtest on indices)"},
    },

    "gap_fade": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable GapFade (fade gaps at open 9:15–9:45 AM)"},
        "MIN_GAP_PCT":        {"value": 1.5,   "type": "float", "min": 0.5, "max": 5.0, "description": "Min gap % to trade"},
        "MAX_GAP_PCT":        {"value": 5.0,   "type": "float", "min": 2.0, "max": 15.0,"description": "Max gap % — beyond this gaps may continue"},
        "ENTRY_WINDOW_MIN":   {"value": 30,    "type": "int",   "min": 5,   "max": 60,  "description": "Minutes after open to allow entry"},
        "RSI_OVERBOUGHT":     {"value": 70,    "type": "int",   "min": 60,  "max": 90,  "description": "RSI threshold for gap-up short"},
        "RSI_OVERSOLD":       {"value": 30,    "type": "int",   "min": 10,  "max": 40,  "description": "RSI threshold for gap-down long"},
        "TARGET_1_R":         {"value": 1.5,   "type": "float", "min": 0.5, "max": 5.0, "description": "Target 1 in multiples of risk"},
        "TARGET_2_R":         {"value": 2.5,   "type": "float", "min": 1.0, "max": 8.0, "description": "Target 2 in multiples of risk"},
    },

    "momentum_reversal": {
        "enabled":            {"value": True,  "type": "bool",  "description": "Enable MomentumReversal (extreme RSI fade)"},
        "RSI_OVERBOUGHT":     {"value": 82,    "type": "int",   "min": 70,  "max": 95,  "description": "RSI level for SHORT reversal signal"},
        "RSI_OVERSOLD":       {"value": 18,    "type": "int",   "min": 5,   "max": 30,  "description": "RSI level for LONG reversal signal"},
        "MAX_ADX":            {"value": 25,    "type": "int",   "min": 10,  "max": 50,  "description": "ADX must be BELOW this (not a strong trend)"},
        "ATR_STOP_BUFFER":    {"value": 0.5,   "type": "float", "min": 0.1, "max": 2.0, "description": "ATR buffer for stop loss"},
        "TARGET_1_R":         {"value": 1.5,   "type": "float", "min": 0.5, "max": 5.0, "description": "Target 1 in multiples of risk"},
        "TARGET_2_R":         {"value": 2.5,   "type": "float", "min": 1.0, "max": 8.0, "description": "Target 2 in multiples of risk"},
        "LOOKBACK_BARS":      {"value": 10,    "type": "int",   "min": 3,   "max": 50,  "description": "Bars to confirm RSI extremity"},
    },
}

# Map strategy key → strategy module path
_MODULE_MAP = {
    "trend_follow":           "strategies.trend_follow",
    "short_trend":            "strategies.short_trend",
    "mean_reversion":         "strategies.mean_reversion",
    "institutional_momentum": "strategies.institutional_momentum",
    "directional_options":    "strategies.directional_options",
    "options_income":         "strategies.options_income",
    "iron_condor":            "strategies.iron_condor",
    "gap_fade":               "strategies.gap_fade",
    "momentum_reversal":      "strategies.momentum_reversal",
}

# Map strategy key → attribute name on strategy_selector
_SELECTOR_ATTR = {
    "trend_follow":           "_trend",
    "short_trend":            "_short_trend",
    "mean_reversion":         "_reversion",
    "institutional_momentum": "_institutional",
    "directional_options":    "_opt_direct",
    "options_income":         "_opt_income",
    "iron_condor":            "_iron_condor",
    "gap_fade":               "_gap_fade",
    "momentum_reversal":      "_momentum_rev",
}


def _load_overrides() -> None:
    """Load persisted overrides from db/config_overrides.json and apply to STRATEGY_CONFIGS.
    Called once at module import time. Values are applied in-memory only here;
    call reapply_all_overrides() after strategy modules are imported to push to module globals.
    """
    if not os.path.exists(_OVERRIDES_PATH):
        return
    try:
        with open(_OVERRIDES_PATH, "r", encoding="utf-8") as f:
            overrides: dict = json.load(f)
        count = 0
        for strategy, params in overrides.items():
            if strategy not in STRATEGY_CONFIGS:
                continue
            for param, value in params.items():
                if param not in STRATEGY_CONFIGS[strategy]:
                    continue
                STRATEGY_CONFIGS[strategy][param]["value"] = value
                _apply_to_module(strategy, param, value)   # no-op if module not yet in sys.modules
                count += 1
        if count:
            logger.info(f"[StrategyConfig] Loaded {count} override(s) from {_OVERRIDES_PATH}")
    except Exception as e:
        logger.warning(f"[StrategyConfig] Could not load overrides from {_OVERRIDES_PATH}: {e}")


def _save_overrides() -> None:
    """Persist all current param values to db/config_overrides.json."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_OVERRIDES_PATH)), exist_ok=True)
        snapshot = {
            strat: {param: entry["value"] for param, entry in params.items()}
            for strat, params in STRATEGY_CONFIGS.items()
        }
        with open(_OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        logger.warning(f"[StrategyConfig] Could not save overrides to {_OVERRIDES_PATH}: {e}")


def _record_param_change(strategy: str, param: str, old_value: str, new_value: str,
                         reason: str = "manual") -> None:
    """Write one row to the param_changes audit table in trades.db."""
    try:
        import sqlite3
        from config.settings import DB_PATH
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ts = datetime.now(tz=ZoneInfo("Asia/Kolkata")).isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO param_changes (ts, strategy, param, old_value, new_value, reason) "
                "VALUES (?,?,?,?,?,?)",
                (ts, strategy, param, old_value, new_value, reason),
            )
    except Exception as e:
        logger.warning(f"[StrategyConfig] Could not write param_changes: {e}")


def reapply_all_overrides() -> None:
    """Re-push all current STRATEGY_CONFIGS values to strategy module globals.
    Call this once after all strategy modules have been imported (from StrategySelector.__init__).
    Needed because _load_overrides() runs at import time before strategy modules exist.
    """
    for strategy, params in STRATEGY_CONFIGS.items():
        for param, entry in params.items():
            _apply_to_module(strategy, param, entry["value"])


def get_schema() -> dict:
    """Return full schema with type/min/max/description for the UI."""
    return STRATEGY_CONFIGS


def get_all_values() -> dict:
    """Return flat {strategy: {param: value}} for quick API response."""
    return {
        strat: {param: entry["value"] for param, entry in params.items()}
        for strat, params in STRATEGY_CONFIGS.items()
    }


def update(strategy: str, param: str, value) -> tuple[bool, str]:
    """
    Update a strategy param at runtime.
    Returns (ok, error_message).
    Pushes the value into the strategy module global so the next cycle picks it up.
    """
    if strategy not in STRATEGY_CONFIGS:
        return False, f"Unknown strategy: {strategy}"
    if param not in STRATEGY_CONFIGS[strategy]:
        return False, f"Unknown param '{param}' for strategy '{strategy}'"

    entry = STRATEGY_CONFIGS[strategy][param]
    param_type = entry.get("type", "str")

    try:
        if param_type == "bool":
            if isinstance(value, str):
                value = value.lower() in ("true", "1", "yes")
            else:
                value = bool(value)
        elif param_type == "int":
            value = int(value)
        elif param_type == "float":
            value = float(value)
    except (ValueError, TypeError) as e:
        return False, f"Type error: {e}"

    if param_type in ("int", "float"):
        if "min" in entry and value < entry["min"]:
            return False, f"Value {value} below minimum {entry['min']}"
        if "max" in entry and value > entry["max"]:
            return False, f"Value {value} above maximum {entry['max']}"

    old_value = str(entry["value"])
    STRATEGY_CONFIGS[strategy][param]["value"] = value
    _apply_to_module(strategy, param, value)
    _save_overrides()
    _record_param_change(strategy, param, old_value, str(value))
    logger.info(f"[StrategyConfig] {strategy}.{param} = {value}")
    return True, ""


def _apply_to_module(strategy: str, param: str, value) -> None:
    """Push updated value into the strategy module's global namespace."""
    import sys

    if param == "enabled":
        _set_instance_enabled(strategy, value)
        return

    # Settings-level params (not module globals)
    if strategy == "institutional_momentum" and param == "CONVICTION_THRESHOLD":
        try:
            import config.settings as _s
            _s.CONVICTION_THRESHOLD = int(value)
        except Exception as e:
            logger.warning(f"[StrategyConfig] Could not update CONVICTION_THRESHOLD in settings: {e}")
        return

    if strategy == "institutional_momentum" and param == "MAX_FO_CAPITAL_PCT":
        try:
            import config.settings as _s
            _s.MAX_FO_CAPITAL_PCT = int(value)
        except Exception as e:
            logger.warning(f"[StrategyConfig] Could not update MAX_FO_CAPITAL_PCT in settings: {e}")
        return

    module_name = _MODULE_MAP.get(strategy)
    if not module_name:
        return
    mod = sys.modules.get(module_name)
    if mod is None:
        return
    if hasattr(mod, param):
        setattr(mod, param, value)
    else:
        logger.debug(f"[StrategyConfig] Module {module_name} has no global '{param}' — skipped")


def _set_instance_enabled(strategy: str, value: bool) -> None:
    """Enable/disable a strategy instance via strategy_selector."""
    try:
        from strategies.strategy_selector import strategy_selector
        attr = _SELECTOR_ATTR.get(strategy)
        if not attr:
            return
        inst = getattr(strategy_selector, attr, None)
        if inst is not None:
            inst.enabled = value
    except Exception as e:
        logger.warning(f"[StrategyConfig] Could not toggle {strategy}.enabled: {e}")


# Apply any persisted overrides immediately at import time.
# Strategy module globals are patched later by reapply_all_overrides()
# once all strategy modules are in sys.modules (called from StrategySelector.__init__).
_load_overrides()
