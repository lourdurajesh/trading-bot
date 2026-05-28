-- ============================================================
-- AlphaLens Trading Bot — Instrument Config Fix
-- Date   : 2026-05-27
-- Bugs   : SILVER price range stale, CRUDEOILM/NATGASMINI rollover_buffer too small
-- Apply  : sqlite3 db/trades.db < scripts/deploy/fix_instruments_20260527.sql
-- Restart: python main.py   (reloads commodity_instruments on init)
-- ============================================================

-- ── 1. SILVER — price range update ─────────────────────────────────
-- Problem : max_price=250000 but silver spot is now ~₹2.65L/kg
--           → every cycle logs "no valid spot (got 266559.0)" and SILVER
--             is completely excluded from trading.
-- Fix     : Widen to 150k–400k (covers ₹1.5L–₹4.0L/kg, ~50% headroom each side)
-- Also    : Correct valid_months — MCX full silver has no May delivery ([3,7,9,12])
UPDATE commodity_instruments
SET    min_price    = 150000,
       max_price    = 400000,
       valid_months = '[3, 7, 9, 12]'
WHERE  name = 'SILVER';

-- ── 2. SILVERM / SILVERMIC — align seeds with current DB ───────────
-- DB already has 60k–500k for these two. Seeds are now fixed in code.
-- No DB change needed for SILVERM/SILVERMIC — values are already correct.
-- (This comment is intentional to document why they're not included here.)

-- ── 3. CRUDEOILM — rollover_buffer fix ─────────────────────────────
-- Problem : rollover_buffer=3 means the bot switches to the next month
--           only when day >= (month_end − 3), i.e. from the 28th.
--           But MCX Crude Oil Mini expires around the 19th each month.
--           From May 20 to May 27 the bot was still pointing to
--           MCX:CRUDEOILM26MAYFUT (expired) → "no valid spot (got None)".
-- Fix     : buffer=12 → switches when day >= 19 (31−12), covering the ~19th expiry.
UPDATE commodity_instruments
SET    rollover_buffer = 12
WHERE  name = 'CRUDEOILM';

-- ── 4. NATGASMINI — rollover_buffer fix ────────────────────────────
-- Problem : Same as CRUDEOILM. Natural Gas Mini expires on the last
--           Wednesday of the month (~26th for May-2026).
--           rollover_buffer=3 → switches only from the 28th — 2 days late.
-- Fix     : buffer=5 → switches when day >= 26 (31−5).
UPDATE commodity_instruments
SET    rollover_buffer = 5
WHERE  name = 'NATGASMINI';

-- ── 5. NATURALGAS — align rollover_buffer with NATGASMINI ──────────
-- Natural Gas full contract has same expiry schedule as NATGASMINI.
-- Was already 5 in DB — no change needed. Documenting for clarity.

-- ── Verify ─────────────────────────────────────────────────────────
SELECT name, min_price, max_price, rollover_buffer, valid_months
FROM   commodity_instruments
WHERE  name IN ('SILVER','SILVERM','SILVERMIC','CRUDEOILM','NATGASMINI','NATURALGAS')
ORDER  BY name;
