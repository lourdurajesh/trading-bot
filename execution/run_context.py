"""
run_context.py
──────────────
SINGLE source for "how does this book behave" — the small set of flags that make
LIVE, PAPER and LEARNING differ. The trading pipeline (signal → size → place →
exit → ledger) is ONE control flow; only this context varies, never duplicated
code (see docs/UNIFIED_EXECUTION_SPEC.md).

  field              LIVE      PAPER      LEARNING
  place_real_orders  True      False      False
  enforce_funds      True      True       False   (learning records every signal)
  strategy_set       curated   curated    ()=all
  risk_budget        TOTAL_CAPITAL × RISK_PER_TRADE_PCT/100   (same rule everywhere)

risk_budget is identical across modes on purpose: live sizes off TOTAL_CAPITAL
(risk_manager), so paper and learning must too — that is what makes paper predict
live and removes the old 1%-of-wallet divergence. The ₹5L learning wallet is for
P&L reporting only; it does NOT size trades.

Stage 2 consumes `risk_budget` (sizing). Stages 3+ consume the flags + strategy_set
(mode switching, fund gating, strategy enablement).
"""
from dataclasses import dataclass

from config.settings import TOTAL_CAPITAL, RISK_PER_TRADE_PCT, LIVE_STRATEGIES

LIVE     = "LIVE"
PAPER    = "PAPER"
LEARNING = "LEARNING"


def _live_strategy_set() -> tuple:
    """Strategies the LIVE/PAPER book trades. Priority:
      1. LIVE_STRATEGIES env (explicit operator override), if set;
      2. else the promotion gate — strategies whose stage == 'live' (settable from the
         dashboard). Default stage is 'live', so with no env and no demotions this is the
         whole catalog == today's 'all enabled strategies trade' behaviour (non-breaking).
    """
    env = tuple(s.strip() for s in (LIVE_STRATEGIES or "").split(",") if s.strip())
    if env:
        return env
    try:
        from config.strategy_toggles import live_stage_set
        return live_stage_set()
    except Exception:
        return ()


@dataclass(frozen=True)
class RunContext:
    mode: str
    place_real_orders: bool
    enforce_funds: bool
    strategy_set: tuple = ()          # () = all strategies (LEARNING)
    risk_pct: float = RISK_PER_TRADE_PCT
    risk_capital_base: float = TOTAL_CAPITAL

    @property
    def risk_budget(self) -> float:
        """₹ risked per trade — the input to execution.sizing (same rule for all books)."""
        return self.risk_capital_base * self.risk_pct / 100.0

    def trades_strategy(self, strategy: str) -> bool:
        """Whether this book trades a given strategy. Empty set = all (LEARNING)."""
        if not self.strategy_set:
            return True
        base = (strategy or "").replace("_LRN", "").replace("_LEARNING", "")
        return base in self.strategy_set


def live_context() -> RunContext:
    return RunContext(LIVE, place_real_orders=True, enforce_funds=True,
                      strategy_set=_live_strategy_set())


def paper_context() -> RunContext:
    return RunContext(PAPER, place_real_orders=False, enforce_funds=True,
                      strategy_set=_live_strategy_set())


def learning_context() -> RunContext:
    return RunContext(LEARNING, place_real_orders=False, enforce_funds=False,
                      strategy_set=())


def active_context() -> RunContext:
    """The active execution book (LIVE or PAPER) per settings.RUN_MODE — the SINGLE
    source for 'do we place real broker orders?'. LEARNING is parallel, not active."""
    from config.settings import RUN_MODE
    return paper_context() if RUN_MODE == PAPER else live_context()


def is_paper() -> bool:
    """True when the active book simulates fills (no broker call)."""
    return not active_context().place_real_orders


def is_live() -> bool:
    return active_context().place_real_orders


__all__ = ["RunContext", "live_context", "paper_context", "learning_context",
           "active_context", "is_paper", "is_live", "LIVE", "PAPER", "LEARNING"]
