"""
exit_policy.py
──────────────
SINGLE source for strategy-aware exit STYLE selection. Maps a strategy name to
how its winners should be managed, from config/strategy_exits.json (no hard-coded
classification in code). Used by PositionManager (the ONE exit engine every book —
live, paper, and learning since slice 6c — runs through) so Equity/Options/MCX
all classify exits identically.

Styles:
  trend_trail           — Chandelier trail + 1R partial book + run to T2 (let it run).
  mean_reversion_target — hard FULL exit at the target (the reversion snaps back).
  convex_trail          — wide trail + EOD, no fixed target (option buying; fat tails).
"""
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "config", "strategy_exits.json")

TREND_TRAIL    = "trend_trail"
MEAN_REVERSION = "mean_reversion_target"
CONVEX_TRAIL   = "convex_trail"
_VALID = {TREND_TRAIL, MEAN_REVERSION, CONVEX_TRAIL}


def _load() -> dict:
    try:
        with open(_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


_MAP = _load()
_DEFAULT = _MAP.get("_default", TREND_TRAIL)


def _base_name(strategy: str) -> str:
    """Strip the learning suffix so 'SimpleRSI_LRN' maps like 'SimpleRSI'."""
    s = (strategy or "").strip()
    for suf in ("_LRN", "_LEARNING"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def exit_style(strategy: str) -> str:
    """Return the exit style for a strategy (config-driven, suffix-tolerant)."""
    style = _MAP.get(_base_name(strategy), _DEFAULT)
    return style if style in _VALID else _DEFAULT


__all__ = ["exit_style", "TREND_TRAIL", "MEAN_REVERSION", "CONVEX_TRAIL"]
