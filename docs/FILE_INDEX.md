# File Index

> **Rebuilt 2026-07-09 from a line-by-line audit against the actual code.** Supersedes the
> 2026-06-23 version, which claimed "Phase-U + Phase-V unification complete" — that claim was
> **false**: NSE (production + learning) is unified, but **MCX and US remain full private
> pipelines**, and the dashboard/config/risk layers are only half-migrated. This index records
> the real state so future sessions resume from ground truth. See `docs/TECH_SPEC.md` (target)
> and `docs/UNIFICATION_TASKS.md` (work plan).

## How to read this

Every file maps to **one** architectural component. A file is healthy when it belongs cleanly to
one component; the core problem is the three "engines" that each span C1–C5 + C10 by themselves.

**Pipeline components (the trade control-flow):**
`C1` Strategy Layer (data→Signal) · `C2` Risk & Sizing · `C3` Order Routing · `C4` Exit
Management · `C5` Ledger & P&L · `C6` Run Context (LIVE/PAPER/LEARNING) · `C7` Instrument
Adapter (chain/lot/tick/session) · `C8` Broker Adapter (Fyers/Alpaca) · `C9` Market Data ·
`C10` Orchestration.

**Supporting components:** `C11` Analytics · `C12` Intelligence · `C13` Dashboard/API ·
`C14` Reviews & Agents · `C15` Backtesting · `C16` Ops/Scripts/Tests · `C17` Infra.

**Target control-flow (spec):**
```
price feed → Strategy.evaluate → Signal → RiskSizer.size_and_gate
          → OrderRouter.place → PositionManager.manage → Ledger.record
   asset-class differences live in ADAPTERS, never in duplicated control flow
```

**Verdict legend:**
- ✅ **CORE** — in the unified pipeline, single-source, compliant
- 🔶 **PARTIAL** — shared in principle but still diverges / only half-wired
- ❌ **DIVERGENT** — a parallel/duplicate engine that violates "one engine"
- 🧰 **SUPPORT** — legitimate non-pipeline tool (compliance N/A, just needs to be correct)
- 🗑️ **DEAD/JUNK** — unused, vestigial, or stale — deletion candidate

**Entry points (how the bot runs):** `watchdog.py` (systemd) → `main.py` → `execution/orchestrator.py`
→ segment adapters. `api/dashboard_api.py` serves the UI on :8000. Cron: `generate_token.py`,
`nightly_agent.py`, `weekly_agent.py`.

---

## `execution/` — order & exit plumbing

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `run_context.py` | 90 | C6 | `RunContext` (LIVE/PAPER/LEARNING) mode flags | ✅ CORE | Keep — the one mode source |
| `sizing.py` | 109 | C2 | `shares_to_fit`/`lots_to_fit` size primitive | ✅ CORE | Keep; make MCX/US size only via this |
| `fees.py` | 55 | C5 | Transaction-cost facade over `cost_model` | ✅ CORE | Keep |
| `ledger.py` | 299 | C5 | Unified trades store (`ledger` + `segment`, compat views nse/mcx/us/live) | ✅ CORE | Keep; converge the 4 segment schemas over time |
| `exit_rules.py` | 84 | C4 | Exit primitives (premium SL/target, underlying trail, ratchet) | ✅ CORE | Keep — shared by all segments |
| `exit_signals.py` | 152 | C4 | Structural exits (ATR trail, swing/trend break, momentum fade) | ✅ CORE | Keep |
| `exit_policy.py` | 54 | C4 | Strategy→exit-style map from config | ✅ CORE | Keep |
| `position_manager.py` | 1112 | C4 | Exit engine — **only NSE prod+learning use it** | 🔶 PARTIAL | **Make it THE exit engine**; migrate MCX & US onto it |
| `orchestrator.py` | 147 | C10 | `TradingOrchestrator` — unifies scheduling, wraps **4 `run_cycle`s** | 🔶 PARTIAL | Extend so adapters delegate to shared risk→router→PM path |
| `order_manager.py` | 645 | C3 | NSE signal→order; calls `broker.place_order` **directly** | 🔶 PARTIAL | Route through `order_router`; keep only signal-intake/confirm role |
| `order_router.py` | 45 | C3 | Broker place/cancel wrapper — **not used by `order_manager`** | 🔶 PARTIAL | Make it the single placement path for all segments incl. NSE |
| `options_executor.py` | 698 | C7 | NSE chain fetch (→`chain_service`), strike/expiry select, BS fallback | ✅ CORE | Keep (NSE options adapter) |
| `fyers_broker.py` | 277 | C8 | Fyers REST adapter (NSE+MCX) | ✅ CORE | Keep; stop callers reaching into `_client` |
| `alpaca_broker.py` | 152 | C8 | Alpaca (US) adapter — sim until keyed | ✅ CORE | Keep |

## `strategies/` — the C1 layer (mostly healthy)

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `base_strategy.py` | 228 | C1 | Base class: `Signal`/`Direction`, data access, `evaluate()` contract | ✅ CORE | Keep — the strategy contract |
| `strategy_selector.py` | 463 | C1+C2+C3+C10 | NSE production loop: routing + gates + submit — **one of the 4 `run_cycle`s** | 🔶 PARTIAL | Split: keep routing table; move gating/submit to shared path |
| `directional_options.py` | 207 | C1 | Index single long call/put | ✅ CORE | Keep (routed: prod + learning) |
| `institutional_momentum.py` | 353 | C1 | Conviction-driven ATM index options | ✅ CORE | Keep (routed) |
| `trend_follow.py` | 265 | C1 | Equity long breakout | ✅ CORE | Keep (routed: prod + learning) |
| `short_trend.py` | 306 | C1 | Equity short breakdown | ✅ CORE | Keep (routed) |
| `mean_reversion.py` | 325 | C1 | Range fade | ✅ CORE | Keep (routed: prod + learning) |
| `momentum_reversal.py` | 261 | C1 | Extreme-RSI snap-back | ✅ CORE | Keep (routed) |
| `gap_fade.py` | 271 | C1 | Opening-gap fade (9:15–9:45) | ✅ CORE | Keep (routed) |
| `options_income.py` | 174 | C1 | Short strangle (premium selling) | ✅ CORE | Keep (routed) |
| `iron_condor.py` | 265 | C1 | Defined-risk condor — **instantiated + toggle-listed but NEVER routed** | 🗑️ DEAD | Delete (or wire). Dead since ≥2026-06 |
| `options_strategy_config.py` | 39 | C1 cfg | Config **only for `iron_condor`** | 🗑️ DEAD | Delete with condor |
| `simple_rsi.py` | 150 | C1 | Learning baseline (returns `Signal`) | ✅ CORE | Keep (learning) |
| `simple_momentum.py` | 144 | C1 | Learning baseline | ✅ CORE | Keep (learning) |
| `reversal_core.py` | 91 | C1+C4 | **SINGLE SOURCE** reversal pattern+exit+trail math (NSE/US/learning) | ✅ CORE | Keep — a model of the target |
| `reversal_5m.py` | 235 | C1 | Live NSE index reversal (5m/3m) — learning-wired only | ✅ CORE | Keep (learning) |
| `mcx_base.py` | 268 | C1 | Base for MCX spreads (+ `mcx_live_volume`, config) | ✅ CORE | Keep |
| `mcx/__init__.py` | 6 | C17 | Package exports | 🧰 SUPPORT | Keep |
| `mcx/trend_spread.py` | 196 | C1 | MCX trend debit spread | ✅ CORE | Keep (registered) |
| `mcx/breakout_spread.py` | 202 | C1 | MCX breakout debit spread | ✅ CORE | Keep (registered) |
| `mcx/rsi_reversal.py` | 99 | C1 | MCX RSI-reversal spread | ✅ CORE | Keep (registered) |

## Root — engines & orchestration

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `main.py` | 629 | C10 | Master loop: boot brokers/streams, build orchestrator, scheduled jobs | ✅ CORE | Keep — drives everything via orchestrator |
| `learning_engine.py` | 678 | C10 (+shared C2/C3/C4/C5) | NSE learning book — **risk/order/exit/ledger already shared**; only its scan loop + dashboard read-shims remain private | 🔶 PARTIAL | **The migration template.** Later fold scan into orchestrator, drop read-shims |
| `commodity_options_learning.py` | 2629 | C1–C5+C10 | MCX engine — **complete private pipeline**: own entry loop, sizing, order path, **exit engine (`_check_exits` = COPPER bug)**, chain-mark P&L | ❌ DIVERGENT | **Worst offender.** Migrate exits→PositionManager first, then sizing→RiskManager, then entry→orchestrator |
| `us_reversal.py` | 204 | C1+C4+C5+C10 | US reversal engine — private loop; **reuses `reversal_core`+`exit_rules`+`ledger`** | ❌ DIVERGENT | Small; migrate exit loop→PositionManager, entry→orchestrator |
| `paper_trading.py` | 686 | C5 (parallel) | Old paper wallet — `mirror_learning_*` **dead** (~150 LoC); only `close_order` + Paper-tab stats still wired | 🗑️ DEAD/JUNK | Repoint Paper tab to ledger, remove PM remnant call, delete file |

## `risk/` — the C2 layer (+ misfiled C4/C5)

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `portfolio_tracker.py` | 653 | C4+C5 | Position state, live P&L, stats; per-book; holds unified `_compute_pnl_r`. 11 importers — **not MCX/US** | 🔶 PARTIAL | Keep; make MCX/US route through it. Belongs in `execution/`, not `risk/` |
| `risk_manager.py` | 380 | C2 | Kill switch, daily loss, heat; per-book. **NSE prod+learning only** | 🔶 PARTIAL | Keep as the one gate; migrate MCX/US onto it |
| `options_risk.py` | 443 | C2 | Options allocation/lots/caps; per-book `OptionsRiskGate`. NSE options | ✅ CORE | Keep; MCX options should gate here too |
| `daily_risk_budget.py` | 260 | C2 | Per-strategy/daily budget — **MCX-only, parallel to `RiskManager`** | ❌ DIVERGENT | Fold into `RiskManager`, then delete |

## `config/` — settings & reference data

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `settings.py` | 329 | C6/C17 | Central env config; `RUN_MODE` **derived** from `PAPER_TRADING`; `LIVE_STRATEGIES`; `BOT_MODE` | 🔶 PARTIAL | Make `RUN_MODE` primary, retire derivation |
| `strategy_toggles.py` | 97 | C1 cfg | On/off source via `strategy_settings` table | ✅ CORE | Keep — the intended single on/off source |
| `strategy_config.py` | 328 | C1 cfg | Per-strategy params — **also carries `enabled` flags (dup of toggles)** | 🔶 PARTIAL | Keep params, remove `enabled` flags |
| `strategy_matrix.py` | 134 | C1/C13 | Strategy×regime matrix (dashboard display) | 🧰 SUPPORT | Keep |
| `mcx_engine_settings.py` | 369 | C7/C2 | MCX tunables — parallel settings namespace | 🔶 PARTIAL | Keep for now; consolidate long-term |
| `market_holidays.py` | 386 | C7 | NSE holiday calendar | 🧰 SUPPORT | Keep |
| `mcx_calendar.py` | 208 | C7 | MCX session calendar | 🧰 SUPPORT | Keep |
| `watchlist.py` | 116 | C1/C9 | NSE/US universe | 🧰 SUPPORT | Keep |
| `learning_watchlist.py` | 74 | C1/C9 | Learning-lab universe | 🧰 SUPPORT | Keep |
| `logging_ist.py` | 76 | C17 | IST logging setup | 🧰 SUPPORT | Keep |
| `__init__.py` | 0 | C17 | Package init | 🧰 SUPPORT | Keep |
| `cost_rates.json` | — | C5 | Transaction-cost rates | ✅ CORE | Keep (single fee source) |
| `exit_signals.json` | — | C4 | Structural-exit thresholds | ✅ CORE | Keep |
| `strategy_exits.json` | — | C4 | Strategy→exit-style map | ✅ CORE | Keep |
| `nse_instruments.json` | — | C7 | F&O lot sizes / strike steps | ✅ CORE | Keep |
| `entry_filters.json` | — | C11 | S/R config — read only by `support_resistance` | 🧰 SUPPORT | Keep; verify still wanted |

## `data/` — C9 market data (fully unified — the model pattern)

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `data_store.py` | 377 | C9 | In-memory OHLCV+LTP store — one store, 33 importers, no parallel store | ✅ CORE | Keep |
| `fyers_stream.py` | 671 | C8/C9 | Fyers WS (NSE **and** MCX) → `data_store` | ✅ CORE (adapter) | Keep |
| `alpaca_stream.py` | 244 | C8/C9 | Alpaca WS (US) → `data_store` | ✅ CORE (adapter) | Keep |
| `__init__.py` | 0 | C17 | Package init | 🧰 SUPPORT | Keep |

## `analysis/` — C11 analytics (+ C5/C7 living here)

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `indicators.py` | 356 | C11 | TA indicators (26 importers) | ✅ CORE | Keep — one indicator source |
| `cost_model.py` | 100 | C5 | Transaction costs from `cost_rates.json` | ✅ CORE | Keep |
| `options_chain.py` | 215 | C7 | **`chain_service` — the ONE chain parser** (NSE + MCX) | ✅ CORE | Keep — U1 win |
| `regime_detector.py` | 248 | C11 | Regime classification used by **trading** | ✅ CORE | Keep |
| `options_engine.py` | 292 | C11 | IV rank / options analytics (16 importers) | ✅ CORE | Keep |
| `regime_engine.py` | 224 | C11 | **Second** regime module — dashboard/`strategy_matrix` only | 🔶 PARTIAL | Verify no logic dup with `regime_detector`; keep only display |
| `oi_analyzer.py` | 552 | C11 | OI analysis → conviction | 🧰 SUPPORT | Keep |
| `spread_quality.py` | 231 | C11 | Option-spread liquidity (MCX) | 🧰 SUPPORT | Keep |
| `signal_health.py` | 296 | C11 | Skip-reason / drought monitor | 🧰 SUPPORT | Keep |
| `support_resistance.py` | 113 | C11 | S/R levels (reads `entry_filters.json`) | 🧰 SUPPORT | Keep |
| `trade_analytics.py` | 396 | C11 | Post-trade analytics | 🧰 SUPPORT | Keep |
| `trade_decision_audit.py` | 323 | C11 | Decision audit trail | 🧰 SUPPORT | Keep |
| `edge_monitor.py` | 626 | C11 | Weekly edge-degradation monitor | 🧰 SUPPORT | Keep |
| `iv_percentile.py` | 182 | C11 | IV percentile — dashboard only | 🧰 SUPPORT | Keep |
| `vwap_engine.py` | 205 | C11 | VWAP — dashboard only | 🧰 SUPPORT | Keep |
| `opening_range.py` | 249 | C11 | Opening range — dashboard only | 🧰 SUPPORT | Keep |
| `breadth_engine.py` | 183 | C11 | Market breadth — dashboard only | 🧰 SUPPORT | Keep |
| `__init__.py` | 0 | C17 | Package init | 🧰 SUPPORT | Keep |

## `intelligence/` — C12 AI/context

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `intelligence_engine.py` | 200 | C12 | Approve/reject/resize gate (fail-open) — **only NSE production** | 🔶 PARTIAL | Route all books through it (or scope NSE-only consciously) |
| `conviction_scorer.py` | 496 | C12 | Pre-market F&O conviction → `InstitutionalMomentum` | ✅ CORE | Keep — feeds a strategy |
| `analyst_agent.py` | 327 | C12 | Claude analyst (sim when AI off) | 🧰 SUPPORT | Keep |
| `fundamental_guard.py` | 245 | C12 | Fundamental veto — engine + `learning_engine` direct | 🧰 SUPPORT | Keep |
| `macro_data.py` | 314 | C12 | Macro snapshot (7 consumers) | 🧰 SUPPORT | Keep |
| `news_scraper.py` | 343 | C12 | News feed | 🧰 SUPPORT | Keep |
| `nse_participant_collector.py` | 376 | C12 | FII participant OI collector | 🧰 SUPPORT | Keep |
| `premarket_analyzer.py` | 279 | C12 | Pre-market context | 🧰 SUPPORT | Keep |
| `theme_detector.py` | 339 | C12 | Sector/theme detection | 🧰 SUPPORT | Keep |
| `universe_scanner.py` | 427 | C12 | Dynamic universe scans | 🧰 SUPPORT | Keep |

## `api/` + `dashboard/` — C13

| File | LoC | Component | Purpose | Verdict | Suggestion |
|---|---|---|---|---|---|
| `segment_readers.py` | 86 | C13 | `segment`→reader adapter behind `/ledger/*` — **the target pattern** | ✅ CORE | Keep — build on this |
| `dashboard_api.py` | 2497 | C13 | FastAPI backend — **95 endpoints**: unified `/book`+`/ledger` **and** legacy clones `/commodity`×20, `/learning`×3, `/us`×2, `/paper`×2 | 🔶 PARTIAL | Retire duplicate clones; split the monolith by concern |
| `dashboard/index.html` | — | C13 | React UI (single file) | 🧰 SUPPORT | Keep; audit which endpoints it calls |
| `api/__init__.py` | 0 | C17 | Package init | 🧰 SUPPORT | Keep |

## Root — reviews & cron agents (C14)

| File | LoC | Purpose | Verdict | Suggestion |
|---|---|---|---|---|
| `daily_plan.py` | 863 | Pre-market plan | 🧰 SUPPORT | Keep |
| `daily_review.py` | 322 | End-of-day review — **reads old `paper_trades` directly** | 🔶 SUPPORT | Repoint to ledger when retiring `paper_trading` |
| `weekly_review.py` | 325 | Weekly performance review | 🧰 SUPPORT | Keep |
| `journal_analyser.py` | 930 | Trade-journal analytics | 🧰 SUPPORT | Keep |
| `portfolio_analyser.py` | 954 | Portfolio analytics | 🧰 SUPPORT | Keep |
| `nightly_agent.py` | 422 | Nightly watchlist/IV/housekeeping | 🧰 SUPPORT | Keep |
| `weekly_agent.py` | 369 | Weekly maintenance/analytics | 🧰 SUPPORT | Keep |
| `system_health.py` | 182 | Health snapshot | 🧰 SUPPORT | Keep |

## Root & `notifications/` — infra (C17)

| File | LoC | Purpose | Verdict | Suggestion |
|---|---|---|---|---|
| `watchdog.py` | 364 | Process supervisor (systemd entry) | ✅ CORE (infra) | Keep |
| `token_manager.py` | 217 | Fyers token lifecycle | ✅ CORE (infra) | Keep |
| `generate_token.py` | 237 | Fyers auth token generation (cron) | ✅ CORE (infra) | Keep |
| `audit_log.py` | 342 | Append-only audit trail (7 importers) | ✅ CORE (infra) | Keep |
| `notifications/alert_service.py` | 344 | Telegram/alert dispatch | 🧰 SUPPORT (infra) | Keep; harden (single-channel/fire-and-forget) |

## `backtesting/` — C15

| File | LoC | Purpose | Verdict | Suggestion |
|---|---|---|---|---|
| `backtest_engine.py` | 418 | Core backtest loop | ✅ CORE | Keep |
| `data_fetcher.py` | 235 | Historical data | ✅ CORE | Keep |
| `performance.py` | 230 | Metrics | ✅ CORE | Keep |
| `walk_forward.py` | 242 | Walk-forward (wired into `run_analysis`) | ✅ CORE | Keep |
| `monte_carlo.py` | 195 | Risk-of-ruin / drawdown | ✅ CORE | Keep |

> `run_full_backtest.py` imports the **real** strategy classes (not re-implementations) — arch-compliant.

## `scripts/` — C16 ops & one-offs

| File | LoC | Purpose | Verdict | Suggestion |
|---|---|---|---|---|
| `run_backtest.py` · `run_full_backtest.py` | 135 · 1050 | Backtest runners | 🧰 SUPPORT | Keep |
| `run_analysis.py` | 224 | Analysis runner (walk-forward + monte-carlo) | 🧰 SUPPORT | Keep |
| `strategy_pnl.py` | 65 | Epoch-aware strategy ranking | 🧰 SUPPORT | Keep |
| `fetch_lot_sizes.py` | 97 | Refresh F&O lot sizes | 🧰 SUPPORT | Keep (live ops) |
| `seed_test_trades.py` | 109 | Sandbox trade seeding (dev) | 🧰 SUPPORT | Keep (dev) |
| `migrate_unified_ledger.py` | 164 | One-time ledger migration (done) | 🗑️ STALE | Archive/delete |
| `repair_paper_wallet.py` | 125 | One-time wallet repair | 🗑️ STALE | Delete |
| `resize_open_learning_unified.py` | 115 | One-time resize — refs dead `mirror_learning_open` | 🗑️ STALE | Delete |
| `parity_check.py` | 97 | Old-vs-new verification one-off | 🗑️ STALE | Delete (or keep if still run) |
| `_archive/` (17 files) | ~2900 | Archived one-off backtests — no live imports | 🗑️ JUNK | Delete folder |

## `tests/` — C16

| File | LoC | Purpose | Verdict | Suggestion |
|---|---|---|---|---|
| `test_pipeline.py` | 367 | NSE pipeline smoke test | 🔶 SUPPORT | Keep — `options_risk_gate` sub-test fails on a stale fixture; fix it |
| `test_learning_pipeline.py` | 241 | Slice-6c learning wiring test | ✅ SUPPORT | Keep (passes) |
| `test_sizing.py` | 139 | U4 sizing regression | ✅ SUPPORT | Keep |

> **Coverage gap:** only NSE is tested. **Zero coverage for MCX, US, exit management, or the
> dashboard feeds** — the direct cause of recurring COPPER-exit and R-column bugs. Any migration
> must add tests for the migrated engine.

---

## Rollup — the unification state in one place

| Bucket | What | Files |
|---|---|---|
| ✅ **Healthy** (shared primitive + adapters) | data store, chain parser, indicators, cost/fees, sizing, ledger, run_context, exit primitives, segment_readers, most strategy classes, all analytics/intelligence/infra | ~90 |
| 🔶 **Half-migrated** | `position_manager` (NSE-only), `orchestrator`+`strategy_selector` (4 run_cycles), `order_manager`/`order_router` (two order layers), risk stack (NSE-only), dashboard (unified + clones), config toggles (two sources), `run_context` derivation | ~10 |
| ❌ **Divergent** (per-asset engines) | `commodity_options_learning` (2629 LoC), `us_reversal`, `daily_risk_budget` | 3 |
| 🗑️ **Delete** | `iron_condor`(+config), `paper_trading` (after repoint), `daily_risk_budget` (after fold), 4 one-off scripts, `scripts/_archive/`, dead `/commodity|learning|us|paper` API clones | ~25 + endpoints |

**The thesis, proven by the audit:** every layer built as **"one shared thing + adapters"**
(`data`, `chain`, `fees`, `sizing`, `ledger`) is healthy. Every layer built as **"one engine per
asset class"** (entry loop, exits, risk gate, P&L) is the divergent one. `learning_engine` proves
the fix works — it already runs NSE learning through the shared engine. The remaining work is to
give **MCX and US the same treatment**, then delete the parallel machinery they leave behind.
