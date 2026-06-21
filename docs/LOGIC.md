# AlphaLens Trading Bot — Master Logic Reference

> Single source of truth for every strategy rule, parameter, threshold, formula,
> and exit condition in the bot. Update this file whenever logic changes in code.
>
> Last updated: 2026-05-13

---

## Table of Contents

1. [System Configuration](#1-system-configuration)
2. [Market Hours & Session Rules](#2-market-hours--session-rules)
3. [Regime Detector](#3-regime-detector)
4. [Strategy Selector — Routing Logic](#4-strategy-selector--routing-logic)
5. [Trend Follow Strategy](#5-trend-follow-strategy)
6. [Mean Reversion Strategy](#6-mean-reversion-strategy)
7. [Momentum Reversal Strategy](#7-momentum-reversal-strategy)
8. [Gap Fade Strategy](#8-gap-fade-strategy)
9. [Simple Momentum Strategy](#9-simple-momentum-strategy)
10. [Options Income Strategy (Short Strangle)](#10-options-income-strategy-short-strangle)
11. [Directional Options Strategy (Debit Spread)](#11-directional-options-strategy-debit-spread)
12. [Institutional Momentum Strategy](#12-institutional-momentum-strategy)
13. [Iron Condor Strategy](#13-iron-condor-strategy)
14. [Signal Validation Rules](#14-signal-validation-rules)
15. [Risk Manager — Trade Approval Pipeline](#15-risk-manager--trade-approval-pipeline)
16. [Position Manager — Exit Rules](#16-position-manager--exit-rules)
17. [Persistence & State Recovery](#17-persistence--state-recovery)
18. [Paper Trading Engine](#18-paper-trading-engine)
19. [Cooldown System](#19-cooldown-system)
20. [Known Bugs & Fixes (Incident Log)](#20-known-bugs--fixes-incident-log)

---

## 1. System Configuration

**File:** `config/settings.py`

### Capital & Risk

| Parameter | Default | Env Var | Notes |
|-----------|---------|---------|-------|
| TOTAL_CAPITAL | ₹5,00,000 | `TOTAL_CAPITAL` | Base portfolio value |
| RISK_PER_TRADE_PCT | 1.5% | `RISK_PER_TRADE_PCT` | Hard limit 3% — never exceed |
| MAX_OPEN_POSITIONS | 10 | `MAX_OPEN_POSITIONS` | No new trades beyond this |
| MAX_PORTFOLIO_HEAT | 60% | — | Sum of all open risks / capital |
| DAILY_LOSS_LIMIT_PCT | 3.0% | `DAILY_LOSS_LIMIT_PCT` | Triggers kill switch |

### Options-Specific Risk

| Parameter | Default | Notes |
|-----------|---------|-------|
| MAX_OPTIONS_ALLOCATION_PCT | 30% | Max capital in all options combined |
| MAX_OPTIONS_LOTS_PER_TRADE | 2 | Hard lot cap per single options trade |
| MIN_OPTION_LTP | ₹5.0 | Avoid near-zero premium options |
| MIN_OPTION_OI | 500 | Minimum open interest per strike |
| OPTIONS_DTE_FORCE_EXIT | 3 days | Force close when this close to expiry |
| OPTIONS_VIX_LIMIT | 27.0 | Block short premium strategies above this |
| DAILY_OPTIONS_LOSS_LIMIT_PCT | 2.0% | Stricter daily loss cap for options |
| MAX_OPTIONS_TRADE_PCT | 5.0% | Max capital per single options trade |

### Signal Quality Gates

| Parameter | Default | Notes |
|-----------|---------|-------|
| MIN_SIGNAL_CONFIDENCE | 0.65 | Below this → signal discarded |
| MIN_RISK_REWARD | 1.5 | Below this → signal discarded |
| SYMBOL_COOLDOWN_MINUTES | 60 | Lockout period after STOP / EOD / MAX_HOLD exit |

---

## 2. Market Hours & Session Rules

**File:** `main.py` → `_is_market_hours()`

### Active Trading Window

```
NSE Session:  09:15 – 15:30 IST (weekdays only)
```

- Weekends (Sat/Sun): bot sleeps entirely
- Outside NSE hours: `strategy_selector.run_cycle()` does NOT run
- The bot only trades NSE stocks — no US market hours session

### Entry Cutoff

**File:** `execution/order_manager.py` → `_execute()`

```
NSE Entry Allowed:  09:15 – 15:15 IST only
```

- New positions for any `NSE:` symbol are **blocked** after 15:15 IST
- This prevents the open → EOD-forced-close → re-signal loop
- EOD_EXIT_TIME in position_manager = 15:15 — must match this cutoff

### Opening Blackout (MeanReversion only)

```
MeanReversion Blocked:  09:15 – 09:44 IST
MeanReversion Active:   09:45 IST onwards
```

- First 30 minutes of NSE produce gap-distorted RSI and BB readings
- A gap-up open looks identical to a genuine overbought setup
- Wait for at least 2 completed 15m candles before trusting reversion signals
- See [Bug 4 in Section 15](#bug-4--meanreversion-fired-on-opening-candle-gap-noise)

---

## 3. Regime Detector

**File:** `analysis/regime_detector.py`  
**Timeframe used:** 1H candles  
**Cache:** 15 minutes per symbol (REGIME_REFRESH_MINUTES = 15)

### Regime Types

| Regime | Market Condition | Strategies Activated |
|--------|-----------------|---------------------|
| TRENDING | Strong directional momentum | TrendFollow, DirectionalOptions |
| RANGING | Sideways, oscillating | MeanReversion, OptionsIncome, IronCondor |
| BREAKOUT | Tight squeeze about to expand | TrendFollow, DirectionalOptions |
| VOLATILE | Extreme moves, ATR spike | DirectionalOptions |
| UNKNOWN | Insufficient data | None — symbol skipped |

### Detection Logic (evaluated top-down, first match wins)

#### VOLATILE
```
Trigger if:
  (ATR% > 3.0) OR (RSI > 85 OR RSI < 15)
```

#### BREAKOUT
```
bb_squeeze   = bb_width < 0.04
adx_rising   = adx > 20 AND adx[-1] > adx[-3]
price_thrust = abs(slope) > 1.0

Trigger if: bb_squeeze AND (adx_rising OR price_thrust)
```

#### TRENDING
```
adx_strong   = adx > 25
slope_steep  = abs(slope) > 0.3

Trigger if: adx_strong AND slope_steep
```

#### RANGING
```
adx_weak     = adx < 20
slope_flat   = abs(slope) < 0.3

Trigger if: adx_weak AND slope_flat
```

### Confidence Formula

```
confidence = 0.5 + 0.5 × (conditions_true / total_conditions)
```

- Minimum: 0.5 (even if all conditions barely pass)
- Maximum: 1.0 (all conditions strongly met)
- Bonus for extreme values (e.g. ADX > 35, slope > 0.5) adds to conditions_true count

---

## 4. Strategy Selector — Routing Logic

**File:** `strategies/strategy_selector.py`

### Symbol Evaluation Order (per cycle)

```
1. Is symbol on cooldown?         → skip
2. Has open position already?     → skip
3. _evaluate_symbol(symbol)
   a. Get regime (1H)
   b. Route to strategy by regime (see table below)
   c. Run intelligence layer (news, macro, Claude analyst)
   d. If approved → order_manager.submit(signal)
   e. If rejected by intelligence → 60-min cooldown
   f. If rejected by risk/margin  → 5-min cooldown
```

### Regime → Strategy Routing

| Regime | Primary (fast, no API) | Secondary (parallel, API calls) |
|--------|----------------------|--------------------------------|
| TRENDING | TrendFollow | DirectionalOptions |
| BREAKOUT | TrendFollow | DirectionalOptions |
| RANGING | MeanReversion | OptionsIncome + IronCondor (parallel) |
| VOLATILE | — | DirectionalOptions |
| UNKNOWN | skip | — |

### Cycle Logging Format

```
[StrategySelector] Cycle N — no signals | 26 symbols:
  no_data=X         # data store not ready (< 50 candles)
  regime_unknown=X  # regime could not be determined
  no_setup=X        # regime known but no signal triggered
  cooldown=X        # symbol in cooldown period
  open_pos=X        # position already open for this symbol
  invalid=X         # signal generated but failed is_valid() check
```

---

## 5. Trend Follow Strategy

**File:** `strategies/trend_follow.py`  
**Active Regime:** TRENDING, BREAKOUT  
**Signal Timeframe:** 1H (entry candle), confirmed on Daily

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| BREAKOUT_LOOKBACK | 20 bars | Close must exceed highest of last 20 bars |
| MIN_RVOL | 1.4 | Relative volume minimum |
| ATR_STOP_MULTIPLIER | 1.5 | Stop = entry − (1.5 × ATR) |
| TARGET_1_R | 2.0 | First target = entry + (2 × risk) |
| TARGET_2_R | 3.0 | Second target = entry + (3 × risk) |
| MIN_ADX | 20 | Minimum ADX for trend strength |
| MAX_RSI_ENTRY | 75 | Reject if already overbought |

### Entry Conditions (ALL must pass)

```
1. Regime = TRENDING or BREAKOUT
2. EMA alignment: EMA9 > EMA21 > EMA50 (all on 1H)
3. Breakout: close > max(high[-21:-1]) — 20-bar high breakout
4. Volume: relative_volume >= 1.4
5. RSI: rsi <= 75 (not extended)
6. ADX: adx >= 20
7. Daily confirm (if data available): daily_EMA9 > daily_EMA21
8. R:R: (target_1 - entry) / risk >= 1.5
9. Confidence: >= 0.65
```

### Position Sizing

```
Entry   = LTP
Stop    = Entry - (ATR × 1.5)
Risk    = Entry - Stop
Target1 = Entry + (2.0 × Risk)    ← 2R: partial exit 50% here
Target2 = Entry + (3.0 × Risk)    ← 3R: exit remaining 50%
Target3 = 52-week high (if > Target2, else 0)
```

### Confidence Scoring

| Factor | Max Weight | Thresholds |
|--------|-----------|------------|
| Regime quality | 0.20 | `regime.confidence × 0.20` |
| ADX strength | 0.20 | >35: 0.20 / >25: 0.14 / >20: 0.08 |
| Volume (RVOL) | 0.20 | >2.5: 0.20 / >2.0: 0.15 / >1.5: 0.10 / else: 0.05 |
| RSI quality | 0.15 | 55–70: 0.15 / 50–55 or 70–75: 0.08 |
| Daily alignment | 0.15 | bullish: 0.15 / else: 0.0 |
| Momentum score | 0.10 | `momentum / 10.0 × 0.10` |
| **Total** | **1.0** | Capped at 1.0 |

---

## 6. Mean Reversion Strategy

**File:** `strategies/mean_reversion.py`  
**Active Regime:** RANGING only  
**Signal Timeframe:** 15m (entry), filtered on 1H

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| RSI_OVERSOLD | 35 | Long entry threshold |
| RSI_OVERBOUGHT | 65 | Short entry threshold |
| BB_PROXIMITY_PCT | 0.005 | Price within 0.5% of band = "near band" |
| ATR_STOP_BUFFER | 0.5 | Extra ATR added beyond swing high/low |
| OPENING_BLACKOUT_END | 09:45 IST | No trades before this time |

### Entry Conditions — LONG Setup (ALL must pass)

```
1. Regime = RANGING
2. Time >= 09:45 IST (opening blackout elapsed)
3. RSI(14) on 15m < 35 (oversold)
4. LTP <= lower_BB × (1 + 0.005) — at or below lower band
5. EMA(50) on 1H direction = "up" or "neutral" (not in downtrend)
6. Stop = min(recent_swing_lows[-3:]) - (0.5 × ATR)
         [fallback: LTP - (2.0 × ATR) if no swings found]
7. Target_1 = EMA(21) on 15m (the mean to revert to)
8. Target_2 = Entry + (2.0 × Risk)
9. R:R: (Target_1 - Entry) / Risk >= 1.5
10. Confidence >= 0.65
```

### Entry Conditions — SHORT Setup (ALL must pass)

```
1. Regime = RANGING
2. Time >= 09:45 IST
3. RSI(14) on 15m > 65 (overbought)
4. LTP >= upper_BB × (1 - 0.005) — at or above upper band
5. EMA(50) on 1H direction = "down" or "neutral"
6. Stop = max(recent_swing_highs[-3:]) + (0.5 × ATR)
         [fallback: LTP + (2.0 × ATR) if no swings found]
7. Target_1 = EMA(21) on 15m
8. Target_2 = Entry - (2.0 × Risk)
9. R:R: (Entry - Target_1) / Risk >= 1.5
10. Confidence >= 0.65
```

### Confidence Scoring (same structure for LONG and SHORT)

| Factor | Max Weight | LONG Thresholds | SHORT Thresholds |
|--------|-----------|-----------------|-----------------|
| Regime quality | 0.25 | `regime.confidence × 0.25` | same |
| RSI extremity | 0.30 | <25: 0.30 / <30: 0.22 / <35: 0.15 | >75: 0.30 / >70: 0.22 / >65: 0.15 |
| Band penetration | 0.25 | >1% below: 0.25 / any below: 0.15 / near: 0.08 | >1% above: 0.25 / any above: 0.15 / near: 0.08 |
| Base pass bonus | 0.20 | always added if all conditions pass | same |
| **Total** | **1.0** | Capped at 1.0 | Capped at 1.0 |

### Important: Stop Loss Direction for SHORT Trades

For SHORT trades: `stop_loss > entry_price` is **correct and expected**.

```
Example: entry=2411.89, stop=2429.03, target=2372.09 (SHORT)
- Entry: sell short at 2411.89
- Stop:  buy back (loss) if price rises to 2429.03
- Target: buy back (profit) if price falls to 2372.09
```

This looks inverted when scanning the database but is correct behaviour.

---

## 7. Momentum Reversal Strategy

**File:** `strategies/momentum_reversal.py`
**Active Regime:** RANGING, VOLATILE
**Signal Timeframe:** 1H
**Hold Type:** Intraday

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| RSI_OVERBOUGHT | 82 | SHORT reversal trigger |
| RSI_OVERSOLD | 18 | LONG reversal trigger |
| MAX_ADX | 25 | ADX must be below — no strong trend |
| ATR_STOP_BUFFER | 0.5 | ATR buffer beyond swing extreme |
| TARGET_1_R | 1.5 | First target at 1.5R |
| TARGET_2_R | 2.5 | Second target at 2.5R |
| LOOKBACK_BARS | 10 | Bars checked for RSI extremity duration |
| MIN_RVOL | 1.3 | Volume spike required for capitulation signal |

### Entry Conditions (ALL must pass)

```
1. Regime = RANGING or VOLATILE (extremes extend in trending markets)
2. RSI >= 82 (SHORT) or <= 18 (LONG)
3. RSI has been extreme for >= 2 consecutive bars (exhaustion)
4. ADX < 25 (low trend strength)
5. RVOL >= 1.3 (volume spike = capitulation / euphoria)
6. R:R >= 1.5
7. Confidence >= 0.65
```

### SL and Target Calculation

```
LONG:
  swing_low = min(high[-10:-1])
  stop      = min(swing_low, entry) - (0.5 × ATR)
  risk      = entry - stop
  target_1  = entry + (1.5 × risk)
  target_2  = min(EMA21, entry + (2.5 × risk))   ← EMA21 as mean target if closer

SHORT:
  swing_high = max(high[-10:-1])
  stop       = max(swing_high, entry) + (0.5 × ATR)
  risk       = stop - entry
  target_1   = entry - (1.5 × risk)
  target_2   = max(EMA21, entry - (2.5 × risk))   ← EMA21 as mean target if closer
```

T2 anchors to EMA21 (the mean this reversal is snapping back to) rather than a pure R-multiple.

### Confidence Scoring

| Factor | Max Weight | Thresholds |
|--------|-----------|------------|
| RSI extremity | 0.30 | SHORT: ≥90: 0.30 / ≥85: 0.22 / ≥82: 0.14 |
| ADX weakness | 0.20 | <15: 0.20 / <20: 0.13 / <25: 0.07 |
| Volume spike | 0.20 | ≥3.0: 0.20 / ≥2.0: 0.14 / ≥1.5: 0.09 / else: 0.04 |
| Exhaustion bars | 0.15 | ≥5: 0.15 / ≥3: 0.09 / ≥2: 0.05 |
| Regime bonus | 0.15 | RANGING: 0.15 / VOLATILE: 0.08 |
| **Total** | **1.0** | Capped at 1.0 |

---

## 8. Gap Fade Strategy

**File:** `strategies/gap_fade.py`
**Active Regime:** Any (gap is the setup, not regime)
**Signal Timeframe:** 5m (first 15 min candles), filtered by 1H regime
**Hold Type:** Intraday

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| MIN_GAP_PCT | 0.5% | Minimum gap size to consider fading |
| RSI_OVERBOUGHT | 70 | Gap-up short requires RSI above this |
| RSI_OVERSOLD | 30 | Gap-down long requires RSI below this |
| TARGET_1_R | 1.5 | Fallback T1 if gap fill is inside entry |
| TARGET_2_R | 2.5 | Second target |
| STOP_ATR_BUFFER | 0.3 | ATR buffer beyond gap extreme |

### Entry Conditions (ALL must pass)

```
Gap-Up SHORT:
  1. Today open > yesterday close by >= 0.5%
  2. RSI >= 70 (overbought into the gap)
  3. Price is near today's high (inside the gap)
  4. R:R >= 1.5

Gap-Down LONG:
  1. Today open < yesterday close by >= 0.5%
  2. RSI <= 30 (oversold into the gap)
  3. Price near today's low
  4. R:R >= 1.5
```

### SL and Target Calculation

```
Gap-Up SHORT:
  stop     = today_high (first 3 bars) + (0.3 × ATR)
  risk     = stop - entry
  target_1 = prev_close                 ← gap fill (statistical primary target)
  target_2 = entry - (2.5 × risk)
  [if target_1 >= entry: target_1 = entry - (1.5 × risk)]  ← fallback

Gap-Down LONG:
  stop     = today_low (first 3 bars) - (0.3 × ATR)
  risk     = entry - stop
  target_1 = prev_close                 ← gap fill
  target_2 = entry + (2.5 × risk)
  [if target_1 <= entry: target_1 = entry + (1.5 × risk)]  ← fallback
```

T1 is structural — gaps fill ~70% of the time, so `prev_close` is the primary exit. T2 is a pure R-multiple fallback.

---

## 9. Simple Momentum Strategy

**File:** `strategies/simple_momentum.py`
**Active Regime:** TRENDING, BREAKOUT
**Signal Timeframe:** 1H
**Hold Type:** Intraday

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| ATR_MULT | 1.5 | Stop = entry ± (1.5 × ATR) |
| TARGET_R | 3.0 | Single target at 3R |
| MIN_BARS | 30 | Minimum candles required |

### SL and Target Calculation

```
LONG:
  stop   = ltp - (1.5 × ATR)
  target = ltp + (3.0 × risk)   ← single target, no partial exit

SHORT:
  stop   = ltp + (1.5 × ATR)
  target = ltp - (3.0 × risk)
```

No T2 — simpler single-target structure. Exit is all-or-nothing at 3R.

---

## 10. Options Income Strategy (Short Strangle)

**File:** `strategies/options_income.py`  
**Active Regime:** RANGING  
**Structure:** Sell OTM call + sell OTM put (net credit received)

### Eligible Symbols

```
NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, NSE:FINNIFTY-INDEX
```

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| MIN_IV_RANK | 50 | IV must be elevated to sell premium |
| MIN_DTE | 20 days | Minimum days to expiry |
| MAX_DTE | 45 days | Maximum days to expiry |
| SHORT_DELTA | 0.16 | ~1 SD OTM strikes |
| PROFIT_TARGET | 50% | Close when 50% of max credit captured |

### Entry Conditions

```
1. Regime = RANGING
2. IV Rank >= 50 (premium is rich enough to sell)
3. Live options chain available
4. Both OTM strikes selectable at target delta (0.16)
5. Net credit > 0
```

### Position Mechanics

```
Entry  = short_call_premium + short_put_premium  (credit received)
Stop   = entry × 2.0   (position value doubles — 2× max credit = exit)
Target = entry × 0.50  (keep 50% of credit)
```

### Confidence Formula

```
confidence = min(0.50 + (iv_rank - 50) / 100, 0.85)
```

---

## 11. Directional Options Strategy (Debit Spread)

**File:** `strategies/directional_options.py`  
**Active Regime:** TRENDING, BREAKOUT  
**Structure:** Buy ATM + Sell OTM call (bull spread) or put (bear spread)

### Eligible Symbols

```
NSE indices only (not individual equities)
LTP must be >= ₹1,000 (data quality guard)
```

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| MAX_IV_RANK | 40 | Only buy when options are cheap |
| MIN_DTE | 7 days | Short-dated for gamma |
| MAX_DTE | 21 days | |
| ATM_DELTA | 0.40 | Buy leg target delta |
| SPREAD_DELTA_DIFF | 0.5 | Sell leg is 0.5 delta lower |
| NET_DEBIT_RATIO | 0.65 | Net cost ≈ 65% of ATM premium |

### Direction Selection

```
Bullish (Call Spread):  bullish_ema_alignment AND RSI > 50
Bearish (Put Spread):   bearish_ema_alignment AND RSI < 50
```

### Position Mechanics

```
Debit   = ATM_option.ltp × 0.65  (net cost after OTM premium)
Stop    = Debit × 0.50            (exit if 50% of debit lost)
Target1 = (ATM_strike - OTM_strike) × 0.35  (max spread profit × 35%)
```

### Confidence Formula

```
confidence = min(regime.confidence × 0.90, 0.85)
```

---

## 12. Institutional Momentum Strategy

**File:** `strategies/institutional_momentum.py`
**Active Regime:** TRENDING, BREAKOUT
**Signal Timeframe:** 1H
**Hold Type:** Intraday (options)
**Structure:** Single ATM call or put (directional conviction trade)

### Overview

Uses the conviction scorer (FII/DII data, OI analysis, macro) to determine
institutional directional bias. Buys ATM options when conviction is high.

### SL and Target Calculation

```
Entry   = current ATM option premium (LTP)
Stop    = premium × 0.70     ← exit at 30% premium loss
Target1 = premium × 0.55     ← exit at 55% premium gain (profit expressed per unit)
```

These are **percentage-of-premium** gates, not R-multiples. The entire premium is
at risk, so the stop is tighter (30% loss exits) and target is asymmetric (55% gain).

### Position Sizing

```
capital_pct = conviction_score × scaling_factor   (35–50% of capital at high conviction)
lots        = min(capital_pct × TOTAL_CAPITAL / (premium × lot_size),
                  MAX_FO_CAPITAL_PCT-based cap,
                  MAX_OPTIONS_LOTS_PER_TRADE)
```

---

## 13. Iron Condor Strategy

**File:** `strategies/iron_condor.py`  
**Active Regime:** RANGING  
**Structure:** Short call spread + short put spread (4 legs, defined risk)

### Eligible Symbols

```
Indices:  NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, NSE:FINNIFTY-INDEX
Equities: RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK (if equity_condors enabled)
          Minimum stock price ≥ ₹500
```

### Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| MIN_IV_RANK | 40 | Minimum for premium worthwhile |
| MAX_IV_RANK | 80 | Above this = too much gamma risk |
| MIN_DTE | 21 days | Theta acceleration zone |
| MAX_DTE | 45 days | |
| SHORT_CALL_DELTA | 0.20 | ~1 SD OTM short call |
| SHORT_PUT_DELTA | 0.20 | ~1 SD OTM short put |
| WING_WIDTH_PCT | 0.02 | Long leg = 2% of spot away |
| PROFIT_TARGET | 50% | Close at 50% credit captured |
| STOP_MULT | 2.0 | Exit at 2× net credit (loss) |

### Construction

```
short_call_strike = chain strike nearest delta 0.20 (OTM call)
short_put_strike  = chain strike nearest delta 0.20 (OTM put)
wing_width        = max(round(spot × 0.02 / step) × step, step)
long_call_strike  = short_call_strike + wing_width
long_put_strike   = short_put_strike  - wing_width

gross_credit = short_call.ltp + short_put.ltp
debit_paid   = black_scholes(long_call) + black_scholes(long_put)
net_credit   = gross_credit - debit_paid
max_loss     = wing_width - net_credit
```

### Confidence Formula

```
iv_window   = MAX_IV_RANK - MIN_IV_RANK  (= 40)
iv_score    = (iv_rank - MIN_IV_RANK) / iv_window
confidence  = min(0.55 + iv_score × 0.30, 0.85)
```

---

## 14. Signal Validation Rules

**File:** `strategies/base_strategy.py` → `Signal.is_valid()`

All signals pass through `is_valid()` before the intelligence layer runs.

### Universal Checks

```
entry > 0          (required)
stop_loss > 0      (required)
target_1 > 0       (required)
0.0 <= confidence <= 1.0
```

### Direction-Specific Stop Logic

| Signal Type | Direction | Valid Stop Condition |
|-------------|-----------|---------------------|
| Equity / Futures | LONG | `stop_loss < entry` |
| Equity / Futures | SHORT | `stop_loss > entry` |
| Options SHORT (strangle, condor) | — | `stop_loss > entry` (value rises = loss) |
| Options LONG (debit spread) | — | `stop_loss < entry` (premium decays = loss) |

### R:R Calculation

```
risk     = abs(entry - stop_loss)
reward   = abs(target_1 - entry)
rr_ratio = reward / risk   (0.0 if risk == 0)
```

---

## 15. Risk Manager — Trade Approval Pipeline

**File:** `risk/risk_manager.py`

Every signal passes ALL checks sequentially before execution.

### Approval Pipeline

| Step | Check | Rejection Reason |
|------|-------|-----------------|
| 1 | Kill switch not active | `KILL_SWITCH_ACTIVE` |
| 2 | `signal.is_valid()` passes | `INVALID_SIGNAL` |
| 3 | R:R >= MIN_RISK_REWARD (1.5) | `RR_TOO_LOW` |
| 4 | Open positions < 10 | `MAX_POSITIONS` |
| 5 | No duplicate symbol already open | `DUPLICATE_SYMBOL` |
| 6 | Portfolio heat < 60% | `PORTFOLIO_HEAT` |
| 7 | Daily P&L loss < 3.0% | `DAILY_LOSS_LIMIT` |
| 8 | Options allocation < 30% | `OPTIONS_ALLOCATION` |
| 9 | Options risk gate (VIX, LTP, OI, lots) | `OPTIONS_RISK_GATE` |
| 10 | Position size > 0 shares | `ZERO_SIZE` |

### Position Sizing — Equity

```
risk_amount    = TOTAL_CAPITAL × (RISK_PER_TRADE_PCT / 100)
                 e.g. 500000 × 0.015 = ₹7,500 per trade

risk_per_share = entry - stop_loss
min_risk       = entry × 0.001   (guard: must be at least 0.1% of entry)

if risk_per_share < min_risk: reject

shares         = floor(risk_amount / risk_per_share)
capital_at_risk = shares × risk_per_share
```

### Position Sizing — Options

```
lots            = min(risk_budget / cost_per_lot,
                      cap_budget / cost_per_lot,
                      MAX_OPTIONS_LOTS_PER_TRADE)
lot_size        = from live options chain (NIFTY=75, BANKNIFTY=35, FINNIFTY=65)
units           = lots × lot_size
capital_at_risk = entry_premium × units
```

### Kill Switch Trigger

```
Triggered when: |daily_realised_pnl| / TOTAL_CAPITAL >= 3.0%
Effect:         All new orders blocked (both equity + options)
Reset:          Manual only — via dashboard /kill-switch/reset endpoint
```

---

## 16. Position Manager — Exit Rules

**File:** `execution/position_manager.py`

Runs on the **fast loop (every 5 seconds)** via `position_manager.check_all()`.

### Global Exit Constants

| Constant | Value | Notes |
|----------|-------|-------|
| EOD_EXIT_TIME | 15:15 IST | Intraday close — no overnight holding |
| MAX_HOLDING_DAYS | 20 days | Force close stale positions |
| PARTIAL_EXIT_PCT | 50% | Exit half at Target 1 |
| BREAKEVEN_TRIGGER | 1.0R profit | Move SL to entry price |
| TRAIL_TRIGGER | 1.5R profit | Begin trailing stop |
| DYNAMIC_TARGET_START_R | 3.0R | First dynamic milestone after T1 |
| DYNAMIC_TARGET_STEP | 1.0R | Advance target by this many R per milestone |

### LONG Position — Exit Decision Tree

```
1. time >= 15:15 IST          → EOD_FORCED exit (100%)
2. days_held >= 20            → MAX_HOLD exit (100%)
3. ltp <= effective_stop      → STOP exit (100%)
4. [after T1] advance dynamic target milestone if profit_r >= milestone
5. ltp >= target_2 (post T1)  → TARGET2 exit ONLY if dynamic target not active
6. ltp >= target_1 (first)    → TARGET1 partial exit (50%) + SL to BE + activate dynamic target
7. profit >= 1.5R             → update trailing stop (ratchet)
8. profit >= 1.0R             → move SL to breakeven (once)
```

### SHORT Position — Exit Decision Tree

```
1. time >= 15:15 IST          → EOD_FORCED exit (100%)
2. days_held >= 20            → MAX_HOLD exit (100%)
3. ltp >= effective_stop      → STOP exit (100%)
4. [after T1] advance dynamic target milestone if profit_r >= milestone
5. ltp <= target_2 (post T1)  → TARGET2 exit ONLY if dynamic target not active
6. ltp <= target_1 (first)    → TARGET1 partial exit (50%) + SL to BE + activate dynamic target
7. profit >= 1.5R             → update trailing stop (ratchet)
8. profit >= 1.0R             → move SL to breakeven (once)
```

### Trailing Stop Calculation

```
trail_distance = original_risk × 0.8   (0.8R)

LONG:  new_sl = ltp - trail_distance
       apply only if new_sl > current_sl  (ratchet — never moves down)

SHORT: new_sl = ltp + trail_distance
       apply only if new_sl < current_sl  (ratchet — never moves up)
```

The trailing stop is **persisted to SQLite** via `portfolio_tracker.update_stop_loss()` on every advance. On restart, it is restored from the persisted `stop_loss` field — no loss of protection.

### Dynamic Target (Ratcheting)

After T1 partial exit, the static T2 is replaced by a ratcheting milestone system:

```
On T1 hit:
  _dynamic_target_r[symbol] = 3.0   ← activate at 3R milestone

Each tick (when already_partial):
  if profit_r >= current_milestone:
    current_milestone += 1.0        ← advance: 3R → 4R → 5R → ...
    log alert: "Target extended to {N}R = ₹{price}"
```

The trailing stop remains the **sole exit trigger** once dynamic target is active.
The dynamic target only advances the milestone and fires a notification — it never
directly exits the position. This lets the bot ride strong moves (e.g. 8R) without
locking in an exit at a premature static T2.

```
Example — entry ₹100, SL ₹98 (risk=₹2):
  T1=₹104 hit  → sell 50%, SL→BE, dynamic target activated at 3R
  ₹106 (3R)   → milestone hit → alert "target extended to 4R = ₹108"
  ₹108 (4R)   → alert "target extended to 5R = ₹110"
  ₹107        → trailing stop (0.8R below ₹108 = ₹106.40) hit → STOP exit
```

### Options Position — Exit Rules

#### Debit Spread (buying premium)

```
1. time >= 15:15 IST          → EOD_FORCED exit
2. dte <= 3 days              → DTE_FORCE_EXIT
3. option_ltp <= entry × 0.50 → STOP (50% premium loss)
4. option_ltp >= target_1     → TARGET profit exit
```

#### Short Strangle / Iron Condor (selling premium)

```
1. time >= 15:15 IST              → EOD_FORCED exit
2. dte <= 3 days                  → DTE_FORCE_EXIT
3. position_value >= entry × 2.0  → STOP (value doubled)
4. position_value <= entry × 0.50 → TARGET (50% decay captured)
```

Options do **not** use trailing stops or dynamic targets (theta decay changes the math).

### Cooldown After Exit

After every STOP, EOD_FORCED, or MAX_HOLD close, a 60-minute cooldown is applied
to the symbol so the same signal cannot re-fire immediately:

```python
if reason in ("STOP", "EOD_FORCED", "MAX_HOLD"):
    strategy_selector.apply_cooldown(symbol)   # 60 min default
```

TARGET1 / TARGET2 exits do NOT apply cooldown — trade worked, symbol is re-evaluable.

---

## 17. Persistence & State Recovery

**Files:** `execution/position_manager.py`, `risk/risk_manager.py`, `risk/options_risk.py`

### Position Manager State

All in-memory tracking is persisted so restarts do not lose protection:

| State | How persisted | Restored on restart |
|-------|--------------|---------------------|
| Trailing SL / breakeven level | `portfolio_tracker.update_stop_loss()` writes to SQLite `stop_loss` column | Yes — `_reconstruct_state_from_position()` reads persisted `stop_loss` on first tick |
| `_partial_exited` | Inferred: LONG `stop_loss >= entry_price` = T1 was hit | Yes — reconstructed |
| `_breakeven_applied` | Same inference as above | Yes — reconstructed |
| `_trailing_stops` | Set to persisted `stop_loss` value on reconstruction | Yes |
| `_dynamic_target_r` | Set to `ceil(current_profit_r) + 1` on reconstruction | Yes — inferred from live price |

**Reconstruction logic (first tick per position after restart):**
```python
t1_was_hit = (direction == "LONG"  and pos.stop_loss >= entry) or
             (direction == "SHORT" and pos.stop_loss <= entry)
if t1_was_hit:
    restore _partial_exited, _breakeven_applied, _trailing_stops, _dynamic_target_r
```

### Risk Manager State

Persisted to `db/risk_state.json` on every mutation:

```json
{
  "kill_switch_active": true,
  "kill_switch_reason": "Daily loss 3.1% hit limit 3%",
  "daily_pnl": -15000.0,
  "daily_reset_date": "2026-05-13"
}
```

**Restore rules:**
- Kill switch → always restored regardless of date (stays active until manual reset)
- Daily P&L → only restored if `daily_reset_date == today` (new day = fresh start)

### Options Risk Gate State

Same pattern, persisted to `db/options_risk_state.json`:

```json
{
  "kill_switch_active": false,
  "kill_switch_reason": "",
  "daily_pnl": -2500.0,
  "daily_reset_date": "2026-05-13"
}
```

**Critical:** Without this persistence, a bot restart after hitting the daily loss limit
would silently clear the kill switch and resume trading.

---

## 18. Paper Trading Engine

**File:** `paper_trading.py`  
**Enabled via:** `PAPER_TRADING=true` in `.env`

### Fill Simulation

```
LONG entry  fill = LTP × (1 + 0.0005)   # +0.05% slippage
SHORT entry fill = LTP × (1 - 0.0005)   # -0.05% slippage
LONG exit   fill = LTP × (1 - 0.0005)
SHORT exit  fill = LTP × (1 + 0.0005)
```

### P&L Calculation

```
LONG gross P&L  = (exit_price - entry_price) × position_size
SHORT gross P&L = (entry_price - exit_price) × position_size
brokerage       = (entry_price + exit_price) × position_size × 0.0003  (0.03% per leg)
net P&L         = gross P&L - brokerage
```

### Database

- Table: `paper_trades` in `db/trades.db`
- Separate from live `trades` table
- Status: `OPEN` or `CLOSED`
- Exit reasons: `STOP`, `TARGET1`, `TARGET2`, `EOD_FORCED`, `MAX_HOLD`

---

## 19. Cooldown System

**File:** `strategies/strategy_selector.py`

### When Cooldown is Applied

| Event | Duration | Applied By |
|-------|----------|------------|
| STOP exit | 60 min (SYMBOL_COOLDOWN_MINUTES) | `position_manager._exit_position()` |
| EOD_FORCED exit | 60 min | `position_manager._exit_position()` |
| MAX_HOLD exit | 60 min | `position_manager._exit_position()` |
| Intelligence rejection | 60 min | `strategy_selector.run_cycle()` |
| Risk / margin rejection | 5 min | `strategy_selector.run_cycle()` |
| TARGET1 / TARGET2 exit | none | — |

### Cooldown Check

```python
# Per cycle, per symbol:
if strategy_selector._is_on_cooldown(symbol):
    skipped_cooldown += 1
    continue
```

Cooldowns are stored in memory (`_cooldowns: dict[str, datetime]`). They reset
if the bot restarts — this is acceptable since restarts also reload data.

---

## 20. Known Bugs & Fixes (Incident Log)

### Bug 1 — `_is_market_hours()` ran NSE strategies during US market hours

**Discovered:** 2026-04-08  
**File:** `main.py` → `_is_market_hours()`  
**Severity:** Critical — caused ₹10.9L phantom paper losses

**What happened:**
A US market hours block (`19:00–01:30 IST`) caused `_is_market_hours()` to return
True for NSE stocks 6+ hours after market close. Strategy evaluation ran overnight,
generating signals on NESTLEIND and HINDUNILVR every 60 seconds.

**Fix:** Removed US market hours block entirely. Bot is NSE-only; US block is not needed
until Phase 9 (US markets). `_is_market_hours()` now returns True only for NSE hours
(09:15–15:30 IST, weekdays).

---

### Bug 2 — EOD forced-close + missing cooldown = infinite re-entry loop

**Discovered:** 2026-04-08  
**Files:** `execution/position_manager.py`, `execution/order_manager.py`  
**Severity:** Critical — amplified Bug 1 into 120× NESTLEIND phantom trades

**What happened:**
Any position opened after 15:15 IST was immediately force-closed as EOD_FORCED
(since current time >= 15:15). After closing, `_open_positions` was emptied — so
`has_open_position()` returned False — and the signal fired again next cycle.
Combined with Bug 1 (overnight evaluation), NESTLEIND was entered 120 times
(₹7,956 loss each = ₹-9,54,740 phantom).

**Fix 1 — Entry guard in `order_manager._execute()`:**
```python
# Block NSE entries outside 09:15–15:15 IST
if signal.symbol.startswith("NSE:"):
    if not (nse_open <= now_ist.time() <= eod_cutoff):
        return  # blocked
```

**Fix 2 — Cooldown wired to `position_manager._exit_position()`:**
```python
if reason in ("STOP", "EOD_FORCED", "MAX_HOLD"):
    strategy_selector.apply_cooldown(symbol)
```

---

### Bug 3 — Cooldown never applied after real trade close (stop hit)

**Discovered:** 2026-04-08  
**File:** `execution/position_manager.py` → `_exit_position()`  
**Severity:** High — caused 6× duplicate TCS SHORT entries in 7 minutes

**What happened:**
`strategy_selector.apply_cooldown()` was documented as "called after a losing trade"
but was never wired to the actual trade close event. After each stop hit, the symbol
had no cooldown → re-evaluated next cycle → same signal → stop hit again. TCS was
entered and stopped out 6 times in 7 minutes (09:15–09:22, 2026-04-01).

**Actual unique loss:** ₹-22,335 (6 trades)  
**Would have been:** ₹-7,259 (1 trade, first entry)

**Fix:** See Bug 2 Fix 2 above — same code patch.

---

### Bug 4 — MeanReversion fired on opening candle gap noise

**Discovered:** 2026-04-08  
**File:** `strategies/mean_reversion.py` → `evaluate()`  
**Severity:** Medium — caused all 7 TCS trades to be structurally wrong setups

**What happened:**
The first 15m candle at 09:15 includes the overnight gap. A gap-up open produces
RSI > 65 and price at upper BB — identical to a genuine overbought reversal setup.
The regime detector classified TCS as RANGING (from prior day 1H data), but TCS was
actually in a slow gap-continuation uptrend. All 6 SHORT trades hit the same stop
(₹2429.03 = prior swing high) before reverting.

**Fix:** 30-minute opening blackout added as step 0 in `evaluate()`:
```python
OPENING_BLACKOUT_END = dtime(9, 45)

if datetime.now(tz=IST).time() < OPENING_BLACKOUT_END:
    return None  # skip — wait for gap noise to settle
```

**Why 09:45 and not 09:30:**
The 09:30 candle is still influenced by opening auction prints. 09:45 ensures
at least 2 full intraday 15m candles exist, giving meaningful RSI and BB data.

---

### Corrected P&L After Deduplication (2026-04-01 to 2026-04-02)

| Metric | Raw DB (with duplicates) | Corrected (unique trades) |
|--------|--------------------------|--------------------------|
| Total records | 145 | 13 |
| Total P&L | ₹-10,95,538 | ₹-84,677 |
| Win rate | — | 7.7% (1W / 12L) |
| Avg winner | — | ₹+11,842 |
| Avg loser | — | ₹-8,043 |

All 13 legitimate trades were on 2026-04-01. Bugs 1+2 produced 132 phantom duplicate
entries across 2026-04-01 night and 2026-04-02.

---

### Bug 5 — `target_2` missing from `get_open_positions()` — T2 never triggered

**Discovered:** 2026-05-13
**File:** `risk/portfolio_tracker.py` → `get_open_positions()`
**Severity:** High — T2 exit never fired for any position

**What happened:**
`get_open_positions()` returned a dict for each position that omitted `target_2`.
In `_check_position()`, `pos_dict.get("target_2", 0)` always returned 0.
The T2 check `if target_2 > 0 and ltp >= target_2` never fired.
All positions were either trailing-stopped out or waited for a static T2 that was
effectively disabled. After T1, the remaining 50% had no exit trigger except the trailing stop.

**Fix:** Added `"target_2": pos.target_2` to the dict returned by `get_open_positions()`.

---

### Bug 6 — Trailing SL and breakeven not persisted — SL reverted to original on restart

**Discovered:** 2026-05-13
**Files:** `execution/position_manager.py`, `risk/portfolio_tracker.py`
**Severity:** Critical — bot restarts during a live trade reverted the stop to the original risky level

**What happened:**
`_move_stop_to_breakeven()` and `_update_trailing_stop()` updated only the
in-memory `_trailing_stops` dict. The `Position.stop_loss` field in the dataclass
(and in SQLite) was never updated. On restart, `_load_open_positions()` reloaded the
original entry-time SL from the DB — all trailing stop progress was lost.

Example: entered at ₹100, SL ₹98. After trailing to SL=₹105, bot restarted.
SL reverted to ₹98. Price then fell from ₹107 to ₹99 — should have stopped at ₹105,
instead the position was held through a ₹6 adverse move.

Additionally, `_partial_exited` and `_breakeven_applied` were lost on restart, causing
the bot to attempt a second partial exit on T1 for positions already past T1.

**Fix:**
1. `portfolio_tracker.update_stop_loss(symbol, new_sl)` now called from both
   `_move_stop_to_breakeven()` and `_update_trailing_stop()` — writes to Position object + DB.
2. `_reconstruct_state_from_position()` runs on first tick per symbol after restart.
   Infers T1-was-hit from `stop_loss >= entry_price` (LONG) and restores all tracking sets.

---

### Bug 7 — Kill switch and daily P&L not persisted — reset silently on restart

**Discovered:** 2026-05-13
**Files:** `risk/risk_manager.py`, `risk/options_risk.py`
**Severity:** Critical — daily loss limit protection was neutralised by any restart

**What happened:**
Both `_kill_switch_active` and `_daily_realised_pnl` were pure in-memory variables.
If the daily loss limit (3%) triggered the kill switch at 10:30 AM and the bot
(or watchdog) restarted at 11:00 AM, all protection reset to zero:
- Kill switch → inactive (trading resumes immediately)
- Daily P&L → ₹0 (as if no losses occurred yet today)

**Fix:** Both modules now write `db/risk_state.json` and `db/options_risk_state.json`
on every mutation (kill switch trigger/reset, daily P&L update, new-day reset).
On startup, state is loaded from file:
- Kill switch active → **always** restored (stays locked until manual reset)
- Daily P&L → **only** restored if `daily_reset_date == today`

---

## Quick Reference — All Thresholds

| Category | Parameter | Value |
|----------|-----------|-------|
| **Capital** | Total Capital | ₹5,00,000 |
| | Risk Per Trade | 1.5% = ₹7,500 |
| | Daily Loss Limit | 3.0% = ₹15,000 |
| | Max Portfolio Heat | 60% |
| | Max Open Positions | 10 |
| **Options** | Max Allocation | 30% of capital |
| | Max Lots / Trade | 2 |
| | Min Option LTP | ₹5 |
| | VIX Limit (short) | 27.0 |
| | DTE Force Exit | 3 days |
| **Signal Quality** | Min Confidence | 65% |
| | Min R:R | 1.5 |
| | Cooldown After Loss | 60 min |
| **Trend Follow** | Breakout Lookback | 20 bars |
| | Min RVOL | 1.4× |
| | ATR Stop | 1.5× ATR |
| | Target 1 | 2R |
| | Target 2 | 3R |
| | Min ADX | 20 |
| | Max RSI Entry | 75 |
| **Mean Reversion** | RSI Oversold | < 35 |
| | RSI Overbought | > 65 |
| | BB Proximity | 0.5% |
| | ATR Stop Buffer | 0.5× ATR |
| | Opening Blackout | Until 09:45 IST |
| **Momentum Reversal** | RSI Overbought | ≥ 82 (SHORT) |
| | RSI Oversold | ≤ 18 (LONG) |
| | Max ADX | 25 |
| | ATR Stop Buffer | 0.5× ATR (beyond swing extreme) |
| | Target 1 | 1.5R |
| | Target 2 | EMA21 or 2.5R (whichever closer) |
| | Min RVOL | 1.3× |
| **Gap Fade** | Min Gap Size | 0.5% |
| | RSI Overbought (short) | ≥ 70 |
| | RSI Oversold (long) | ≤ 30 |
| | ATR Stop Buffer | 0.3× ATR |
| | Target 1 | prev_close (gap fill) |
| | Target 2 | 2.5R |
| **Simple Momentum** | ATR Stop | 1.5× ATR |
| | Single Target | 3R |
| **Institutional Momentum** | Stop | −30% of premium |
| | Target 1 | +55% of premium |
| **Options Income** | Min IV Rank | 50 |
| | DTE Range | 20–45 days |
| | Short Delta | 0.16 |
| | Profit Target | 50% credit |
| | Stop | 2× credit |
| **Directional Options** | Max IV Rank | 40 |
| | DTE Range | 7–21 days |
| | Stop | 50% of debit |
| **Iron Condor** | IV Rank Range | 40–80 |
| | DTE Range | 21–45 days |
| | Wing Width | 2% of spot |
| | Stop | 2× net credit |
| **Exit Rules** | EOD Close | 15:15 IST |
| | Max Hold | 20 days |
| | Partial Exit at T1 | 50% |
| | Breakeven Trigger | 1.0R profit |
| | Trailing Stop Trigger | 1.5R profit |
| | Trail Distance | 0.8R |
| | Dynamic Target Start | 3.0R (after T1) |
| | Dynamic Target Step | 1.0R per milestone |
