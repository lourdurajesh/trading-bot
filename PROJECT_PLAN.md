# AlphaLens Trading Bot — Project Plan, Cleanup & Roadmap

> Living document. Update the status boxes as work lands. Created 2026-06-16.

---

## 0. How to use this document

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

**Working rules (do not break these):**
1. **Local changes only.** Never edit the server directly. We deploy in batches at a logical
   completion point, then verify.
2. **No static values.** Every tunable number lives in `.env` / JSON / DB and is surfaced for
   configuration — never hard-coded.
3. **Measure before money.** No strategy goes to `PAPER_TRADING=false` until it shows positive
   expectancy on out-of-sample data.
4. **One logical change per commit**, with a clear message. Mark files for deletion in this doc
   first; delete only after the verification step for that item is checked.

**Server facts:** bot at `http://140.245.26.69:8000`, host `ubuntu@140.245.26.69`.
Entry points — systemd `trading-bot.service` → `watchdog.py` (supervises `main.py`);
`trading-dashboard.service` → static server on :3000; cron → `generate_token.py`,
`nightly_agent.py`, `weekly_agent.py`.

---

## 1. Current state (2026-06-16)

**Fixed today (committed, deployed):**
- `[x]` Intelligence layer fail-open — AI outage no longer halts the pipeline (`55db503`).
- `[x]` NIFTY50 / all-index option-chain expiry parsing — valid-epoch retry (`474311c`).
- `[x]` MCX futures no longer traded via equity strategies (`13f2bc7`).
- `[x]` First production trade in 2 months placed (BANKNIFTY paper).

**Reality check:**
- Everything is **paper** (`PAPER_TRADING=true`). No real money has traded.
- Production `trades` table: was empty for 2 months, now opening trades.
- Commodity system: 131 paper trades, **P&L is Black-Scholes estimate (fictional)**, 2 of 3
  strategies net-losing.
- **No strategy has a validated, out-of-sample edge.** We do not yet know any true win rate.

---

## 2. The goal & the math

**Target:** net **₹1,000/day** average, intraday/options first, then AI swing, then US market.

**Why it's an engineering target, not a fantasy** — using a real live quote
(`NIFTY 24250CE @ ₹65.5`, lot 75 = ₹4,912/lot, exits SL −30% / target +50%, R:R 1.67):

| Win rate | Expectancy / trade | Trades/day for ₹1,000 |
|---:|---:|---:|
| 45% | +₹214 | ~4.7 |
| 50% | +₹411 | ~2.4 |
| 55% | +₹608 | ~1.6 |

Breakeven win rate ≈ **39.5%** (after ~₹80 round-trip cost). So ₹1,000/day = **2–3
winning-biased index-option trades/day at a 45–55% win rate.** The whole question reduces to:
*do the strategies hold ≥45% win rate at 1.67 R:R on unseen data?* — which we must **measure**.

**Definition of done for the goal:** a strategy set that, on walk-forward out-of-sample
backtest **and** 2+ weeks of honest paper trading, produces ≥ ₹1,000/day median with a
risk-of-ruin under an agreed threshold — *then* go live small.

---

# PART A — PROJECT CLEANUP

Goal: make it obvious what is used vs. dead, so future work is fast and safe.
Method used: AST import-graph reachability from the **real** production entry points
(`watchdog`, `main`, `generate_token`, `nightly_agent`, `weekly_agent`, `api.dashboard_api`).

Result: **85 production modules · 9 manual/CLI tools · ~6 genuinely removable items.**

## A1. Codebase map

- **Production (85):** reachable from an entry point. Keep.
- **Manual/CLI tools (9):** have `__main__`, run by hand or cron-adjacent. Keep the useful ones,
  archive the one-time ones (see A3).
- **Dashboard-only analytics:** `analysis/{breadth_engine,iv_percentile,opening_range,
  regime_engine,vwap_engine}.py`, `config/strategy_matrix.py`, `daily_plan.py`, `daily_review.py`,
  `journal_analyser.py`, `portfolio_analyser.py` — imported **only** by `api/dashboard_api.py`.
  Used (render dashboard panels) but **not in the trade-decision path**. Keep; note for A4.

## A2. Files marked for DELETION

> Delete only after the **Verify** box is checked. All deletions are local; released later.

| # | File | Why | Verify before delete | Status |
|---|------|-----|----------------------|--------|
| D1 | `fix_entry_prices.py` | One-shot migration (BS→real entry prices). Job done; no `__main__`, imported by nothing. | Confirm no learning trades still need backfill. | `[ ]` |
| D2 | `validate_edges.py` | Superseded by `validate_edges_v2.py` (v2 docstring states v1's model is wrong). | Diff v1 vs v2; confirm v2 covers all 3 edges. | `[ ]` |
| D3 | `/tmp/depscan*.py`, `scripts/diag_chain.py` | Throwaway diagnostics (diag already removed). | — (already deleted) | `[x]` |
| D4 | `.claude/worktrees/modest-gauss-007baa/` | Stale git worktree (full duplicate tree) from an earlier spawned task. | `git worktree remove` once branch `claude/modest-gauss-007baa` is merged/abandoned. | `[ ]` |

## A3. Files to ARCHIVE (move to `scripts/_archive/`, do not hard-delete)

One-time migrations already applied (their tables exist in prod). Keep for disaster-rebuild.

| # | File | Status |
|---|------|--------|
| R1 | `scripts/migrate_audit_tables.py` | `[ ]` |
| R2 | `scripts/migrate_commodity_instruments.py` | `[ ]` |
| R3 | `scripts/cleanup_index_trades.py` | `[ ]` |
| R4 | `scripts/seed_fii_history.py` | `[ ]` |

## A4. Consolidation candidates (review, not urgent)

| # | Item | Question | Status |
|---|------|----------|--------|
| C1 | `analysis/regime_detector.py` (trading) vs `analysis/regime_engine.py` (dashboard) | Two regime systems — can the dashboard reuse the trading one? | `[ ]` |
| C2 | `run_backtest.py` vs `run_full_backtest.py` | Overlap? Keep one canonical backtest entry. | `[ ]` |
| C3 | `backtesting/monte_carlo.py`, `backtesting/walk_forward.py` | **Not dead — wire these into the backtest phase (B3).** Risk-of-ruin + out-of-sample. | `[ ]` |
| C4 | `strategies/iron_condor.py` + `strategies/options_strategy_config.py` | IronCondor is disabled in the selector (1% historical win rate). Keep code, confirm it never fires. | `[ ]` |
| C5 | Dormant-but-wired strategies | `gap_fade`, `momentum_reversal`, `options_income`, `short_trend` — imported by selector; confirm each actually fires and is wanted. | `[ ]` |

## A5. Cleanup execution steps

- `[ ]` **A5.1** Verify & delete D1, D2 (local). Commit: `chore: remove dead one-shot scripts`.
- `[ ]` **A5.2** Create `scripts/_archive/`, move R1–R4 there. Commit: `chore: archive applied migrations`.
- `[ ]` **A5.3** `git worktree remove` D4 after branch disposition decided.
- `[ ]` **A5.4** Resolve C1–C5 (separate small PRs; defer if not blocking).
- `[ ]` **A5.5** Add a top-level `ARCHITECTURE.md` mapping each package → role (so this never rots again).

---

# PART B — THE ₹1,000/DAY WORKFLOW

Sequenced so each phase produces a number the next phase needs. Do not skip ahead — tuning
before measuring is guessing.

## B1. Measurement foundation (you cannot optimize what you can't measure)

- `[ ]` **B1.1 Real P&L only.** Stop reporting `pnl_approx`/Black-Scholes as results. Mark every
  trade to real LTP/fill; label any estimate explicitly as simulated in UI + DB.
  Files: `commodity_options_learning.py`, `learning_engine.py`, `dashboard/index.html`.
- `[ ]` **B1.2 Verify lot sizes.** `config/nse_instruments.json` shows BANKNIFTY=15 (real 35),
  NIFTY mismatch (25 vs 75). Wrong lot = wrong sizing **and** wrong P&L. Correct all against
  current NSE; keep them in the JSON (no hard-coding).
- `[ ]` **B1.3 Trade journal schema.** Ensure every closed trade stores: entry/exit fill, fees,
  R-multiple, strategy, regime, signals/day. This is the raw material for B3/B6.
- **Exit criteria:** dashboard P&L equals a hand-computed P&L for 5 sample trades.

## B2. Execution correctness (place the right trade at the right size)

- `[ ]` **B2.1 Position sizing fits the budget.** Today ~9,000 `risk_budget` rejections because
  size overshoots caps. Change: size **down** to the max that fits per-trade & daily risk,
  instead of rejecting. Files: `risk/daily_risk_budget.py`, `risk/options_risk.py`,
  `risk/portfolio_tracker.py`.
- `[ ]` **B2.2 Real cost model.** Brokerage + STT + exchange + spread per round trip, configurable
  in `.env`/JSON, applied to expectancy and to exit math.
- `[ ]` **B2.3 Fill realism.** Use bid/ask, reject illiquid strikes (fixes the OI=0 false
  rejects — same chain-data root we just touched). File: `execution/options_executor.py`,
  `analysis/spread_quality.py`.
- **Exit criteria:** a signal that should fire is placed at a sane size; rejection log near-zero.

## B3. Edge discovery — BACKTEST (the decisive phase)

- `[ ]` **B3.1** Make `run_backtest.py` the canonical entry; ensure it pulls ≥ 1–2 yrs history
  per strategy (`backtesting/data_fetcher.py`).
- `[ ]` **B3.2** Wire **walk-forward** validation (`backtesting/walk_forward.py`) — train/test
  splits so results are out-of-sample, not curve-fit.
- `[ ]` **B3.3** Wire **Monte Carlo** (`backtesting/monte_carlo.py`) for risk-of-ruin & drawdown
  distribution per strategy.
- `[ ]` **B3.4** Produce a per-strategy report: **win rate, avg win/loss, expectancy,
  trades/day, max drawdown, risk-of-ruin.** Plug each into the §2 table → know exactly how many
  trades/day ₹1,000 needs, or that a strategy has no edge and must be cut.
- **Exit criteria:** a ranked table of strategies by out-of-sample expectancy. **This is the
  go/no-go gate for the whole goal.**

## B4. Edge improvement (raise expectancy on the survivors)

- `[ ]` **B4.1** Disable net-losing strategies (commodity BreakoutSpread @20%, RSIReversal @33%).
- `[ ]` **B4.2** Per-strategy tune exits (R:R, trailing, time-stop) and entry filters
  (RSI band, regime, IV) to push expectancy up — re-backtest each change.
- `[ ]` **B4.3** Signal frequency: measure signals/day; if too sparse for ₹1,000, add/loosen
  setups (only those that backtest positive).
- **Exit criteria:** survivor set whose combined expectancy × frequency ≥ ₹1,000/day in backtest.

## B5. Daily targeting & risk governor (don't give back the ₹1,000)

- `[ ]` **B5.1** Daily P&L governor: at **+₹1,000** tighten/stop new entries; at **−₹X** halt
  for the day. Configurable. New small module under `risk/`.
- `[ ]` **B5.2** Fixed fractional per-trade risk cap so a normal losing streak can't blow the
  runway (risk-of-ruin from B3.3 sets the fraction).
- **Exit criteria:** governor enforces the daily ceiling/floor in a simulated multi-trade day.

## B6. Honest paper validation → go live small

- `[ ]` **B6.1** Run the survivor set in paper with **real measurement** for ≥ 2 weeks.
- `[ ]` **B6.2** Compare live-paper stats to backtest (within tolerance = edge is real).
- `[ ]` **B6.3** Go live with **minimum size** (1 lot), daily loss cap tight, monitor.
- `[ ]` **B6.4** Scale size only after live matches paper.
- **Exit criteria:** live small matches paper/backtest; then scale.

---

# PART C — US MARKET TRADING (later stage)

Defer until Part B is producing ₹1,000/day reliably on NSE. Scaffolding already exists.

## C1. Current US scaffolding (already present)
- `execution/alpaca_broker.py` (currently simulation — no API key), `data/alpaca_stream.py`,
  US symbols in `config/watchlist.py` (`ALL_US_SYMBOLS`), `NYSE_OPEN_IST/CLOSE_IST` in settings.

## C2. Phases
- `[ ]` **C2.1** Activate Alpaca (paper) — keys in `.env`, verify stream + broker init.
- `[ ]` **C2.2** Confirm which strategies apply to US equities (trend/mean-reversion/momentum)
  vs NSE-options-only ones; route via `strategy_selector`.
- `[ ]` **C2.3** US market-hours gating (IST conversion already stubbed), holiday calendar.
- `[ ]` **C2.4** US cost model (commission-free but spread/SEC fees), position sizing in USD.
- `[ ]` **C2.5** Backtest US strategies (reuse B3 harness) → out-of-sample edge before paper.
- `[ ]` **C2.6** Honest US paper run → go live small (mirror B6).
- `[ ]` **C2.7** Options on US underlyings (later) — only if NSE options proven first.

---

## Appendix — Quick reference: production module inventory

**Entry points:** `watchdog.py` → `main.py`; cron `generate_token.py`, `nightly_agent.py`,
`weekly_agent.py`; dashboard `api/dashboard_api.py`.

**Core trade loop:** `strategies/strategy_selector.py` → strategies → `intelligence/
intelligence_engine.py` → `execution/order_manager.py` → `execution/position_manager.py` →
`risk/*` → `risk/portfolio_tracker.py`.

**Data:** `data/{data_store,fyers_stream,alpaca_stream}.py`,
`execution/{fyers_broker,alpaca_broker,options_executor}.py`.

**Learning/MCX:** `learning_engine.py`, `commodity_options_learning.py`,
`strategies/mcx_base.py`, `strategies/mcx/{trend_spread,breakout_spread,rsi_reversal}.py`.

**Manual tools (keep):** `run_backtest.py`, `run_full_backtest.py`, `run_analysis.py`,
`validate_edges_v2.py`, `validate_fo_leverage.py`, `weekly_review.py`, `tests/test_pipeline.py`.
