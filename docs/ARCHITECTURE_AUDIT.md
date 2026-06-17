# Architecture Audit — Pipeline Duplication Map

> Requested 2026-06-17: is Equity / Index-Options / MCX one unified pipeline, or three?
> **Verdict: three separate pipelines with duplicated logic.** This maps every divergence
> with file:line refs so the unification scope is concrete. Evidence-based, not from memory.

## TL;DR

`main.py` drives **three independent orchestrators** that each re-implement the full
watch → evaluate → size → place → exit loop:

- `strategies/strategy_selector.py` — NSE equity + index options (the "production" pipeline)
- `learning_engine.py` — learning-lab paper trades
- `commodity_options_learning.py` — MCX commodity options

They do **not** share order placement, exit management, or option-chain parsing. They don't even
share the strategy interface or the signal type. The only genuinely single-source pieces are
`cost_model.py`, `nse_instruments.json`, and (partially) the `Signal`/`BaseStrategy` types.

---

## Stage-by-stage duplication map

### 1. Orchestration (entry loop)
| Pipeline | Entry point |
|---|---|
| Production | `strategy_selector.run_cycle()` → `_evaluate_symbol()` → `_try_strategy()` (strategy_selector.py:86) |
| Learning | `learning_engine.run_cycle()` (learning_engine.py:90) |
| MCX | `commodity_options.run_cycle()` → `_evaluate()` (commodity_options_learning.py:1115) |
| Wired in | `main.py:381` (prod), `:385` (learning), `:466` (MCX) — three separate calls |

**Divergence:** three hand-written loops over symbols, each with its own cooldown / regime /
earnings / session gating.

### 2. Strategy interface + signal type — **diverges 3 ways**
| Pipeline | Method | Returns |
|---|---|---|
| Production NSE | `BaseStrategy.evaluate(symbol)` (base_strategy.py:149) | `Signal` dataclass (base_strategy.py:44) |
| Learning (simple_*) | `evaluate(symbol)` (simple_rsi.py:52) | **raw `dict`** |
| MCX | `MCXStrategy.generate_signal(df, spot, now)` (mcx_base.py:197) | **`MCXSignalResult`** (mcx_base.py:130) |

**Divergence:** different method name (`evaluate` vs `generate_signal`), different return type
(`Signal` vs `dict` vs `MCXSignalResult`), and different **data-access contract** — NSE strategies
self-fetch via `self.get_ohlcv/get_ltp`; MCX strategies receive `(df, spot, now)` as parameters.
`learning_engine` carries a `_sig_to_learning_dict()` shim to paper over the Signal/dict mismatch.

### 3. Option chain fetch + parse — **two full copies**
| Pipeline | Implementation |
|---|---|
| NSE options | `options_executor._get_chain` / `_chain_lookup` / `_select_from_chain` / `get_best_option` + layout normalisers |
| MCX options | `commodity_options_learning._get_chain` / `_chain_lookup` / `_leg_data` |

**Divergence + proven cost:** these are independent parsers. The flat-format Fyers chain bug was
fixed in `options_executor` (NIFTY) and **had to be fixed again** in `commodity_options_learning`
(B2.3) — same bug, two places. This is the exact "change it in one place" failure.

### 4. Risk / position sizing — **3 different schemes**
| Pipeline | Sizing |
|---|---|
| Production | `risk_manager` + `risk/options_risk.py` + `portfolio_tracker` (RISK_PER_TRADE_PCT, heat, options caps) |
| Learning | **none** — fixed 1 unit/lot |
| MCX | `commodity_options._compute_lots()` (commodity_options_learning.py:660) + `risk/daily_risk_budget.py` |

**Divergence:** the per-trade/daily risk gate (`daily_risk_budget`) is used by MCX but the
production pipeline uses a different risk stack; learning has no sizing at all.

### 5. Order placement — **3–4 paths**
| Pipeline | Entry write |
|---|---|
| Production | `order_manager.submit()` (order_manager.py:44) → `fyers_broker.place_order` |
| Learning | `learning_engine._open_trade()` (learning_engine.py:217) → DB + `paper_trading.mirror_learning_open()` (paper_trading.py:500) |
| MCX | `commodity_options._execute_real_entry()` (commodity_options_learning.py:741) and `_open_trade()` (:1936) → `fyers_broker.place_order` |

**Divergence:** two independent Fyers `place_order` call sites (prod + MCX) with separate
fill-confirmation/unwind logic; learning has its own paper-mirror path.

### 6. Exit management — **3 engines, different rule sets**
| Pipeline | Exit engine | Rules implemented |
|---|---|---|
| Production | `position_manager.check_all()` / `_check_options_position()` (position_manager.py:64, :274) | STOP, TARGET, TRAIL, BREAKEVEN, **DTE force-exit**, EOD |
| Learning | `learning_engine._check_exits()` (learning_engine.py:299) | STOP, TARGET, TRAIL, BREAKEVEN, **TIME_EXIT**, EOD |
| MCX | `commodity_options.check_exits()` / `_check_exits()` (commodity_options_learning.py:1573, :1592) | **STOP_SPOT** (spot-based), TARGET, TRAIL, EOD (no breakeven, no DTE, no time-stop) |

**Divergence (highest risk for live):** the stop *semantics* differ — production stops on option
LTP, MCX stops on the underlying **spot**. Rule coverage differs (DTE vs TIME_EXIT vs neither).
A change to "how we exit" must be made in three places, correctly, three different ways.

### 7. Ledger / P&L
| Pipeline | Table | P&L basis |
|---|---|---|
| Production | `trades` (`portfolio_tracker`) | broker fills |
| Learning | `learning_trades` | real option LTP (B1.1) |
| MCX | `commodity_learning_trades` | chain-mark or estimate (B1.1) |

**Shared (good):** `analysis/cost_model.py` + `config/cost_rates.json` are now single-source and
used by both learning and MCX — this is the model the rest should follow.

### 8. Broker layer
`execution/fyers_broker.py` (NSE + MCX) and `execution/alpaca_broker.py` (US) — reasonable
adapter shape already, but callers reach into `fyers_broker._client` directly from multiple places.

---

## Blast radius — how many places a change touches today

| Change | Places today | Ideal |
|---|---|---|
| Fix option-chain parsing | **2** (options_executor + commodity) — *proven this session* | 1 |
| Change SL / exit logic | **3** (position_manager + learning + commodity) | 1 |
| Change position sizing / risk gate | **3** | 1 |
| Change order placement / fill handling | **2–3** | 1 |
| Add a new asset class (e.g. US options) | a whole new pipeline | 1 adapter |
| Change transaction-cost model | **1** (`cost_model.py`) ✅ | 1 — *the target pattern* |

---

## Target architecture (one logic + adapters)

```
            ┌──────────────── ONE control flow ────────────────┐
 price feed → Strategy.evaluate(ctx) → Signal
            → RiskSizer.size_and_gate(Signal)        (one risk/sizing)
            → OrderRouter.place(Signal)              (one entry; BrokerAdapter per exchange)
            → PositionManager.manage(Position)       (one exit engine: SL/target/trail/EOD/DTE)
            → Ledger.record(...)                     (one trades store + cost_model)
            └───────────────────────────────────────────────────┘
   Asset-specific differences live in DATA via adapters, not in duplicated control flow:
     • InstrumentAdapter  — chain fetch/parse, lot size, tick, costs, session hours
                            (NSE-eq / NSE-opt / MCX-opt)   ← ONE chain parser
     • BrokerAdapter      — Fyers (NSE+MCX) / Alpaca (US)
```

Strategies already share `BaseStrategy` — the fix is to (a) make **all** strategies return one
`Signal` type (retire the learning `dict` and `MCXSignalResult`), and (b) collapse the three
orchestrators/exit-engines/order-paths into one each, parameterised by an `InstrumentAdapter`.

---

## Suggested migration order (incremental, each independently shippable)

Lowest-risk first; each step removes one class of "fix it twice" without a big-bang rewrite.

- **U1. Unify the option-chain layer.** Make the MCX engine call `options_executor` (one parser).
  Removes the #1 proven duplication. *(Highest value, most self-contained.)*
- **U2. Unify the signal type.** All strategies → `Signal`; delete the `dict` / `MCXSignalResult`
  variants and the `_sig_to_learning_dict` shim. Adapter for `(df,spot,now)` vs `evaluate(symbol)`.
- **U3. Unify exit management.** One `PositionManager` with the full rule set + an adapter for
  spot-vs-LTP stop semantics. Retire the two duplicate `_check_exits`.
- **U4. Unify sizing/risk.** One `RiskSizer` (the B2.1 size-to-fit logic) for all asset classes.
- **U5. Unify order placement + ledger.** One `OrderRouter` + one trades store with a `segment`
  column; keep paper/real as a mode flag, not a separate pipeline.
- **U6. Collapse the three `run_cycle`s** into one orchestrator over an instrument list whose
  entries carry their adapter.

**Estimate:** U1 is ~1 focused PR (high value, low risk). U2–U6 are each a meaningful PR; the full
set is a multi-session refactor. All doable on paper before go-live, which is the right time.

## Recommendation
Do **U1 first** (it directly retires the bug class that bit us in B2.3), then sequence U2–U6 before
B6 (go-live). Keep B3/B3.5 (backtest) running in parallel — it doesn't conflict with the refactor.
