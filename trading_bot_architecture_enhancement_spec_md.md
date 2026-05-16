# Trading Bot Enhancement Specification (Incremental Architecture Upgrade)

## Objective

Enhance the existing stable options trading bot architecture without disrupting current working logic.

The goal is to improve:

- Market regime understanding
- Trade quality filtering
- Observability and diagnostics
- Options intelligence
- Risk architecture
- Statistical analysis capability

The current system behavior and execution framework should remain largely intact.

This is an incremental enhancement project, NOT a rewrite.

---

# Core Design Principles

## Preserve Existing Stable Logic

Do NOT:
- rewrite conviction engine
- redesign existing strategy logic
- change existing order execution flow unnecessarily
- alter stable broker integration behavior

Enhancements must be:
- modular
- feature-flag enabled
- backward compatible

---

# Existing System Assumptions

Current bot already supports:

- Conviction scoring
- TrendSpread / BreakoutSpread strategies
- Options spread execution
- Strike selection
- Risk thresholds
- Broker integration
- Live/paper trading modes

---

# Enhancement Architecture

```text
Market Data Layer
    ↓
Regime Detection Layer
    ↓
Strategy Enablement Layer
    ↓
Signal Engine
    ↓
Trade Quality Filters
    ↓
Risk Engine
    ↓
Execution Engine
    ↓
Observability & Analytics
```

---

# PHASE 1 — Observability & Diagnostics

---

## TASK 1 — Trade Decision Audit Logging

### Objective

Log EVERY:
- trade
- rejected trade
- no-trade decision

for post-analysis.

---

## Requirements

Create structured logs/table:

```text
TradeDecisionAudit
```

### Fields

| Field | Type |
|---|---|
| timestamp | datetime |
| symbol | string |
| strategy | string |
| conviction_score | float |
| threshold | float |
| market_regime | string |
| IV | float |
| IV_percentile | float |
| PCR | float |
| VIX | float |
| breadth_score | float |
| VWAP_position | string |
| opening_range_state | string |
| decision | enum |
| rejection_reason | string |
| data_snapshot | json |

---

## Decision Enum

```text
TRADE
NO_TRADE
REJECTED_BY_FILTER
DISABLED_BY_REGIME
```

---

# TASK 2 — Trade Outcome Attribution

### Objective

Track WHY trades succeed/fail.

---

## Create

```text
TradeOutcomeMetrics
```

### Fields

| Field | Type |
|---|---|
| trade_id | guid |
| entry_time | datetime |
| exit_time | datetime |
| pnl | decimal |
| pnl_r_multiple | decimal |
| mfe | decimal |
| mae | decimal |
| theta_impact | decimal |
| IV_change | decimal |
| delta_entry | decimal |
| gamma_entry | decimal |
| vega_entry | decimal |
| spread_slippage | decimal |
| exit_reason | enum |

---

## Exit Reason Enum

```text
TARGET
STOPLOSS
TIME_EXIT
MANUAL_EXIT
RISK_ENGINE
EXPIRY_EXIT
```

---

# TASK 3 — Market Regime Classification Engine

### Objective

Classify market state continuously.

---

## Regime Enum

```text
TREND_UP
TREND_DOWN
BREAKOUT
CHOPPY
MEAN_REVERSION
HIGH_VOLATILITY
LOW_VOLATILITY
```

---

## Inputs

Use:
- ADX
- VWAP behavior
- ATR expansion
- opening range breakout
- breadth
- VIX
- volume expansion

---

## Output

```text
market_regime_score
market_regime
confidence
```

---

# PHASE 2 — Intraday Market Intelligence

---

# TASK 4 — Breadth Engine

### Objective

Measure true market participation.

---

## Inputs

Track:
- advancing stocks
- declining stocks
- sector breadth
- volume breadth
- top-weight stock contribution

---

## Output Metrics

```text
advance_decline_ratio
sector_strength_map
volume_breadth_score
index_concentration_score
```

---

# TASK 5 — VWAP Acceptance Engine

### Objective

Improve institutional participation detection.

---

## Do NOT use simple:

```text
price > VWAP
```

---

## Track

| Metric |
|---|
| time_above_vwap |
| rejection_count |
| reclaim_count |
| vwap_distance |
| vwap_slope |

---

## Output

```text
VWAP_ACCEPTED
VWAP_REJECTED
VWAP_NEUTRAL
```

---

# TASK 6 — Opening Range Engine

### Objective

Understand open behavior.

---

## Inputs

First:
- 5m
- 15m
- 30m ranges

---

## Detect

| State |
|---|
| OPENING_DRIVE |
| OPEN_REJECTION |
| GAP_FILL |
| ORB_BREAKOUT |
| INSIDE_RANGE |

---

# PHASE 3 — Options Intelligence

---

# TASK 7 — IV Percentile Engine

### Objective

Contextualize implied volatility.

---

## Requirements

Calculate:
- IV percentile
- IV rank
- historical IV bands

---

## Storage

```text
IVContext
```

---

## Outputs

| Output |
|---|
| iv_percentile |
| iv_rank |
| volatility_regime |

---

# TASK 8 — Greeks Snapshot Capture

### Objective

Capture option sensitivity at entry.

---

## Store

| Greek |
|---|
| delta |
| gamma |
| theta |
| vega |

for:
- long leg
- short leg
- net position

---

# TASK 9 — Spread Quality Engine

### Objective

Reject poor-quality spreads.

---

## Filters

| Filter |
|---|
| bid_ask_spread |
| liquidity |
| open_interest |
| RR_ratio |
| theta_efficiency |
| premium_efficiency |

---

## Output

```text
spread_quality_score
spread_rejection_reason
```

---

# PHASE 4 — Strategy Orchestration

---

# TASK 10 — Separate Macro Bias from Entry Logic

### Current Problem

Global conviction blocks all strategies.

---

## New Architecture

```text
Macro Bias Layer
    ↓
Strategy Enablement
    ↓
Entry Trigger
```

---

## Example

| Macro Bias | Allowed |
|---|---|
| Neutral | Breakout only |
| Bullish | TrendSpread |
| Bearish | Put spreads |
| Choppy | Disable directional |

---

# TASK 11 — Strategy Enablement Matrix

### Create configuration-driven matrix.

---

## Example

```json
{
  "TREND_UP": ["TrendSpread"],
  "BREAKOUT": ["BreakoutSpread"],
  "CHOPPY": [],
  "HIGH_VOLATILITY": ["CreditSpread"]
}
```

---

# TASK 12 — Cooldown Engine

### Objective

Prevent repeated losses in poor conditions.

---

## Example Rules

```text
3 failed breakout trades
→ disable breakout strategy for session
```

---

## Cooldown Triggers

| Trigger |
|---|
| consecutive_losses |
| volatility_spike |
| slippage_spike |
| spread_widening |

---

# PHASE 5 — Risk Architecture

---

# TASK 13 — Daily Risk Budget Engine

### Hard Limits

| Rule |
|---|
| max_daily_loss |
| max_strategy_drawdown |
| max_open_positions |
| max_correlated_positions |

---

## Mandatory

Risk engine must:
- override strategy engine
- force shutdown when breached

---

# TASK 14 — Volatility-Aware Position Sizing

### Objective

Adapt exposure based on volatility.

---

## Example

| VIX Regime | Position Size |
|---|---|
| Low | 1.2x |
| Neutral | 1x |
| High | 0.5x |

---

# TASK 15 — Liquidity Protection Engine

### Reject trades with:

| Condition |
|---|
| low OI |
| wide spreads |
| abnormal slippage |
| illiquid strikes |

---

# PHASE 6 — Analytics & Research

---

# TASK 16 — Expectancy Analytics Dashboard

### Aggregate by:

| Dimension |
|---|
| strategy |
| market regime |
| IV percentile |
| weekday |
| expiry week |
| time of day |

---

## Metrics

| Metric |
|---|
| win_rate |
| profit_factor |
| sharpe |
| expectancy |
| drawdown |
| avg_MAE |
| avg_MFE |

---

# TASK 17 — Missed Trade Tracker

### Objective

Track filtered-out opportunities.

---

## Log

```text
signal_generated = true
trade_taken = false
reason = threshold_rejection
```

---

## Later Compare

| Compare |
|---|
| hypothetical pnl |
| expectancy impact |
| drawdown impact |

---

# TASK 18 — Shadow Mode Validation

### Objective

Test new features safely.

---

## Requirements

New logic must support:

```text
shadow_mode = true
```

Meaning:
- evaluate
- score
- simulate
- DO NOT execute live

---

# Technical Requirements

---

# Feature Flags

Every new module must support:

```json
{
  "enabled": true
}
```

for safe rollout.

---

# Backward Compatibility

If module fails:
- existing system must continue functioning.

---

# Persistence

All new metrics/logs should be persisted in:
- PostgreSQL preferred
- time-series optimized schema where applicable

---

# Performance Constraints

Enhancements must NOT:
- materially delay execution
- block order placement
- freeze market data processing

Target:
- sub-second strategy evaluation

---

# Non-Goals

Do NOT implement:

- machine learning models
- AI prediction engines
- neural networks
- reinforcement learning
- auto-optimization loops

Current priority is:
robust market-state architecture

not predictive AI.

---

# Recommended Implementation Order

## Highest Priority

1. Audit logging  
2. Regime engine  
3. IV percentile  
4. VWAP engine  
5. Spread quality engine  

---

## Medium Priority

6. Breadth engine  
7. Opening range engine  
8. Cooldown engine  
9. Risk budgeting  

---

## Later

10. Expectancy dashboard  
11. Missed-trade analytics  
12. Shadow mode experimentation  

---

# Success Criteria

Enhancements should improve:

| Metric | Expected Outcome |
|---|---|
| drawdown | lower |
| overtrading | lower |
| trade selectivity | higher |
| expectancy stability | higher |
| regime adaptability | higher |
| observability | dramatically higher |

WITHOUT:
- materially reducing execution stability
- destabilizing existing profitable logic
- increasing operational complexity excessively.

