"""
strategy_selector.py
────────────────────
Orchestrates which strategy runs on which symbol.
Routes each symbol to the correct strategy based on current market regime,
enforces cooldowns, and manages strategy allocation caps.

Called by main.py on every evaluation cycle.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from analysis.regime_detector import Regime, regime_detector
from config.settings import MIN_SIGNAL_CONFIDENCE, SYMBOL_COOLDOWN_MINUTES
from execution.order_manager import order_manager
from intelligence.intelligence_engine import intelligence_engine
from risk.portfolio_tracker import portfolio_tracker
from strategies.base_strategy import Signal
from strategies.trend_follow import TrendFollowStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.options_income import OptionsIncomeStrategy
from strategies.directional_options import DirectionalOptionsStrategy
from strategies.iron_condor import IronCondorStrategy
from strategies.institutional_momentum import InstitutionalMomentumStrategy

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
        self._trend        = TrendFollowStrategy()
        self._reversion    = MeanReversionStrategy()
        self._opt_income   = OptionsIncomeStrategy()
        self._opt_direct   = DirectionalOptionsStrategy()
        self._iron_condor  = IronCondorStrategy()
        self._institutional = InstitutionalMomentumStrategy()

        # Thread pool for parallel options strategy evaluation.
        # Options strategies block on Fyers chain API calls — running them
        # concurrently cuts wall-clock time from N×latency to ~1×latency
        # because OptionsExecutor's chain cache is shared across threads.
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="options_eval"
        )

        # Cooldown tracker: symbol → datetime when cooldown expires
        # Loaded from DB on startup so restarts don't lose cooldown state.
        self._cooldowns: dict[str, datetime] = {}
        self._init_cooldown_db()
        self._load_cooldowns()

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

        # Diagnostic counters — logged at INFO so dry runs are visible
        skipped_cooldown  = 0
        skipped_position  = 0
        skipped_no_data   = 0
        skipped_regime    = 0
        skipped_no_signal = 0
        skipped_invalid   = 0

        # Prioritise high-liquidity symbols first
        symbols = self._get_ordered_symbols()

        for symbol in symbols:
            if self._is_on_cooldown(symbol):
                skipped_cooldown += 1
                continue
            if portfolio_tracker.has_open_position(symbol):
                skipped_position += 1
                continue

            signal = self._evaluate_symbol(symbol)
            if signal is None:
                # Distinguish no-data from no-setup by checking data availability
                from data.data_store import store
                if not store.is_ready(symbol, "1H", min_candles=50):
                    skipped_no_data += 1
                else:
                    regime_result = regime_detector.get_regime(symbol, "1H")
                    from analysis.regime_detector import Regime
                    if regime_result.regime == Regime.UNKNOWN:
                        skipped_regime += 1
                    else:
                        skipped_no_signal += 1
                continue

            # Gate: reject structurally invalid signals before the expensive
            # intelligence layer (news scraper, macro, Claude analyst).
            if not signal.is_valid():
                logger.warning(
                    f"[StrategySelector] {symbol} signal invalid before intelligence "
                    f"(entry={signal.entry}, sl={signal.stop_loss}, t1={signal.target_1}) — skipping"
                )
                skipped_invalid += 1
                continue

            # Run intelligence layer before submission
            intel = intelligence_engine.evaluate(signal)
            if not intel.approved:
                logger.info(f"[StrategySelector] {symbol} blocked by intelligence: {intel.summary[:100]}")
                try:
                    from audit_log import audit_log
                    audit_log.rejection(signal, reason=intel.summary[:300], layer="intelligence")
                except Exception:
                    pass
                # Apply cooldown so we don't re-evaluate every 60 seconds
                self.apply_cooldown(symbol, minutes=60)
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
            else:
                # Order rejected post-intelligence (risk/margin/profit check failed).
                # Apply a short cooldown to avoid hammering the same symbol next cycle.
                self.apply_cooldown(symbol, minutes=5)

        if signals_submitted:
            logger.info(
                f"[StrategySelector] Cycle {self._cycle_count}: "
                f"{len(signals_submitted)} signal(s) submitted from {len(symbols)} symbols."
            )
        else:
            logger.info(
                f"[StrategySelector] Cycle {self._cycle_count} — no signals | "
                f"{len(symbols)} symbols: "
                f"no_data={skipped_no_data} regime_unknown={skipped_regime} "
                f"no_setup={skipped_no_signal} cooldown={skipped_cooldown} "
                f"open_pos={skipped_position} invalid={skipped_invalid}"
            )

        # Feed health monitor — records skip reasons + drought tracking
        try:
            from analysis.signal_health import skip_collector, health_monitor
            skip_records = skip_collector.flush()
            health_monitor.update(skip_records, signals_fired=len(signals_submitted))
            if signals_submitted:
                health_monitor.record_trade()
        except Exception:
            pass

        return signals_submitted

    def apply_cooldown(self, symbol: str, minutes: int = None) -> None:
        """
        Apply cooldown to a symbol after any trade exit.
        Persisted to DB so bot restarts don't clear the cooldown.
        """
        duration   = minutes or SYMBOL_COOLDOWN_MINUTES
        expires_at = datetime.now(tz=IST) + timedelta(minutes=duration)
        self._cooldowns[symbol] = expires_at
        self._persist_cooldown(symbol, expires_at)
        logger.info(f"[StrategySelector] Cooldown applied to {symbol} for {duration} min (until {expires_at.strftime('%H:%M')}).")

    # ─────────────────────────────────────────────────────────────
    # COOLDOWN PERSISTENCE
    # ─────────────────────────────────────────────────────────────

    def _init_cooldown_db(self) -> None:
        try:
            import sqlite3, os
            from config.settings import DB_PATH
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS symbol_cooldowns (
                        symbol     TEXT PRIMARY KEY,
                        expires_at TEXT
                    )
                """)
        except Exception as e:
            logger.warning(f"[StrategySelector] Cooldown DB init failed: {e}")

    def _load_cooldowns(self) -> None:
        """Load non-expired cooldowns from DB on startup."""
        try:
            import sqlite3
            from config.settings import DB_PATH
            now_str = datetime.now(tz=IST).isoformat()
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT symbol, expires_at FROM symbol_cooldowns WHERE expires_at > ?",
                    (now_str,)
                ).fetchall()
            for symbol, expires_str in rows:
                try:
                    expires_at = datetime.fromisoformat(expires_str)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=IST)
                    self._cooldowns[symbol] = expires_at
                except Exception:
                    pass
            if self._cooldowns:
                logger.info(f"[StrategySelector] Restored {len(self._cooldowns)} cooldown(s) from DB: {list(self._cooldowns.keys())}")
        except Exception as e:
            logger.warning(f"[StrategySelector] Could not load cooldowns from DB: {e}")

    def _persist_cooldown(self, symbol: str, expires_at: datetime) -> None:
        try:
            import sqlite3
            from config.settings import DB_PATH
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO symbol_cooldowns (symbol, expires_at) VALUES (?, ?)",
                    (symbol, expires_at.isoformat()),
                )
        except Exception as e:
            logger.warning(f"[StrategySelector] Could not persist cooldown for {symbol}: {e}")

    def get_status(self) -> dict:
        """Returns selector status for dashboard."""
        return {
            "cycle_count":        self._cycle_count,
            "strategies_enabled": {
                "institutional_momentum": self._institutional.enabled,
                "trend_follow":           self._trend.enabled,
                "mean_reversion":         self._reversion.enabled,
                "options_income":         self._opt_income.enabled,
                "directional_options":    self._opt_direct.enabled,
                "iron_condor":            self._iron_condor.enabled,
            },
            "symbols_on_cooldown": len([
                s for s, exp in self._cooldowns.items()
                if exp > datetime.now(tz=IST)
            ]),
        }

    def __del__(self):
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _evaluate_options_parallel(
        self,
        symbol:     str,
        strategies: list,
    ) -> Optional[Signal]:
        """
        Evaluate multiple options strategies for a symbol concurrently.

        Each strategy's evaluate() may block on a Fyers API chain fetch.
        Running them in parallel reduces wall-clock time from N×latency
        to ~1×latency because OptionsExecutor's chain cache is shared.

        Returns the highest-confidence signal found, or None.
        Times out after 10 seconds to avoid blocking the main loop.
        """
        futures = {
            self._executor.submit(self._try_strategy, strat, symbol): strat
            for strat in strategies
        }

        best: Optional[Signal] = None
        for future in as_completed(futures, timeout=10):
            try:
                sig = future.result()
                if sig is not None:
                    if best is None or sig.confidence > best.confidence:
                        best = sig
            except Exception as e:
                strat = futures[future]
                logger.error(
                    f"[StrategySelector] Parallel eval error "
                    f"({strat.name}/{symbol}): {e}"
                )
        return best

    def _evaluate_symbol(self, symbol: str) -> Optional[Signal]:
        """
        Run the appropriate strategy for a single symbol.
        Options strategies are evaluated in parallel to reduce chain-fetch latency.
        Returns Signal if a valid setup is found, else None.
        """
        # Skip if on cooldown
        if self._is_on_cooldown(symbol):
            return None

        # Skip if already have open position
        if portfolio_tracker.has_open_position(symbol):
            return None

        # ── INSTITUTIONAL override — highest priority ─────────────
        # Check conviction_scorer before regime routing.
        # On high-conviction days (score >= 7), institutional_momentum overrides
        # all other strategies for BANKNIFTY and NIFTY index symbols.
        if symbol in ("NSE:NIFTYBANK-INDEX", "NSE:NIFTY50-INDEX"):
            signal = self._try_strategy(self._institutional, symbol)
            if signal:
                return signal

        # Get regime
        regime_result = regime_detector.get_regime(symbol, "1H")
        regime = regime_result.regime

        if regime == Regime.UNKNOWN:
            return None

        # Route to strategy based on regime
        if regime in (Regime.TRENDING, Regime.BREAKOUT):
            # Equity trend first (no network call — fast path)
            signal = self._try_strategy(self._trend, symbol)
            if signal:
                return signal
            # Directional options in parallel (single strategy, wrapped for consistency)
            return self._evaluate_options_parallel(symbol, [self._opt_direct])

        if regime == Regime.RANGING:
            # Mean reversion first (no network call — fast path)
            signal = self._try_strategy(self._reversion, symbol)
            if signal:
                return signal
            # IronCondor disabled: 3-year backtest shows win rate of 1% on index regimes —
            # indices almost never stay in a tight ±2% range for a full 30-day cycle.
            return self._evaluate_options_parallel(symbol, [self._opt_income])

        if regime == Regime.VOLATILE:
            # Directional debit spread (indices only) — options are cheap in volatile markets.
            signal = self._evaluate_options_parallel(symbol, [self._opt_direct])
            if signal:
                return signal
            # IronCondor disabled — see RANGING comment above.
            return self._evaluate_options_parallel(symbol, [self._opt_income])

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
        if expiry and datetime.now(tz=IST) < expiry:
            return True
        # Clean up expired cooldowns
        if expiry:
            del self._cooldowns[symbol]
        return False

    def _get_ordered_symbols(self) -> list[str]:
        """
        Returns full symbol list ordered by priority.
        Priority symbols are evaluated first each cycle.
        Reads ALL_NSE_SYMBOLS dynamically so dynamic watchlist updates are reflected.
        """
        import config.watchlist as _wl
        priority = list(_wl.PRIORITY_SYMBOLS)
        rest = [
            s for s in (_wl.ALL_NSE_SYMBOLS + _wl.ALL_US_SYMBOLS)
            if s not in priority
        ]
        return priority + rest


# ── Module-level singleton ────────────────────────────────────────
strategy_selector = StrategySelector()
