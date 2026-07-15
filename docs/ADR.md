# Architecture Decision Records

> Append-only log of decisions that shaped the system. **Never edit a past ADR** — supersede it
> with a new one and mark the old `Superseded by ADR-NNN`. This log exists because prior sessions
> re-litigated (or silently reversed) settled decisions, and a stale "✅ RESOLVED" banner on an old
> doc misled every session after it. If a decision isn't here, it isn't decided.
>
> Format: **Status · Context · Decision · Consequences.** Keep each entry short.

---

## ADR-001 — One Evaluator; Entry and Exit are subclasses, not separate engines
**Status:** Accepted (2026-07-09)
**Context:** Entry and Exit both do "iterate a scope, evaluate rules, execute an order." The system
had grown 4 entry loops (per book) + 3 exit loops (per asset class) = duplicated control flow.
**Decision:** One `Evaluator` base (loop + execute→ledger tail). `EntryEvaluator` and `ExitEvaluator`
differ only in four hooks (scope, skip, evaluate, on_signal). 7 loops → 2.
**Consequences:** Per-asset/per-mode behaviour moves to `SegmentAdapter` + `RunContext`. A change to
entry or exit logic is made once. See TECH_SPEC §4.

## ADR-002 — P&L and stops mark the instrument you HOLD, not the underlying
**Status:** Accepted (2026-07-09)
**Context:** MCX/US stops triggered on **spot** while P&L was on the **option spread**. The COPPER
trade went +₹4,000 → −₹1,934 by stopping at "breakeven spot," which is a real loss after theta.
**Decision:** Exits and realised P&L mark the position's own `monitor_symbol`. Spot may inform a
trail, but the realised number is the held instrument's.
**Consequences:** MCX/US exit migration (Phase 2) must use instrument-based marks. See §8.1.

## ADR-003 — Profit-lock on exits is mandatory
**Status:** Accepted (2026-07-09)
**Context:** The MCX trail sat a full `trail_dist` behind the peak, so a winner's stop only ever
returned to ≈entry — winners round-tripped to losses. Recurred across instruments because it was
tuned per-instrument instead of fixed structurally.
**Decision:** Once a trade reaches a configured favourable move, the stop moves to guarantee a
**positive** exit (and/or books a partial). A winner may never round-trip to a loss.
**Consequences:** Baked into the shared `ExitEvaluator`, not per-engine. See §8.2.

## ADR-004 — One R formula, computed once at close
**Status:** Accepted (2026-07-09)
**Context:** PAPER left R null; LEARNING computed a points-based, fee-exclusive R; readers drifted.
`realised_pnl / capital_at_risk` was rejected because `capital_at_risk` = full premium for options
but stop-distance for equity (different meanings).
**Decision:** `pnl_r = realised_pnl / (qty × |entry − original_stop|)`, fee-inclusive, computed once
in the shared tracker at close and persisted. No reader recomputes it.
**Consequences:** Both books already use this since 2026-07-07. See §8.4.

## ADR-005 — LEARNING wallet is a fixed ₹5L P&L ledger, not a compounding balance
**Status:** Accepted (carried from 2026-06-22, reaffirmed 2026-07-09)
**Context:** Sizing off the compounding wallet balance produced a ₹66L wallet and divergence from LIVE.
**Decision:** LEARNING sizes off a **fixed ₹5L** base (= TOTAL_CAPITAL, so it sizes identically to
LIVE/PAPER). The wallet is a P&L ledger only (balance = 5L + Σrealised); it never gates entries or
feeds the risk budget.
**Consequences:** LEARNING stays comparable to LIVE. See §5.

## ADR-006 — RunContext is the only mode variation; LIVE/PAPER exclusive, LEARNING always-on
**Status:** Accepted (2026-07-09)
**Context:** Mode behaviour was scattered across `PAPER_TRADING`/`BOT_MODE`/per-engine branches.
**Decision:** One `RunContext` carries all mode differences (`place_real_orders`, `enforce_funds`,
`strategy_set`, `risk_budget`, `conflict_policy`). LIVE and PAPER are mutually exclusive
(`RUN_MODE`); LEARNING is PAPER-execution-with-all-strategies-no-cap, run in parallel.
**Consequences:** `RUN_MODE` becomes primary; the `PAPER_TRADING` derivation is retired (Phase 7).

## ADR-007 — Live exit-logic changes require backtest + shadow mode before shipping
**Status:** Accepted (2026-07-09)
**Context:** The flat `arm_profit_pct` gate underperformed both an ATR-relative gate and doing
nothing; a closed-bar backtest didn't match live forming-candle behaviour.
**Decision:** Exit-arming is volatility-relative (ATR-based). Any change to live exit logic is
backtested, then **shadow-logged** (candidate runs alongside the live rule on real ticks, logging
only) before it is switched on.
**Consequences:** Shadow-logging infrastructure stays in `PositionManager`/`ExitEvaluator`. See §8.3.

## ADR-008 — Segment differences live in adapters, never in new engines/loops
**Status:** Accepted (2026-07-09)
**Context:** Each asset class grew its own engine; each new asset class threatened a new pipeline.
**Decision:** A new asset class = a new `SegmentAdapter` (universe, session, chain, broker,
stop-semantics). A new mode = a new `RunContext`. Never a new engine, loop, sizer, parser, or store.
**Consequences:** The healthy layers (`data_store`+streams, `chain_service`) are the model. See §6, §10.

## ADR-009 — Scope: single-process bot now; enterprise platform deferred
**Status:** Accepted (2026-07-09)
**Context:** External review framed this as the start of an "Enterprise Trading Platform" (internal
event bus, plugin architecture, service versioning, performance SLAs, distributed execution).
**Decision:** Stay a **single-process bot**. Adopt the review's production-hardening items that fit
(state machines, startup recovery/reconciliation, failure policies, structured logging, health,
expanded testing — TECH_SPEC §11–§14). **Defer** the enterprise/distributed concerns until the
single-process bot is unified and stable.
**Consequences:** No event bus / plugin framework / versioning work now. Revisit post-unification.

## ADR-010 — Bot and UI/API are separate applications, coupled only via shared stores
**Status:** Accepted (2026-07-09)
**Context:** The dashboard API runs as a daemon thread inside `main.py` (`_start_dashboard`), so a
UI/API fault shares the bot's process. The bot must trade reliably regardless of the UI.
**Decision:** The **Bot** (headless background trading process) and the **UI/API** (Configuration +
Visualization) are separate applications. They share **only** persistent state — the **Config store**
(bot reads live, UI writes) and the **Ledger store** (bot writes, UI/reviews read). No direct calls,
no shared process. The bot trades whether or not the UI is running.
**Consequences:** Split the API into its own process (UNIFICATION_TASKS Phase 6c). The decoupling
mechanism already exists — the bot reads config live from the shared DB (e.g. `strategy_toggles`).
See TECH_SPEC §1 (two diagrams) and §2.6.

## ADR-011 — IronCondor deleted (not viable on Indian indices)
**Status:** Accepted (2026-07-14)
**Context:** IronCondor was instantiated but never routed (dead), disabled with a "poor backtest"
note. User asked to verify before deleting. A proper BS-repriced backtest was run (49 trades,
NIFTY/BANKNIFTY/FINNIFTY, 12 months) plus a 9-way param sweep (strike width 1.0–1.5σ × stop 1.0–2.0×).
**Decision:** Delete it. **Every** parameter config was net-negative (expectancy −4.9 to −22/trade).
Structural cause: 0.5×-credit target vs up-to-2×-credit stop is 1:4 reward:risk, needing ~80% win
rate; achieved only 57–73% because indices breach the short strikes too often. No tuning escapes it.
**Consequences:** Removed `iron_condor.py` + `options_strategy_config.py` and all references
(commit ac2a0e0). Method (BS-reprice + full param sweep) is the template for validating any future
premium-selling strategy before wiring it.

---

## Backlog of decisions still to record (as they're made)
- MCX-live scope (stays PAPER/LEARNING through migration, or into LIVE at Phase 5?)
- US go-live (Alpaca keying) timing
- Reconciliation policy for the ORPHAN case (broker holds a position the ledger doesn't)
