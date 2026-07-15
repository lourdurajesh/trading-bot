# Unification Task Plan

> Ordered low→high risk, each independently shippable and **verified locally first** (no prod
> deploy until the whole set runs clean locally). Target model: `docs/TECH_SPEC.md`. Current state:
> `docs/FILE_INDEX.md`. Rule for every task: one change, verify, then next — no batching.

## Phase 0 — Cleanup (safe deletions, no behaviour change)
| # | Task | Files | Verify (local) | Risk |
|---|---|---|---|---|
| 0a | Delete dead `iron_condor` + `options_strategy_config` (instantiated, never routed) | `strategies/iron_condor.py`, `strategies/options_strategy_config.py`, `strategy_selector.py` | bot imports & boots; `test_pipeline` green | very low |
| 0b | Delete stale one-off scripts + `scripts/_archive/` | `scripts/{migrate_unified_ledger,repair_paper_wallet,resize_open_learning_unified,parity_check}.py`, `scripts/_archive/` | no live imports break (grep) | very low |
| 0c | Single strategy on/off source — remove `enabled` flags from `strategy_config`, keep `strategy_toggles` | `config/strategy_config.py`, `strategy_selector.py` | toggling a strategy still works | low |

## Phase 1 — Extract the `Evaluator` base (structural foundation)
| # | Task | Files | Verify (local) | Risk | Status |
|---|---|---|---|---|---|
| 1a | Define `Evaluator` base (loop + `_execute` tail + hooks) | `execution/evaluator.py` (new) | unit test the base with a fake scope/rules | medium | ✅ done (`3faaba4`) |
| 1b | Reframe `PositionManager` as `ExitEvaluator(Evaluator)` — no behaviour change for NSE | `execution/position_manager.py` | `test_pipeline` + `test_learning_pipeline` green; NSE exits identical | medium | ✅ done (`3df380f`, live) |

## Phase 2 — Unify exits onto `ExitEvaluator`
| # | Task | Files | Verify (local) | Risk | Status |
|---|---|---|---|---|---|
| 2a | **US exits → `ExitEvaluator`** (warm-up: small, already reuses `exit_rules`) | `us_reversal.py`, `execution/position_manager.py`, `+ tests/test_us_exit.py` | new US-exit test; replay a US trade matches | medium | ⏭️ deferred (user: skip US) |
| 2b | **MCX exit *logic*** — profit-lock floor (§8.2) + shared `structural_exit` reversal rule | `commodity_options_learning.py`, `execution/exit_signals.py` | **replay COPPER — must NOT round-trip to a loss** (verified: −₹2,397 → ≈+₹1,794) | high | ✅ logic done (`252c77c` + `9bbf79a`, live) |
| 2c | **MCX exit *loop* → `ExitEvaluator`** — fold `_check_exits` onto the shared base (instrument-based stop §8.1) | `commodity_options_learning.py`, `execution/position_manager.py`, `+ tests/test_mcx_exit.py` | new MCX-exit test; behaviour parity | high | ⏳ pending (rules now shared; only the loop is still MCX-local) |

## Phase 3 — Unify risk & sizing
| # | Task | Files | Verify (local) | Risk |
|---|---|---|---|---|
| 3 | MCX/US size + gate via `RiskManager`/`sizing`; delete `daily_risk_budget` | `risk/`, MCX/US engines | `test_sizing` regression; funds respected per RunContext | medium |

## Phase 4 — Unify order routing
| # | Task | Files | Verify (local) | Risk |
|---|---|---|---|---|
| 4 | Route `order_manager` through `order_router`; one placement path (only `_execute` calls broker) | `execution/order_manager.py`, `order_router.py` | `test_pipeline`; paper fill works; one broker call site (grep) | medium |

## Phase 5 — Extract `Evaluator`, fold entry loops onto it
| # | Task | Files | Verify (local) | Risk | Status |
|---|---|---|---|---|---|
| 1 | `Evaluator` base (shared loop + hooks + error isolation) + unit test | `execution/evaluator.py`, `tests/test_evaluator.py` | unit test ALL PASS | med | ✅ done (`3faaba4`) |
| 2a | Fold NSE **learning** entry loop → `_LearningEntryEvaluator(Evaluator)` | `learning_engine.py` | learning pipeline test + scope smoke-check | med | ✅ done (`3faaba4`) |
| 2b | Fold NSE **production** loop → `_ProductionEntryEvaluator(Evaluator)` | `strategies/strategy_selector.py` | `test_pipeline` + identical diagnostic output | high | ✅ done (`5345b32`) |
| 2c | Fold **MCX** loop → `_MCXEntryEvaluator(Evaluator)` | `commodity_options_learning.py` | scope + run_cycle smoke-check (DB present) | high | ✅ done (`18004a5`) |
| 2d | Fold **US** loop | `us_reversal.py` | — | med | ⏭️ deferred (user: skip US) |
| 5c | Orchestrator schedules the entry Evaluators per segment | `execution/orchestrator.py` | main→orchestrator→run_cycle→EntryEvaluator | — | ✅ done (orchestrator already unified; entry folds complete the path via the `run_cycle` seam) |

**Entry-side complete:** 3 of 4 entry loops (NSE prod, NSE learning, MCX) run on the one `Evaluator`
base, scheduled by the one orchestrator. `run_cycle` is retained as the per-engine seam (owns
`_cycle_count`/diagnostics, used by tests) — not "retired", but now a 3-line delegator.

**Exit-side status (2026-07-15):**
- **NSE — done & live.** `PositionManager` now *is* `ExitEvaluator(Evaluator)` (`3df380f`); production +
  learning books exit through the one shared engine on the fast tick.
- **MCX — logic done & live, loop still local.** The two exit *behaviours* that were missing — the
  **profit-lock floor** (`252c77c`) and the shared **`structural_exit` reversal rule** (`9bbf79a`) — now
  run inside MCX `_check_exits`, so MCX has the same reversal/lock protection NSE has (this is what
  permanently kills the COPPER round-trip class). What remains (2c) is purely structural: fold MCX's
  `_check_exits` *loop* onto the `ExitEvaluator` base so all three segments share one loop, not just one
  rule-set. Lower urgency now that the money-losing behaviour gap is closed.
- **US — deferred** (user: skip US) for both the entry fold (2d) and exit fold (2a).

Target end-state unchanged: the orchestrator schedules `EntryEvaluator` (slow) + `ExitEvaluator`
(fast) per segment. Entry side reached this; exit side reached it for NSE, shares rules for MCX.

## Phase 6 — Retire the parallel machinery
| # | Task | Files | Verify (local) | Risk |
|---|---|---|---|---|
| 6a | Repoint Paper tab + `daily_review` to ledger; remove PM `close_order` remnant; **delete `paper_trading.py`** | `paper_trading.py`, `position_manager.py`, `daily_review.py`, `dashboard_api.py` | Paper tab matches ledger | medium |
| 6b | Retire `/commodity\|learning\|us\|paper` trade/stat clones → `/book` + `/ledger?segment=`; split the 2497-line `dashboard_api.py` | `api/dashboard_api.py`, `dashboard/index.html` | UI renders only from unified feeds | medium |
| 6c | **Split the API into its own process** (out of `main.py`'s daemon thread) — bot runs headless (ADR-010) | `main.py`, `api/dashboard_api.py`, `deploy/` (new service unit) | bot trades with the API stopped; UI still reads ledger / writes config; no shared process | low |

## Phase 7 — Config tidy
| # | Task | Files | Verify (local) | Risk |
|---|---|---|---|---|
| 7 | Make `RUN_MODE` primary (retire `PAPER_TRADING` derivation); reconcile `regime_engine`/`regime_detector` | `config/settings.py`, `run_context.py`, `analysis/regime_engine.py` | mode switch works; no logic dup | low |

## Phase 8 — Close the test gap
| # | Task | Files | Verify (local) | Risk |
|---|---|---|---|---|
| 8 | Coverage: MCX entry+exit, US entry+exit, profit-lock, dashboard feed parity; fix stale `options_risk_gate` fixture | `tests/` | full suite green | low |

---

## Sequencing logic
- **Phase 0** is pure deletion — immediate, no behaviour change, shrinks the surface.
- **Phase 1** builds the `Evaluator` base and proves it by reframing the *existing* `PositionManager`
  with zero NSE behaviour change — the safe foundation.
- **Phase 2** is the high-value payoff: US first (low-risk pattern proof), then **MCX — which
  permanently kills the COPPER class** via `ExitEvaluator` + profit-lock.
- **Phases 3–5** collapse the remaining divergence (risk, orders, then the entry loops into
  `EntryEvaluator`), landing the 7-loops → 2-loops target.
- **Phase 6–7** delete the parallel machinery the migration makes redundant.
- **Phase 8** locks it so this never regresses.

## Open decisions (need your call before starting)
1. **US vs MCX first in Phase 2?** Plan does US first as a low-risk warm-up; MCX is the one actively
   losing money. Swap if you want the COPPER fix sooner (accepting the higher-risk migration first).
2. **MCX-live scope** — stays PAPER/LEARNING through this migration (spec §5), or bring MCX into the
   LIVE book as part of Phase 5?
3. **Stopgap?** Want the targeted MCX profit-lock applied *now* (small, within the existing
   `_check_exits`) as a bridge until Phase 2b, or wait and fix it properly in the migration?
