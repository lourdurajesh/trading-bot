# Unified Execution Architecture — Technical Spec

Status: DRAFT for approval · Author: pairing session 2026-06-22
Supersedes the ad-hoc split between `order_manager`/`portfolio_tracker` (main),
`paper_trading` (learning mirror wallet), `learning_engine`, and
`commodity_options_learning`.

---

## 1. Problem statement

Today the bot runs **three parallel trade systems**, each with its own sizing,
fees, P&L and storage. The same symbol can appear with different share counts
and P&L on different tabs, and a fee change requires edits in several files.

| System | Signals | Sizing | Fees | Store | Tab |
|---|---|---|---|---|---|
| Main pipeline | TrendFollow, options, institutional | `sizing.shares_to_fit`, **1.5%×TOTAL_CAPITAL** | order_manager est. | `trades` (portfolio_tracker) | Trading |
| Learning mirror | SimpleRSI/Momentum, DirectionalOptions | inline **1%×wallet** | paper_trading | `paper_trades` + `learning_trades` | Paper / Learning |
| Commodity (MCX) | MCX spreads | `sizing.lots_to_fit` | commodity engine | `commodity_learning_trades` | Commodity |

Consequence: paper trading does **not** faithfully predict live, so it can't be
trusted for go-live. Example: ABCAPITAL = 1491 shares (main, 1.5%) vs 1013
(learning, 1%) — two systems, two rules.

## 2. Target model — one trading system, two run-modes + a sandbox

Design it like a real broker stack: **one** signal→risk→size→execute→monitor→
ledger pipeline. Behaviour differs only by a small **RunContext**, never by
duplicated code.

```
Strategies → Signal → RiskEngine(size+fees+funds) → ExecutionEngine → Ledger → Monitor/Exit
                                   ▲                        ▲
                              RunContext               RunContext
```

### 2.1 RunContext (the only thing that varies)

| Field | LIVE | PAPER | LEARNING |
|---|---|---|---|
| `place_real_orders` | **true** (broker) | false (simulate fill) | false (simulate fill) |
| `enforce_funds` | **true** (margin/available) | true (sim wallet) | **false** (ignore capital) |
| `strategy_set` | curated 1–2 | curated 1–2 (same as LIVE) | **ALL strategies** |
| `risk_budget` | available funds × RISK% | wallet × RISK% | TOTAL_CAPITAL × RISK% (fixed) |
| `segments` | equity + options + **MCX** | equity + options + **MCX** | equity + options + MCX |
| `ledger mode tag` | LIVE | PAPER | LEARNING |

Rules:
- **LIVE and PAPER are mutually exclusive** — exactly one is active (existing
  `BOT_MODE`/`PAPER_TRADING` collapses into one `RUN_MODE = LIVE|PAPER`).
- **LEARNING is always-on**, runs in parallel for research. It is just
  PAPER execution with all strategies and no fund cap. (This is the "Learning &
  Paper may be merged" the requirement calls out — same machinery, different
  RunContext.)
- **MCX is a segment, not a system** — commodity signals flow through the same
  engine and are subject to the active RUN_MODE.

### 2.2 Single shared primitives (no duplication)

1. **Sizing** — `execution/sizing.py` (exists). Every path calls
   `shares_to_fit` / `lots_to_fit` with `risk_budget` from RunContext. Delete
   the inline 1%-of-wallet sizing in `paper_trading.mirror_learning_open` and any
   other private sizers.
2. **Fees** — NEW `execution/fees.py`: flat ₹20/order (Fyers reality).
   `entry_leg_fee(qty)→20`, `round_trip_fee(qty)→40`. Imported by the execution
   engine, the ledger P&L, and exposed to the dashboard via API so the frontend
   never hard-codes a fee.
3. **P&L** — one formula everywhere: `(exit−entry)×qty − fees` (direction-aware;
   options/MCX use `qty = lots×lot_size`). Open positions mark-to-market with
   `get_last_price` (live LTP, else last candle close).
4. **Ledger** — `execution/ledger.py` (exists) becomes the single position store
   with columns incl. `mode (LIVE|PAPER|LEARNING)`, `segment (EQUITY|OPTIONS|MCX)`,
   `strategy`, `qty`, `entry`, `exit`, `fees`, `pnl`, `status`. Replaces the split
   `trades` / `paper_trades` / `learning_trades` / `commodity_learning_trades`
   (old names retained as compatibility VIEWS during migration).
5. **Monitor/Exit** — `position_manager` drives every OPEN ledger row regardless
   of segment/mode; exits price from the instrument's own LTP (never the index).

### 2.3 Dashboard

- **Trading tab** = all OPEN positions for the active RUN_MODE (LIVE or PAPER),
  every segment incl. MCX, tagged by `segment`/`strategy`, consistent qty/P&L.
- **Learning tab** = ledger rows `mode=LEARNING` (research view, unchanged
  analytics).
- One number per trade, identical wherever it appears.

## 3. Why this satisfies the requirements

- "One live trading logic, no duplicate logic for Learning/Paper/MCX/Live" →
  one engine + one sizer + one fee module + one ledger; behaviour via RunContext.
- "Live considers funds & 1–2 strategies; Learning runs all, ignores funds" →
  RunContext `enforce_funds` + `strategy_set` + `risk_budget`.
- "Live & Paper exclusive" → single `RUN_MODE`.
- "MCX part of Live/Paper" → MCX is a segment in the same engine.
- "Learning & Paper may be merged" → Learning = PAPER context + all strategies.

## 4. Staged delivery (each stage shippable & verifiable, low→high risk)

**Stage 1 — `execution/fees.py` (single fee module).** Extract flat-fee helpers;
route `paper_trading`, `learning_engine`, ledger P&L, and the dashboard through
them. Removes fee duplication. *Verify:* P&L unchanged vs current flat values.

**Stage 2 — single sizer everywhere.** Route `mirror_learning_open` (and any
inline sizers) through `sizing.shares_to_fit`/`lots_to_fit` with a `risk_budget`
arg. Introduce `RunContext` and thread it into sizing. *Verify:* a given signal
sizes identically across LIVE/PAPER/LEARNING per its risk_budget.

**Stage 3 — RUN_MODE unification.** Collapse `BOT_MODE`/`PAPER_TRADING` into one
`RUN_MODE = LIVE|PAPER` (mutually exclusive); make LEARNING an explicit parallel
context. Execution engine reads RunContext for `place_real_orders`/`enforce_funds`.
*Verify:* LIVE places orders; PAPER simulates; LEARNING runs all-strategies/no-cap;
no path computes its own size/fees.

**Stage 4 — single ledger across segments.** Add `mode`/`segment` to the unified
ledger; migrate `trades`/`paper_trades`/commodity into it behind compat views;
point dashboard at it. *Verify:* counts & P&L reconcile to current per-tab totals.

**Stage 5 — MCX into LIVE/PAPER.** Feed commodity signals through the unified
engine so MCX can trade in PAPER/LIVE (not just learning) and appears on the
Trading tab. *Verify:* an MCX paper trade shows on Trading tab with correct P&L.

## 5. Safety / non-negotiables

- Live-money path (`order_manager._execute` live branch, SL placement, broker
  adapters) behaviour is **preserved**; we only inject RunContext and shared
  primitives. No change to broker call semantics.
- Every stage: backup `db/trades.db`, deploy via `git reset --hard origin/main`,
  restart, verify on server before next stage.
- Compatibility VIEWS keep old readers working during ledger migration.
- Feature-flag the ledger cutover so a failure falls back to the old tables.

## 6. Resolved decisions (2026-06-22)

1. **Strategy set = config-driven.** Add `LIVE_STRATEGIES` (settings/.env),
   default = current main-pipeline strategies. LEARNING always runs ALL.
   RunContext.strategy_set is populated from this config.
2. **LEARNING keeps a ₹5L sandbox wallet** for reporting (cumulative P&L from a
   ₹5L base). CRITICAL reconciliation so this does NOT reintroduce divergence:
   - Sizing still uses the shared sizer with `risk_budget = RISK_PER_TRADE_PCT ×
     base`, where the LEARNING base is the **fixed ₹5L starting capital** (NOT the
     current/compounding balance — that bug caused the ₹66L wallet).
   - Since TOTAL_CAPITAL == ₹5L, a symbol sizes identically in LIVE/PAPER/LEARNING.
   - The wallet is a P&L LEDGER (balance = 5L + Σrealised), it does NOT gate
     learning entries (LEARNING ignores fund availability) and does NOT feed back
     into the risk budget. Keep the per-trade-risk cap as a safety net.
3. **MCX-live deferred to Stage 5.** Prove unification on equity + index-options
   first (Stages 1–4); MCX stays learning/paper until then.
