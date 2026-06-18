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
| D1 | `fix_entry_prices.py` | One-shot migration (BS→real entry prices). Job done; no `__main__`, imported by nothing. | Confirm no learning trades still need backfill. | `[x]` deleted |
| D2 | `validate_edges.py` | Superseded by `validate_edges_v2.py` (v2 docstring states v1's model is wrong). | Diff v1 vs v2; confirm v2 covers all 3 edges. | `[x]` deleted |
| D3 | `/tmp/depscan*.py`, `scripts/diag_chain.py` | Throwaway diagnostics (diag already removed). | — (already deleted) | `[x]` |
| D4 | `.claude/worktrees/modest-gauss-007baa/` | Stale git worktree (full duplicate tree) from an earlier spawned task. | `git worktree remove` once branch `claude/modest-gauss-007baa` is merged/abandoned. | `[ ]` |

## A3. Files to ARCHIVE (move to `scripts/_archive/`, do not hard-delete)

One-time migrations already applied (their tables exist in prod). Keep for disaster-rebuild.

| # | File | Status |
|---|------|--------|
| R1 | `scripts/migrate_audit_tables.py` → `scripts/_archive/` | `[x]` |
| R2 | `scripts/migrate_commodity_instruments.py` → `scripts/_archive/` | `[x]` |
| R3 | `scripts/cleanup_index_trades.py` → `scripts/_archive/` | `[x]` |
| R4 | `scripts/seed_fii_history.py` → `scripts/_archive/` | `[x]` |

## A4. Consolidation candidates (review, not urgent)

| # | Item | Question | Status |
|---|------|----------|--------|
| C1 | `analysis/regime_detector.py` (trading) vs `analysis/regime_engine.py` (dashboard) | Two regime systems — can the dashboard reuse the trading one? | `[ ]` |
| C2 | `run_backtest.py` vs `run_full_backtest.py` | Overlap? Keep one canonical backtest entry. | `[ ]` |
| C3 | `backtesting/monte_carlo.py`, `backtesting/walk_forward.py` | **Not dead — wire these into the backtest phase (B3).** Risk-of-ruin + out-of-sample. | `[ ]` |
| C4 | `strategies/iron_condor.py` + `strategies/options_strategy_config.py` | IronCondor is disabled in the selector (1% historical win rate). Keep code, confirm it never fires. | `[ ]` |
| C5 | Dormant-but-wired strategies | `gap_fade`, `momentum_reversal`, `options_income`, `short_trend` — imported by selector; confirm each actually fires and is wanted. | `[ ]` |

## A5. Cleanup execution steps

- `[x]` **A5.1** Verify & delete D1, D2 (local). Done.
- `[x]` **A5.2** Create `scripts/_archive/`, move R1–R4 there. Done.
- `[ ]` **A5.3** `git worktree remove` D4 after branch disposition decided.
- `[ ]` **A5.4** Resolve C1–C5 (separate small PRs; defer if not blocking).
- `[x]` **A5.5** Documentation added: `docs/FILE_INDEX.md` (every module + role + proposed target
  structure) and `docs/STRATEGIES.md` (all strategies + status/performance for review).
- `[ ]` **A5.6** Execute the physical root-folder restructure per `docs/FILE_INDEX.md` proposal
  (dedicated PR, coordinate with server cron/systemd path updates). Not mixed with functional work.

---

# PART B — THE ₹1,000/DAY WORKFLOW

Sequenced so each phase produces a number the next phase needs. Do not skip ahead — tuning
before measuring is guessing.

## B1. Measurement foundation (you cannot optimize what you can't measure)

- `[x]` **B1.1 Real P&L. DONE.** Commodity P&L was `spot_move × delta` (estimate) even for
  live-chain trades. Now at close the engine marks the spread to its **real** current value from
  the live chain (`CHAIN_MARK`), falling back to a **labeled** `ESTIMATE` only when the chain is
  unavailable — recorded in a new `pnl_source` column and surfaced honestly in the dashboard.
  NSE `learning_trades` options P&L was already real (option LTP at entry & exit). Historical
  commodity trades are now correctly labeled `ESTIMATE`. See `docs/DATA_MODEL.md`.
- `[x]` **B1.2 Verify lot sizes. DONE.** Found every index lot wrong (NIFTY 25→65, BANKNIFTY
  15→30, FINNIFTY 25→60, MIDCPNIFTY 75→120) plus several equities. Pulled authoritative
  nearest-expiry lots from the Fyers public symbol master and corrected
  `config/nse_instruments.json`. Built `scripts/fetch_lot_sizes.py` so lots auto-refresh from
  source (run after each quarterly NSE revision) — no more static guesses.
- `[x]` **B1.3 Trade journal schema. DONE.** Documented all three trade tables and their fields
  in `docs/DATA_MODEL.md`; confirmed they capture strategy, fills, R-multiple, DTE, lots, exit
  reason, and timestamps for backtesting. Added `fees` (column ready; populated in B2.2 cost
  model) and `pnl_source`. Aggregates (signals/day, win rate) are computed at analysis time.
- **Exit criteria:** dashboard P&L equals a hand-computed P&L for 5 sample trades — *verify after
  deploy, on the next CHAIN_MARK closes (current session is paper/off-hours).*

## B2. Execution correctness (place the right trade at the right size)

- `[x]` **B2.1 Position sizing fits the budget. DONE.** Root cause: a *single* lot of a high-value
  commodity (e.g. SILVER ≈ ₹43k max-loss = 8.7% of ₹5L) exceeds the risk caps → ~9,000 rejections,
  zero learning data on those instruments. Fix (per decision: *micros + advisory-paper*):
  (1) **paper mode** treats the risk-budget cap as **advisory** — records the trade so we still
  learn the edge (measured by R-multiple, not rupees); (2) **real mode** sizes lots **down** to fit
  the per-strategy budget and **skips** if even 1 lot is too big (use the mini/micro contract).
  Also set server `RISK_PER_TRADE_PCT 10.0 → 1.5` (was above the 3% hard limit).
  Files: `commodity_options_learning.py` (`_compute_lots`, risk-gate call site), server `.env`.
  *Follow-up:* enable mini/micro contracts (SILVERMIC, GOLDM) in the instruments table so real
  mode has a fitting alternative when fulls are skipped.
- `[x]` **B2.2 Real cost model. DONE.** Built `analysis/cost_model.py` (brokerage + STT/CTT +
  exchange + SEBI + stamp + GST) with rates in `config/cost_rates.json` (editable; no hard-coding).
  Commodity P&L is now **net** of costs with `fees` stored per trade; NSE-options learning replaced
  the flat ₹40 with the real model (~₹58/1-lot NIFTY round trip, ~₹200/1-lot high-value MCX spread).
  Files: `analysis/cost_model.py`, `config/cost_rates.json`, `commodity_options_learning.py`,
  `learning_engine.py`.
- `[x]` **B2.3 Fill realism + MCX chain parser. DONE.** Investigation found the MCX chain is in
  the same **flat format** that broke NIFTY — so the commodity engine's `_chain_lookup` (which
  expected the paired format) silently fell back to Black-Scholes and **mislabeled** trades
  `live_chain`. This also meant B1.1's `CHAIN_MARK` could never fire. Rewrote `_chain_lookup` +
  added `_leg_data` to parse the flat rows (real ltp/bid/ask/OI; no DTE refetch needed — the FUT
  symbol pins the expiry). Now: real prices verified live (CRUDEOIL ATM 395 @ 1808 OI, etc.),
  `CHAIN_MARK` fires, net_debit uses **fill realism** (buy ATM at ask, sell OTM at bid), the
  liquidity gate uses **real OI**, and `data_source` is truthful (`live_chain` only when actually
  priced from chain). File: `commodity_options_learning.py`.
  *Tuning note:* `MIN_OI_LONG`/`MIN_OI_SHORT` (500/300) may be high for some commodity strikes
  (e.g. COPPER ATM ≈ 303 OI) — env-configurable; revisit during B4 tuning.
- ~~`execution/options_executor.py`~~ (NIFTY path already fixed earlier); `analysis/spread_quality.py`
  now receives real OI/bid/ask.
- **Exit criteria:** a signal that should fire is placed at a sane size; rejection log near-zero.
- **B2 COMPLETE** — execution is now correct: sizing fits the budget, P&L is net of real costs,
  and prices/liquidity come from the real chain.

## B3. Edge discovery — BACKTEST (the decisive phase)

**Findings (2026-06-17):** substantial infra already exists (`backtest_engine` computes
win-rate/Sharpe/DD/expectancy; `walk_forward` + `monte_carlo` present). Key issues uncovered:
1. It ran on **daily** bars while strategies are intraday (1H/15m) → meaningless samples. Fixed.
2. **1H history** is plentiful (~3 yrs); **15m** is Fyers-capped (~57 days) — needs chunked fetch.
3. The bar-replay was **O(n²)** (fed the whole growing history each bar) → intraday timed out.
   Capped to a 300-bar window → linear, now completes (~1m48s per symbol-strategy on 2yr/1H).
4. Options/MCX can't use historical chains → use the **signal-simulation** path
   (`run_full_backtest.simulate_directional_options`).

- `[~]` **B3.1** Canonical runner done: `run_backtest.py --timeframe` (default 1H) + all 1H equity
  strategies; engine made tractable. **Remaining:** per-eval DataFrame rebuild still slow for the
  full 42-symbol watchlist (~hrs) — vectorising indicators is the next speed lever; 15m chunked
  fetch for MeanReversion/GapFade. First real run (10 symbols, 2yr, 1H) in progress.
- `[ ]` **B3.2** Wire **walk-forward** (`backtesting/walk_forward.py`) — out-of-sample splits.
- `[ ]` **B3.3** Wire **Monte Carlo** (`backtesting/monte_carlo.py`) — risk-of-ruin & DD dist.
- `[ ]` **B3.4** Per-strategy report: **win rate, avg win/loss, expectancy, trades/day, max DD,
  risk-of-ruin** → plug into the §2 table.
- `[ ]` **B3.5** Equity backtest is a *proxy* — the ₹1,000/day engine is index OPTIONS + MCX
  spreads. Wire `simulate_directional_options` + an MCX-spread simulation into the report.
- **Exit criteria:** a ranked table of strategies by out-of-sample expectancy. **Go/no-go gate.**
- **POOLED results (2yr, 1H, trade-weighted — trustworthy):**
  | Strategy | Trades | Win% | PF | Expectancy | Total P&L |
  |---|---|---|---|---|---|
  | MeanReversion | 54 | 43% | 0.58 | −₹1,035 | −₹55,898 |
  | TrendFollow | 169 | 44% | 0.79 | −₹705 | −₹119,136 |
  | ShortTrend | 43 | 49% | 0.96 | −₹70 | −₹3,016 |
  | MomentumReversal | 4 | 0% | 0.00 | −₹7,074 | −₹28,296 |
  **All four equity strategies have negative expectancy & PF < 1** — none has an edge as
  configured. (Earlier "Avg PF 2.24" was the averaging bug; pooled PF 0.58 is the truth.)
  These are the equity PROXY — the ₹1,000/day engine is index options + MCX (B3.5). Feeds B4.

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

## Phase U — Unify execution architecture (MUST precede go-live)

One pipeline for Equity / Index-Options / MCX; asset differences in adapters, not duplicated
control flow. Full map: `docs/ARCHITECTURE_AUDIT.md`. Guardrail: `trading-architecture` skill.

- `[x]` **U1 Unify the option-chain layer.** (U1a + U1b done — ONE parser for NSE + MCX.)
  - `[x]` **U1a** Shared `analysis/options_chain.py` (one fetch + parser, flat+paired); **MCX
    migrated** onto it (deleted its duplicate parser). Verified live + unit-tested.
  - `[x]` **U1b** Migrate `execution/options_executor.py` (NSE) onto `chain_service` →
    exactly one parser. Higher risk (just-fixed NSE path) — done with NIFTY/BANKNIFTY/FINNIFTY
    live verification.
    - `[x]` **slice-1** NSE *fetch* routed through `chain_service` (commit `be03e3e`).
    - `[x]` **slice-2** NSE *parser* retired: `_select_from_chain` rewritten on
      `chain_service.strikes`/`leg_quote`/`synthetic_delta`; deleted `_normalise_layout_b/_c`,
      `_underlying_from_rows`, `_pick_expiry_epoch`, `_get_atm_iv`. Dashboard chain endpoints
      moved to the flat layout. **Improves NSE**: PCR is now real (was hardcoded 0). Verified
      live — NIFTY/BANKNIFTY/FINNIFTY selections byte-identical to pre-change (sim=False), and
      identical to the old code on the DTE-refetch fallback path too.
- `[~]` **U2** Unify the signal type — all strategies → `Signal` (retire `dict` / `MCXSignalResult`).
  - `[x]` **slice-A** Learning equity strategies (`simple_rsi`, `simple_momentum`) now return
    `Signal`; added generic `Signal.meta`; retired the `_sig_to_learning_dict` dict branch. (db25c01)
  - `[ ]` **slice-B** MCX: retire `MCXSignalResult` — `MCXStrategy.generate_signal(df,spot,now)`
    returns a `Signal` (indicator fields → `Signal.meta`); `commodity_options._evaluate` reads
    `Signal`. Keep the `(df,spot,now)` call signature as the MCX adapter (full data-access unify is U6).
- `[ ]` **U3** Unify exit management — one `PositionManager` (SL/target/trail/EOD/DTE) + adapter
  for spot-vs-LTP stop semantics.
- `[ ]` **U4** Unify sizing/risk — one `RiskSizer` (B2.1 size-to-fit) for all asset classes.
- `[ ]` **U5** Unify order placement + ledger — one `OrderRouter` + one trades store (segment column).
- `[ ]` **U6** Collapse the three `run_cycle`s into one orchestrator over instrument+adapter list.

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
