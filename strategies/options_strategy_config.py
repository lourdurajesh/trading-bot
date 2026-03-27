"""
options_strategy_config.py
──────────────────────────
Per-strategy options configuration accessor.

Strategies call get_options_config(key) to get their parameters.
All values are sourced from settings.OPTIONS_STRATEGY_CONFIG, which
in turn reads from environment variables with safe defaults.

Usage:
    from strategies.options_strategy_config import get_options_config
    cfg = get_options_config("iron_condor")
    if not cfg["enabled"]:
        return None
    min_dte = cfg["min_dte"]
"""

from config.settings import OPTIONS_STRATEGY_CONFIG


def get_options_config(strategy_key: str) -> dict:
    """
    Return the config dict for a given options strategy key.

    Raises KeyError if the key is not in OPTIONS_STRATEGY_CONFIG —
    a missing key means a misconfigured strategy and should fail loudly.
    """
    try:
        return OPTIONS_STRATEGY_CONFIG[strategy_key]
    except KeyError:
        raise KeyError(
            f"[OptionsStrategyConfig] No config for '{strategy_key}'. "
            f"Add it to OPTIONS_STRATEGY_CONFIG in config/settings.py."
        )


def is_strategy_enabled(strategy_key: str) -> bool:
    """Returns False for unknown keys (safe default)."""
    return OPTIONS_STRATEGY_CONFIG.get(strategy_key, {}).get("enabled", False)
