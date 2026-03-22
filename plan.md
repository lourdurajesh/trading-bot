# AlphaLens Trading Bot — Master Development Plan

## Project Goal
Build a fully autonomous AI-powered trading bot for NSE/BSE (and eventually US markets)
that runs unattended during market hours, makes intelligent buy/sell decisions using
technical analysis + LLM intelligence + news sentiment, manages risk automatically,
and generates consistent returns to replace a salary long-term.

## Architecture Stack
- **Language**: Python 3.11
- **Broker (India)**: Fyers API v3 (NSE/BSE equities + NFO options)
- **Broker (US)**: Alpaca (future phase)
- **Intelligence**: Claude API (claude-sonnet-4-20250514)
- **Dashboard**: React (single HTML file, no build step)
- **API**: FastAPI + uvicorn
- **Database**: SQLite (trades, audit log, playbooks)
- **Alerts**: Telegram Bot API
- **Scheduler**: Windows Task Scheduler (nightly/weekly agents)

## Repository Structure
```
trading-bot/
├── config/
│   ├── settings.py           # all config + env vars
│   └── watchlist.py          # static watchlist (overridden by dynamic)
├── data/
│   ├── data_store.py         # in-memory tick buffer + candle builder
│   ├── fyers_stream.py       # Fyers WebSocket consumer
│   └── alpaca_stream.py      # Alpaca WebSocket (Phase 7)
├── analysis/
│   ├── indicators.py         # 20+ technical indicators
│   ├── regime_detector.py    # TRENDING/RANGING/VOLATILE/BREAKOUT
│   └── options_engine.py     # Black-Scholes Greeks + IV rank
├── strategies/
│   ├── base_strategy.py      # Signal dataclass + abstract base
│   ├── trend_follow.py       # Momentum breakout (1H + Daily confirm)
│   ├── mean_reversion.py     # RSI + Bollinger Band reversals
│   ├── options_income.py     # Iron Condor / Short Strangle
│   ├── directional_options.py # Debit spreads on breakouts
│   └── strategy_selector.py  # Routes symbols to strategies
├── intelligence/
│   ├── news_scraper.py       # ET, MC, NSE, StockTwits, Reddit
│   ├── macro_data.py         # VIX, FII flows, SPX, crude, USD/INR
│   ├── fundamental_guard.py  # Earnings calendar, corporate actions
│   ├── analyst_agent.py      # Claude API — trade conviction scoring
│   ├── intelligence_engine.py # Orchestrates all 4 layers in parallel
│   ├── theme_detector.py     # Detects market themes from news
│   └── universe_scanner.py  # Scans all 1800 NSE stocks by theme
├── risk/
│   ├── risk_manager.py       # Position sizing, kill switch, heat
│   └── portfolio_tracker.py  # Live P&L, SQLite persistence
├── execution/
│   ├── order_manager.py      # AUTO/MANUAL mode gating
│   ├── fyers_broker.py       # Fyers REST wrapper
│   └── alpaca_broker.py      # Alpaca REST wrapper (Phase 7)
├── backtesting/
│   ├── data_fetcher.py       # 3-year historical OHLCV
│   ├── backtest_engine.py    # Realistic simulation + slippage
│   └── performance.py        # Sharpe, drawdown, profit factor
├── notifications/
│   └── alert_service.py      # Telegram alerts
├── api/
│   └── dashboard_api.py      # FastAPI REST + WebSocket
├── dashboard/
│   └── index.html            # React dashboard (single file)
├── db/
│   ├── trades.db             # SQLite — all trades
│   ├── playbooks/            # nightly playbook JSON files
│   ├── daily_plans/          # daily checklist JSON files
│   ├── historical/           # cached OHLCV CSV files
│   ├── journal_reports/      # journal analysis outputs
│   ├── portfolio_reports/    # portfolio analysis outputs
│   └── weekly_reports/       # weekly agent outputs
├── logs/
│   └── bot.log
├── generate_token.py         # Fyers auto-login (TOTP + PIN)
├── main.py                   # Master orchestrator
├── nightly_agent.py          # Runs 8:30 PM — tomorrow's playbook
├── weekly_agent.py           # Runs Sunday — deep backtest + outlook
├── daily_plan.py             # Morning checklist generator
├── portfolio_analyser.py     # Correlation, stress test, hedging
├── journal_analyser.py       # Behavioural bias detection
├── requirements.txt
├── .env                      # secrets — never commit this
├── .gitignore
└── PLAN.md                   # THIS FILE
```

---

## Completion Status

### Phase 1 — Data Layer ✅ COMPLETE
- [x] config/settings.py
- [x] config/watchlist.py
- [x] data/data_store.py
- [x] data/fyers_stream.py
- [x] data/alpaca_stream.py
- [x] analysis/indicators.py
- [x] analysis/regime_detector.py
- [x] requirements.txt
- [x] .env template

### Phase 2 — Strategy + Execution ✅ COMPLETE
- [x] strategies/base_strategy.py
- [x] strategies/trend_follow.py
- [x] strategies/mean_reversion.py
- [x] strategies/strategy_selector.py
- [x] risk/risk_manager.py
- [x] risk/portfolio_tracker.py
- [x] execution/order_manager.py
- [x] execution/fyers_broker.py
- [x] execution/alpaca_broker.py
- [x] main.py (skeleton)

### Phase 3 — Dashboard + Options ✅ COMPLETE
- [x] notifications/alert_service.py
- [x] api/dashboard_api.py
- [x] dashboard/index.html
- [x] analysis/options_engine.py
- [x] strategies/options_income.py
- [x] strategies/directional_options.py

### Phase 4 — Intelligence Layer ✅ COMPLETE
- [x] intelligence/news_scraper.py
- [x] intelligence/macro_data.py
- [x] intelligence/fundamental_guard.py
- [x] intelligence/analyst_agent.py
- [x] intelligence/intelligence_engine.py

### Phase 5 — Agents + Backtesting ✅ COMPLETE
- [x] intelligence/theme_detector.py
- [x] intelligence/universe_scanner.py
- [x] backtesting/data_fetcher.py
- [x] backtesting/backtest_engine.py
- [x] backtesting/performance.py
- [x] nightly_agent.py
- [x] weekly_agent.py

### Phase 6 — Analysis Tools ✅ COMPLETE
- [x] portfolio_analyser.py
- [x] daily_plan.py
- [x] journal_analyser.py

---

## Phase 7 — Production Hardening 🔴 NOT STARTED
**Priority: CRITICAL — must complete before running real money unattended**

### 7A — Execution Reliability (Week 1)
- [ ] execution/position_manager.py
  - Active exit management every tick
  - Trailing stop logic (move SL to BE after 1R profit)
  - Partial exit at T1 (close 50%, trail rest)
  - EOD forced close for intraday positions (3:15 PM)
  - Max holding period enforcement (20 bars)
- [ ] execution/order_manager.py — order fill confirmation loop
  - Poll get_orders() after every place_order()
  - Confirm actual fill price + quantity
  - Handle partial fills
  - Handle rejections — alert + cancel position record
- [ ] execution/fyers_broker.py — atomic entry+SL placement
  - If SL placement fails after 3 retries: exit entry
  - Use GTT (Good Till Triggered) orders for persistent SL
  - Margin check before every order

### 7B — Crash Resilience (Week 1)
- [ ] watchdog.py
  - Monitors main.py process
  - Restarts within 10 seconds of crash
  - On restart: reconciles positions from Fyers vs local DB
  - Sends Telegram alert on every crash + restart
- [ ] Token auto-refresh (inside main.py)
  - At 11:45 PM: auto-run token refresh
  - Reconnect WebSocket with new token
  - If refresh fails: halt new orders, alert
- [ ] WebSocket gap recovery (fyers_stream.py)
  - On reconnect: fetch REST candles to fill gap
  - Mark signals during gap window as LOW CONFIDENCE
  - WebSocket health monitoring + daily disconnect report

### 7C — Two-Loop Architecture (Week 2)
- [ ] main.py refactor
  - Fast loop (every 5s): monitors open positions, entry zone hits
  - Slow loop (every 60s): full signal evaluation + intelligence
  - Pre-load entry zones from nightly playbook at 9:15 AM
- [ ] Minimum profit threshold in risk_manager.py
  - Compute expected_net = expected_gross - fees
  - Skip if expected_net < MIN_TRADE_PROFIT (₹500)

### 7D — Paper Trading Engine (Week 2)
- [ ] paper_trading.py
  - Real signals, simulated execution
  - Fills at next-bar open + slippage simulation
  - Separate P&L tracking from live
  - Runs in parallel with live mode
  - Generates same alerts labeled [PAPER]

### 7E — Options Execution (Week 3)
- [ ] execution/options_executor.py
  - Fetch live NFO options chain
  - Select liquid strike nearest to target delta
  - Construct NSE:SYMBOL+DATE+STRIKE+CE/PE string
  - Place order with correct lot size
  - Greeks-based exit (close when theta > premium)
  - Roll position when near expiry

### 7F — Audit Trail (Week 3)
- [ ] audit_log.py
  - Append-only SQLite table — never UPDATE/DELETE
  - Log: every signal, every order, every fill, every rejection
  - Log: every manual override, mode change, kill switch event
  - Export to CSV for review

---

## Phase 8 — Multi-User Platform 🔴 NOT STARTED
**Only start after single-user version profitable for 4+ weeks**

### Legal Prerequisites (Do Before Code)
- [ ] Consult CA/lawyer — SEBI regulations for managing others' money
- [ ] Understand SEBI registered investment advisor (RIA) requirements
- [ ] Structure: fee-based advisory vs discretionary management

### 8A — User Management
- [ ] User model: user_id, name, broker_credentials (encrypted), capital, risk_settings
- [ ] Credential encryption (Fernet symmetric encryption — never plain text in DB)
- [ ] Per-user Fyers token management
- [ ] Per-user capital and risk parameter isolation

### 8B — Multi-User Execution
- [ ] All orders tagged with user_id
- [ ] Position sizing per user's capital (not global TOTAL_CAPITAL)
- [ ] Per-user daily loss limit and kill switch
- [ ] Per-user portfolio tracking and P&L

### 8C — Client Dashboard
- [ ] Separate client view (read-only, shows their P&L only)
- [ ] Admin view (sees all users, aggregate stats)
- [ ] Monthly P&L statements (PDF export)
- [ ] Commission calculation (e.g. 20% of profits above high watermark)

### 8D — Infrastructure
- [ ] Move from laptop to cloud (AWS/GCP — always-on VM)
- [ ] Process separation (data engine / trading engine / intelligence)
- [ ] Redis for inter-process communication
- [ ] PostgreSQL instead of SQLite (for multi-user concurrent access)
- [ ] HTTPS for dashboard (nginx reverse proxy)
- [ ] Automated backups

---

## Phase 9 — US Markets + Advanced Features 🔴 FUTURE
- [ ] Alpaca execution (paper + live)
- [ ] US market theme detection (Fed, earnings, macro)
- [ ] Cross-market correlation (Nifty vs SPX relationship)
- [ ] Reinforcement learning — self-improving strategy parameters
- [ ] Earnings play strategies (strangle before earnings)
- [ ] Sector rotation model

---

## Deployment Checklist (Before Going Live)
- [ ] Phase 7A complete and tested in paper mode
- [ ] Phase 7B complete — bot survives overnight unattended
- [ ] 2 weeks paper trading — win rate > 50%, profit factor > 1.3
- [ ] Kill switch tested — confirms it actually stops orders
- [ ] One manual order tested via API (not bot) to confirm Fyers execution
- [ ] SL order placement confirmed on Fyers order book
- [ ] Telegram alerts tested end-to-end
- [ ] Dashboard accessible and live P&L updating
- [ ] .env backed up securely (not in git)
- [ ] generate_token.py scheduled in Task Scheduler
- [ ] nightly_agent.py scheduled at 8:30 PM
- [ ] weekly_agent.py scheduled Sunday 9:00 AM

---

## How to Continue in a New Claude Session

1. Paste this into the new chat:

```
I am building an autonomous trading bot for NSE/BSE.
GitHub repo: [YOUR REPO URL]
Current status: [copy the phase you're on from this file]
Next task: [copy the specific item to build]

Read the PLAN.md in the repo for full context.
Continue from where we left off.
```

2. Claude will read the repo and continue without re-explaining anything.

---

## Key Design Decisions (Don't Change Without Good Reason)
- Python over .NET — trading ecosystem, libraries, community
- FastAPI over Django — lightweight, async, perfect for this use case
- Single HTML dashboard — no npm, no build step, easy to modify
- SQLite over PostgreSQL — sufficient for single user, zero ops overhead
- Simulation mode default — never live without explicit opt-in
- MANUAL mode default — never auto-execute without explicit trust
- Intelligence layer async — never blocks the trading engine
- GTT orders for SL — persist even if bot crashes

---

## Risk Warnings (Read Before Every Session)
- Never disable the kill switch
- Never set RISK_PER_TRADE_PCT above 2%
- Never run live without confirmed SL orders on broker
- Never skip paper trading validation
- Test every new feature in paper mode first
- Keep .env file backed up — losing it means regenerating all tokens