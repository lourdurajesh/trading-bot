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
    "IronCondor":            "OPTIONS",
    "Reversal3m":            "OPTIONS",
    "Reversal5m":            "OPTIONS",
    "TrendSpread":           "MCX",
    "BreakoutSpread":        "MCX",
    "RSIReversalSpread":     "MCX",
}

# Promotion stages — the human approval gate (Spec principle 6). A strategy is promoted
# candidate → forward_test → live. Only `live`-stage strategies trade the LIVE/PAPER book
# (the real money / its paper twin); candidate/forward_test run ONLY in the forward-test
# harness. Default 'live' preserves today's behaviour (every enabled strategy trades the book).
STAGES = ("candidate", "forward_test", "live")
DEFAULT_STAGE = "live"

_cache: dict | None = None
_stage_cache: dict | None = None


def _base(name: str) -> str:
    n = (name or "").strip()
    for suf in ("_LRN", "_LEARNING"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n


def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS strategy_settings "
                 "(name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, "
                 f" stage TEXT DEFAULT '{DEFAULT_STAGE}')")
    # Safe migration for pre-existing tables (added 'stage' in slice 7).
    try:
        conn.execute(f"ALTER TABLE strategy_settings ADD COLUMN stage TEXT DEFAULT '{DEFAULT_STAGE}'")
    except Exception:
        pass


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


def _stages() -> dict:
    global _stage_cache
    if _stage_cache is not None:
        return _stage_cache
    d = {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure(conn)
            for name, stg in conn.execute("SELECT name, stage FROM strategy_settings"):
                d[name] = stg or DEFAULT_STAGE
    except Exception as e:
        logger.warning(f"[strategy_toggles] stage load failed: {e}")
    _stage_cache = d
    return d


def is_enabled(name: str, default: bool = True) -> bool:
    """Whether a strategy is enabled (unknown/unset → default True)."""
    return _states().get(_base(name), default)


def set_enabled(name: str, on: bool) -> None:
    base = _base(name)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure(conn)
            # Upsert only `enabled` so a stage set earlier is preserved (no clobber).
            conn.execute("INSERT INTO strategy_settings (name, enabled) VALUES (?,?) "
                         "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled",
                         (base, 1 if on else 0))
        _states()[base] = bool(on)
        logger.info(f"[strategy_toggles] {base} → {'ENABLED' if on else 'DISABLED'}")
    except Exception as e:
        logger.error(f"[strategy_toggles] set {base} failed: {e}")


def stage(name: str, default: str = DEFAULT_STAGE) -> str:
    """Promotion stage for a strategy (candidate | forward_test | live)."""
    return _stages().get(_base(name), default)


def set_stage(name: str, stage_value: str) -> None:
    base = _base(name)
    if stage_value not in STAGES:
        raise ValueError(f"invalid stage '{stage_value}' — expected {STAGES}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure(conn)
            # Upsert only `stage` so the enabled flag is preserved (no clobber).
            conn.execute("INSERT INTO strategy_settings (name, stage) VALUES (?,?) "
                         "ON CONFLICT(name) DO UPDATE SET stage=excluded.stage",
                         (base, stage_value))
        _stages()[base] = stage_value
        logger.info(f"[strategy_toggles] {base} stage → {stage_value}")
    except Exception as e:
        logger.error(f"[strategy_toggles] set_stage {base} failed: {e}")


def live_stage_set() -> tuple:
    """Catalog strategies whose promotion stage == 'live' — the live/paper book's strategy
    set (the human approval gate). Default stage is 'live', so by default this is the whole
    catalog (== today's 'all trade' behaviour)."""
    st = _stages()
    return tuple(n for n in CATALOG if st.get(n, DEFAULT_STAGE) == "live")


def all_states() -> list[dict]:
    """Catalog with current enabled flags + promotion stage — for the dashboard."""
    en = _states()
    sg = _stages()
    return [{"name": n, "segment": seg, "enabled": en.get(n, True),
             "stage": sg.get(n, DEFAULT_STAGE)}
            for n, seg in CATALOG.items()]


__all__ = ["is_enabled", "set_enabled", "stage", "set_stage", "live_stage_set",
           "all_states", "CATALOG", "STAGES"]
