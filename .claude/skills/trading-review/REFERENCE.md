# Trading Review — Reference

## Conviction Scoring

Score range −10 to +10. **Default threshold = 7** (env: `CONVICTION_THRESHOLD`).

| Signal | Max pts | Source | Logic |
|--------|---------|--------|-------|
| FII F&O net | ±3 | NSE participant data (prev day) | Day-over-day net change vs baseline |
| OI / PCR | ±2 | Options chain close snapshot | PCR < 0.7 → −2 bearish; > 1.3 → +2 bullish |
| IEP gap | ±2 | NSE pre-open API / Fyers LTP proxy | Only valid 09:00–09:20 IST |
| India VIX | ±1 | `db/vix_history.json` 5-day MA | VIX falling → +1; rising → −1 |
| Gift Nifty | ±1 | S&P 500 overnight via macro_collector | ±0.5% threshold |
| BankNifty RS | ±1 | 5-day return BN vs Nifty | Spread > +0.5% → +1 |

**IEP gap thresholds**:
- > +0.50% → **+2** (strong gap-up)
- +0.15% to +0.50% → **+1** (mild gap-up)
- −0.15% to +0.15% → **0** (flat)
- −0.50% to −0.15% → **−1** (mild gap-down)
- < −0.50% → **−2** (strong gap-down)

**Capital allocation by score**:
- score ≥ 9 → `MAX_FO_CAPITAL_PCT` (default 50%)
- score 7–8 → 35%
- score < 7 → 0% (no NSE equity trades)

**Known IEP issue (fixed v1.1 — `intelligence/conviction_scorer.py`)**:
The 09:10 re-score overwrote the 09:00 reading in `conviction_daily.json`. A strong
early IEP (+2) was silently erased if the Fyers LTP proxy shifted to neutral by 09:10.
Fix: lock-in on first non-zero IEP score for the day.

---

## R-Multiple Reference

Every commodity trade risks exactly **1R** = `net_debit × lot_size`.

| pnl_r | Interpretation |
|-------|---------------|
| +0.9 to +1.1 | Excellent — target hit |
| +0.5 to +0.9 | Good — trailing stop caught most of move |
| 0 to +0.5 | Breakeven zone |
| −0.1 to 0 | False-breakout / quick exit (minimal damage) |
| −0.5 to −0.3 | Partial SL hit |
| ≈ −1.0 | Full stop loss |

**Expectancy** = (win_rate × avg_win_R) − ((1−win_rate) × avg_loss_R). Target > +0.10R.

---

## Strategy Rules

### TrendSpread (dominant strategy — target 50%+ win rate)
- **Signal**: `EMA5 vs EMA20` divergence ≥ 0.3%. RSI 35–50 bearish / 50–65 bullish.
- **Spread**: PUT debit spread (SHORT) or CALL debit spread (LONG). ATM + 1–2 strikes OTM.
- **SL**: Spot-based (`sl_price` in metadata). Closes if spot crosses SL in adverse direction.
- **Target**: `target_pct` % of max_profit (default 65%). Upgrades to `target_upgraded_pct` (82%) if spot moves 2× initial target.
- **Trailing**: Activates at `trail_trigger_pct` (45%). Trails by `trail_debit_pct` (35%) of original debit.
- **Config keys**: `target_pct`, `trail_debit_pct`, `trail_trigger_pct`, `sl_debit_pct`

### BreakoutSpread (**low win rate — 22% all-time — needs gates**)
- **Signal**: Price breaks above/below a key level by `buffer` amount.
- **Problem**: Triggers in ranging markets where EMA5 ≈ EMA20. Buffer of 1–2 points on NICKEL (≈0.06%) is noise-level.
- **Recommended gates to add**: (1) EMA5 must diverge from EMA20 by ≥ 0.5% in breakout direction; (2) RSI < 40 for SHORT or > 60 for LONG; (3) buffer ≥ 0.25% of spot.
- **FALSE_BREAKOUT exit**: Fires if spot reverts back through the breakout level within the check cycle.

### RSIReversalSpread (limited data — 3 trades)
- **Signal**: RSI oversold (<30) → LONG spread; overbought (>70) → SHORT spread.
- **Risk**: Counter-trend. Never enter if EMA5 diverges strongly from EMA20 in the other direction (> 0.5%).
- **Recommended gate**: Require `abs(EMA5 − EMA20) / EMA20 < 0.005` (within 0.5% of each other) before taking reversal.

---

## Instrument Config (commodity_instruments table)

All columns editable via SQL or dashboard API. Bot reads on startup.

| Column | Effect | Update trigger |
|--------|--------|---------------|
| `min_price` | Spot < min → "no valid spot" | Silver rose above 250k → update |
| `max_price` | Spot > max → "no valid spot" | Same |
| `rollover_buffer` | Day ≥ (month_end − buffer) → switch to next month | MCX expiry moved |
| `valid_months` | JSON array of allowed expiry months | Contract schedule changed |
| `target_pct` | Close at this fraction of max_profit | Win rate too low → lower it |
| `sl_debit_pct` | SL as fraction of debit paid | Too many full SL hits → lower it |
| `trail_trigger_pct` | Activate trailing at this fraction | Too many give-backs → lower it |
| `trail_debit_pct` | Trail tightness | Exiting too early → lower it |

### Rollover Buffer Guide (MCX expiry calendar)

| Instrument | MCX expiry | Buffer needed | Current (DB) |
|-----------|-----------|--------------|-------------|
| CRUDEOIL | ~19th of month | 14 | 14 ✅ |
| **CRUDEOILM** | ~19th of month | **12** | 3 ❌ bug |
| NATURALGAS | ~last business day | 5 | 5 ✅ |
| **NATGASMINI** | Last Wednesday | **5** | 3 ❌ bug |
| GOLD | 5th of contract month | 28 | 28 ✅ |
| GOLDM | 5th of alt months | 7 | 7 ✅ |
| SILVER | Restricted months | 7 | 7 ✅ |
| COPPER / COPPERM | Last day | 5 | 5 ✅ |

### Price Range: When to Update

If log shows `no valid spot (got XXXXX, sym=MCX:ABCFUT)`:
- `min_price` = current_spot × 0.60
- `max_price` = current_spot × 1.50
- Run: `scripts/deploy/fix_instruments_YYYYMMDD.sql`

---

## Common Anomaly Patterns

| Log pattern | Meaning | Action |
|-------------|---------|--------|
| `no valid spot (got None)` | Contract symbol expired / wrong month | Increase `rollover_buffer` in DB |
| `no valid spot (got XXXXX)` | Price outside min/max | Update price range in DB |
| `daily entry cap reached (N/N)` | Per-instrument cap hit | Normal — check if cap too low |
| `blocked by risk budget: X%` | Daily loss limit approached | Normal kill-switch — review total loss |
| `IEP [+0]` after earlier `IEP [+2]` | IEP drift bug (pre v1.1) | Fixed in conviction_scorer.py |
| `Fyers LTP=` (no `NSE IEP=` line) | NSE API fell back to Fyers proxy | Check NSE API accessibility |
| `FALSE_BREAKOUT` within 5 min | Immediate reversal after breakout | Add EMA trend gate to BreakoutSpread |

---

## Environment Variables

Defined in `config/settings.py`. Override via `.env` file.

| Variable | Default | Notes |
|----------|---------|-------|
| `CONVICTION_THRESHOLD` | 7 | Lower to 6 cautiously — more trade days but lower quality |
| `DAILY_LOSS_LIMIT_PCT` | 3.0 | ₹15,000 on ₹5L capital |
| `TOTAL_CAPITAL` | 500000 | Base for all % calculations |
| `RISK_PER_TRADE_PCT` | 1.5 | % of capital risked per trade |
| `MAX_OPEN_POSITIONS` | 10 | Hard cap on concurrent positions |

---

## Deployment Checklist

After any change in this environment:

### SQL changes (instrument config)
1. Run `scripts/deploy/fix_instruments_YYYYMMDD.sql` on **prod** `db/trades.db`
2. Restart the bot — `commodity_options_learning.py` reloads DB on init
3. Verify first cycle log: correct symbol in `Subscribed to N symbols` line

### Python code changes (bug fixes)
1. Commit the change locally
2. On prod server: `git pull` then `systemctl restart trading-bot` (or equivalent)
3. Verify in bot.log: no tracebacks on startup, correct IEP log behaviour at 09:00/09:10

### .env changes
1. Edit `.env` on prod server
2. Restart the bot — settings.py reads env on import
3. Verify via `[Main] Conviction score` log line showing updated threshold
