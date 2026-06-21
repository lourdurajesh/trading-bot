# AlphaLens Trading Bot — Master Development Plan

## Project Goal
Build a fully autonomous AI-powered trading bot for NSE/BSE (and eventually US markets)
that runs unattended during market hours, makes intelligent buy/sell decisions using
technical analysis + LLM intelligence + news sentiment, manages risk automatically,
and generates consistent returns to replace a salary long-term.

Secondary goal: Once profitable for 4+ weeks, extend to a commission-based
multi-user trading service.

## Architecture Stack
- **Language**: Python 3.11
- **Broker (India)**: Fyers API v3 (NSE/BSE equities + NFO options)
- **Broker (US)**: Alpaca (Phase 9 — future)
- **Intelligence**: Claude API (claude-sonnet-4-20250514)
- **Dashboard**: React (single HTML file, no build step, no npm)
- **API**: FastAPI + uvicorn
- **Database**: SQLite (trades, audit log, playbooks)
- **Alerts**: Telegram Bot API
- **Scheduler**: Windows Task Scheduler (nightly/weekly agents)

---

## Complete File Structure

```
trading-bot/
│
├── config/
│   ├── __init__.py
│   ├── settings.py              # All config + env var loading
│   └── watchlist.py             # Static watchlist (overridden by dynamic at runtime)
│
├── data/
│   ├── __init__.py
│   ├── data_store.py            # In-memory tick buffer + multi-TF candle builder
│   ├── fyers_stream.py          # Fyers WebSocket v3 consumer + gap recovery
│   └── alpaca_stream.py         # Alpaca WebSocket (Phase 9)
│
├── analysis/
│   ├── __init__.py
│   ├── indicators.py            # 20+ indicators as pure functions (EMA, RSI, MACD, ATR...)
│   ├── regime_detector.py       # TRENDING / RANGING / VOLATILE / BREAKOUT classifier
│   └── options_engine.py        # Black-Scholes Greeks, IV rank, strike selection
│
├── strategies/
│   ├── __init__.py
│   ├── base_strategy.py         # Signal dataclass + abstract BaseStrategy
│   ├── trend_follow.py          # Momentum breakout (1H signal + Daily confirm)
│   ├── mean_reversion.py        # RSI + Bollinger Band reversals (15m signal)
│   ├── options_income.py        # Iron Condor / Short Strangle (high IV rank)
│   ├── directional_options.py   # Debit spreads on strong breakout signals
│   └── strategy_selector.py     # Routes each symbol to correct strategy by regime
│
├── intelligence/
│   ├── __init__.py
│   ├── news_scraper.py          # ET, Moneycontrol, NSE, StockTwits, Reddit scraper
│   ├── macro_data.py            # VIX, FII flows, SPX, crude oil, USD/INR
│   ├── fundamental_guard.py     # Earnings calendar, corporate actions — veto bad trades
│   ├── analyst_agent.py         # Claude API — conviction scoring per signal
│   ├── intelligence_engine.py   # Orchestrates layers 1-4 in parallel (ThreadPoolExecutor)
│   ├── theme_detector.py        # LPG shortage → kitchen appliance stocks (thematic trades)
│   └── universe_scanner.py      # Scans all 1800 NSE stocks, filters by theme + liquidity
│
├── risk/
│   ├── __init__.py
│   ├── risk_manager.py          # Position sizing, kill switch, portfolio heat, daily loss limit
│   └── portfolio_tracker.py     # Live P&L, SQLite persistence, drawdown, win rate
│
├── execution/
│   ├── __init__.py
│   ├── position_manager.py      # ✅ NEW — Active exit management every 5s
│   │                            #   - Stop loss hits → market exit
│   │                            #   - T1 hit → partial exit + SL to breakeven
│   │                            #   - Trailing stop after 1.5R
│   │                            #   - EOD forced close at 3:15 PM
│   │                            #   - Max holding period enforcement
│   ├── order_manager.py         # ✅ REBUILT — AUTO/MANUAL gating + fill confirmation
│   │                            #   - Order fill confirmation loop (30s timeout)
│   │                            #   - Margin check before every order
│   │                            #   - Minimum net profit threshold (₹500)
│   │                            #   - Atomic entry+SL (emergency exit if SL fails)
│   ├── fyers_broker.py          # ✅ REBUILT — Fyers REST wrapper
│   │                            #   - GTT orders (persist through bot crashes)
│   │                            #   - Token verification on startup
│   │                            #   - Position reconciliation after crash
│   │                            #   - Proper funds/margin checking
│   └── alpaca_broker.py         # Alpaca REST wrapper (Phase 9)
│
├── backtesting/
│   ├── __init__.py
│   ├── data_fetcher.py          # 3-year OHLCV from Fyers REST + Yahoo fallback
│   ├── backtest_engine.py       # Realistic simulation: slippage, brokerage, STT
│   └── performance.py           # Sharpe, drawdown, profit factor, A-F grading
│
├── notifications/
│   ├── __init__.py
│   └── alert_service.py         # Telegram: trade opened/closed, signals, kill switch
│
├── api/
│   ├── __init__.py
│   └── dashboard_api.py         # FastAPI: REST + WebSocket (/ws/live every 2s)
│                                #   Endpoints: /stats, /positions, /signals/pending,
│                                #   /signals/{id}/confirm, /signals/{id}/reject,
│                                #   /risk, /mode/{mode}, /kill-switch/reset,
│                                #   /portfolio/analysis, /plan/today, /plan/done/{id},
│                                #   /journal/analysis
│
├── dashboard/
│   └── index.html               # React dashboard (single file, no build step)
│                                #   - Live P&L + positions panel
│                                #   - Pending signals with confirm/reject buttons
│                                #   - Risk metrics + portfolio heat bar
│                                #   - Daily trading checklist (pre-market → closing)
│                                #   - Portfolio analysis (correlation, stress test, hedging)
│                                #   - Journal analysis (behavioural biases, 3 rules)
│                                #   - AUTO/MANUAL mode toggle
│                                #   - Kill switch reset button
│
├── db/                          # Auto-created on first run
│   ├── trades.db                # SQLite — all trades (open + closed)
│   ├── dynamic_watchlist.json   # Written by nightly_agent — tomorrow's symbols
│   ├── playbooks/               # nightly_agent output — YYYYMMDD.json
│   ├── daily_plans/             # daily_plan output — plan_YYYY-MM-DD.json
│   ├── historical/              # Cached OHLCV CSV files (auto-refreshed daily)
│   ├── journal_reports/         # journal_analyser output
│   ├── portfolio_reports/       # portfolio_analyser output
│   └── weekly_reports/          # weekly_agent output
│
├── logs/
│   ├── bot.log                  # Main bot log (INFO level)
│   └── watchdog.log             # Watchdog process log
│
├── generate_token.py            # Fyers auto-login: TOTP + PIN → saves token to .env
├── watchdog.py                  # ✅ NEW — Master process manager
│                                #   - Starts main.py as subprocess
│                                #   - Auto-restarts within 10s of crash
│                                #   - Token auto-refresh at 11:45 PM daily
│                                #   - Position reconciliation after restarts
│                                #   - Telegram alerts on every crash
├── main.py                      # ✅ UPDATED — Bot orchestrator
│                                #   - Two-loop architecture:
│                                #     Fast loop (5s): position_manager.check_all()
│                                #     Slow loop (60s): strategy_selector.run_cycle()
│                                #   - Loads dynamic watchlist on startup
│                                #   - Starts FastAPI dashboard in background thread
├── nightly_agent.py             # Runs 8:30 PM — reads news, detects themes,
│                                #   scans universe, backtests candidates, saves playbook
├── weekly_agent.py              # Runs Sunday 9 AM — full 3-year backtest all symbols,
│                                #   deep universe scan, Claude weekly outlook
├── daily_plan.py                # Morning checklist: 25 time-stamped tasks across
│                                #   4 phases (pre-market, opening, midday, closing)
├── portfolio_analyser.py        # Correlation matrix, sector overexposure, stress tests
│                                #   (10%/20%/40% Nifty drops), hedging suggestions
├── journal_analyser.py          # Reads all trades from SQLite, detects 6 behavioural
│                                #   biases, generates 3 personalised trading rules
│
├── requirements.txt             # All Python dependencies (pinned versions)
├── .env                         # Secrets — NEVER commit. Contains:
│                                #   FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_ACCESS_TOKEN
│                                #   FYERS_CLIENT_ID, FYERS_PIN, FYERS_TOTP_SECRET
│                                #   ALPACA_API_KEY, ALPACA_SECRET_KEY
│                                #   ANTHROPIC_API_KEY
│                                #   BOT_MODE (MANUAL/AUTO)
│                                #   TOTAL_CAPITAL, RISK_PER_TRADE_PCT
│                                #   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
├── .gitignore                   # Excludes .env, db/, logs/, venv/, __pycache__/
└── PLAN.md                      # This file — master development plan
```

---

## Phase Completion Status

### Phase 1 — Data Layer ✅ COMPLETE
- [x] config/settings.py — all env vars, capital, risk params, market hours
- [x] config/watchlist.py — NSE large cap, mid cap, indices, options universe
- [x] data/data_store.py — thread-safe tick buffer, multi-TF OHLCV builder
- [x] data/fyers_stream.py — WebSocket v3, historical seeding, reconnect, gap recovery
- [x] data/alpaca_stream.py — Alpaca WebSocket + historical seeding
- [x] analysis/indicators.py — EMA, SMA, VWAP, RSI, MACD, Stochastic, ATR, BB, ADX, OBV
- [x] analysis/regime_detector.py — ADX + BB Width + EMA slope classifier
- [x] requirements.txt
- [x] .env template

### Phase 2 — Strategy + Execution ✅ COMPLETE
- [x] strategies/base_strategy.py — Signal dataclass, Direction, SignalType
- [x] strategies/trend_follow.py — breakout above 20-bar high + EMA + volume
- [x] strategies/mean_reversion.py — RSI oversold/overbought + Bollinger Band
- [x] strategies/strategy_selector.py — regime-based routing + cooldowns
- [x] risk/risk_manager.py — fixed fractional sizing, kill switch, heat check
- [x] risk/portfolio_tracker.py — SQLite persistence, live P&L, drawdown
- [x] execution/order_manager.py — ✅ REBUILT with fill confirmation + margin check
- [x] execution/fyers_broker.py — ✅ REBUILT with GTT + reconciliation
- [x] execution/alpaca_broker.py — paper + live mode
- [x] main.py — ✅ UPDATED with two-loop architecture

### Phase 3 — Dashboard + Options ✅ COMPLETE
- [x] notifications/alert_service.py — Telegram: all trade events
- [x] api/dashboard_api.py — FastAPI REST + WebSocket + all endpoints
- [x] dashboard/index.html — React: P&L, signals, risk, checklist, analysis panels
- [x] analysis/options_engine.py — Black-Scholes, IV rank, PCR, strike selection
- [x] strategies/options_income.py — Iron Condor / Short Strangle
- [x] strategies/directional_options.py — Debit spreads

### Phase 4 — Intelligence Layer ✅ COMPLETE
- [x] intelligence/news_scraper.py — ET, MC, NSE, StockTwits, Reddit + 15min cache
- [x] intelligence/macro_data.py — VIX, FII flows, SPX, crude, USD/INR + 30min cache
- [x] intelligence/fundamental_guard.py — earnings guard, corporate actions veto
- [x] intelligence/analyst_agent.py — Claude API conviction scoring (0-10)
- [x] intelligence/intelligence_engine.py — parallel orchestration, hard veto logic

### Phase 5 — Agents + Backtesting ✅ COMPLETE
- [x] intelligence/theme_detector.py — news → investment themes (LPG→kitchen stocks)
- [x] intelligence/universe_scanner.py — all 1800 NSE stocks, theme + liquidity filter
- [x] backtesting/data_fetcher.py — 3y OHLCV, Fyers + Yahoo fallback, CSV cache
- [x] backtesting/backtest_engine.py — bar-by-bar, no lookahead, slippage + STT
- [x] backtesting/performance.py — Sharpe, max DD, profit factor, A-F grade
- [x] nightly_agent.py — 8:30 PM: news → themes → scan → backtest → playbook
- [x] weekly_agent.py — Sunday: full backtest all symbols + Claude weekly outlook

### Phase 6 — Analysis Tools ✅ COMPLETE
- [x] portfolio_analyser.py — HHI concentration, correlation matrix, stress tests, hedges
- [x] daily_plan.py — 25-task time-stamped checklist + Claude morning briefing
- [x] journal_analyser.py — 6 bias detectors + 3 personalised rules from trade history

### Phase 7A — Execution Reliability ✅ COMPLETE
- [x] execution/position_manager.py — active exit management every 5 seconds
  - SL hit → market exit
  - T1 hit → 50% exit + SL to breakeven
  - Trailing stop after 1.5R (ratchet — never moves backward)
  - EOD forced close at 3:15 PM
  - Max 20-day holding period enforcement
  - Failed exit → Telegram alert for manual intervention
- [x] execution/order_manager.py — rebuilt with:
  - Fill confirmation loop (polls every 2s, 30s timeout)
  - Margin check before every order
  - Minimum net profit check (₹500 after fees)
  - Atomic entry+SL (emergency exit if SL fails after 3 retries)
- [x] execution/fyers_broker.py — rebuilt with:
  - GTT (Good Till Triggered) orders — SL persists through crashes
  - Token verification on initialise()
  - reconcile_positions() for crash recovery
  - Proper funds/margin API

### Phase 7B — Crash Resilience ✅ COMPLETE
- [x] watchdog.py — process monitor + auto-restart
  - Starts main.py as subprocess, monitors continuously
  - Auto-restarts within 10s of crash
  - Token auto-refresh at 11:45 PM
  - Position reconciliation after every restart
  - Telegram alert on crash + restart
  - Gives up after 10 consecutive restarts
- [x] main.py — two-loop architecture
  - Fast loop (5s): position_manager.check_all()
  - Slow loop (60s): strategy_selector.run_cycle()
- [x] data/fyers_stream.py — WebSocket gap recovery
  - Fetches REST candles on reconnect to fill gap
  - Tracks gap start time, fills on reconnect

---

## Phase 7C — Two-Loop + Profit Filter ✅ COMPLETE (in Phase 7B)
- [x] Fast loop every 5s in main.py
- [x] Minimum profit threshold in order_manager.py

---

## Phase 7C+ — Production Hardening ✅ COMPLETE (2026-03-24)
- [x] analyst_agent.py: API failure → REJECT (was APPROVE — safety critical bug)
- [x] analyst_agent.py: parse failure → REJECT (was APPROVE)
- [x] analyst_agent.py: updated to claude-sonnet-4-6 model
- [x] data_store.py: IST-aligned daily candle boundaries (was UTC — splits NSE sessions)
- [x] data_store.py: get_ltp() thread-safe under lock
- [x] watchdog.py: exponential backoff on restarts (10s→20s→40s…→5min, was fixed 10s)
- [x] watchdog.py: consecutive crash tracking + uptime detection for reset
- [x] config/settings.py: validate_env() startup check for missing creds + risk limits
- [x] main.py: validate_env() called on startup; duplicate load_dotenv() removed
- [x] performance.py: avg_loser default 0 not 1; profit_factor cap 99 not 999
- [x] position_manager.py: paper trading exits route to paper_trading_engine (was calling real broker)
- [x] strategy_selector.py: is_valid() gate before intelligence engine
- [x] strategy_selector.py: 5-min cooldown after order_manager rejection
- [x] directional_options.py: stop_loss = debit × 0.5 (was = entry → RR=0)
- [x] base_strategy.py: is_valid() handles OPTIONS debit spread stop-loss convention

---

## Phase 7D — Paper Trading Engine ✅ COMPLETE

- [x] paper_trading.py
  - Simulates fills at current LTP ± slippage (0.05%)
  - Records to paper_trades table in SQLite (separate from live trades)
  - Tracks paper P&L: win rate, profit factor, avg winner/loser
  - Sends [PAPER]-tagged Telegram alerts on open/close
  - Enabled via PAPER_TRADING=true in .env
  - Dashboard: GET /paper/stats and GET /paper/positions endpoints
- [x] position_manager — paper mode exit routing
  - SL hits / target hits / EOD exits route to paper_trading_engine.close_order()
  - Broker SL updates skipped in paper mode
- [x] Signal quality fixes (2026-03-24)
  - directional_options.py: stop_loss = debit_cost × 0.5 (was = entry, giving RR=0)
  - base_strategy.is_valid(): OPTIONS debit spreads use stop < entry, not SHORT equity logic
  - strategy_selector: is_valid() gate before intelligence engine (no wasted Claude API calls)
  - strategy_selector: 5-min cooldown after order_manager rejection

**Validate:** Monitor paper trading for 2 weeks. Target: win_rate > 50%, profit_factor > 1.3

---

## Phase 7E — Options Execution ✅ COMPLETE (2026-03-24)
- [x] execution/options_executor.py
  - Fetches live NFO options chain from Fyers (60s cache)
  - Selects expiry nearest to DTE range (weekly/monthly detection)
  - Selects strike by TARGET DELTA (not hardcoded math) from live chain
  - Constructs correct Fyers NFO symbol: NSE:NIFTY25JAN24500CE (monthly) or NSE:NIFTY2501234500CE (weekly)
  - Returns actual lot sizes: NIFTY=75, BANKNIFTY=35, FINNIFTY=65
  - Computes PCR (put-call ratio) from chain OI data
  - Falls back to Black-Scholes simulation when chain unavailable
  - update_iv_history() feeds options_engine for IV rank calculation
- [x] directional_options.py — uses live ATM LTP for debit cost (not formula estimate)
- [x] options_income.py — uses live call+put LTP for strangle credit
- [x] API: GET /options/chain/{symbol} — live chain summary on dashboard

## Phase 7F — Audit Trail ✅ COMPLETE (2026-03-24)
- [x] audit_log.py — append-only SQLite DB (db/audit.db, never UPDATE/DELETE)
  - Events: SIGNAL_GENERATED, SIGNAL_REJECTED, ORDER_PLACED, ORDER_FILLED,
    ORDER_FAILED, POSITION_OPENED, POSITION_CLOSED, STOP_HIT, TARGET_HIT,
    TRAILING_STOP, MODE_CHANGE, KILL_SWITCH, PAPER_TRADE, BOT_START, BOT_STOP,
    TOKEN_REFRESH, INTELLIGENCE_VETO
  - CSV export: POST /audit/export
  - Dashboard view: GET /audit/recent?limit=100&event_type=KILL_SWITCH
- [x] Integrated into: order_manager, portfolio_tracker, risk_manager, strategy_selector, main.py

## Phase 7G — Options Safety Hardening ✅ COMPLETE (2026-03-24)

**Goal**: Hard safety nets for fully autonomous options trading. Options can lose 100%
of premium in minutes, so tighter controls than equity are mandatory.

### New: `risk/options_risk.py` — OptionsRiskGate
- [x] **Expiry day protection** — blocks ALL options entries on expiry day (gamma too high)
- [x] **VIX gate** — blocks short premium strategies when India VIX > 25 (configurable)
- [x] **Min premium LTP** — skips options priced < ₹5 (near-zero options = 100%+ noise moves)
- [x] **Lot-size-aware position sizing** — `lots = min(risk_budget/cost_per_lot, cap_budget/cost_per_lot, MAX_LOTS)`
- [x] **Max lots per trade** — hard cap of 2 lots per trade (env: `MAX_OPTIONS_LOTS_PER_TRADE`)
- [x] **Max capital per trade** — max 5% of total capital in a single options trade
- [x] **Separate daily options loss limit** — 2% of capital (env: `DAILY_OPTIONS_LOSS_LIMIT_PCT`)
- [x] **Options kill switch** — halts options trading independently of equity kill switch
- [x] NFO symbol expiry parsing (monthly `25JAN` format + weekly `250123` format)

### Updated: `risk/risk_manager.py`
- [x] OPTIONS signals routed through `options_risk_gate.check()` before approval
- [x] `_calculate_size()` uses lot-based math for OPTIONS (not equity shares formula)
- [x] `update_daily_pnl()` forwards options P&L to options-specific kill switch
- [x] `status()` includes `options` sub-dict for dashboard visibility

### Updated: `execution/position_manager.py`
- [x] OPTIONS positions detected via `signal_type == "OPTIONS"` — separate exit path
- [x] **DTE-based forced exit** — close when expiry is ≤ 3 DTE (env: `OPTIONS_DTE_FORCE_EXIT`)
- [x] **Debit spread exit**: close when option premium drops to 50% of entry (configurable)
- [x] **Short strangle exit**: close when total position value rises to 2× original credit
- [x] **Profit target**: exit debit spread when premium hits `target_1`; strangle at 50% decay
- [x] **EOD exit** applies to options (3:15 PM IST) — no overnight gamma risk
- [x] No trailing stops for options (theta decay changes the math completely)
- [x] Live option LTP monitoring via NFO symbol in data store (not underlying index)

### Updated: `config/settings.py`
- [x] `MAX_OPTIONS_LOTS_PER_TRADE=2` — hard lot cap per trade
- [x] `MIN_OPTION_LTP=5.0` — minimum option premium (INR)
- [x] `MIN_OPTION_OI=500` — minimum open interest for strike selection
- [x] `OPTIONS_DTE_FORCE_EXIT=3` — days before expiry to force-close
- [x] `OPTIONS_VIX_LIMIT=25.0` — max VIX for short premium strategies
- [x] `DAILY_OPTIONS_LOSS_LIMIT_PCT=2.0` — separate options daily loss cap
- [x] `MAX_OPTIONS_TRADE_PCT=5.0` — max % of capital per single options trade

---

## Phase 7H — Paper Trading Bug Fixes ✅ COMPLETE (2026-04-08)

### Background
Paper trading showed a massive loss of ₹10,95,538 after running on 2026-04-01 and
2026-04-02. Log analysis revealed three distinct bugs compounding each other. The real
underlying loss from legitimate trades was only ₹-84,677 across 13 unique trades.

---

### Bug 1 — `_is_market_hours()` ran NSE strategies during US market hours

**File:** `main.py` → `_is_market_hours()`

**Root cause:**
The function returned `True` from 19:00–23:59 IST and 00:00–01:30 IST due to a US
market hours block added for a future multi-market feature. The bot only trades NSE
stocks — there are no US symbols in the watchlist. This caused the evaluation loop to
run `strategy_selector.run_cycle()` for NSE stocks 7+ hours after market close.

```python
# BEFORE (broken) — True at 23:45 IST for NSE stocks
if current_time >= us_open or current_time <= dtime(1, 30):
    return True

# AFTER (fixed) — NSE only
# US market hours block removed entirely
```

**Impact:** Strategy evaluation ran from 19:00–01:30 IST, generating MeanReversion
SHORT signals on NESTLEIND and HINDUNILVR after market close.

---

### Bug 2 — EOD forced-close immediately killed every after-hours position, enabling infinite re-entry

**File:** `execution/position_manager.py` → `_exit_position()`

**Root cause:**
`EOD_EXIT_TIME = 15:15 IST`. Any position opened after 15:15 (including the after-hours
ones from Bug 1) was immediately force-closed within 5 seconds by the position manager's
fast loop. After each forced close, `portfolio_tracker._open_positions` was emptied,
so `has_open_position(symbol)` returned False on the next cycle, and the signal fired
again.

**The loop:**
1. `_is_market_hours()` returns True (US hours) → strategy fires on NESTLEIND
2. Paper trade OPENED → `_open_positions["NESTLEIND"] = position`
3. Position manager (5s later): `23:45 >= 15:15 (EOD)` → force-closes
4. `_open_positions.pop("NESTLEIND")` — position gone
5. Next cycle (60s): `has_open_position` = False → same signal fires → repeat

**Damage:** NESTLEIND entered 120 times (₹-954,740), HINDUNILVR 14 times (₹-69,006).
All phantom — the real trade would have been just 1 entry each.

**Fix:** Added NSE-hours entry guard in `order_manager.py` → `_execute()`:

```python
# Block new entries for NSE symbols outside 09:15–15:15 IST
if signal.symbol.startswith("NSE:"):
    if not (nse_open <= now_ist.time() <= eod_cutoff):
        logger.warning(f"[OrderManager] Blocked entry outside NSE hours: ...")
        return
```

This breaks the loop at entry — no position opened after 15:15, even if the evaluation
loop somehow runs.

---

### Bug 3 — Cooldown never applied after a real trade closed (stop hit)

**File:** `execution/position_manager.py` → `_exit_position()`

**Root cause:**
`strategy_selector.apply_cooldown()` was documented as "called after a losing trade"
but was **never wired** to the actual trade close event. The only callers were:
- Intelligence layer rejection → 60-min cooldown
- Risk/margin rejection → 5-min cooldown

After a STOP, EOD_FORCED, or MAX_HOLD exit, no cooldown was set. So on the next
60-second cycle, `_is_on_cooldown(symbol)` returned False and the strategy re-evaluated
the same symbol immediately.

**Observed impact:** TCS SHORT fired 6 times in 7 minutes (09:15–09:22 on 2026-04-01).
Each position hit its stop and closed within the same minute — but without a cooldown,
the next cycle opened another SHORT immediately.

**Fix:** Cooldown is now applied inside `_exit_position()` after every STOP / EOD_FORCED
/ MAX_HOLD close:

```python
if reason in ("STOP", "EOD_FORCED", "MAX_HOLD"):
    from strategies.strategy_selector import strategy_selector
    strategy_selector.apply_cooldown(symbol)   # uses SYMBOL_COOLDOWN_MINUTES (60 min)
```

TARGET1 / TARGET2 exits do NOT apply cooldown (trade worked — symbol can be re-evaluated).

---

### Bug 4 — MeanReversion fired on opening candle gap noise

**File:** `strategies/mean_reversion.py`

**Root cause:**
The first 15m candle at 09:15 includes the overnight gap. A stock that gaps up 1%
will naturally show RSI > 65 and price at the upper Bollinger Band — both conditions
for a SHORT signal. But this is not a mean-reversion setup; it is a gap that may
continue. The regime detector classified the symbol as RANGING based on the previous
day's 1H data, which was stale the moment the gap occurred.

**Observed impact:** All 6 TCS SHORT entries (09:15–09:22) had the same stop at
₹2429.03 (the pre-open swing high). TCS was slowly grinding upward from the gap open,
testing that stop level repeatedly. A mean-reversion short into a gap-up continuation
day is structurally wrong.

**Fix:** Opening blackout window added as step 0 in `evaluate()`:

```python
OPENING_BLACKOUT_END = dtime(9, 45)   # skip 09:15–09:44

if datetime.now(tz=IST).time() < OPENING_BLACKOUT_END:
    self.log_skip(symbol, "Opening blackout — waiting for 09:45 to avoid gap-open noise")
    return None
```

From 09:45 onwards, at least 2 completed 15m candles exist, RSI is computed on real
intraday price action (not gap artefacts), and the BB has a meaningful intraday range.

**Why 09:45 and not 09:30?**
NSE pre-open session ends at 09:15 and the first live candle completes at 09:30. The
09:30 candle is still heavily influenced by the opening auction print. The second
completed candle (09:30–09:45) provides enough evidence to distinguish a genuine
overbought reversal setup from a gap continuation.

---

### Corrected P&L (after removing phantom duplicates)

| Metric | Raw DB | Corrected |
|--------|--------|-----------|
| Total trades | 145 | 13 |
| Total P&L | ₹-10,95,538 | ₹-84,677 |
| Win rate | — | 7.7% (1W / 12L) |
| Avg winner | — | ₹+11,842 |
| Avg loser | — | ₹-8,043 |

All 13 legitimate trades were on 2026-04-01. No valid signals on 2026-04-02
(bot ran outside market hours all day due to Bug 1+2 cycle).

---

### MeanReversion Strategy — Signal Logic Reference

**Condition for SHORT signal (all must pass):**
1. Regime on 1H = RANGING
2. RSI(14) on 15m > 65 (overbought)
3. LTP ≥ upper Bollinger Band × (1 − 0.005) — price within 0.5% of upper band
4. EMA(50) on 1H is "down" or "neutral" (not in strong uptrend)
5. Current time ≥ 09:45 IST (opening blackout, added 2026-04-08)

**Stop:** recent 15m swing high + 0.5 × ATR  
**Target 1:** middle Bollinger Band (EMA21 on 15m) — the mean  
**Target 2:** entry − 2 × risk (2R extension)

**Condition for LONG signal (mirror):**
1. Regime = RANGING
2. RSI < 35 (oversold)
3. LTP ≤ lower BB × (1 + 0.005)
4. EMA(50) on 1H is "up" or "neutral"
5. Current time ≥ 09:45 IST

**Stop/loss logic reminder:**
For SHORT trades: `stop_loss > entry_price` is correct — stop is placed above entry.
For LONG trades: `stop_loss < entry_price` is correct — stop is placed below entry.
This is the opposite of what looks intuitive when scanning the DB.

---

### Files Changed in This Phase

| File | Change |
|------|--------|
| `main.py` | Removed US market hours block from `_is_market_hours()` — NSE-only now |
| `execution/order_manager.py` | Added NSE entry guard in `_execute()` — blocks new entries after 15:15 IST |
| `execution/position_manager.py` | Wired `apply_cooldown()` after STOP / EOD_FORCED / MAX_HOLD exits |
| `strategies/mean_reversion.py` | Added 09:45 opening blackout in `evaluate()` |

---

## Phase 8 — Multi-User Platform 🔴 NOT STARTED
**Only start after single-user version profitable for 4+ consecutive weeks**

### Legal Prerequisites (Do Before Any Code)
- [ ] Consult CA/lawyer on SEBI regulations
- [ ] Understand SEBI RIA requirements for managing others' money
- [ ] Decide structure: fee-based advisory vs discretionary management

### 8A — User Management
- [ ] User model: user_id, name, encrypted broker credentials, capital, risk settings
- [ ] Fernet symmetric encryption for credentials (never plain text in DB)
- [ ] Per-user Fyers token management and refresh

### 8B — Multi-User Execution
- [ ] All orders tagged with user_id
- [ ] Position sizing from per-user capital (not global TOTAL_CAPITAL)
- [ ] Per-user daily loss limit and kill switch
- [ ] Per-user portfolio tracking, P&L, drawdown

### 8C — Client Dashboard
- [ ] Client view (read-only — shows their P&L, open positions, alerts only)
- [ ] Admin view (all users, aggregate stats, kill switch controls)
- [ ] Monthly P&L statements (PDF export)
- [ ] Commission calculation (e.g. 20% of profits above high watermark)

### 8D — Infrastructure Upgrade
- [ ] Move from laptop to cloud VM (AWS/GCP — always-on)
- [ ] Separate processes: data engine / trading engine / intelligence
- [ ] Redis for inter-process communication
- [ ] PostgreSQL instead of SQLite (concurrent multi-user access)
- [ ] HTTPS dashboard (nginx reverse proxy + SSL cert)
- [ ] Automated daily DB backups

---

## Phase 9 — US Markets + Advanced Features 🔴 FUTURE
- [ ] Alpaca execution (paper + live)
- [ ] US market intelligence (Fed, earnings season, macro)
- [ ] Cross-market correlation model (Nifty vs SPX)
- [ ] Self-improving strategy parameters (reinforcement learning)
- [ ] Earnings play strategies (pre-earnings strangles)
- [ ] Sector rotation model
- [ ] scan_now.py — on-demand "give me top 5 setups right now"

---

## Daily Workflow (Current)

```
08:30 AM  python generate_token.py        # refresh Fyers token
08:45 AM  python watchdog.py              # starts + monitors bot
          cd dashboard && python -m http.server 3000  # serve dashboard
          Open browser: http://localhost:3000

08:30 PM  nightly_agent.py runs auto      # Task Scheduler
Sunday    weekly_agent.py runs auto       # Task Scheduler
```

**Watchdog replaces main.py as your entry point.**
Watchdog starts main.py, monitors it, restarts on crash, refreshes token at 11:45 PM.

---

## Task Scheduler Setup (Windows)

Run in PowerShell as Administrator:

```powershell
# Nightly agent — 8:30 PM daily
$a1 = New-ScheduledTaskAction -Execute "D:\Tech\trading-bot\venv\Scripts\python.exe" `
      -Argument "D:\Tech\trading-bot\nightly_agent.py" -WorkingDirectory "D:\Tech\trading-bot"
$t1 = New-ScheduledTaskTrigger -Daily -At "8:30PM"
Register-ScheduledTask -Action $a1 -Trigger $t1 -TaskName "TradingBotNightly"

# Weekly agent — Sunday 9:00 AM
$a2 = New-ScheduledTaskAction -Execute "D:\Tech\trading-bot\venv\Scripts\python.exe" `
      -Argument "D:\Tech\trading-bot\weekly_agent.py" -WorkingDirectory "D:\Tech\trading-bot"
$t2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "9:00AM"
Register-ScheduledTask -Action $a2 -Trigger $t2 -TaskName "TradingBotWeekly"
```

---

## Go-Live Checklist (Before Running Real Money AUTO Mode)

- [ ] Phase 7D complete — paper trading engine built
- [ ] 2 weeks paper trading completed
- [ ] Paper mode win rate > 50%, profit factor > 1.3
- [ ] Kill switch tested — confirmed it stops orders
- [ ] One manual test order placed via Fyers API (not bot)
- [ ] SL order confirmed on Fyers order book after bot entry
- [ ] GTT order confirmed persisting on Fyers after bot restart
- [ ] Telegram alerts working end-to-end
- [ ] Dashboard live P&L updating correctly
- [ ] Watchdog tested: kill main.py manually, confirm auto-restart
- [ ] Token refresh tested: confirm new token picked up correctly
- [ ] Position reconciliation tested: stop bot mid-trade, restart, confirm positions match
- [ ] .env file backed up securely (encrypted, not in git)

---

## How to Continue in a New Claude Session

**Option A — Public repo (recommended):**
```
Continue my trading bot.
GitHub: github.com/YOUR_USERNAME/trading-bot
Current phase: Phase 7D — paper trading engine
Read PLAN.md for full context and continue.
```

**Option B — Paste PLAN.md:**
```
Continue my trading bot. Here is the full plan:
[paste contents of this file]
Next task: Phase 7D — build paper_trading.py
```

---

## Key Design Decisions (Do Not Change Without Good Reason)

| Decision | Reason |
|---|---|
| Python over .NET | Trading ecosystem, libraries, community support |
| FastAPI over Django/Flask | Lightweight, async, WebSocket support built-in |
| Single HTML dashboard | No npm, no build step, easy to modify and share |
| SQLite over PostgreSQL | Zero ops for single user, sufficient performance |
| Simulation mode default | Never live without explicit FYERS_ACCESS_TOKEN |
| MANUAL mode default | Never auto-execute without explicit trust built |
| Intelligence layer async | Never blocks the trading engine |
| GTT orders for SL | Persists even if bot crashes — critical safety net |
| Watchdog over cron | Handles crashes mid-session, not just daily restarts |
| Two-loop architecture | Fast position monitoring without blocking signals |

---

## Risk Rules (Non-Negotiable)

1. RISK_PER_TRADE_PCT must never exceed 2%
2. DAILY_LOSS_LIMIT_PCT must never exceed 3%
3. Never disable the kill switch
4. Every position MUST have a confirmed broker-side SL order
5. Test every new feature in paper mode before live
6. Never commit .env to git — it contains all your credentials
7. Run paper trading for minimum 2 weeks before switching to AUTO
8. After 2 consecutive losses on a day — stop trading, review tomorrow