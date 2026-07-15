# C4 — Evaluator (Execution Core)

[← back to TECH_SPEC](../TECH_SPEC.md) · Layer: Execution · Status: 🔶 half-migrated

## Purpose
The one loop engine. Iterates a **scope** of instruments, **evaluates** rules against live data, and
when a signal fires, **executes** an order and records it. Entry and Exit are two configurations of
this one core — not two engines. See [ADR-001](../ADR.md).

## Children
| Child | Role | Scope | Rules | Action | Cadence |
|---|---|---|---|---|---|
| `Evaluator` (base) | loop + execute→ledger tail + error isolation + diagnostics | — | — | — | — |
| `EntryEvaluator` | find setups across the universe | universe − open − cooldown | strategies (C1) | OPEN (after C2 risk/size) | slow (per candle) |
| `ExitEvaluator` | manage held positions | open positions | exit policy of each position | CLOSE (partial/full) | fast (per tick) |

The four hooks that differ: `scope()`, `skip()`, `evaluate()`, `on_signal()`. Everything else is shared.

## Interfaces
```
Evaluator.evaluate_once(now)        # called by C10 Orchestrator at a cadence
   for item in self.scope():        # hook
       if self.skip(item): continue # hook
       sig = self.evaluate(item)    # hook
       if sig: self.on_signal(sig)  # hook
Evaluator._execute(action)          # SHARED: order_router.place → ledger.record  (+ logging)
```
Cadence is owned by the Orchestrator (§C10), not the Evaluator — that lets one base run fast (exit)
and slow (entry). State (trail/MAE/MFE/partial flags) lives in the subclass, never the base.

## Owning modules
| | Current | Target |
|---|---|---|
| Exit loop | `execution/position_manager.py` (1112) — NSE only; `commodity_options_learning._check_exits`; `us_reversal` inline | `execution/evaluator.py::ExitEvaluator` (one, all segments) |
| Entry loop | `strategy_selector.run_cycle`, `learning_engine.run_cycle`, `commodity_options_learning.run_cycle`, `us_reversal.run_cycle` (4 copies) | `execution/evaluator.py::EntryEvaluator` (one) |
| Exit rule primitives | `execution/exit_rules.py`, `exit_signals.py`, `exit_policy.py` ✅ already shared | keep |

## Dependencies
- **Uses:** C1 strategies (evaluate), C2 RiskManager/sizing (entry on_signal), C3 order_router
  (execute), C5 ledger (record), C7 instrument adapter, C9 data_store, C6 RunContext.
- **Used by:** C10 Orchestrator (schedules `evaluate_once`).

## Exit behaviour — non-negotiable rules (baked in, see [ADR-002/003/004/007](../ADR.md))
1. **Mark the instrument you hold, not the underlying.** Spot-based stops on option spreads are a
   defect (COPPER: +₹4,000 → −₹1,934 at a breakeven-spot stop). Exits/P&L mark `monitor_symbol`.
2. **Profit-lock is mandatory.** Past a configured favourable move, the stop guarantees a positive
   exit; a winner may never round-trip to a loss.
3. **Structural-exit arming is volatility-relative** (ATR), and any change ships only after
   backtest + shadow-log on real ticks.
4. **One R formula:** `realised_pnl / (qty × |entry − original_stop|)`, computed once at close.

## Exit policy (per position, from `exit_policy.py` + `strategy_exits.json`)
Supported exit types the ExitEvaluator applies: **SL · Target · Partial · Trailing · Profit-Lock ·
Structural · Time · EOD · DTE (options) · Manual.** Each strategy attaches a policy at entry; the
policy is stored on the position and looked up at exit (exit does not re-select a strategy).

## Current state → target
- **Today:** ExitEvaluator exists in embryo as `PositionManager` (NSE prod+learning only). MCX and US
  run private exit loops. Four separate entry loops. 7 loops total.
- **Target:** 1 base + `EntryEvaluator` + `ExitEvaluator`; MCX/US differences via SegmentAdapter;
  modes via RunContext. **2 loops total.**

## Related tasks
[UNIFICATION_TASKS](../UNIFICATION_TASKS.md): Phase 1 (extract base, reframe PositionManager), Phase 2
(US then MCX exits — MCX kills COPPER), Phase 5 (EntryEvaluator, retire the 4 run_cycles).

## Open items
- US-first vs MCX-first in Phase 2 (pending decision).
- Whether a `SCALING` position state is needed for adds (currently partial-exit stays `OPEN` + flag).
