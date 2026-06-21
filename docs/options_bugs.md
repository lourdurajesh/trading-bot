# Options Trading Bug Fixes

Diagnosed from `logs/bot.log`. Three bugs prevent options strategies from
ever executing a real trade. Fix in this order — each one unblocks the next.

---

## Bug 1 — DirectionalOptions: Entry = Stop → always rejected  ❌ CRITICAL

**File:** `strategies/directional_options.py`

**Symptom from logs:**
```
SIGNAL SHORT NSE:NIFTY50-INDEX | Entry: 50.84 | SL: 50.84 | RR: 0.0
OrderManager: REJECTED NSE:NIFTY50-INDEX: Signal failed basic validity check
```
1,014 signals fired, 429 OrderManager rejections — 100% rejection rate.

**Root cause:**
`options_executor.get_best_option()` returns `None` (no live chain data),
so the code uses the simulation fallback:

```python
debit_cost = spot * iv * 0.015
```

But `spot` is returning ~50.84 instead of Nifty's real value (~22,000).
The guard `if not ltp or ltp < 1000: return None` should block this, but
the logged entry price of 50.84 proves the wrong LTP is leaking through.
`debit_cost` ends up equal to `stop_loss` (both ~50), so `is_valid()` fails.

**Fix — `strategies/directional_options.py`:**
1. Add a sanity-check after the fallback block:
```python
if debit_cost <= 0 or debit_cost > spot * 0.05:
    self.log_skip(symbol, f"Debit cost {debit_cost:.2f} invalid for spot {spot:.2f}")
    return None
```
2. Add a debug log to expose the bad value at runtime:
```python
logger.debug(f"[DirectionalOptions] spot={spot}, iv={iv:.2f}, debit={debit_cost:.2f}")
```
3. Check `options_executor.get_best_option()` — if Fyers options chain API
   is not set up, `opt` will always be `None` and the fallback will keep
   running. Fix the chain fetch or disable the strategy until it's wired up.

---

## Bug 2 — IronCondor + OptionsIncome: never fire  ❌ CRITICAL

**Files:** `strategies/iron_condor.py`, `strategies/options_income.py`

**Symptom:** Zero signals from either strategy across entire log history.

**Root cause:** Both strategies gate on IV rank:
- `OptionsIncome` requires `iv_rank > 50`
- `IronCondor` requires `40 < iv_rank < 80`

`options_engine.get_iv_rank()` is returning `None` for all symbols —
no live IV data from Fyers. Both strategies log-skip silently every cycle.

**Verify:** Add a temporary log in `analysis/options_engine.py`:
```python
logger.info(f"[OptionsEngine] IV rank {symbol}: {iv_rank}")
```

**Fix — `analysis/options_engine.py`:**
Use India VIX as a fallback proxy for Nifty IV rank. VIX ~15 = low (rank ~30),
VIX ~20 = normal (rank ~50), VIX ~25+ = high (rank ~70+).

Or for a quick unblock — use a neutral fallback so strategies can at least
be tested:
```python
# In options_engine.get_iv_rank(), if live data unavailable:
if iv_rank is None or iv_rank < 0:
    logger.warning(f"[OptionsEngine] IV rank unavailable for {symbol}, using 50")
    return 50.0
```

---

## Bug 3 — All options strategies fire outside market hours  ❌

**Symptom from logs:**
```
2026-03-24 01:00:46  SIGNAL SHORT NSE:NIFTY50-INDEX ...
2026-03-24 01:29:48  SIGNAL SHORT NSE:NIFTY50-INDEX ...
```
Signals every minute from 01:00–01:29 AM IST. NSE closes at 15:30.

**Root cause:** None of the three options strategies have a market hours
check. Equity strategies have `OPENING_BLACKOUT_END` but options have nothing.

**Fix — add to top of `evaluate()` in all three options strategy files:**
```python
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN  = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)

now = datetime.now(tz=_IST).time()
if not (_MARKET_OPEN <= now <= _MARKET_CLOSE):
    return None
```

Files to update:
- `strategies/directional_options.py`
- `strategies/iron_condor.py`
- `strategies/options_income.py`

---

## Bug 4 — FundGuard vetoes Nifty50 as "earnings in 0 days"  ⚠️ LOGIC ERROR

**Symptom from logs:**
```
FundGuard VETO NIFTY50: Earnings in 0 days (24-Mar-2026) — swing trade blocked
```
1,832 total vetoes. Nifty50 is an index — it has no earnings date.
This blocks every DirectionalOptions signal on Nifty even when the other
bugs are fixed.

**Root cause:** `intelligence/fundamental_guard.py` is applying equity
earnings logic to index symbols. It's picking up a spurious date artefact.

**Fix — `intelligence/fundamental_guard.py`:**
```python
def check(self, symbol: str, ...) -> ...:
    if "-INDEX" in symbol:
        return None   # indices have no earnings — bypass guard
    ...
```

---

## Fix order

| # | Bug | File(s) | Effort |
|---|---|---|---|
| 1 | Market hours gate — stops overnight signal spam | `directional_options.py`, `iron_condor.py`, `options_income.py` | 5 min |
| 2 | FundGuard index bypass — removes 1,832 wasted vetoes | `intelligence/fundamental_guard.py` | 2 min |
| 3 | DirectionalOptions entry/stop sanity check + debug log | `strategies/directional_options.py` | 15 min |
| 4 | IV rank fallback so IronCondor/OptionsIncome can fire | `analysis/options_engine.py` | 20 min |

Fix 1 and 2 first — they're one-liners and clean up the log noise that
makes debugging 3 and 4 harder.

After all four fixes, run the bot in paper mode during market hours and
check logs for:
- `[DirectionalOptions] SIGNAL` with `RR > 0` and no rejection
- `[IronCondor] SIGNAL` or `[OptionsIncome] SIGNAL` appearing
- No overnight signals
- No FundGuard vetoes on index symbols
