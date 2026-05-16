"""
strategy_matrix.py
──────────────────
Spec TASK 11: Strategy Enablement Matrix.

Maps market regime → list of allowed strategy names.
This separates macro bias (what the market IS doing) from entry logic
(what signal triggered the trade).

Feature flag: STRATEGY_MATRIX_ENABLED (default True).
When disabled, all strategies are allowed regardless of regime.

Allows runtime override via environment variable STRATEGY_MATRIX_CONFIG
(JSON string) or by editing this file and restarting.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

ENABLED = os.getenv("STRATEGY_MATRIX_ENABLED", "true").lower() != "false"

# ── Default regime → strategy enablement matrix ───────────────────
# Strategy names must match exactly what each strategy reports as self.name
# or the "strategy" field in signals/trades.
DEFAULT_MATRIX: dict[str, list[str]] = {
    "TREND_UP": [
        "TrendSpread",          # MCX: follow the trend with call spreads
        "BreakoutSpread",       # MCX: ride breakouts in trending markets
        "InstitutionalMomentum",# NSE: momentum in institutional flow direction
        "SimpleMomentum",       # NSE learning: EMA crossover
    ],
    "TREND_DOWN": [
        "TrendSpread",          # MCX: follow the trend with put spreads
        "BreakoutSpread",       # MCX: ride breakdown moves
        "InstitutionalMomentum",# NSE: short momentum
        "SimpleMomentum",
    ],
    "BREAKOUT": [
        "BreakoutSpread",       # Primary — breakout is the signal
        "TrendSpread",          # Can ride the new trend leg
    ],
    "CHOPPY": [
        # Directional strategies disabled — only premium-capture styles
        # (currently no iron condor / credit spread implemented)
        # Add "IronCondor" here when implemented
    ],
    "MEAN_REVERSION": [
        "RSIReversalSpread",    # MCX: reversal spread on stretched moves
        "MeanReversion",        # NSE: mean reversion equity strategy
        "SimpleRSI",            # NSE learning: RSI reversal
    ],
    "HIGH_VOLATILITY": [
        # Widen stops, reduce size — only high-conviction setups
        "TrendSpread",          # allowed but with reduced lots (handled in risk engine)
        "BreakoutSpread",
    ],
    "LOW_VOLATILITY": [
        "RSIReversalSpread",    # IV low = debit spreads cheaper — good for reversals
        "MeanReversion",
        "SimpleRSI",
        "SimpleMomentum",
    ],
    "UNKNOWN": [
        # Unknown regime = no data yet; allow all learning strategies but not live
        "SimpleRSI",
        "SimpleMomentum",
    ],
}


class StrategyMatrix:
    """
    Checks whether a strategy is allowed under the current market regime.

    Usage:
        from config.strategy_matrix import strategy_matrix
        from analysis.regime_engine import MarketRegime

        allowed = strategy_matrix.get_allowed(MarketRegime.TREND_UP)
        # → ["TrendSpread", "BreakoutSpread", ...]

        if strategy_matrix.is_allowed("TrendSpread", MarketRegime.CHOPPY):
            # False — TrendSpread disabled in choppy markets
    """

    def __init__(self):
        self._matrix = self._load_matrix()

    def _load_matrix(self) -> dict[str, list[str]]:
        """Load matrix from env var override, or use default."""
        env_config = os.getenv("STRATEGY_MATRIX_CONFIG", "")
        if env_config:
            try:
                loaded = json.loads(env_config)
                logger.info("[StrategyMatrix] Loaded custom matrix from STRATEGY_MATRIX_CONFIG")
                return loaded
            except Exception as e:
                logger.warning(f"[StrategyMatrix] Failed to parse env config: {e} — using default")
        return DEFAULT_MATRIX

    def get_allowed(self, regime) -> list[str]:
        """
        Returns list of allowed strategy names for a given regime.
        `regime` can be a MarketRegime enum or its string value.
        """
        if not ENABLED:
            return list({s for strategies in self._matrix.values() for s in strategies})

        regime_key = regime.value if hasattr(regime, "value") else str(regime)
        allowed = self._matrix.get(regime_key, [])
        if not allowed:
            logger.info(f"[StrategyMatrix] No strategies allowed in regime: {regime_key}")
        return allowed

    def is_allowed(self, strategy_name: str, regime) -> bool:
        """True if strategy_name is enabled for the given regime."""
        if not ENABLED:
            return True
        return strategy_name in self.get_allowed(regime)

    def log_disable(self, strategy_name: str, regime) -> None:
        """Log that a strategy was skipped due to regime filter."""
        regime_key = regime.value if hasattr(regime, "value") else str(regime)
        logger.info(
            f"[StrategyMatrix] {strategy_name} DISABLED — "
            f"regime {regime_key} does not allow this strategy"
        )


# Singleton
strategy_matrix = StrategyMatrix()
