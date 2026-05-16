-- =============================================================================
-- update_commodity_params.sql
-- Populates per-instrument SL / trailing / target parameters in the
-- commodity_instruments table.
--
-- Deploy:
--   sqlite3 db/trades.db < scripts/update_commodity_params.sql
--
-- Parameters explained:
--   sl_debit_pct          : exit when spread mark-to-market loss = this % of net_debit
--   trail_debit_pct       : trailing SL distance = this % of net_debit from peak spot
--   trail_trigger_pct     : trailing activates after gain = this % of net_debit
--   target_pct            : initial profit target = this % of max_profit
--   target_upgraded_pct   : target upgrades to this % after strong momentum
--   target_upgrade_mult   : upgrade fires when spot moves >= mult × SL distance
--
-- Tiers:
--   GOLD/GOLDM        → Medium-low IV (0.18). Tight SL, high target.
--   SILVER/variants   → High IV (0.28-0.32). Wide SL to survive intraday noise.
--   CRUDEOIL/M        → Medium-high IV (0.35-0.38). Balanced.
--   NATURALGAS/MINI   → Highest IV (0.45-0.48). Widest SL; weather spikes.
--   COPPER/M          → Medium IV (0.25-0.28). Tight; range-bound instrument.
--   BASE METALS misc  → Low-medium IV. Tight SL, moderate target.
--   AGRI              → High uncertainty. Conservative targets.
-- =============================================================================

-- ── Precious Metals: Gold family ─────────────────────────────────────────────
-- IV ~0.18: smaller daily moves, less noise → tighter SL is safe
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.35,
    trail_debit_pct      = 0.25,
    trail_trigger_pct    = 0.45,
    target_pct           = 0.65,
    target_upgraded_pct  = 0.80,
    target_upgrade_mult  = 2.0
WHERE name IN ('GOLD', 'GOLDM', 'GOLDGUINEA', 'GOLDPETAL');

-- ── Precious Metals: Silver family (IV 0.28-0.32, wide SL for intraday noise) ─
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.50,
    trail_debit_pct      = 0.35,
    trail_trigger_pct    = 0.50,
    target_pct           = 0.65,
    target_upgraded_pct  = 0.82,
    target_upgrade_mult  = 2.0
WHERE name IN ('SILVER', 'SILVERM', 'SILVERMIC');

-- ── Energy: Crude Oil family ──────────────────────────────────────────────────
-- IV ~0.35-0.38: moderate-high volatility. EIA inventory day = spike risk.
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.40,
    trail_debit_pct      = 0.30,
    trail_trigger_pct    = 0.50,
    target_pct           = 0.65,
    target_upgraded_pct  = 0.80,
    target_upgrade_mult  = 2.0
WHERE name IN ('CRUDEOIL', 'CRUDEOILM');

-- ── Energy: Natural Gas family ────────────────────────────────────────────────
-- IV ~0.45-0.48: highest volatility in our universe.
-- US EIA storage report (Thursdays) causes 3-6% spikes. Widest SL.
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.55,
    trail_debit_pct      = 0.40,
    trail_trigger_pct    = 0.55,
    target_pct           = 0.65,
    target_upgraded_pct  = 0.80,
    target_upgrade_mult  = 2.5
WHERE name IN ('NATURALGAS', 'NATGASMINI');

-- ── Base Metals: Copper family ────────────────────────────────────────────────
-- IV ~0.25-0.28: follows LME. Range-bound with occasional breakouts.
-- Keep SL tight — Copper has poor win rate on breakout strategy (0/4 this week).
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.38,
    trail_debit_pct      = 0.28,
    trail_trigger_pct    = 0.45,
    target_pct           = 0.65,
    target_upgraded_pct  = 0.78,
    target_upgrade_mult  = 2.0
WHERE name IN ('COPPER', 'COPPERM');

-- ── Base Metals: Nickel family ────────────────────────────────────────────────
-- IV ~0.35-0.38: similar to crude, sensitive to EV battery demand news.
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.42,
    trail_debit_pct      = 0.30,
    trail_trigger_pct    = 0.50,
    target_pct           = 0.65,
    target_upgraded_pct  = 0.78,
    target_upgrade_mult  = 2.0
WHERE name IN ('NICKEL', 'NICKELM');

-- ── Base Metals: Zinc, Aluminium, Lead ───────────────────────────────────────
-- IV ~0.20-0.26: lower volatility, tight ranges. Conservative params.
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.38,
    trail_debit_pct      = 0.28,
    trail_trigger_pct    = 0.45,
    target_pct           = 0.60,
    target_upgraded_pct  = 0.75,
    target_upgrade_mult  = 2.0
WHERE name IN ('ZINC', 'ZINCMINI', 'ALUMINIUM', 'ALUMINI', 'LEAD', 'LEADMINI');

-- ── Agricultural ─────────────────────────────────────────────────────────────
-- IV ~0.30-0.40: seasonal and weather-driven moves. High uncertainty.
-- Conservative target — don't hold through macro shock reversals.
UPDATE commodity_instruments SET
    sl_debit_pct         = 0.45,
    trail_debit_pct      = 0.35,
    trail_trigger_pct    = 0.55,
    target_pct           = 0.60,
    target_upgraded_pct  = 0.75,
    target_upgrade_mult  = 2.0
WHERE name IN ('COTTON', 'MENTHAOIL');

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT
    name,
    printf('%.2f', sl_debit_pct)        AS sl_pct,
    printf('%.2f', trail_debit_pct)     AS trail_pct,
    printf('%.2f', trail_trigger_pct)   AS trigger_pct,
    printf('%.2f', target_pct)          AS target_pct,
    printf('%.2f', target_upgraded_pct) AS upgraded_pct,
    enabled
FROM commodity_instruments
ORDER BY name;
