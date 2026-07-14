"""
evaluator.py — the ONE loop engine (TECH_SPEC §4 / ADR-001).

Both Entry and Exit are the same core job: iterate a *scope* of items, *evaluate* rules
against each, and route any resulting signals to execution. They differ only in four hooks
(`scope`, `skip`, `evaluate`, `on_signal`); the base owns the loop, per-item error isolation,
and cycle bookkeeping. This is the abstraction the four `run_cycle`s collapse into,
parameterised by RunContext + book (not by copied loops).

Phase 5 step 1: this base is introduced first, wired to nothing. Book entry loops are folded
onto it one at a time (steps 2+), each verified against its existing behaviour.
"""
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


class Evaluator:
    """Base loop. Subclasses fill in the four hooks; the loop and error isolation are shared.

    Contract:
      scope(now)          -> iterable of items to evaluate (symbols, positions, (symbol,strats)…)
      skip(item, now)     -> True to skip this item (cooldown, already-open, …)
      evaluate(item, now) -> iterable of Signals/actions (0+; supports 1-per-item AND bake-off)
      on_signal(sig, now) -> True if the signal was acted on (submitted/executed)
      after_cycle(acted, now) -> optional per-cycle bookkeeping (diagnostics, health)
    """

    def __init__(self, name: str):
        self.name = name
        self._cycle_count = 0

    def evaluate_once(self, now: Any = None) -> list:
        """One pass over the scope. Called by the orchestrator at the book's cadence.
        Per-item and per-signal work is isolated so one bad symbol never aborts the cycle."""
        self._cycle_count += 1
        acted: list = []
        for item in self.scope(now):
            try:
                if self.skip(item, now):
                    continue
                for signal in (self.evaluate(item, now) or ()):
                    if signal is None:
                        continue
                    try:
                        if self.on_signal(signal, now):
                            acted.append(signal)
                    except Exception as e:
                        logger.error(f"[{self.name}] on_signal error ({item}): {e}")
            except Exception as e:
                logger.error(f"[{self.name}] evaluate error ({item}): {e}")
        try:
            self.after_cycle(acted, now)
        except Exception as e:
            logger.debug(f"[{self.name}] after_cycle error: {e}")
        return acted

    # ── hooks (subclasses override) ──────────────────────────────
    def scope(self, now: Any) -> Iterable:
        raise NotImplementedError

    def skip(self, item: Any, now: Any) -> bool:
        return False

    def evaluate(self, item: Any, now: Any) -> Optional[Iterable]:
        raise NotImplementedError

    def on_signal(self, signal: Any, now: Any) -> bool:
        raise NotImplementedError

    def after_cycle(self, acted: list, now: Any) -> None:
        return None
