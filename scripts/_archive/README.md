# Archived one-time scripts

These migrations/seeds have **already been applied** to the production database.
They are kept only for disaster-recovery / rebuild-from-scratch. Do not run them
against the live DB without checking the target tables don't already exist.

| Script | Applied | Purpose |
|--------|---------|---------|
| `migrate_audit_tables.py` | yes | Created `trade_decision_audit` / `trade_outcome_metrics`. |
| `migrate_commodity_instruments.py` | yes | Created `commodity_instruments` (MCX_CONTRACTS). |
| `cleanup_index_trades.py` | yes | One-off cleanup of bad index trades. |
| `seed_fii_history.py` | yes | Seeded historical FII participant data. |
