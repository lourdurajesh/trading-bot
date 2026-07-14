"""
strategy_toggles.py
───────────────────
SINGLE source for per-strategy enable/disable. One switch per strategy governs
whether it RUNS in ANY book (live, paper, learning, MCX) — set from the dashboard,
persisted in DB (strategy_settings), cached in-process and refreshed on write.

Used at every signal-generation gate (strategy_selector, learning_engine,
commodity_options) via is_enabled(name); the dashboard reads all_states() and
writes set_enabled(name, on). Name matching is suffix-tolerant ('SimpleRSI_LRN'
toggles with 'SimpleRSI').
"""
import logging
import sqlite3

from config.settings import DB_PATH

logger = logging.getLogger(__name__)

# Catalog of toggleable strategies → segment label (EQUITY | OPTIONS | MCX).
CATALOG = {
    "TrendFollow":           "EQUITY",
    "ShortTrend":            "EQUITY",
    "MeanReversion":         "EQUITY",
    "InstitutionalMomentum": "EQUITY",
    "GapFade":               "EQUITY",
    "MomentumReversal":      "EQUITY",
    "SimpleRSI":             "EQUITY",
    "SimpleMomentum":        "EQUITY",
    "DirectionalOptions":    "OPTIONS",
    "OptionsIncome":         "OPTIONS",
    "Reversal3m":            "OPTIONS",
    "Reversal5m":            "OPTIONS",
    "TrendSpread":           "MCX",
    "BreakoutSpread":        "MCX",
    "RSIReversalSpread":     "MCX",
}

_cache: dict | None = None


def _base(name: str) -> str:
    n = (name or "").strip()
    for suf in ("_LRN", "_LEARNING"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS strategy_settings "
                 "(name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)")


def _states() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    d = {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure(conn)
            for name, en in conn.execute("SELECT name, enabled FROM strategy_settings"):
                d[name] = bool(en)
    except Exception as e:
        logger.warning(f"[strategy_toggles] load failed: {e}")
    _cache = d
    return d


def is_enabled(name: str, default: bool = True) -> bool:
    """Whether a strategy is enabled (unknown/unset → default True)."""
    return _states().get(_base(name), default)


def set_enabled(name: str, on: bool) -> None:
    base = _base(name)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure(conn)
            conn.execute("INSERT OR REPLACE INTO strategy_settings (name, enabled) VALUES (?,?)",
                         (base, 1 if on else 0))
        _states()[base] = bool(on)
        logger.info(f"[strategy_toggles] {base} → {'ENABLED' if on else 'DISABLED'}")
    except Exception as e:
        logger.error(f"[strategy_toggles] set {base} failed: {e}")


def all_states() -> list[dict]:
    """Catalog with current enabled flags — for the dashboard."""
    st = _states()
    return [{"name": n, "segment": seg, "enabled": st.get(n, True)}
            for n, seg in CATALOG.items()]


__all__ = ["is_enabled", "set_enabled", "all_states", "CATALOG"]
