# Bot Refinement Plan — Edge Validation & Tuning

Status: PLANNED (target start ~next week). Author: pairing session 2026-06-23.
Pick up here after the unification work (Phase U + Phase V) — see
`docs/UNIFIED_EXECUTION_SPEC.md` and memory `phase-u-execution-unification`.

---

## Where the bot is now (context for whoever resumes)

- **Architecture unified:** one `TradingOrchestrator` (`execution/orchestrator.py`)
  over all segments; single sources for sizing (`execution/sizing.py`), costs
  (`execution/fees.py` → `analysis/cost_model.py` + `config/cost_rates.json`), exit
  policy (`execution/exit_policy.py`), run mode (`execution/run_context.py`),
  per-strategy on/off (`config/strategy_toggles.py`), and one positions/trades feed
  (`/book/positions`, `/book/trades`) + one dashboard `TradesTable`.
- **Mode:** PAPER (`RUN_MODE=PAPER`). Not live-validated.
- **Clean baseline:** trade history reset 2026-06-23 (`app_meta.analysis_epoch`); only
  post-fix data counts. 3 losers disabled (SimpleRSI, MCX BreakoutSpread,
  RSIReversalSpread). 13 strategies ON.
- **Structural exits** (`execution/exit_signals.py`) live in learning (paper) only.
- **Tools:** `scripts/strategy_pnl.py` (epoch-aware live ranking),
  `scripts/seed_test_trades.py` (sandbox seeding).

## The core problem this plan solves

Every refinement decision so far rests on **6–40 live trades** — statistically noise.
The money question is unanswered: *does any strategy have positive expectancy after
real costs?* **Principle: measure before tuning.** No strategy/exit/param change is
"done" until validated on a meaningful sample (backtest history or ≥~50 forward trades),
ranked by **expectancy (₹/trade)**, not win rate.

---

## Workstream 1 — Backtest backbone (DO FIRST; highest leverage)

**Goal:** a repeatable "rank every strategy by net expectancy over history" report —
the gate for all other decisions and the prerequisite for going live.

**Why:** turns "I think TrendSpread is good" into "TrendSpread: +0.18R over 940 trades,
14% maxDD" — and de-risks the eventual `PAPER_TRADING=false` flip.

**Tasks:**
1. Audit `backtesting/backtest_engine.py` + `backtesting/performance.py` — confirm it
   replays a strategy over historical candles and produces trades.
2. Ensure it uses the SAME shared logic as live: `execution/sizing`, `execution/fees`
   (net-of-cost P&L), `execution/exit_policy` + `execution/exit_signals` for exits.
   If it forks any of these, route it through the single sources (guardrail).
3. New `scripts/backtest_all.py`: run every enabled strategy over 1–2 years per symbol;
   output a table: trades, win%, **expectancy ₹/R**, profit factor, max drawdown,
   avg MAE/MFE, exit-reason histogram. Sort by expectancy.
4. **Walk-forward**: optimize params on window N, test on N+1 (no curve-fitting).
5. Wire results into the Backtest dashboard tab (reuse, don't clone).

**Acceptance:** one command ranks all strategies by net expectancy with drawdown, on
years of data; numbers reconcile with the live `strategy_pnl.py` shape.

**Files:** `backtesting/*`, `scripts/backtest_all.py`, `execution/{sizing,fees,exit_policy,exit_signals}.py`.

## Workstream 2 — Regime gating (likely biggest P&L lever)

**Goal:** gate each strategy to the market regime it works in, instead of permanently
disabling it.

**Why:** the disabled losers were all mean-reversion; winners were trend. Mean-reversion
bleeds in trends, trend bleeds in chop. A reversion strategy that's *off in trends* may
flip to profitable. Static keep/kill throws away usable strategies.

**Tasks:**
1. Use/extend the regime classifier (enhancement spec TASK 3; check
   `config/strategy_matrix.py`, `/market/strategy-matrix`) → TREND_UP/DOWN, RANGE, CHOP,
   HIGH_VOL via ADX / VWAP behavior / ATR expansion.
2. Add a regime dimension to `config/strategy_toggles` (or a `strategy_matrix`): each
   strategy enabled per-regime, config-driven.
3. Gate at the same single points the on/off toggle uses (`_try_strategy`, learning loops,
   commodity loop) — `enabled AND regime_ok`.
4. **Validate with Workstream 1**: backtest each strategy split by regime; confirm the
   gating improves expectancy before enabling.

**Acceptance:** backtest shows per-regime expectancy; strategies only fire in their
profitable regimes; net expectancy improves vs ungated.

## Workstream 3 — Exit tuning via backtest

**Goal:** tune the structural exits (`exit_signals.json`) and resolve the trailing-method
split, measured not guessed.

**Why:** exits are ~half the edge (the "went green then round-tripped" pain). Current
thresholds (arm %, ATR mult, swing lookback, RSI fade) are guesses.

**Tasks:**
1. Backtest each exit family + threshold per strategy (grid/sensitivity) on history.
2. Resolve U6 leftover: merge the two trailing methods (position_manager fixed-R +
   dynamic-target vs learning Chandelier/ATR) into one, chosen by backtest.
3. Promote structural exits into `position_manager` (live path) once paper + backtest
   validate them.

**Acceptance:** exit config is backtest-justified; one trailing method; live + learning
use the identical exit stack.

## Workstream 4 — Data-quality & silent-failure monitoring (reliability/trust)

**Goal:** the bot shouts when something breaks silently.

**Why:** the Alpaca bar bug dropped *every* US bar silently; the ₹66L wallet corruption
went unnoticed. A bot you'll trust with money must self-detect these.

**Tasks:**
1. Feed-staleness alerts: per-segment "no tick in X min during session" → alert.
2. Sanity bounds: P&L / wallet / position-size outside plausible ranges → alert (would
   have caught the ₹66L wallet and the index-as-premium exit).
3. Strategy-silence: an enabled strategy generating 0 signals for N sessions → flag.
4. Reuse existing `analysis/signal_health` + `alert_service`; add a small anomaly layer.

**Acceptance:** a simulated stale feed / impossible P&L raises an alert.

---

## First task next week — validate the S/R entry filter (concrete kickoff)

The support/resistance room filter (`analysis/support_resistance.py` + `config/entry_filters.json`,
wired into ShortTrend/TrendFollow, 2026-06-24) is the first thing to run through the
backtest loop, because it's already live and shaping entries:
1. **A/B backtest** ShortTrend + TrendFollow over history **with vs without** the S/R
   filter → does net expectancy / win-rate / drawdown improve? (Confirms the idea.)
2. **Walk-forward** the S/R thresholds (`pivot_window`, `min_room_atr`, `level_buffer_pct`,
   `oversold_floor`/`overbought_ceiling`) — optimise on window N, test on N+1 → confirm
   they generalise (not curve-fit). Keep the filter only if WF out-of-sample holds.
This doubles as the first real exercise of the Workstream-1 harness.

## Sequencing

1. **Workstream 1 first** — nothing else can be trusted without it.
2. Then **Workstream 3** (exits) and **Workstream 2** (regime) — both depend on the
   backtest harness; do exits first (faster payoff) or in parallel.
3. **Workstream 4** alongside (independent, low-risk).
4. Only after 1–3 show positive net expectancy: curate `LIVE_STRATEGIES`, then plan the
   `PAPER_TRADING=false` go-live (separate checklist).

## Non-goals (resist these)

- Adding more strategies (need *validated* ones, not more).
- New asset classes / going live before edge is proven.
- UI polish beyond retiring the two leftover per-tab tables (`LearningTradesTable`,
  `CommodityTradesTable`).
- Tuning anything on live samples < ~50 trades.

## Open questions for kickoff

1. How much clean history is available per segment (NSE eq/options, MCX, US) for backtests?
2. Backtest fill assumptions — slippage model for options (premium) vs equity?
3. Regime classifier: build new or is `config/strategy_matrix` usable as-is?
4. Go-live capital + per-trade risk for the final sizing once edge is proven.
