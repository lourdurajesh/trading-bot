# Trade Data Model & P&L Honesty

> The three trade tables, the key fields backtesting needs, and — critically — how to tell a
> **real** P&L from an **estimated** one. Updated 2026-06-16 (B1.1 / B1.3).

## The three trade tables

| Table | Written by | What it holds | P&L basis |
|-------|-----------|---------------|-----------|
| `trades` | production order/position managers | Live/paper production trades | Real fills (broker) |
| `learning_trades` | `learning_engine.py` | NSE equity + index-option paper trades | **Real** — option LTP at entry & exit |
| `commodity_learning_trades` | `commodity_options_learning.py` | MCX commodity-option spreads | Real chain-mark **or** estimate (now labeled) |

## P&L honesty — the `pnl_source` field (B1.1)

Every closed `commodity_learning_trades` row now records **how its P&L was derived**:

| `pnl_source` | Meaning | Trust |
|--------------|---------|-------|
| `CHAIN_MARK` | Marked to the **real** option spread (current ATM−OTM premiums) at close | ✅ real |
| `ESTIMATE` | `spot_move × spread_delta` heuristic — **not a real fill** | ⚠ estimate |

- At close, the engine first tries to fetch the live option chain and price both legs
  (`_realized_pnl_from_chain`). If both legs price → `CHAIN_MARK`. If the chain is unavailable
  (e.g. illiquid strike, off-session) → falls back to the labeled `ESTIMATE`.
- **All 131 historical commodity trades were `ESTIMATE`** (spot×delta) — they're now correctly
  labeled, so past "P&L" is no longer presented as if it were real.
- The dashboard shows a per-trade tag (`real` / `est*`) and a summary line counting each.

> Why this matters: you cannot optimize toward ₹1,000/day on a fictional number. `ESTIMATE` P&L
> is directional only; backtest/edge decisions (B3) must use real-marked trades.

## Key fields for backtesting (B1.3)

`commodity_learning_trades` captures: `strategy`, `instrument`, `direction`, `opt_type`,
`spot_at_entry`/`spot_at_exit`, `atm_strike`/`otm_strike`, `net_debit` (entry spread),
`max_profit`, `spread_width`, `rr`, `iv_used`, `lot_size`, `lots`, `dte`, `pnl_approx`, `pnl_r`,
`pnl_source`, `fees`, `data_source` (entry pricing), `exit_reason`, `entry_time`/`exit_time`,
`metadata` (spread-quality score, etc.).

`learning_trades` captures: `strategy`, `direction`, `entry_price`/`exit_price` (real option LTP),
`stop_loss`, `target`, `rr_planned`, `pnl_pts` (per-unit move), `pnl_r`, `fees`, `mae_pts`/`mfe_pts`,
`exit_reason`, `entry_time`/`exit_time`, `metadata` (instrument_type, lot_size, regime context).

**Still to populate (B2.2 cost model):** `fees` is captured as a column but defaults to 0 for
commodity trades until the real cost model lands. Net-of-cost P&L is a B2 task.

**Aggregates (not columns):** signals/day and win-rate/expectancy are computed at analysis time
from `entry_time` counts and `pnl_r` — no per-trade field needed.
