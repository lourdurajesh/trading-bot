# Deploy Notes — Bug Fixes (2026-05-27)

Three bugs diagnosed from today's session. All fixes are in this commit.
Two steps to deploy on the prod server: run the SQL script, then restart the bot.

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
