# AlphaLens Trading Bot — Roadmap & Task Backlog

Last updated: 2026-05-16  
Status legend: `[ ]` pending · `[~]` in progress · `[x]` done

---

## A. Production Hardening (remaining items)

### A1. Infrastructure
- [ ] **DB backup cron job** — daily copy of `trades.db` to `/home/ubuntu/backups/trades_YYYYMMDD.db`, retain 30 days
  ```bash
  0 16 * * 1-5 /home/ubuntu/trading-bot/scripts/backup_db.sh
  ```
- [ ] **Log rotation** — add `/etc/logrotate.d/trading-bot` (daily, 30-day retain, compress)
- [ ] **UptimeRobot monitor** — external HTTP monitor on `/stats` endpoint, 5-min interval, SMS/email alert
- [ ] **API_HOST=127.0.0.1** in `.env` — lock dashboard to localhost, access via SSH tunnel only
- [ ] **DASHBOARD_API_KEY** — set a strong random secret in `.env` (already enforced in code, key not yet configured)
- [ ] **CORS lockdown** — `allow_origins=["*"]` in `dashboard_api.py` should be changed to `["http://localhost:3000"]` or the actual dashboard origin

### A2. Minor code gaps
- [ ] **`_peak_value` not persisted** — drawdown % resets to 0 on every restart; add to `db/risk_state.json`
  - File: `risk/portfolio_tracker.py` — save `_peak_value` in `_save_state()` / load in `_load_state()`
- [ ] **`_trade_counter` resets to 0** — trade IDs can collide if two trades open in the same second after restart
  - Fix: seed counter from `MAX(id)` of today's trades on startup
- [ ] **Market holiday list** — `config/market_holidays.py` must be updated every year (NSE publishes in Dec)
  - Schedule: first week of January each year

### A3. Before going live (paper → real money gate)
- [ ] Paper trading for minimum 4 consecutive weeks with no crashes
- [ ] All Telegram alerts firing correctly
- [ ] Token refresh working autonomously for 1 full week (11:45 PM auto-refresh)
- [ ] Watchdog surviving at least one VM reboot with auto-restart
- [ ] `PAPER_TRADING=false` only after all above are verified
- [ ] Capital set to real `TOTAL_CAPITAL` in `.env`
- [ ] Risk params reviewed: `RISK_PER_TRADE_PCT`, `DAILY_LOSS_LIMIT_PCT`, `MAX_PORTFOLIO_HEAT`

---

## B. Learning Feedback Loop (strategy param tuning)

The goal: use paper trade results to systematically improve strategy parameters rather than guessing.

### Phase 1 — Observability (build this first)

**B1. Persist param changes across restarts**
- [x] Create `db/config_overrides.json` — runtime param changes written here, loaded at startup before hardcoded defaults
- [x] Update `config/strategy_config.py` to load overrides from file on init; `reapply_all_overrides()` called from `StrategySelector.__init__` after all modules imported
- [x] Update `api/dashboard_api.py` POST `/config/strategies/{strategy}/{param}` — now writes to file on every change; also added `_require_api_key` guard (was unprotected)
- [x] Files: `config/strategy_config.py`, `api/dashboard_api.py`, `strategies/strategy_selector.py`

**B2. Param change audit trail**
- [x] Added `param_changes` table to `trades.db` (created in `portfolio_tracker._init_db()`)
- [x] Written on every dashboard param change via `strategy_config._record_param_change()`
- [x] Exposed via `GET /param-changes?limit=N&strategy=X` in dashboard API

**B3. Richer edge_monitor hints for all strategies**
- [x] Extended `_STRATEGIES` list to include `ShortTrend`, `MomentumReversal`, `GapFade`
- [x] Per-strategy `TuningHint` dataclass with `rule`, `text`, `confidence` (0–1)
- [x] Common rules (all equity strategies): stop-hit rate >60%, MAE≈stop, MFE>>target, RANGING regime WR collapse
- [x] Strategy-specific rules: RSI threshold tightness (SimpleRSI/MeanReversion), CONVICTION_THRESHOLD direction (InstitutionalMomentum), MAX_IV_RANK (DirectionalOptions), MIN_GAP_PCT (GapFade)
- [x] `_load_trades()` now fetches `exit_reason` from both DBs for stop-hit analysis
- [x] File: `analysis/edge_monitor.py`

**B4. Weekly tuning Telegram summary**
- [x] `_send_alert()` rewritten: degraded strategies, hints with confidence %, last param change date, "wait — only N trades since last change" warning
- [x] Hints filtered to confidence ≥ 60% in Telegram output
- [x] `_get_last_param_changes()` and `_trades_since_last_change()` helpers added
- [x] File: `analysis/edge_monitor.py`

### Phase 2 — Controlled tuning process (manual workflow)

Weekly ritual every Monday after receiving the edge report:

```
1. Read edge report Telegram message (auto-sent at 8:45 AM)
2. For each WARN or ALERT strategy:
   a. Minimum 30 paper trades required before changing anything
   b. Check param_changes table — was this param changed in last 30 days?
      → If yes: skip, wait for more data
      → If no: apply the suggested change via dashboard
   c. Log reason in the dashboard change entry
3. Never change more than 1 param per strategy per week
4. Strategy on ALERT for 3 consecutive weeks → disable it
5. Review again in 4 weeks to assess impact
```

**B5. Backtesting before applying changes**
- [ ] Before applying any param change to live paper trading, run a backtest first:
  ```bash
  python run_backtest.py --strategy trend_follow --param ATR_STOP_MULTIPLIER=2.0 --days 90
  ```
- [ ] `run_backtest.py` currently exists — verify it accepts per-param overrides
- [ ] Add a quick comparison report: "current params vs proposed params" over last 90 days

### Phase 3 — Semi-automated suggestions (after 3 months of Phase 2)

- [ ] Edge monitor generates hint with a confidence score (0–1)
- [ ] If confidence > 0.8 AND trade_count > 50 AND no change in 30 days:
  - Draft a proposed change and send to Telegram as a **pending approval** message
  - Dashboard shows "Suggested change — approve / reject"
  - Only applies after explicit approval — never silent auto-apply
- [ ] File: `analysis/edge_monitor.py`, `api/dashboard_api.py`

---

## C. Scheduled Tasks (maintenance calendar)

| When | Task | How |
|---|---|---|
| Daily 16:00 IST | DB backup | cron: `backup_db.sh` |
| Every Monday 8:45 AM | Edge monitor + tuning hints | Already automated |
| Every Monday 9:00 AM | Review edge report and apply/reject hints | Manual |
| First week of January | Update `config/market_holidays.py` for new year | Manual |
| Every 3 months | Review open positions P&L vs benchmarks | Manual |
| Every 6 months | Full strategy review — disable underperformers | Manual |

---

## D. Future Enhancements (nice to have)

### D1. Risk & execution
- [ ] **Partial fill guard** — if `fill_qty < signal.position_size × 0.8`, abort and cancel (currently just warns)
- [ ] **Options Greeks tracking** — record delta, gamma, theta at entry in `options_meta`; use for DTE-based exit refinement
- [ ] **Slippage tracking** — record `expected_entry` vs `actual_fill` per trade; alert if avg slippage > 0.15%

### D2. Intelligence
- [ ] **Regime-aware param switching** — maintain two param sets per strategy (trending / ranging), auto-switch based on `regime_detector` output
- [ ] **FII data integration in real-time** — currently collected at 17:30, could influence next-day conviction score more dynamically
- [ ] **Conviction scorer for individual stocks** — currently only `BANKNIFTY`; extend to top 10 NSE stocks

### D3. Infrastructure
- [ ] **Nginx reverse proxy + HTTPS** — if dashboard ever needs to be accessed remotely without SSH tunnel
- [ ] **Prometheus metrics endpoint** — expose bot health (open positions, daily PnL, last tick age) for Grafana dashboard
- [ ] **Structured logging (JSON)** — replace plain-text logs with JSON lines for easier log aggregation and search
- [ ] **Multi-day backtest comparison CLI** — compare strategy performance across different market regimes (trending Q1 vs choppy Q2)

### D4. Learning engine improvements
- [ ] **Regime-conditional learning stats** — `get_stats()` currently global; add `by_regime` breakdown
- [ ] **Time-of-day attribution** — which hours produce the best setups per strategy (already in daily analytics for production trades, add to learning engine)
- [ ] **Auto-generated strategy scorecards** — weekly PDF/text summary per strategy: win rate, avg R, best/worst exit reason, regime fit

---

## E. Known Technical Debt

| Item | File | Priority | Notes |
|---|---|---|---|
| `_peak_value` not persisted | `portfolio_tracker.py` | Low | Drawdown resets on restart, reporting only |
| `_trade_counter` resets | `portfolio_tracker.py` | Low | ID collision extremely unlikely |
| Old positions lack `original_stop_loss` | `trades.db` | Low | Pre-fix positions use legacy SL inference |
| `allow_origins=["*"]` | `dashboard_api.py` | Medium | Needs to be locked down |
| `edge_monitor` hints only for SimpleRSI | `edge_monitor.py` | High | Phase 1 item B3 above |
| Param changes not persisted across restarts | `strategy_config.py` | High | Phase 1 item B1 above |
| Alpaca stream has no gap fill on reconnect | `alpaca_stream.py` | Medium | Fyers has it, Alpaca does not |

---

## F. Completed (session 2026-05-16)

- [x] Bug 1: Double-exit race condition — PENDING_CLOSE status + startup reconciliation
- [x] Bug 2: Duplicate SL-M orders — cancel old SL before placing new, store `sl_order_id`
- [x] Bug 3: Naked options leg on partial fill — rollback all placed legs on failure
- [x] Bug 4: Orphaned broker position on fill timeout — final check after cancel
- [x] Bug 5: Partial exit size not persisted — `update_position_size()` added
- [x] Bug 6: State reconstruction uses moved SL — `original_stop_loss` column added
- [x] Bug 7: SL-M missing product code for swing positions
- [x] Bug 8: Options exit qty always 0 — pass actual position size
- [x] Bug 9: FyersStream max reconnects not enforced — alert + system_health on feed dead
- [x] Bug 10: Watchdog 30-min pause with no info — lists open positions in alert
- [x] Bug 11: Cooldowns — already persisted (verified, no change needed)
- [x] Bug 12: Token refresh restart — warns about open swing positions
- [x] Bug 13: Dashboard API unauthenticated — `X-API-Key` on all mutating endpoints
- [x] Bug 14: Partial fill qty unknown — warns and logs, does not silently default
- [x] Bug 15: Trailing stop frozen after breakeven — uses `original_stop_loss` for risk
- [x] Bug 16: PAPER_TRADING inconsistency — single source in `config/settings.py`
- [x] Bug 17: Order ID whitespace mismatch — `.strip()` on both sides
- [x] Bug 18: `closed_trades` empty on restart — today's trades loaded from DB
- [x] `t1_hit` boolean column — exact T1 state reconstruction after restart
- [x] `options_meta` column — options positions now survive restarts correctly
- [x] `os._exit(0)` → `sys.exit(0)` — allows atexit/finally blocks on shutdown
