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
from execution.evaluator import Evaluator  # shared loop engine (Phase 5)
from intelligence.intelligence_engine import intelligence_engine
from risk.portfolio_tracker import portfolio_tracker
from strategies.base_strategy import Signal
from strategies.trend_follow import TrendFollowStrategy
from strategies.short_trend import ShortTrendStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.options_income import OptionsIncomeStrategy
from strategies.directional_options import DirectionalOptionsStrategy
from strategies.institutional_momentum import InstitutionalMomentumStrategy
from strategies.gap_fade import GapFadeStrategy
from strategies.momentum_reversal import MomentumReversalStrategy

logger = logging.getLogger(__name__)


class _ProductionEntryEvaluator(Evaluator):
    """NSE production entry loop on the shared Evaluator (Phase 5 step 2b).

    Behaviour preserved from StrategySelector.run_cycle: ordered symbols; cooldown +
    open-position skips (with the six diagnostic counters); regime-routed single-strategy
    evaluate; conflict-direction block; is_valid gate; intelligence gate (60m cooldown on
    reject, audit-logged); analyst size adjust; submit (5m cooldown on a post-intelligence
    reject); cycle logging + signal-health feed. The base adds per-item error isolation."""

    def __init__(self, selector):
        super().__init__("StrategySelector")
        self._sel = selector

    def scope(self, now):
        # per-cycle diagnostics (reset here — scope() runs first each evaluate_once)
        self._c = {"cooldown": 0, "position": 0, "no_data": 0,
                   "regime": 0, "no_signal": 0, "invalid": 0}
        syms = self._sel._get_ordered_symbols()
        self._n_symbols = len(syms)
        return syms

    def skip(self, symbol, now):
        if self._sel._is_on_cooldown(symbol):
            self._c["cooldown"] += 1
            return True
        if portfolio_tracker.has_open_position(symbol):
            self._c["position"] += 1
            return True
        return False

    def evaluate(self, symbol, now):
        signal = self._sel._evaluate_symbol(symbol)
        if signal is not None and self._sel._is_conflicting_direction(symbol, signal.direction.value):
            logger.warning(
                f"[StrategySelector] CONFLICT BLOCKED: {signal.strategy} wants "
                f"{signal.direction.value} {symbol} but an opposite position is already open"
            )
            signal = None
        if signal is None:
            from data.data_store import store
            if not store.is_ready(symbol, "1H", min_candles=50):
                self._c["no_data"] += 1
            else:
                regime_result = regime_detector.get_regime(symbol, "1H")
                if regime_result.regime == Regime.UNKNOWN:
                    self._c["regime"] += 1
                else:
                    self._c["no_signal"] += 1
            return []
        if not signal.is_valid():
            logger.warning(
                f"[StrategySelector] {symbol} signal invalid before intelligence "
                f"(entry={signal.entry}, sl={signal.stop_loss}, t1={signal.target_1}) — skipping"
            )
            self._c["invalid"] += 1
            return []
        return [signal]

    def on_signal(self, signal, now):
        intel = intelligence_engine.evaluate(signal)
        if not intel.approved:
            logger.info(f"[StrategySelector] {signal.symbol} blocked by intelligence: {intel.summary[:100]}")
            try:
                from audit_log import audit_log
                audit_log.rejection(signal, reason=intel.summary[:300], layer="intelligence")
            except Exception:
                pass
            self._sel.apply_cooldown(signal.symbol, minutes=60)
            return False
        if intel.size_factor < 1.0:
            signal.position_size = int(signal.position_size * intel.size_factor)
            logger.info(f"[StrategySelector] {signal.symbol} size reduced to {intel.size_factor:.0%} by analyst")
        signal.reason = f"{signal.reason} | AI: {intel.verdict} ({intel.conviction:.1f}/10)"
        signal_id = order_manager.submit(signal)
        if signal_id:
            return True
        # Order rejected post-intelligence (risk/margin/profit check) — short cooldown.
        self._sel.apply_cooldown(signal.symbol, minutes=5)
        return False

    def after_cycle(self, acted, now):
        if acted:
            logger.info(
                f"[StrategySelector] Cycle {self._sel._cycle_count}: "
                f"{len(acted)} signal(s) submitted from {self._n_symbols} symbols."
            )
        else:
            c = self._c
            logger.info(
                f"[StrategySelector] Cycle {self._sel._cycle_count} — no signals | "
                f"{self._n_symbols} symbols: "
                f"no_data={c['no_data']} regime_unknown={c['regime']} "
                f"no_setup={c['no_signal']} cooldown={c['cooldown']} "
                f"open_pos={c['position']} invalid={c['invalid']}"
            )
        try:
            from analysis.signal_health import skip_collector, health_monitor
            skip_records = skip_collector.flush()
            health_monitor.update(skip_records, signals_fired=len(acted))
            if acted:
                health_monitor.record_trade()
        except Exception:
            pass


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
        self._short_trend  = ShortTrendStrategy()
        self._reversion    = MeanReversionStrategy()
        self._opt_income   = OptionsIncomeStrategy()
        self._opt_direct   = DirectionalOptionsStrategy()
        self._institutional  = InstitutionalMomentumStrategy()
        self._gap_fade       = GapFadeStrategy()
        self._momentum_rev   = MomentumReversalStrategy()
        self._entry_evaluator = None   # lazy — shared Evaluator loop (Phase 5 step 2b)

        # Re-push any persisted overrides now that all strategy modules are imported.
        # _load_overrides() ran at strategy_config import time (before these modules
        # existed in sys.modules), so the module-global setattr had no effect then.
        from config.strategy_config import reapply_all_overrides
        reapply_all_overrides()

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

        Phase 5 step 2b: the loop now lives in the shared `_ProductionEntryEvaluator`
        on the Evaluator base (one loop engine for every book). Behaviour preserved —
        ordered symbols, cooldown/open-position skips + diagnostics, regime routing,
        conflict block, is_valid gate, intelligence gate, submit, health feed.
        """
        self._cycle_count += 1
        if self._entry_evaluator is None:
            self._entry_evaluator = _ProductionEntryEvaluator(self)
        return self._entry_evaluator.evaluate_once()

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
                "short_trend":            self._short_trend.enabled,
                "mean_reversion":         self._reversion.enabled,
                "options_income":         self._opt_income.enabled,
                "directional_options":    self._opt_direct.enabled,
                "gap_fade":               self._gap_fade.enabled,
                "momentum_reversal":      self._momentum_rev.enabled,
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
        # all other strategies for BANKNIFTY, NIFTY, and FINNIFTY index symbols.
        if symbol in ("NSE:NIFTYBANK-INDEX", "NSE:NIFTY50-INDEX", "NSE:FINNIFTY-INDEX"):
            signal = self._try_strategy(self._institutional, symbol)
            if signal:
                return signal

        # Get regime
        regime_result = regime_detector.get_regime(symbol, "1H")
        regime = regime_result.regime

        if regime == Regime.UNKNOWN:
            return None

        # GapFade is time-gated (9:15–9:45 AM) — check it first across all regimes
        # so a gap setup is never missed because the regime routed elsewhere.
        signal = self._try_strategy(self._gap_fade, symbol)
        if signal:
            return signal

        # Route to strategy based on regime
        if regime in (Regime.TRENDING, Regime.BREAKOUT):
            # Try both directional equity strategies — TrendFollow (LONG) and
            # ShortTrend (SHORT). Each has its own EMA-stack gate so only one
            # will fire: if market is bullish → TrendFollow wins; if bearish →
            # ShortTrend wins. Whichever fires first is returned.
            signal = self._try_strategy(self._trend, symbol)
            if signal:
                return signal
            signal = self._try_strategy(self._short_trend, symbol)
            if signal:
                return signal
            # Directional options in parallel (single strategy, wrapped for consistency)
            return self._evaluate_options_parallel(symbol, [self._opt_direct])

        if regime == Regime.RANGING:
            # Mean reversion first (no network call — fast path)
            signal = self._try_strategy(self._reversion, symbol)
            if signal:
                return signal
            # MomentumReversal in ranging — catches extreme RSI setups
            signal = self._try_strategy(self._momentum_rev, symbol)
            if signal:
                return signal
            # IronCondor removed 2026-07-14: BS-repriced backtest (49 trades, 3 indices, 12mo)
            # was net-negative across all 9 param configs — 1:4 reward:risk needs ~80% WR,
            # got 57-73%. Indices breach the short strikes too often. See docs/ADR.md.
            return self._evaluate_options_parallel(symbol, [self._opt_income])

        if regime == Regime.VOLATILE:
            # MomentumReversal thrives in volatile markets (extreme RSI + mean revert)
            signal = self._try_strategy(self._momentum_rev, symbol)
            if signal:
                return signal
            # Directional debit spread (indices only) — options are cheap in volatile markets.
            signal = self._evaluate_options_parallel(symbol, [self._opt_direct])
            if signal:
                return signal
            # IronCondor removed — see RANGING comment above.
            return self._evaluate_options_parallel(symbol, [self._opt_income])

        return None

    def _try_strategy(self, strategy, symbol: str) -> Optional[Signal]:
        """Safely call a strategy's evaluate() method."""
        # Per-strategy on/off (single source: config/strategy_toggles, UI-controlled).
        from config.strategy_toggles import is_enabled
        if not is_enabled(getattr(strategy, "name", "")):
            return None
        try:
            signal = strategy.evaluate(symbol)
            if signal and signal.confidence >= MIN_SIGNAL_CONFIDENCE:
                signal.hold_type = getattr(strategy, "hold_type", "intraday")
                return signal
        except Exception as e:
            logger.error(f"[StrategySelector] Strategy {strategy.name} error on {symbol}: {e}")
        return None

    def _is_conflicting_direction(self, symbol: str, direction: str) -> bool:
        """
        Returns True if an open position already exists for this symbol in the
        OPPOSITE direction — prevents two strategies from holding both sides of
        the same stock simultaneously with real capital.
        """
        pos = portfolio_tracker.get_position(symbol)
        if pos is None:
            return False
        opposite = "SHORT" if direction == "LONG" else "LONG"
        return pos.direction == opposite

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
