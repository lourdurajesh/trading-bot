---
name: trading-review
description: Daily analysis of AlphaLens trading bot activity. Reads db/trades.db and logs/bot.log to produce a structured P&L report, strategy breakdown, conviction score audit, open position status, anomaly detection, and fine-tuning recommendations. Use when user says "review my trades", "daily review", "how did the bot do", "/trading-review", or asks about trade performance, strategy results, conviction scores, or bot anomalies for any date.
---

# Trading Bot Daily Review

## Quick Start

```
/trading-review              # today (IST)
/trading-review 2026-05-26   # specific past date
```

Run from **D:\Tech\trading-bot** (project root).

## Workflow

### Step 1 — Run the data gatherer
```bash
cd D:\Tech\trading-bot
python .claude/skills/trading-review/scripts/daily_report.py
# or for a specific date:
python .claude/skills/trading-review/scripts/daily_report.py --date 2026-05-26
```
Capture the full output — it is the raw feed for your analysis.

### Step 2 — Analyse in this order

1. **Conviction Score** — Did IEP/FII/OI align? Was threshold met? Any IEP drift between 09:00 and 09:10? Flag if `iep_score` changed between runs.
2. **P&L Summary** — Total PnL, win rate, avg R, daily loss % vs 3% hard limit.
3. **Open Positions** — Hours open, trailing stop status, DTE remaining, SL distance.
4. **Closed Trades** — Walk through each: right strategy for the setup? entry quality? exit quality?
5. **Strategy Breakdown** — TrendSpread / BreakoutSpread / RSIReversal win rates today vs all-time.
6. **Anomalies** — `no valid spot`, `risk budget` blocks, false breakouts, missed entries (blocked by cap/cooldown despite valid signal).
7. **Cooldowns Active** — What instruments are blocked tonight/tomorrow and until when.

### Step 3 — Output Format

```
## Daily Review — YYYY-MM-DD

### Conviction Score
| Signal | 09:00 | 09:10 | Delta |
[row per signal + total row]
Verdict: TRADE DAY ✅ / NO TRADE ❌ — [one-line reason]

### P&L Summary
| Metric | Value |
| Total trades | N (closed: N, open: N) |
| Wins / Losses | N / N (WR: N%) |
| Total PnL | ₹XX,XXX |
| Best trade | +₹X,XXX (+X.XXR) SYMBOL STRATEGY |
| Worst trade | -₹X,XXX (-X.XXR) SYMBOL STRATEGY |
| Daily loss used | X.X% of 3.0% limit |
| Wallet balance | ₹X,XX,XXX |

### Open Positions  [skip section if none]
[one line per: SYMBOL | DIR | entry spot | current SL | hours open | unrealised PnL estimate]

### What Went Right
[bullet per winning trade — why entry was valid, what the bot executed well]

### What Went Wrong
[bullet per losing trade — root cause, what should have been different]

### Strategy Performance
| Strategy | Today W/L | Today PnL | All-time W% | All-time PnL | Avg R |

### Anomalies 🔴🟠🟡
[numbered list — severity emoji, description, recommended fix referencing REFERENCE.md]

### Recommendations
[numbered, specific, actionable — reference exact config param or code location]
```

## Reference
See [REFERENCE.md](REFERENCE.md) for: signal definitions, R-multiple system,
strategy rules, instrument config guide, rollover buffer calendar, anomaly patterns,
env vars, and deployment checklist.
