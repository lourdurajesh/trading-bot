# File Index

> Every module and what it does, grouped by role. Updated 2026-06-21 (audit: root decluttered,
> backtest runners moved to `scripts/`, one-off dev backtests archived, Phase-U unification
> modules added — `execution/{sizing,ledger,exit_rules,order_router}.py`, `api/segment_readers.py`,
> `strategies/reversal_core.py`).

**Entry points (how the bot actually runs):**
- `watchdog.py` → systemd `trading-bot.service`; supervises and restarts `main.py`.
- `main.py` → master orchestrator: streams, strategy loop, dashboard thread, scheduled jobs.
- `generate_token.py`, `nightly_agent.py`, `weekly_agent.py` → cron jobs.
- `api/dashboard_api.py` → FastAPI dashboard (started by `main.py` on :8000).

---

## Root — orchestration & engines

| File | Purpose |
|------|---------|
| `main.py` | Master loop: boot brokers/streams, run strategy_selector + learning + commodity cycles, scheduled hooks (conviction score, OI snapshot, FII collect, token refresh). |
| `watchdog.py` | Process supervisor (systemd entry) — keeps `main.py` alive. |
| `learning_engine.py` | Paper "learning lab": runs simple equity strategies + index-options path on a watchlist, logs labeled trades. |
| `commodity_options_learning.py` | MCX commodity-options engine: signals, spread construction, exits, P&L for `strategies/mcx/`. |
| `us_reversal.py` | US index-ETF (SPY/QQQ) Reversal options PAPER engine; logic shared via `strategies/reversal_core.py`; a segment of the unified ledger (folds fully into the pipeline at U6/U7). |
| `token_manager.py` | Fyers token lifecycle — refresh, health check, proactive pre-6 AM renewal. |
| `generate_token.py` | One-time/daily Fyers auth token generation (cron). |
| `audit_log.py` | Append-only audit trail (bot events, rejections). |

## Root — scheduled agents & reviews (cron / manual)

| File | Purpose |
|------|---------|
| `nightly_agent.py` | Nightly: dynamic watchlist, IV history, housekeeping (cron). |
| `weekly_agent.py` | Weekly maintenance/analytics (cron). |
| `daily_plan.py` | Pre-market plan (consumed by dashboard). |
| `daily_review.py` | End-of-day review (dashboard). |
| `weekly_review.py` | Weekly performance review (manual). |
| `journal_analyser.py` | Trade-journal analytics (dashboard). |
| `portfolio_analyser.py` | Portfolio analytics (dashboard). |
| `system_health.py` | Health snapshot. |

## Root — other

| File | Purpose |
|------|---------|
| `paper_trading.py` | Paper-trading harness (imported by `position_manager`/`learning_engine`; mirrors live trades to a paper wallet). |

> Manual backtest/analysis runners moved to `scripts/` (2026-06-21): `run_backtest.py`,
> `run_full_backtest.py`, `run_analysis.py` — run as `PYTHONPATH=. python scripts/run_backtest.py …`.
> `validate_edges_v2.py` / `validate_fo_leverage.py` were deleted (stale one-off analyses).

## `strategies/` — see [STRATEGIES.md](STRATEGIES.md) for full logic

| File | Purpose |
|------|---------|
| `strategy_selector.py` | Routes each symbol → strategy by regime; runs intelligence + risk gates; submits orders. |
| `base_strategy.py` | Base class: data access, Signal/Direction types, logging. |
| `directional_options.py` | Index single long call/put (TRENDING/BREAKOUT/VOLATILE). |
| `institutional_momentum.py` | Conviction-driven ATM index options. |
| `trend_follow.py` / `short_trend.py` | Equity long-breakout / short-breakdown. |
| `mean_reversion.py` / `momentum_reversal.py` | Range fade / extreme-RSI snap-back. |
| `gap_fade.py` | Opening-gap fade (9:15–9:45). |
| `options_income.py` | Short strangle (premium selling). |
| `iron_condor.py` + `options_strategy_config.py` | Defined-risk condor (**disabled**). |
| `simple_rsi.py` / `simple_momentum.py` | Learning-lab baselines (paper only). |
| `reversal_core.py` | **SINGLE SOURCE** for the Reversal pattern + exit + trailing-stop maths (shared by NSE live, US, learning exits, and the archived backtests). |
| `reversal_5m.py` | Live NSE index Reversal (Reversal5m / Reversal3m) on 5m/3m bars. |
| `mcx_base.py` | Base class for MCX spread strategies. |
| `mcx/trend_spread.py` · `mcx/breakout_spread.py` · `mcx/rsi_reversal.py` | MCX commodity-option spreads. |

## `execution/` — broker & order plumbing

| File | Purpose |
|------|---------|
| `fyers_broker.py` | Fyers REST wrapper: orders, positions, funds, chain fetch. |
| `alpaca_broker.py` | Alpaca (US) wrapper — simulation until keyed (Part C). |
| `options_executor.py` | Option-chain fetch/parse, expiry+strike selection, lot/strike resolution, BS fallback. |
| `order_manager.py` | Signal → order: risk submit, fills, alerts, position open. |
| `position_manager.py` | Open-position monitor: stops/targets/trailing, EOD/DTE/time exits. |
| `exit_rules.py` | **ONE** option-exit decision module — premium SL/target, underlying trail, spot-breach, breakdown (U3; used by learning/US/MCX/production). |
| `order_router.py` | **ONE** broker-selection + place/cancel entry — Fyers (NSE/MCX) / Alpaca (US) (U5). |
| `sizing.py` | **ONE** size-to-fit sizer — lots/shares for every asset class (U4). |
| `ledger.py` | **ONE** trades store: the `ledger` table (+ `segment`) behind read-only compat views `learning_trades`/`commodity_learning_trades`/`us_reversal_trades` (U5-slice-2). |

## `data/` — market data

| File | Purpose |
|------|---------|
| `data_store.py` | In-memory OHLCV + LTP store, snapshot persistence. |
| `fyers_stream.py` | Fyers WebSocket consumer (NSE + MCX). |
| `alpaca_stream.py` | Alpaca WebSocket consumer (US). |

## `analysis/` — indicators & market analytics

| File | Purpose |
|------|---------|
| `indicators.py` | TA indicators (EMA, RSI, ATR, ADX, Bollinger, RVOL, etc.). |
| `regime_detector.py` | Market regime classification (**used by trading**). |
| `regime_engine.py` | Regime analytics for dashboard (consolidation candidate C1). |
| `options_engine.py` | IV rank / options analytics. |
| `oi_analyzer.py` | Open-interest analysis (feeds conviction score). |
| `iv_percentile.py` · `vwap_engine.py` · `opening_range.py` · `breadth_engine.py` | Dashboard analytics panels. |
| `spread_quality.py` | Option spread liquidity/quality checks (used by MCX engine). |
| `signal_health.py` | Skip-reason / drought health monitor. |
| `trade_analytics.py` · `trade_decision_audit.py` | Post-trade analytics + decision audit. |
| `edge_monitor.py` | Weekly edge-degradation monitor. |

## `intelligence/` — the "AI/context" layer

| File | Purpose |
|------|---------|
| `intelligence_engine.py` | Orchestrates the 4 layers; returns approve/reject/resize (now **fail-open**). |
| `analyst_agent.py` | Claude analyst (configurable; simulation heuristics when AI off). |
| `macro_data.py` · `news_scraper.py` · `fundamental_guard.py` | Macro snapshot · news · fundamental veto. |
| `conviction_scorer.py` | Pre-market F&O conviction score (drives InstitutionalMomentum). |
| `nse_participant_collector.py` | FII participant OI collector. |
| `premarket_analyzer.py` · `theme_detector.py` · `universe_scanner.py` | Pre-market context, theme/universe scans. |

## `risk/` — capital protection

| File | Purpose |
|------|---------|
| `portfolio_tracker.py` | Positions, P&L, paper wallet, stats (DB-backed). |
| `risk_manager.py` | Kill switch, daily loss limit, portfolio heat. |
| `options_risk.py` | Options-specific risk gates (allocation, lots, per-trade caps). |
| `daily_risk_budget.py` | Per-strategy / daily loss budgeting (see B2.1 — sizing fix). |

## `config/` — settings & reference data

| File | Purpose |
|------|---------|
| `settings.py` | Central env-backed config (all tunables). |
| `nse_instruments.json` + `scripts/fetch_lot_sizes.py` | F&O lot sizes / strike steps (auto-refreshable). |
| `watchlist.py` · `learning_watchlist.py` | NSE/US universe · learning-lab universe. |
| `strategy_config.py` · `strategy_matrix.py` | Per-strategy overrides · strategy/regime matrix (dashboard). |
| `market_holidays.py` · `mcx_calendar.py` | NSE holidays · MCX session calendar. |
| `mcx_engine_settings.py` | MCX engine tunables. |
| `logging_ist.py` | IST-timestamped logging setup. |

## `backtesting/` — validation harness (central to B3)

| File | Purpose |
|------|---------|
| `backtest_engine.py` | Core backtest loop. |
| `data_fetcher.py` | Historical data for backtests. |
| `performance.py` | Metrics (win rate, expectancy, drawdown). |
| `walk_forward.py` | Out-of-sample walk-forward validation (**wire in — C3/B3.2**). |
| `monte_carlo.py` | Risk-of-ruin / drawdown distribution (**wire in — C3/B3.3**). |

## `notifications/`, `api/`, `dashboard/`, `tests/`, `deploy/`

| Path | Purpose |
|------|---------|
| `notifications/alert_service.py` | Telegram/alert dispatch. |
| `api/dashboard_api.py` | FastAPI backend: serves the UI + API on :8000; loopback-bound, write-auth middleware (X-API-Key on all writes). |
| `api/segment_readers.py` | Maps a `segment` (nse/mcx/us) → trades/stats/review reader behind the unified `/ledger/*` endpoints (U7). |
| `dashboard/index.html` | React dashboard UI (served by `dashboard_api` at `/`; API base derived from `window.location`). |
| `tests/test_pipeline.py` | Pipeline smoke test. |
| `tests/test_sizing.py` | U4 sizing regression (new == old across a grid). |
| `deploy/trading-bot.service` | systemd unit. |
| `scripts/` | Manual runners — `run_backtest.py`, `run_full_backtest.py`, `run_analysis.py`; ops — `fetch_lot_sizes.py`, `migrate_unified_ledger.py`. `_archive/` holds one-off dev backtests (`backtest_reversal_*`, `diagnose_equity`, `backtest_equity_exit`) + old migrations. |

---

## Cleanup done (2026-06-21 audit)

- Standalone docs moved into `docs/` (LOGIC, ROADMAP, options_bugs, PLAN, architecture_enhancement_spec).
- Backtest/analysis runners moved root → `scripts/`; one-off dev backtests → `scripts/_archive/`.
- Deleted stale one-offs (`validate_edges_v2.py`, `validate_fo_leverage.py`) + local cruft
  (`fyers*.log`, a stray `.env` copy).
- Root now holds only live engines, entry points, review/report tools, and `paper_trading.py`.

## Remaining (optional, future — coordinate with server release)

The package layout is clean; the root could be grouped further **without moving server-referenced
entry points** (`watchdog.py`, `main.py`, `generate_token.py`, `nightly_agent.py`, `weekly_agent.py`
must stay at root, or systemd/cron paths break):

```
  core/        learning_engine.py, commodity_options_learning.py, us_reversal.py, token_manager.py, audit_log.py
  reviews/     daily_plan.py, daily_review.py, weekly_review.py, journal_analyser.py, portfolio_analyser.py, system_health.py
```

> ⚠️ Moving `core/` modules updates absolute imports across many files; moving `reviews/` changes how
> they're invoked. A dedicated, fully-verified PR — **not** mixed with functional work — done when
> server cron paths can be updated in the same release. (Tracked as PROJECT_PLAN A5.4.)
