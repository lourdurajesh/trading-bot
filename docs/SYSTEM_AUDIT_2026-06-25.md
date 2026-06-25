# System Audit — File-by-File Purpose & Duplication Map (2026-06-25)

> **Why this exists:** to map *every* module's purpose + actions and pin down exactly
> where trading logic is **duplicated or crossed**, before any further refactor. Evidence-based:
> every claim below was verified against code on branch `unify-exits-u3-slice3`
> (parent `origin/main` @ 9be973a), via signatures + import/call greps — not from the docs.
>
> **Headline correction:** `docs/ARCHITECTURE_AUDIT.md` carries a 2026-06-23 banner saying the
> 3-pipeline duplication is **"RESOLVED"**. It is **not**. Phase U/V created shared *decision*
> modules (`sizing`, `fees`, `exit_policy`, `exit_signals`, `exit_rules`, `run_context`, `ledger`,
> `order_router`) and a thin `TradingOrchestrator` **façade** — but the four **engines** below still
> each own the full watch→size→place→exit→store loop. The façade just calls their old `run_cycle()`s.
> This gap is the root cause of the issues found this week (paper positions not running structural
> exits; paper book with no buying-power gate).

---

## 0. The four engines that still each own a full pipeline

| Engine | Loop entry | Owns (its own copy of) |
|---|---|---|
| `strategies/strategy_selector.py` | `run_cycle()` | NSE equity + index-options generation → `order_manager` → `position_manager` |
| `learning_engine.py` | `run_cycle()` | learning-lab generation **+ its own exit engine** `_check_exits` + paper mirror |
| `commodity_options_learning.py` | `run_cycle()` / `check_exits()` | MCX: **own chain parser, own sizing+funds, own exits, own ledger** |
| `us_reversal.py` | `run_cycle()` | US SPY/QQQ: own enter/exit/store |

`execution/orchestrator.py` `TradingOrchestrator` → `NSEMainAdapter/NSELearningAdapter/MCXAdapter/USAdapter`
each just delegate: `strategy_selector.run_cycle()`, `learning_engine.run_cycle()`,
`commodity_options.run_cycle()`, `us_reversal.run_cycle()`. **It owns scheduling + error isolation
only — not the trading logic.** (orchestrator.py:80–126)

---

## PART A — Concern duplication matrix (the crossings)

Verdict legend: ✅ single source · ⚠️ shared decision but multiple consumers/wrappers · ❌ duplicated logic

| Concern | The ONE place (intended) | Where it ACTUALLY lives | Verdict |
|---|---|---|---|
| **Orchestration loop** | `orchestrator.TradingOrchestrator` | + `strategy_selector.run_cycle` + `learning_engine.run_cycle` + `commodity_options.run_cycle` + `us_reversal.run_cycle` (4 real loops) | ❌ |
| **Signal type** | `base_strategy.Signal` | + learning `dict` (`_sig_to_learning_dict`) + `mcx_base.MCXSignalResult` (`generate_signal`) | ❌ |
| **Option chain fetch/parse** | `analysis/options_chain.py` (`chain_service`) | NSE (`options_executor`) and MCX (`commodity`) BOTH delegate to `chain_service` (`_get_chain`/`_leg_data`/`_chain_lookup` are 1-line wrappers; no raw `optionchain()` outside the service). `_bs_chain` is a justified MCX synthetic fallback, not a parser. **[Corrected 2026-06-25 slice-4: U1 already unified this — verified against code]** | ✅ |
| **Position sizing** | `execution/sizing.py` (`shares_to_fit`/`lots_to_fit`) | consumers: `risk_manager._calculate_size`, `options_risk._calculate_lots`, `commodity._compute_lots`, `institutional_momentum`; **`learning_engine._paper_position_size` does NOT use it** | ⚠️ |
| **Funds / wallet / buying power** | — (no single owner) | `risk/portfolio_tracker` (prod positions, **no depleting cash**), `paper_trading.py` (₹5L wallet, learning mirrors only), `commodity._get_available_funds`, `order_manager._check_margin` (broker funds), `fyers_broker.get_funds` | ❌ |
| **Risk gate** | `risk/risk_manager.validate` | + `risk/options_risk.check` (options) + `risk/daily_risk_budget.check` (MCX/per-strategy) | ❌ |
| **Order placement / fills** | `execution/order_router.py` + `order_manager` | order_router used by `order_manager` + `commodity`; **`learning_engine._open_trade` (paper mirror) and `us_reversal._maybe_enter` bypass it** | ❌ |
| **Exit engine** | `execution/position_manager.py` | + `learning_engine._check_exits` (+8 helper exits) + `commodity._check_exits` (spot stops) + `us_reversal._close`. Shared *decision* via `exit_policy/exit_signals/exit_rules` ⚠️ but four engines wrap them differently | ❌ |
| **Trades store / ledger** | `execution/ledger.py` (`ledger` table + views) | `ledger.record` used by learning + commodity + us_reversal; **production book writes its own `trades` table via `portfolio_tracker._save_position`** (does NOT use ledger) | ❌ |
| **Transaction costs** | `analysis/cost_model.py` ← `execution/fees.py` | single source, all engines + dashboard use it | ✅ |
| **Instrument/lot/strike data** | `config/nse_instruments.json` | single source via `options_executor.get_lot_size/get_strike_step` | ✅ |
| **Run mode (LIVE/PAPER/LEARNING)** | `execution/run_context.py` | `RunContext` exists; but mode also read via `settings.PAPER_TRADING`, `order_manager.mode` (AUTO/CONFIRM), and engine identity (learning==paper-by-construction) | ⚠️ |
| **Strategy on/off** | `config/strategy_toggles.py` | single source, gated per generation point | ✅ |
| **Indicators** | `analysis/indicators.py` | single source | ✅ |
| **Dashboard data feed** | `api/segment_readers.py` + `/book/*`, `/ledger/*` | **legacy per-segment endpoints still live**: `learning_trades/stats/review`, `commodity_trades/stats`, `us_stats/trades`, `get_paper_stats/positions` | ❌ |

---

## PART B — File-by-file (purpose · key actions · duplication flag)

### Root — engines & entry points
- **`main.py`** (629L) — boot brokers/streams, build orchestrator, run master loop + scheduled jobs (conviction, OI, FII, token, snapshots). *Actions:* `start`, `_run_loop`, `_start_dashboard`, `_reconcile_pending_closes`, `_catch_up_conviction_score`, `_load_dynamic_watchlist`. **OWNS:** process lifecycle. OK.
- **`watchdog.py`** — systemd supervisor; restarts `main.py`. OK.
- **`learning_engine.py`** (1320L) — "learning lab": generate on watchlist + index-options, **own exit engine**, paper mirror. *Actions:* `run_cycle`, `_run_index_options_learning`, `_open_trade`, **`_check_exits` + `_underlying_trail_exit` + `_underlying_breakdown_exit` + `_update_chandelier_stop` + `_apply_time_tightening` + `_check_partial_booking` + `_check_signal_reversal` + `_check_bb_middle_exit` + `_check_volatility_exit`**, `_paper_position_size`, `_real_qty`, `_db_insert/_db_close`, `_sig_to_learning_dict`. **❌ DUPLICATES:** exit engine, sizing, order/store, signal type. *Richest exit ruleset of the four — ironically the sandbox.*
- **`commodity_options_learning.py`** (2607L) — MCX engine, fully self-contained. *Actions:* `run_cycle`, `_evaluate`, `_build_trade`, **own chain (`_get_chain/_leg_data/_chain_lookup/_bs_chain`)**, **own sizing+funds (`_compute_lots/_get_available_funds`)**, **own exits (`check_exits/_check_exits`)**, real order path (`_execute_real_entry/_execute_real_exit/_confirm_fill`), **own ledger (`_init_db/_db_insert/_db_close`)**, instrument CRUD. **❌ DUPLICATES:** chain, sizing, funds, exits, orders, store. *Largest single file; the biggest divergence.*
- **`us_reversal.py`** (204L) — US SPY/QQQ reversal paper engine. *Actions:* `run_cycle`, `_maybe_enter`, `_close`, `_persist_open/_persist_close`, own BS calc. **❌ DUPLICATES:** enter/exit/store (uses `ledger.record` for store though). Uses `reversal_core` ✅ for pattern maths.
- **`token_manager.py`** / **`generate_token.py`** — Fyers token lifecycle/cron. OK, single source.
- **`audit_log.py`** (342L) — append-only audit trail (signals, rejections, fills, mode changes). OK, single source.
- **`paper_trading.py`** — ₹5L paper wallet **for learning mirrors only** (per its own docstring); writes `paper_wallet` + mirror trades. **❌ CROSSES:** funds/wallet (a *second* wallet concept the production book doesn't share).

### Root — reviews/cron (read-mostly, lower risk)
- `nightly_agent.py`, `weekly_agent.py`, `daily_plan.py`, `daily_review.py`, `weekly_review.py`, `journal_analyser.py`, `portfolio_analyser.py`, `system_health.py` — scheduled analytics/reviews consumed by dashboard. *Watch:* `daily_review.py` re-implements its own trade fetch (`fetch_closed_today`/`fetch_paper_closed_today`) — a read-side duplicate of the ledger/segment readers. ⚠️

### `execution/` — plumbing + the shared decision modules
- **`orchestrator.py`** — `TradingOrchestrator` + 4 segment adapters. **Façade only** (delegates to engine `run_cycle`s). ⚠️ (the intended single loop, not yet real)
- **`order_manager.py`** (613L) — production signal→order: `submit` → risk → `_check_min_profit` → **`_check_margin`** → `_execute`/`_execute_options` → `_confirm_fill`. **Crosses funds** (margin check uses broker funds / `TOTAL_CAPITAL` fallback / 25% intraday; never rejects equity, only shrinks).
- **`position_manager.py`** (893L) — production exit engine: `check_all`/`_check_position`/`_check_options_position`, `_structural_exit_reason` (added this branch), trailing/breakeven/dynamic-target, EOD/DTE/MAX_HOLD. **OWNS** the production exit engine (one of 4).
- **`order_router.py`** (45L) — `OrderRouter.place/cancel` + `get_broker`. ✅ intended single entry, but only `order_manager` + `commodity` use it.
- **`sizing.py`** (105L) — `units_in_budget/lots_to_fit/lots_capped/shares_to_fit`. ✅ decision; ⚠️ `shares_to_fit` lacks an exposure `cap_budget` (only options have it) and `learning_engine` bypasses it.
- **`fees.py`** + **`analysis/cost_model.py`** + `config/cost_rates.json` — ✅ single cost source.
- **`exit_policy.py`** — strategy→style map (trend_trail/mean_reversion/convex_trail). ✅ decision.
- **`exit_signals.py`** — structural exits (ATR/swing/trend-break/momentum-fade/stagnation), arm-in-profit. ✅ decision; consumers diverge.
- **`exit_rules.py`** — option premium/underlying/spot exit decisions. ✅ decision.
- **`run_context.py`** — `RunContext` (LIVE/PAPER/LEARNING): `place_real_orders/enforce_funds/strategy_set/risk_budget`. ✅ exists; ⚠️ not the sole mode source.
- **`ledger.py`** (233L) — `ledger` table + compat views + `record/update_fields/get_rows`. ⚠️ "one store" but production uses `portfolio_tracker.trades` instead.
- **`options_executor.py`** (698L) — NSE chain fetch/parse, expiry+strike select, lot/strike, BS fallback, `get_trail_points/get_exit_mode`. **❌ DUPLICATES chain** vs `analysis/options_chain.py` + MCX parser.
- **`fyers_broker.py`** / **`alpaca_broker.py`** — broker adapters (orders/positions/funds). ✅ adapter shape; ⚠️ callers reach `_client` directly elsewhere.

### `risk/`
- **`portfolio_tracker.py`** (547L) — positions, P&L, paper wallet, `trades` table, stats. **OWNS production positions+store**; **no depleting cash gate**.
- **`risk_manager.py`** (329L) — `validate` (kill-switch, R:R, max-positions, duplicate, **heat**, daily-loss, options-alloc), `_calculate_size`/`_calculate_options_size`. **❌ no free-funds/notional gate**; sizes off static `TOTAL_CAPITAL`.
- **`options_risk.py`** (427L) — options allocation/lots/per-trade caps, own kill-switch + daily P&L. **❌ DUPLICATES risk gate** for options.
- **`daily_risk_budget.py`** (260L) — per-strategy/daily loss budget. Used by MCX. **❌ third risk gate.**

### `strategies/`
- **`base_strategy.py`** — `Signal`/`Direction`/`SignalType`, `BaseStrategy.evaluate` + data access. ✅ base (but not universal — MCX/learning diverge).
- **`strategy_selector.py`** (463L) — production routing: regime→strategy, intelligence+risk gates, submit; cooldowns. OWNS production generation.
- **`reversal_core.py`** — ✅ single source for reversal pattern/trail maths (NSE+US+learning+backtest).
- **`mcx_base.py`** — `MCXStrategy.generate_signal` → `MCXSignalResult`. **❌ DUPLICATES signal interface.**
- Strategy implementations (`directional_options`, `institutional_momentum`, `trend_follow`, `short_trend`, `mean_reversion`, `momentum_reversal`, `gap_fade`, `options_income`, `iron_condor`, `simple_rsi`, `simple_momentum`, `reversal_5m`, `mcx/*`) — each emits a signal. ✅ mostly (simple_* return dicts; mcx return MCXSignalResult).

### `analysis/`, `intelligence/`, `data/`
- `options_chain.py` `OptionsChainService` — ✅ intended single chain service (under-adopted).
- `cost_model.py` ✅; `indicators.py` ✅; `regime_detector.py` (trading) vs `regime_engine.py` (dashboard) — ⚠️ two regime classifiers (consolidation candidate).
- `intelligence_engine.py` (+ `analyst_agent`, `macro_data`, `news_scraper`, `fundamental_guard`, `conviction_scorer`, `nse_participant_collector`, `premarket_analyzer`, `theme_detector`, `universe_scanner`) — ✅ AI/context layer, single `evaluate` entry (fail-open).
- `oi_analyzer`, `options_engine`, `iv_percentile`, `vwap_engine`, `opening_range`, `breadth_engine`, `spread_quality`, `signal_health`, `trade_analytics`, `trade_decision_audit`, `edge_monitor`, `support_resistance` — analytics, mostly single-purpose. ✅
- `data_store.py` (OHLCV/LTP store) ✅; `fyers_stream.py`/`alpaca_stream.py` (WS consumers) ✅.

### `api/`, `config/`, `backtesting/`, `notifications/`
- **`dashboard_api.py`** (2476L) — FastAPI. **❌ CROSSES the unified feed**: `/book/*` + `/ledger/*` (unified) coexist with legacy `learning_*`, `commodity_*`, `us_*`, `get_paper_*` endpoints. Consolidation target.
- `segment_readers.py` ✅ unified read mapping (segment→trades/stats/review).
- `config/settings.py` ✅ central tunables; `strategy_toggles.py` ✅; `strategy_config.py`/`strategy_matrix.py` (params/matrix); `mcx_*`, `market_holidays`, `watchlist` reference data.
- `backtesting/{backtest_engine,data_fetcher,performance,walk_forward,monte_carlo}.py` — engine had its **own** exit model; structural exits wired in on this branch (still not byte-identical to live — Workstream-1 audit item). ⚠️
- `notifications/alert_service.py` ✅ single Telegram/alert dispatch.

---

## PART C — Why we keep going in circles (root cause)

1. **Decision modules were unified; engines were not.** `sizing/fees/exit_policy/exit_signals/exit_rules/
   ledger/order_router/run_context` are shared *functions*, but `strategy_selector`, `learning_engine`,
   `commodity_options_learning`, `us_reversal` each still **wrap them in their own loop** — and several
   bypass them (learning sizing, learning/US order path, production ledger). So a fix to one engine's
   wrapper doesn't reach the others.
2. **The orchestrator is a façade.** It schedules the four engines; it does not own the pipeline. "One
   pipeline" on paper, four in code.
3. **Mode and segment are encoded as *separate engines*, not parameters.** "Learning" *is* an engine,
   not a mode flag on the one pipeline; MCX/US are engines, not adapters. This is the core violation of
   the stated architecture (modes + segments must be parameters of one pipeline).
4. **Funds/wallet has no owner at all** — five partial implementations, and the production paper book
   has none, which is why it "buys without limit."

**Net:** the true remaining work is **U2 (one Signal) → U3 (one exit engine) → U4 (one sizer+funds) →
U5 (one order path + one ledger) → U6 (collapse the 4 run_cycles into the orchestrator)**, plus a
**single Wallet/BuyingPower owner** and **dashboard endpoint consolidation**. The shared decision
modules are the right foundation; the engines must become thin adapters that call them, then disappear.

*(No plan/changes in this doc — mapping only, per request. Plan next.)*
