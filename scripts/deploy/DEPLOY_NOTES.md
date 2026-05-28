# Deploy Notes — Bug Fixes (2026-05-27 / 2026-05-28)

Five bugs diagnosed across two sessions. All fixes are pure code changes.
**2026-05-27 fixes**: run the SQL script (fix_instruments_20260527.sql), then restart.
**2026-05-28 fixes**: code-only — no SQL or env changes needed.

---

## What Changed

### Bug 1 — IEP Lock-in Fix (conviction_scorer.py)
**File**: `intelligence/conviction_scorer.py`

**Problem**: The 09:10 IEP-refined re-score re-fetched the Fyers LTP proxy and got
a different price than the 09:00 reading. Since the NSE API was unavailable both times,
both calls used Fyers LTP. At 09:00 it read +0.50% gap (+2 IEP). By 09:10 the LTP had
moved to −0.14% (flat, 0 IEP). The 09:10 score overwrote the 09:00 entry in
`conviction_daily.json`, dropping the total score from +6 to +4 (below threshold 7).
Result: no NSE equity trades fired on a valid bullish setup day.

**Fix**: Three changes to `conviction_scorer.py`:
1. `_score_iep()` — immediately returns the stored IEP if today's saved value is non-zero
   (lock-in on first directional reading, never overwritten by a later neutral re-fetch).
2. `_save_daily_score()` — appends both runs (initial + iep_refined) instead of replacing.
   Both entries are preserved in `conviction_daily.json` for audit.
3. `get_for_date()` — when multiple entries exist for a day, returns the one with the
   highest absolute IEP score (so the lock-in check always sees the strongest reading).

**No env/config changes needed.**

---

### Bug 2 — SILVER Price Range Stale (commodity_instruments)
**Files**: `commodity_options_learning.py` (seed) + SQL script below

**Problem**: SILVER `max_price` was 200,000 (set ~2024 when silver was ₹95k/kg).
Silver now trades at ~₹265,000/kg. Every cycle logged:
  `SILVER no valid spot (got 266559.0, sym=MCX:SILVER26JULFUT)`
SILVER was completely excluded from signal generation and trading.

**Fix**:
- Seed in code: min=150,000 → max=400,000 (50% headroom each side of current price)
- DB: SQL script (see below)
- Also corrected `valid_months` from `[3,5,7,9,12]` to `[3,7,9,12]` — MCX full
  silver has no May delivery month.

---

### Bug 3 — CRUDEOILM / NATGASMINI Rollover Buffer Too Small
**Files**: `commodity_options_learning.py` (seed) + SQL script below

**Problem**: Both instruments had `rollover_buffer=3`. The rollover logic switches
to the next contract month when `today.day >= month_end − buffer`.
- CRUDEOILM expiry: ~19th of each month → needs buffer ≥ 12
- NATGASMINI expiry: last Wednesday (~26th) → needs buffer ≥ 5
- With buffer=3, the switch to June happened only on May 28. The May contracts
  expired on ~May 19 (CRUDEOILM) and May 27 (NATGASMINI). Result: 8+ days of
  `no valid spot (got None)` for both instruments.

**Fix**:
- CRUDEOILM rollover_buffer: 3 → 12
- NATGASMINI rollover_buffer: 3 → 5
- NATURALGAS rollover_buffer: was 3 in seed, now 5 (aligns with NATGASMINI)

---

### Bug 4 — MCX Trading on Holidays (commodity_options_learning.py + market_holidays.py)
**Files**: `commodity_options_learning.py`, `config/market_holidays.py`

**Problem**: `run_cycle()` had no holiday check — it only verified MCX market hours
(09:00–23:30 IST). On exchange holidays, `commodity_options.run_cycle()` in `main.py`
fired as normal because `_is_market_hours()` (which calls `is_trading_holiday()`) only
gates NSE equity strategies, not MCX commodity cycles.

**Fix**:
1. Added `is_mcx_holiday(d)` function to `config/market_holidays.py`.
   - Uses existing `NSE_HOLIDAYS` (national holidays common to both exchanges).
   - Added `MCX_EXTRA_HOLIDAYS` set for MCX-only closures (e.g. Muharram) — empty for
     now; update it each year from https://www.mcxindia.com/market-data/market-holidays
2. `run_cycle()` now calls `is_mcx_holiday(today)` at the top and returns early if True.
   Logs: `[CommOpts] MCX trading holiday (YYYY-MM-DD) — skipping entry scan`

**No env/config changes needed. To add a future MCX-only holiday:**
```python
# config/market_holidays.py → MCX_EXTRA_HOLIDAYS
date(2026, 7, 6),   # Muharram
```

---

### Bug 5 — 30-Min Opening Blackout Only Applied to BreakoutSpread
**File**: `commodity_options_learning.py`

**Problem**: The 30-minute opening blackout (`if now.hour == 9 and now.minute < 30`)
existed ONLY inside `_check_breakout_spread()`. The two higher-priority strategies —
`TrendSpread` (priority 1) and `RSIReversalSpread` (priority 2) — had no such guard.
Since `TrendSpread` fires first in the evaluation chain, a valid EMA crossover at 09:01
would fire a trade immediately, bypassing the intended 30-minute wait.

**Fix**:
1. Added `MCX_OPEN_WAIT_MINUTES = 30` module-level constant (configurable).
2. Moved the opening blackout check into `run_cycle()` — it now applies to ALL
   strategies before `_evaluate()` is called:
   ```python
   if now.hour == 9 and now.minute < MCX_OPEN_WAIT_MINUTES:
       return  # wait until 09:30 before any entry scan
   ```
3. Removed the duplicated `if now.hour == 9 and now.minute < 30: return None` from
   `_check_breakout_spread()` and replaced it with a comment referencing the central guard.

**No env/config changes needed. To change the opening wait (e.g. to 15 min):**
Edit `MCX_OPEN_WAIT_MINUTES = 15` in `commodity_options_learning.py` top-of-file constants.

---

## Deployment Steps

### Step 1 — Apply SQL to prod DB

```bash
# On the prod server, from the project root:
sqlite3 db/trades.db < scripts/deploy/fix_instruments_20260527.sql
```

Verify the output shows:
```
CRUDEOILM  |  2000  |  20000  |  12  |  []
NATGASMINI |   100  |   2000  |   5  |  []
SILVER     | 150000 | 400000  |   7  |  [3, 7, 9, 12]
```

### Step 2 — Pull and restart

```bash
git pull
# Restart however your prod bot runs, e.g.:
python main.py
# or if running as a service:
systemctl restart alphalens-bot
```

### Step 3 — Verify on next startup

In `logs/bot.log` check for:
```
[CommOpts] Loaded 23 instruments
Subscribed to N symbols — MCX: ...
```
Confirm `MCX:SILVER26JULFUT`, `MCX:CRUDEOILM26JUNFUT`, `MCX:NATGASMINI26JUNFUT`
appear in the subscription list (no more MAYFUT for energy minis).

At 09:00 and 09:10 tomorrow, confirm:
```
[PreMarket] Fyers LTP=XXXXX ...
[ConvictionScorer]   IEP [+X]: Pre-open IEP locked +X (bullish, first reading preserved)
```
The word **"locked"** means the fix is active. If you see **"stored"** it is the post-09:20 path.

**MCX opening blackout check** — in `logs/bot.log` between 09:00–09:29:
```
[CommOpts] Opening blackout active — Xmin until entry scan begins
```
At 09:30 the message disappears and normal cycle logs resume.

**MCX holiday check** — on the next trading holiday, the log should show:
```
[CommOpts] MCX trading holiday (YYYY-MM-DD) — skipping entry scan
```
instead of any cycle or entry activity.

---

## Rollback

If anything goes wrong the SQL change can be undone:
```sql
UPDATE commodity_instruments SET min_price=50000, max_price=250000,
       valid_months='[3, 7, 9, 12]' WHERE name='SILVER';
UPDATE commodity_instruments SET rollover_buffer=3 WHERE name IN ('CRUDEOILM','NATGASMINI');
```
The Python code change can be reverted with `git revert`.

---

## No .env Changes Required

All three fixes are code + DB only. No environment variables added or changed.

The existing `CONVICTION_THRESHOLD=7` in `.env` remains the right value once the
IEP lock-in fix is in place — the threshold was never the problem.
