# Technical Spec — System Architecture (Summary + Index)

> **Greenfield target** for the trading bot: the architecture the codebase converges to. This is
> the **summary**; each component has a detail doc under [`docs/components/`](components/) and each
> cross-cutting concern under [`docs/cross-cutting/`](cross-cutting/). Current per-file state is in
> [`FILE_INDEX.md`](FILE_INDEX.md); the migration path in [`UNIFICATION_TASKS.md`](UNIFICATION_TASKS.md);
> decisions in [`ADR.md`](ADR.md). Written 2026-07-09. Supersedes `UNIFIED_EXECUTION_SPEC.md`.
>
> **Scope:** a **single-process bot** (see [ADR-009](ADR.md)). Enterprise concerns (event bus,
> plugins, service versioning, distributed execution) are deferred.

---

## 1. System overview

The system is **two independent applications** that share only persistent state:

- **The Bot** — a headless background process that trades four segments (**NSE equity, NSE options,
  MCX options, US options**) in three modes (**LIVE / PAPER / LEARNING**). It reads config, ingests
  market data, runs the pipeline, and writes the ledger. **It has no UI and does not depend on one.**
- **The UI/API** — a separate application with two jobs: **Configuration** (writes the config store the
  bot reads) and **Visualization** (reads the ledger store the bot writes). **If it is down, the bot
  keeps trading.**

They are coupled *only* through two shared stores — the **Config store** (bot reads, UI writes) and the
**Ledger store** (bot writes, UI/reviews read). No direct calls between them; no shared process.

> **Current-state gap:** today the API runs as a daemon thread *inside* `main.py` (`_start_dashboard`).
> Target: a **separate process**, so a UI/API crash or restart cannot affect trading. (Migration item.)

### Diagram A — The Bot (headless background process)
```
  Config store ──(read live every cycle)──┐
                                           ▼
  C9 Market Data ─ticks─► C11 Analytics ─► C12 Intelligence ─► C1 Strategy ─► Signal
   (streams→store)         (regime/IV/OI)     (context gate)     (→ Signal)      │
                                                                                  ▼
        ┌───────────────────────── C4 Evaluator ──────────────────────────┐
        │  EntryEvaluator → C2 Risk&Sizing → C3 OrderRouter → C8 Broker    │
        │  ExitEvaluator  → C3 OrderRouter → C8 Broker                     │
        └───────────────────────────────┬─────────────────────────────────┘
                                         ▼
                              C5 Ledger & P&L ──(write)──► Ledger store
   scheduled by C10 Orchestrator · moded by C6 RunContext · adapted by C7/C8 · underpinned by C17
   ══ runs with NO UI; if the API/UI is down, the bot keeps trading ══

   (Background scheduled jobs — C14 reviews/agents, token refresh — also read the Ledger store and
    write reports; they are separate from the trading loop and from the UI.)
```

### Diagram B — The UI/API (separate application)
```
                    ┌──────────────── Browser (UI) ────────────────┐
                    │   Configuration              Visualization    │
                    │   (toggles, params,          (positions, P&L, │
                    │    instruments, mode)          trades, health) │
                    └──────┬───────────────────────────────▲────────┘
                           │ write                          │ read
                    ┌──────▼──────┐                  ┌──────┴───────┐
                    │ Config store│                  │ Ledger store │   ← the ONLY coupling
                    └──────▲──────┘                  └──────▲───────┘     to the Bot (Diagram A)
                           │ read live                      │ write
                    ══════════════ B O T   P R O C E S S ══════════════
```

The whole Bot is **one pipeline + shared primitives + adapters**. Asset class and run mode are the only
variables, and they vary by **data (SegmentAdapter) and config (RunContext)** — never by duplicated code.

## 2. Architecture principles
1. **One of each pipeline component** — one Evaluator, sizer, order path, chain parser, P&L formula,
   ledger. A new asset class is a `SegmentAdapter`; a new mode is a `RunContext`. Never a new engine.
2. **Adapters, not branches** — asset-class differences (chain, lot, session, broker, stop-semantics)
   live in data/adapters; the control flow never forks on segment.
3. **Config, not code** — every tunable in `.env`/JSON/DB; no magic numbers.
4. **Single source of truth per concern** — including the dashboard (one feed per data-concern with
   `segment` as a param, not cloned endpoints) and docs (this spec + ADR, append-only).
5. **Compute once, read many** — P&L/R computed once at close and persisted; readers never recompute.
6. **Headless bot, separate UI** — the Bot is a standalone background process; the UI/API is a
   separate application. They share **only** persistent state (Config store the bot reads, Ledger
   store the bot writes) — no direct calls, no shared process. The bot trades whether or not the UI
   is running. The UI does two things only: **Configuration** and **Visualization**.

## 3. Complete component map (C1–C17)

Each links to its detail doc (responsibility · children · files · interfaces · current state · target).

### Pipeline
| # | Component | Responsibility | Current state | Detail |
|---|---|---|---|---|
| C1 | Strategy Layer | Detect setup → emit `Signal` | ✅ healthy (17/21 clean) | [C1](components/C1-strategy.md) |
| C2 | Risk & Sizing | Size + gate entry signals | 🔶 NSE-only; MCX/US parallel | [C2](components/C2-risk-sizing.md) |
| C3 | Order Routing | Place/cancel via broker | 🔶 two order layers | [C3](components/C3-order-routing.md) |
| C4 | **Evaluator** | The one loop: iterate scope → evaluate → execute (Entry/Exit) | 🔶 exit NSE-only; 4 entry loops | [C4](components/C4-evaluator.md) |
| C5 | Ledger & P&L | One trade store + one P&L/R formula | 🔶 store unified; P&L 3 ways | [C5](components/C5-ledger-pnl.md) |
| C6 | Run Context | LIVE/PAPER/LEARNING mode | 🔶 RUN_MODE derived, not primary | [C6](components/C6-run-context.md) |
| C7 | Instrument Adapter | Chain/lot/tick/session per asset | ✅ chain unified | [C7](components/C7-instrument-adapter.md) |
| C8 | Broker Adapter | Fyers / Alpaca | ✅ healthy | [C8](components/C8-broker-adapter.md) |
| C9 | Market Data | Streams → one store | ✅ healthy (the model) | [C9](components/C9-market-data.md) |
| C10 | Orchestration | Schedule Evaluators per segment | 🔶 wraps 4 run_cycles | [C10](components/C10-orchestration.md) |

### Bot-side supporting (run inside / alongside the Bot process)
| # | Component | Responsibility | Current state | Detail |
|---|---|---|---|---|
| C11 | Analytics | Indicators, regime, OI, options analytics | ✅ healthy; 1 dup flag | [C11](components/C11-analytics.md) |
| C12 | Intelligence | Context gate + providers | 🔶 gate NSE-only | [C12](components/C12-intelligence.md) |
| C14 | Reviews & Agents | Background scheduled jobs — read Ledger, write reports | 🧰 healthy; 1 retire-tail | [C14](components/C14-reviews.md) |

### Separate application (UI/API — not part of the Bot process)
| # | Component | Responsibility | Current state | Detail |
|---|---|---|---|---|
| C13 | Dashboard/API | **Configuration** (writes Config store) + **Visualization** (reads Ledger store) | 🔶 unified+clones; **runs in-process (should be separate)** | [C13](components/C13-dashboard.md) |
| C15 | Backtesting | Validation harness | ✅ healthy | [C15](components/C15-backtesting.md) |
| C16 | Ops/Scripts/Tests | Runners, migrations, tests | 🔶 test-coverage gap | [C16](components/C16-ops-tests.md) |
| C17 | Infra | Token, audit, logging, notifications, watchdog | ✅ healthy | [C17](components/C17-infra.md) |

### Cross-cutting concerns
- [Data contracts & state machines](cross-cutting/contracts-state-machines.md) — `Signal`, `Position`, `Order`, `Ledger` + lifecycle enums
- [Reliability & recovery](cross-cutting/reliability-recovery.md) — startup reconciliation, feed/broker/DB failure policy
- [Observability](cross-cutting/observability.md) — per-trade story log, heartbeat/health, circuit breakers
- [Testing](cross-cutting/testing.md) — unit / integration / replay / regression tiers

## 4. Layered architecture (data flow)

**Trade path:** `C9 data → C11 analytics → C12 gate → C1 strategy → Signal → C4 EntryEvaluator
(→ C2 risk → C3 order → C8 broker) → C5 ledger → C4 ExitEvaluator (→ C3 → C5)`, all scheduled by
`C10`, moded by `C6`, adapted by `C7/C8`.

**Read path:** `C5 ledger → C13 dashboard / C14 reviews`.

**Offline:** `C15 backtesting` replays historical data through the same `C1` strategies; `C16` holds
runners and tests. `C17` (token/audit/log/watchdog) underpins everything.

The detailed sequence (with state transitions and failure branches) is in
[contracts-state-machines](cross-cutting/contracts-state-machines.md) and
[reliability-recovery](cross-cutting/reliability-recovery.md).

## 5. Current state vs target (inventory rollup)

| Bucket | What | Action |
|---|---|---|
| ✅ Healthy (shared + adapters) | data store+streams, chain parser, indicators, cost/fees, sizing, ledger store, run_context, exit primitives, segment_readers, strategy classes, analytics/intelligence/infra | Keep — the model |
| 🔶 Half-migrated | exit engine (NSE-only), 4 entry loops, two order layers, risk stack (NSE-only), dashboard (unified+clones), config toggles (two sources), **API in-process with the bot** | Migrate onto shared; split API to its own process |
| ❌ Divergent (per-asset engines) | `commodity_options_learning` (2629 LoC), `us_reversal`, `daily_risk_budget` | Fold into Evaluator/RiskManager |
| 🗑️ Delete | `iron_condor`(+cfg), `paper_trading`, dead scripts/`_archive/`, dashboard clones | Remove |

**Thesis:** layers built as *"one shared thing + adapters"* are healthy; layers built as *"one engine
per asset class"* are the divergent ones. `learning_engine` proves the fix works (NSE learning already
runs the shared engine). The work is to give MCX and US the same treatment. Detail:
[FILE_INDEX.md](FILE_INDEX.md), plan: [UNIFICATION_TASKS.md](UNIFICATION_TASKS.md).

## 6. Scope & non-goals
- **In scope:** single-process bot — unified pipeline, segment adapters, the production-hardening in
  the cross-cutting docs (state machines, recovery, observability, testing).
- **Deferred (ADR-009):** internal event bus, plugin architecture, service versioning, performance
  SLAs, distributed/multi-process execution.

## Index
- Components: [C1](components/C1-strategy.md) · [C2](components/C2-risk-sizing.md) ·
  [C3](components/C3-order-routing.md) · [C4](components/C4-evaluator.md) ·
  [C5](components/C5-ledger-pnl.md) · [C6](components/C6-run-context.md) ·
  [C7](components/C7-instrument-adapter.md) · [C8](components/C8-broker-adapter.md) ·
  [C9](components/C9-market-data.md) · [C10](components/C10-orchestration.md) ·
  [C11](components/C11-analytics.md) · [C12](components/C12-intelligence.md) ·
  [C13](components/C13-dashboard.md) · [C14](components/C14-reviews.md) ·
  [C15](components/C15-backtesting.md) · [C16](components/C16-ops-tests.md) ·
  [C17](components/C17-infra.md)
- Cross-cutting: [contracts & state machines](cross-cutting/contracts-state-machines.md) ·
  [reliability](cross-cutting/reliability-recovery.md) · [observability](cross-cutting/observability.md) ·
  [testing](cross-cutting/testing.md)
- Companion docs: [FILE_INDEX](FILE_INDEX.md) · [UNIFICATION_TASKS](UNIFICATION_TASKS.md) · [ADR](ADR.md)
