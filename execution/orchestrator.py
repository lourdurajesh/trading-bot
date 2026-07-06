"""
orchestrator.py
───────────────
ONE control flow over every trading segment (Phase-V V4). Replaces main.py's four
separate run_cycle calls (strategy_selector / learning_engine / commodity_options /
us_reversal) with a single orchestrator that owns the cycle, the per-segment
scheduling gate, and error isolation.

Each segment is a SegmentAdapter. Segment specifics (chain fetch, strike/spread
selection, recording store) stay inside the adapter — which today delegates to the
existing engine — so behavior is preserved while the top-level pipeline is unified.
Shared concerns (sizing, fees, exit policy, strategy on/off) already live in their
single sources and are reached through the engines the adapters call.

main.py drives two ticks:
  run_fast_monitors()  — every loop (~1s): exits for each segment that self-gates.
  run_generation(now)  — every EVAL_INTERVAL_SECONDS (60s): entries; each adapter
                         decides via should_generate() whether its session is open.
"""
import logging

logger = logging.getLogger(__name__)


class SegmentAdapter:
    """One trading segment. Override the hooks; keep segment logic in the engine."""
    name = "segment"

    def should_generate(self, now) -> bool:
        return True

    def generate(self) -> None:
        """Scan + open entries for this segment (delegates to the engine)."""

    def fast_monitor(self) -> None:
        """Fast exit/position check (delegates to the engine). Optional."""


class TradingOrchestrator:
    def __init__(self):
        self._adapters: list[SegmentAdapter] = []

    def register(self, adapter: SegmentAdapter) -> None:
        self._adapters.append(adapter)
        logger.info(f"[Orchestrator] registered segment: {adapter.name}")

    @property
    def segments(self) -> list[str]:
        return [a.name for a in self._adapters]

    def run_generation(self, now) -> None:
        for a in self._adapters:
            try:
                if a.should_generate(now):
                    a.generate()
            except Exception as e:
                logger.exception(f"[Orchestrator] {a.name} generation error: {e}")

    def run_fast_monitors(self) -> None:
        for a in self._adapters:
            try:
                a.fast_monitor()
            except Exception as e:
                logger.debug(f"[Orchestrator] {a.name} monitor error: {e}")


# ── Segment adapters (delegate to existing engines) ────────────────────────────

class NSEMainAdapter(SegmentAdapter):
    """Production NSE book (equity + index options) via strategy_selector +
    position_manager. Trades only during NSE market hours."""
    name = "NSE-main"

    def __init__(self, is_nse_open):
        self._is_open = is_nse_open

    def should_generate(self, now) -> bool:
        return bool(self._is_open())

    def generate(self) -> None:
        from strategies.strategy_selector import strategy_selector
        strategy_selector.run_cycle()

    def fast_monitor(self) -> None:
        if self._is_open():
            from execution.position_manager import position_manager
            position_manager.check_all()


class NSELearningAdapter(SegmentAdapter):
    """NSE learning lab (all strategies, paper). Since slice 6c, exits run through
    its own PositionManager instance (the ONE shared exit engine, per-book) --
    same fast-monitor cadence as the production book, not just once per 60s
    generation tick."""
    name = "NSE-learning"

    def __init__(self, is_nse_open):
        self._is_open = is_nse_open

    def should_generate(self, now) -> bool:
        return bool(self._is_open())

    def generate(self) -> None:
        from learning_engine import learning_engine
        learning_engine.run_cycle()

    def fast_monitor(self) -> None:
        if self._is_open():
            from learning_engine import learning_engine
            learning_engine._nse_position_manager.check_all()


class MCXAdapter(SegmentAdapter):
    """MCX commodity options. Session-hours gate is internal to the engine, so it is
    always invoked; entries on the 60s tick, exits on the fast tick."""
    name = "MCX"

    def generate(self) -> None:
        from commodity_options_learning import commodity_options
        commodity_options.run_cycle()

    def fast_monitor(self) -> None:
        from commodity_options_learning import commodity_options
        commodity_options.check_exits()


class USAdapter(SegmentAdapter):
    """US index-ETF Reversal (SPY/QQQ), paper. US-session gate is internal; exits run
    inside its run_cycle."""
    name = "US"

    def generate(self) -> None:
        from us_reversal import us_reversal
        us_reversal.run_cycle()


def build_orchestrator(is_nse_open) -> TradingOrchestrator:
    """Construct the orchestrator with every segment registered (the one pipeline)."""
    o = TradingOrchestrator()
    o.register(NSEMainAdapter(is_nse_open))
    o.register(NSELearningAdapter(is_nse_open))
    o.register(MCXAdapter())
    o.register(USAdapter())
    return o


__all__ = ["TradingOrchestrator", "SegmentAdapter", "build_orchestrator"]
