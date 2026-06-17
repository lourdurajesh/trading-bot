---
name: trading-architecture
description: Architectural guardrail for the AlphaLens trading bot. MUST be consulted before adding or changing ANY trade logic — a strategy, signal, price/chain fetch, option pricing, position sizing/risk, order placement, exit rule, P&L/cost, or a new asset class (NSE equity, NSE/index options, MCX, US). Enforces ONE unified pipeline (watch price → strategy → signal → size → place → exit → ledger) with asset differences in adapters, never in duplicated control flow. Use whenever the task touches strategies/, execution/, risk/, analysis/options*, learning_engine.py, commodity_options_learning.py, order/exit/sizing/pricing, or asks to "add a strategy / instrument / asset class" or "change how we enter/exit/price/size".
---

# Trading Bot Architecture Guardrail

**Read this BEFORE writing or changing trade logic. Goal: one logic for Equity, Index
Options, and MCX. A change to "how we price / size / enter / exit" must be made in ONE place.**

Full evidence + migration plan: `docs/ARCHITECTURE_AUDIT.md`. Roadmap: `PROJECT_PLAN.md`.

## The non-negotiable principle

```
ONE control flow:
  price feed → Strategy.evaluate(ctx) → Signal
            → RiskSizer.size_and_gate(Signal)     (one sizing + risk gate)
            → OrderRouter.place(Signal)           (one entry; BrokerAdapter per exchange)
            → PositionManager.manage(Position)    (one exit engine: SL/target/trail/EOD/DTE)
            → Ledger.record(...)                  (one trades store + cost_model)
```
Asset-class differences live in **DATA / adapters**, never in copied control flow:
- **InstrumentAdapter** — chain fetch/parse, lot size, tick, costs, session hours (NSE-eq / NSE-opt / MCX-opt / US).
- **BrokerAdapter** — Fyers (NSE+MCX) / Alpaca (US).

## Single source of truth — where each concern MUST live

| Concern | The ONE place | Never duplicate into |
|---|---|---|
| Option chain fetch + parse | `execution/options_executor.py` | a second parser (MCX had one — being removed) |
| Transaction costs | `analysis/cost_model.py` + `config/cost_rates.json` | inline fee math |
| Lot sizes / strike steps | `config/nse_instruments.json` (+ `scripts/fetch_lot_sizes.py`) | hard-coded numbers |
| Tunable values | `.env` / JSON / DB (surfaced for config) | literals in code |
| Strategy → output | `BaseStrategy.evaluate() → Signal` | `dict` or `MCXSignalResult` variants |
| Position sizing / risk | one RiskSizer (target) | per-engine sizing |
| Exit rules | one PositionManager (target) | per-engine `_check_exits` |
| Order placement | one OrderRouter (target) | per-engine `place_order` call sites |

## Before you code — answer these

1. **Am I about to duplicate logic that already exists for another asset class?**
   If yes → STOP. Extend the shared component or add an adapter. Do not copy-paste a pipeline.
2. **Is there a hard-coded number?** → move it to `.env`/JSON/DB (the standing rule).
3. **Does my change need to apply to Equity AND Options AND MCX?** If yes, it must land in the
   shared layer so it applies to all three at once — not in one engine.
4. **Am I adding a new strategy?** It returns a `Signal` from `evaluate()`. No new signal type.
5. **Am I adding a new asset class / instrument?** Add an InstrumentAdapter entry, not a new
   `run_cycle` / exit engine / order path.

## Hard rules (refuse / flag if asked to violate)

- Do **not** create a 4th parallel pipeline, a 2nd chain parser, a 2nd cost model, or a 2nd
  exit engine. If a task seems to need that, surface it and propose the adapter instead.
- Do **not** put asset-specific branching in control flow when it belongs in an adapter/config.
- Do **not** hard-code lot sizes, fees, thresholds, times — they are config.
- When fixing a bug in pricing/exit/sizing, **grep for sibling copies** and fix the shared
  source, or flag that the duplication must be collapsed (this is why B2.3 had to be fixed twice).

## Current reality (migration state — keep this honest)

As of 2026-06-17 the bot still has **three pipelines** (production NSE via
`strategy_selector.py`, learning via `learning_engine.py`, MCX via
`commodity_options_learning.py`). Unification is planned as **Phase U (U1–U6)** in
`PROJECT_PLAN.md`, sequenced before go-live:
- U1 unify chain layer · U2 unify Signal type · U3 unify exit · U4 unify sizing ·
  U5 unify order+ledger · U6 collapse the three `run_cycle`s.

Until U-phases land, when touching one engine **check whether the same change is needed in the
sibling engines** and say so explicitly. Every new change should move toward the unified shape,
never add to the divergence.

## Quick self-check to emit when doing trade-logic work

> Architecture check: this change touches **<concern>**. Single source of truth is **<file>**.
> Sibling copies that may need the same change: **<list or 'none'>**. Hard-coded values moved to
> config: **<yes/n-a>**. Moves toward unified pipeline: **<yes/how>**.
