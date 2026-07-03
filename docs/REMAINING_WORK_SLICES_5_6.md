# Remaining unification work — Slices 5 & 6 (executable spec)

> Context-loss insurance. Slices 0–4 are DEPLOYED to live-paper (2026-06-25); slice 7
> (promotion gate) is built on branch `slice7-promotion-gate`. This doc specifies the two
> remaining slices precisely enough to execute in a focused session **with a live paper
> session available** (needed for behavioural parity). Plan: `~/.claude/plans/mossy-sauteeing-deer.md`.
> Guardrail: consult the `trading-architecture` skill first. Gate: `scripts/parity_check.py`.

## What is ALREADY shared (do NOT rebuild — verified against code)
- Sizing (`execution/sizing.py`), ledger (`execution/ledger.py`, DB-path injectable), order
  placement (`execution/order_router.py` + Fyers/Alpaca adapters = the Execution Engine),
  exit *decisions* (`exit_policy`/`exit_signals`/`exit_rules`), chain (`analysis/options_chain.chain_service`),
  costs (`cost_model`/`fees`), `reversal_core`. MCX/US/learning engines already import these.
- The duplication that remains is the **orchestration loops + engine-specific exit wrappers**,
  not the segment-specific adapter code (spread construction, BS estimation, MCX session
  calendar, instrument CRUD are legitimate InstrumentAdapter code — keep them).

---

## Slice 5 — fold MCX + US into the portfolio engine

**Goal:** MCX and US strategies emit `Signal`s consumed by the ONE pipeline
(Strategy → RiskSizer → OrderRouter → Ledger → PositionManager) instead of their own
`run_cycle`/`_open_trade`/`_check_exits`. US stays in the portfolio engine (user's call),
even though it's paper-only.

**Steps (each its own branch, parity-verified vs main on a LIVE session):**
1. **Spot-stop parameter in `PositionManager`.** MCX exits on the underlying SPOT
   (`STOP_SPOT` via `exit_rules.underlying_exit`), not the option premium. Add a stop-mode
   to `_check_options_position`: `options_meta["stop_mode"] = "spot" | "premium"` (default
   premium). When `spot`, use `exit_rules.underlying_exit`/`spot_breached` on the underlying
   instead of `premium_exit`. The structural-exit path (slice 2) already runs on the
   underlying — reuse it. *Files:* `execution/position_manager.py`.
2. **Spread construction as an adapter.** Move `commodity_options._build_trade` (ATM/OTM
   strike pick, net-debit, spread legs) into an MCX adapter function that returns a `Signal`
   with `options_meta` carrying the legs. The unified `order_manager._execute_options` already
   places multi-leg via `order_router`. *Files:* new `strategies/mcx/adapter.py` (or keep in
   `commodity_options`, expose a `build_signal()`), `execution/order_manager.py`.
3. **Route MCX through the orchestrator.** `execution/orchestrator.MCXAdapter.generate()`
   currently calls `commodity_options.run_cycle()`. Change it to: for each enabled MCX
   instrument/strategy, get a `Signal` (step 2) → `order_manager.submit(signal, ctx)` with an
   MCX `RunContext`. Retire `commodity_options.run_cycle`/`_evaluate`/`_open_trade`/`check_exits`/
   `_check_exits`. Keep instrument CRUD, session calendar (`config/mcx_calendar`), `_bs_chain`,
   `get_full_chain` (MCX adapter/display). *Files:* `execution/orchestrator.py`, `commodity_options_learning.py`.
4. **MCX session gating** stays via `config/mcx_calendar` — the MCXAdapter calls it in
   `should_generate(now)` (the orchestrator already has that hook).
5. **US** the same way via `USAdapter`: `us_reversal._maybe_enter` → a `Signal`; exits via
   `PositionManager` (underlying %-trail = `exit_rules.underlying_exit`, already used). Retire
   `us_reversal.run_cycle`/`_close`. Keep BS premium modelling as the US adapter.
6. **MCX `position_size`/funds** already use shared `sizing.lots_to_fit` + `_get_available_funds`;
   route through `risk_manager` (options path) so the one gate + exposure cap apply.

**Verify:** on a LIVE MCX session, `parity_check.py` before/after must show the SAME MCX
trades (entry/exit/strike/premium/reason) via the one pipeline. MCX is paper → reversible.

---

## Slice 6 — run LEARNING entry+exit through the ONE engine (firm directive — see memory `one-engine-never-compromise`)

> **Non-negotiable:** learning must use the SAME engine (entry AND exit) as paper/live — never a
> parallel/patched `learning_engine`. Sub-steps below; **6a is DONE**.

**6a — DONE (branch `slice6-learning-one-engine`, verified):** `risk/portfolio_tracker.py` is now
keyed by **trade-id** (`_open_positions[pos.id]`), so one book can hold several positions per
symbol (the bake-off). Symbol methods (`get_position`/`close_position`/mutations/`has_open_position`)
return the FIRST match → live book byte-identical; added `get_position_by_id()`. Verified: two
positions on one symbol coexist + isolate; live single-position unchanged.

**6b — route `PositionManager` exits by trade-id (NEXT — the delicate one).** ~62 sites in
`execution/position_manager.py` key exit state (`_trailing_stops`/`_partial_exited`/
`_breakeven_applied`/`_dynamic_target_r`) and call tracker mutations by **symbol**. Refactor:
in `_check_position`/`_check_options_position` derive `key = pos.id`; change the helpers
(`_move_stop_to_breakeven`, `_update_trailing_stop`, `_update_dynamic_target`,
`_reconstruct_state_from_position`, `reset_symbol`, `_exit_position`, `_partial_exit`,
`_exit_options_position`, `_update_broker_sl`) to take the position (use `pos.id` for state +
`get_position_by_id`, `pos.symbol` for orders/logging); add id-based tracker mutations
(`update_stop_loss`/`update_position_size`/`mark_t1_hit` are PM-only → make them id-based).
**This is deployed exit code — do it as ONE careful pass, gated by `parity_check.py` on the live
paper book (must stay byte-identical) + a unit test with two positions on one symbol.** Also
skip the duplicate-symbol reject in `risk_manager` when the context is LEARNING.

**6c — the learning runtime.** New same-repo entrypoint that instantiates its own
`PortfolioTracker` + `PositionManager(book=LEARNING, store, tracker)` (both already per-book
instantiable) + a LEARNING `RunContext` (no funds, all strategies, multi-position), runs every
strategy through `risk_manager` → `order_manager` (record-only) → the shared `PositionManager`
exits. **Delete `learning_engine`'s own `_check_exits` (+ helpers) and entry loop.**

### (original detail retained below)
## Slice 6 (orig) — extract the online forward-test harness (separate DB) + trade-id re-key

**6a — Separate learning DB (the user's original explicit request).**
- The forward-test/learning books (NSE-learning `nse` segment + US `us` segment) move to
  `db/learning.db` so they never sit in the live `trades.db`. MCX stays in `trades.db` (it
  becomes a real `live`-adjacent segment in slice 5).
- Use the slice-0a `Ledger(db_path)`: in `learning_engine.py` and `us_reversal.py`, replace
  module-level `ledger.record/update_fields/init` with an instance
  `learning_ledger = Ledger(LEARNING_DB_PATH)` (`LEARNING_DB_PATH = os.path.join(dirname(DB_PATH),
  "learning.db")`, env-overridable). Reads (`get_trades`) point at `learning.db`.
- **Dashboard reads:** `api/segment_readers.py` must read `nse`/`us` segments from
  `learning.db` and `mcx`/`live` from `trades.db`. Add a per-segment DB map (or a
  `Ledger(db).get_rows(segment)` call keyed by segment). `_collect_book_trades` likewise.
- **Migration:** `scripts/migrate_learning_db.py` — copy `nse` + `us` segment rows from
  `trades.db` ledger into `learning.db` (via `Ledger`), verify byte-identical, then delete
  those segments from `trades.db`. Backup first.
- **Verify:** `/book/trades` + `/ledger/*?segment=nse|us` identical pre/post; `trades.db` no
  longer holds nse/us rows; `learning.db` holds them.

**6b — Trade-id re-key of `PositionManager` (so the harness can use the ONE exit engine).**
- Today exit state (`_trailing_stops`/`_partial_exited`/`_breakeven_applied`/`_dynamic_target_r`)
  is keyed by `symbol`. The harness holds MANY positions per symbol (one per strategy), so
  re-key by `pos.id` (trade id). For the portfolio book (one position/symbol) id↔symbol is
  1:1 → behaviour identical.
- Thread the position id into the helpers: `_check_position`/`_check_options_position` already
  fetch `pos` (has `.id` and `.symbol`); pass `pos` (or `pos.id`) to `_move_stop_to_breakeven`,
  `_update_trailing_stop`, `_update_dynamic_target`, `_reconstruct_state_from_position`,
  `reset_symbol`. Use `pos.id` for the four state dicts; keep `pos.symbol` for orders/logging.
- **Verify (unit, no live data):** construct `PositionManager` with a fake tracker holding TWO
  positions on the SAME symbol with different ids; assert their trailing/partial state is
  independent. Plus single-book parity (id↔symbol 1:1) unchanged.

**6c — Harness runtime.** A same-repo entrypoint (e.g. `forward_test.py`) that runs the
bake-off (all strategies in parallel, per-`(strategy,symbol)`, no wallet) using the shared
library + `PositionManager` (now trade-id keyed) writing to `learning.db`. Retire
`learning_engine`'s forked `_check_exits` (+ helpers) in favour of the shared `PositionManager`.

---

## Deploy (gated; per `prod-deploy-git-model`, my boundary = your trigger)
Per slice: backup `db/*.db` → stop bot → `git reset --hard origin/main` → run that slice's
migration (if any) → start bot → `parity_check.py` capture/diff. MCX/US folds need a live
session to confirm behaviour before trusting them.
