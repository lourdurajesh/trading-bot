"""
strategy_selector.py
────────────────────
Orchestrates which strategy runs on which symbol.
Routes each symbol to the correct strategy based on current market regime,
enforces cooldowns, and manages strategy allocation caps.

Called by main.py on every evaluation cycle.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_SIGNAL_CONFIDENCE, SYMBOL_COOLDOWN_MINUTES
from config.watchlist import ALL_NSE_SYMBOLS, ALL_US_SYMBOLS, PRIORITY_SYMBOLS
from execution.order_manager import order_manager
from intelligence.intelligence_engine import intelligence_engine
from risk.portfolio_tracker import portfolio_tracker
from strategies.base_strategy import Signal
from strategies.trend_follow import TrendFollowStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.options_income import OptionsIncomeStrategy
from strategies.directional_options import DirectionalOptionsStrategy

logger = logging.getLogger(__name__)


class StrategySelector:
    """
    Evaluates all watchlist symbols and submits valid signals to OrderManager.

    Usage:
        selector = StrategySelector()
        selector.run_cycle()     # call every N minutes from main.py
    """

    def __init__(self):
        # Instantiate all strategy modules
        self._trend      = TrendFollowStrategy()
        self._reversion  = MeanReversionStrategy()
        self._opt_income = OptionsIncomeStrategy()
        self._opt_direct = DirectionalOptionsStrategy()

        # Cooldown tracker: symbol → datetime when cooldown expires
        self._cooldowns: dict[str, datetime] = {}

        # Cycle counter for logging
        self._cycle_count = 0

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def run_cycle(self) -> list[Signal]:
        """
        Main evaluation loop. Runs all symbols through their assigned strategy.
        Returns list of signals submitted this cycle.
        """
        self._cycle_count += 1
        signals_submitted = []

        # Prioritise high-liquidity symbols first
        symbols = self._get_ordered_symbols()

        for symbol in symbols:
            signal = self._evaluate_symbol(symbol)
            if signal:
                # Run intelligence layer before submission
                intel = intelligence_engine.evaluate(signal)
                if not intel.approved:
                    logger.info(f"[StrategySelector] {symbol} blocked by intelligence: {intel.summary[:100]}")
                    continue
                # Adjust position size if analyst says reduce
                if intel.size_factor < 1.0:
                    signal.position_size = int(signal.position_size * intel.size_factor)
                    logger.info(f"[StrategySelector] {symbol} size reduced to {intel.size_factor:.0%} by analyst")
                # Attach intelligence summary to signal reason
                signal.reason = f"{signal.reason} | AI: {intel.verdict} ({intel.conviction:.1f}/10)"
                signal_id = order_manager.submit(signal)
                if signal_id:
                    signals_submitted.append(signal)

        if signals_submitted:
            logger.info(
                f"[StrategySelector] Cycle {self._cycle_count}: "
                f"{len(signals_submitted)} signal(s) submitted from {len(symbols)} symbols."
            )
        else:
            logger.debug(
                f"[StrategySelector] Cycle {self._cycle_count}: "
                f"No signals from {len(symbols)} symbols."
            )

        return signals_submitted

    def apply_cooldown(self, symbol: str, minutes: int = None) -> None:
        """
        Apply cooldown to a symbol after a losing trade.
        Called by portfolio_tracker on loss close.
        """
        duration = minutes or SYMBOL_COOLDOWN_MINUTES
        self._cooldowns[symbol] = datetime.now(tz=timezone.utc) + timedelta(minutes=duration)
        logger.info(f"[StrategySelector] Cooldown applied to {symbol} for {duration} minutes.")

    def get_status(self) -> dict:
        """Returns selector status for dashboard."""
        return {
            "cycle_count":        self._cycle_count,
            "strategies_enabled": {
                "trend_follow":    self._trend.enabled,
                "mean_reversion":  self._reversion.enabled,
            },
            "symbols_on_cooldown": len([
                s for s, exp in self._cooldowns.items()
                if exp > datetime.now(tz=timezone.utc)
            ]),
        }

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _evaluate_symbol(self, symbol: str) -> Optional[Signal]:
        """
        Run the appropriate strategy for a single symbol.
        Returns Signal if a valid setup is found, else None.
        """
        # Skip if on cooldown
        if self._is_on_cooldown(symbol):
            return None

        # Skip if already have open position
        if portfolio_tracker.has_open_position(symbol):
            return None

        # Get regime
        regime_result = regime_detector.get_regime(symbol, "1H")
        regime = regime_result.regime

        if regime == Regime.UNKNOWN:
            return None

        # Route to strategy based on regime
        if regime in (Regime.TRENDING, Regime.BREAKOUT):
            # Try equity trend first, then directional options
            return (
                self._try_strategy(self._trend, symbol)
                or self._try_strategy(self._opt_direct, symbol)
            )

        if regime == Regime.RANGING:
            # Try mean reversion first, then options income
            return (
                self._try_strategy(self._reversion, symbol)
                or self._try_strategy(self._opt_income, symbol)
            )

        if regime == Regime.VOLATILE:
            # Only directional options in volatile market
            return self._try_strategy(self._opt_direct, symbol)

        return None

    def _try_strategy(self, strategy, symbol: str) -> Optional[Signal]:
        """Safely call a strategy's evaluate() method."""
        try:
            signal = strategy.evaluate(symbol)
            if signal and signal.confidence >= MIN_SIGNAL_CONFIDENCE:
                return signal
        except Exception as e:
            logger.error(f"[StrategySelector] Strategy {strategy.name} error on {symbol}: {e}")
        return None

    def _is_on_cooldown(self, symbol: str) -> bool:
        """Check if a symbol is currently in cooldown period."""
        expiry = self._cooldowns.get(symbol)
        if expiry and datetime.now(tz=timezone.utc) < expiry:
            return True
        # Clean up expired cooldowns
        if expiry:
            del self._cooldowns[symbol]
        return False

    def _get_ordered_symbols(self) -> list[str]:
        """
        Returns full symbol list ordered by priority.
        Priority symbols are evaluated first each cycle.
        """
        priority = [s for s in PRIORITY_SYMBOLS]
        rest = [
            s for s in (ALL_NSE_SYMBOLS + ALL_US_SYMBOLS)
            if s not in priority
        ]
        return priority + rest


# ── Module-level singleton ────────────────────────────────────────
strategy_selector = StrategySelector()
