# Strategy Reference

> What each strategy does, when it fires, how it exits, and its current status.
> Source of truth is the code; this is the human-readable review layer. Updated 2026-06-16.

There are **three independent trading systems**:

| System | Where | Money | Routes via |
|--------|-------|-------|------------|
| **Production NSE** | `strategies/` via `strategy_selector.py` | Paper now → live later | Market regime |
| **Learning (paper lab)** | `learning_engine.py` | Paper only, never live | Runs all on a watchlist |
| **MCX commodity options** | `commodity_options_learning.py` + `strategies/mcx/` | Paper now | Own engine |

---

## Quick status board

| Strategy | System | Asset | Status | Notes |
|----------|--------|-------|--------|-------|
| InstitutionalMomentum | Production | Index options | Active (high-conviction days) | Needs conviction score ≥ threshold |
| DirectionalOptions | Production | Index options | **Active** (fixed today) | Single long call/put |
| TrendFollow | Production + Learning | Equity | Active | Long breakout |
| ShortTrend | Production | Equity | Active | Short breakdown |
| MeanReversion | Production + Learning | Equity | Active | Range fade |
| MomentumReversal | Production | Equity | Active | Extreme-RSI snap-back |
| GapFade | Production | Equity | Active | 9:15–9:45 only |
| OptionsIncome | Production | Index options | Active | Short strangle |
| IronCondor | Production | Index options | **DISABLED** | 1% historical win rate |
| SimpleRSI | Learning | Equity | Paper only | Baseline |
| SimpleMomentum | Learning | Equity | Paper only | Baseline |
| TrendSpread | MCX | Commodity options | **Active — winner** | +₹103k paper, 49% WR |
| BreakoutSpread | MCX | Commodity options | **To disable (B4.1)** | −₹12k, 20% WR |
| RSIReversalSpread | MCX | Commodity options | **To disable (B4.1)** | −₹19k, 33% WR |

> ⚠️ All P&L above is paper and partly Black-Scholes-estimated (see PROJECT_PLAN B1.1). Treat as
> directional, not validated. No strategy has an out-of-sample backtested edge yet (B3).

---

## Production NSE strategies

Routed by `strategy_selector.py` based on `regime_detector` output. Every signal then passes the
intelligence layer (now fail-open) and risk gates before an order is placed.

### InstitutionalMomentum — `strategies/institutional_momentum.py`
- **Asset / TF:** NIFTY/BANKNIFTY/FINNIFTY index options · daily-driven
- **Fires when:** pre-market conviction score (from `conviction_scorer.py`) is high; not indicator-driven
- **Entry:** wait till 9:30, buy ATM call (bullish) / put (bearish) on first VWAP pullback; weekly 3–7 DTE
- **Size:** score 7–8 → 35% capital; score 9–10 → 50%
- **Exit:** +55% target / −30% stop (R:R ~1.83); hard time-stop before 2:30 PM
- **Status:** active only on qualifying days

### DirectionalOptions — `strategies/directional_options.py`
- **Asset / TF:** index options (NIFTY/BANKNIFTY/FINNIFTY) · 1H
- **Fires when:** regime TRENDING/BREAKOUT/VOLATILE, IV rank < `MAX_IV_RANK`, clean 3-EMA stack
- **Entry:** single **long** call (bullish, RSI 50–`OPTIONS_LONG_RSI_MAX`) or put (bearish, `OPTIONS_SHORT_RSI_MIN`–50); ~0.40 delta, 7–21 DTE
- **Exit (configurable):** SL −`OPTIONS_LONG_SL_PCT` (30%), target +`OPTIONS_LONG_TARGET_PCT` (50%)
- **Blackouts:** no entries before `NSE_NO_ENTRY_BEFORE` (09:45) or after `NO_NEW_OPTIONS_ENTRY_TIME` (15:10)
- **Status:** active; chain parsing fixed today (all 3 indices return real options)

### TrendFollow — `strategies/trend_follow.py`
- **Asset/TF:** equity · 1H (confirmed on daily) · swing
- **Entry:** regime TRENDING/BREAKOUT, EMA9>21>50, break above 20-bar high, RVOL ≥ 1.4
- **Exit:** stop 1.5×ATR below entry candle low; T1 2R, T2 3R

### ShortTrend — `strategies/short_trend.py`
- **Asset/TF:** equity · 1H · intraday (NSE short-sell is intraday only)
- **Entry:** TRENDING, EMA9<21<50, close < 20-bar low, RSI 30–55, ADX ≥ 20, RVOL > 1.2, daily agrees
- **Exit:** stop 15-bar swing high + 0.5 ATR; T1 −1.5R, T2 −2.5R

### MeanReversion — `strategies/mean_reversion.py`
- **Asset/TF:** equity · 15m (filtered on 1H) · intraday
- **Entry:** regime RANGING; LONG RSI<40 near lower Bollinger, SHORT RSI>60 near upper; EMA50 direction filter
- **Exit:** stop beyond swing; target EMA21 (mean) then band

### MomentumReversal — `strategies/momentum_reversal.py`
- **Asset/TF:** equity · 1H · intraday
- **Entry:** RANGING/VOLATILE; RSI ≥ 82 (short) or ≤ 18 (long), ADX < 25, RVOL ≥ 1.3, extreme held ≥ N bars
- **Exit:** stop ATR beyond extreme; T1 1.5R, T2 2.5R or EMA21

### GapFade — `strategies/gap_fade.py`
- **Asset/TF:** equity · 5m · intraday, **9:15–9:45 only**
- **Entry:** significant opening gap within bounds, RSI extreme at open, first-bar RVOL ≥ 1.2; fades the gap
- **Exit:** stop beyond gap extreme + 0.3 ATR; T1 gap fill (prev close), T2 R-multiple

### OptionsIncome — `strategies/options_income.py`
- **Asset/TF:** index options · daily · swing (carryforward)
- **Entry:** IV rank > 50, regime RANGING; sell **short strangle** 1 SD OTM, 20–45 DTE
- **Exit:** 50% of max credit profit target; stop multiple of credit

### IronCondor — `strategies/iron_condor.py` — **DISABLED**
- Defined-risk 4-leg strangle. Disabled in the selector (3-yr backtest ~1% win rate on index regimes).
  Code retained; confirm it never fires (cleanup C4).

---

## Learning strategies (paper lab — never live)

Run by `learning_engine.py` against `config/learning_watchlist.py`. Intentionally simple baselines to
build intuition and a labeled dataset. **MCX commodities were removed from here (fixed earlier).**

### SimpleRSI — `strategies/simple_rsi.py`
- 15m RSI(14): LONG < 35, SHORT > 65; stop 1.5×ATR; target 2R; window 10:00–15:15.

### SimpleMomentum — `strategies/simple_momentum.py`
- 1H EMA9/EMA21 crossover + RSI filter; stop 1.5×ATR; target 3R; window 09:45–13:30.

(TrendFollow and MeanReversion above are also reused in the learning loop.)

---

## MCX commodity options strategies (`strategies/mcx/`)

Debit-spread strategies on MCX commodities, run by `commodity_options_learning.py`. Base class in
`strategies/mcx_base.py`. **These are the only system with real trade volume so far.**

### TrendSpread — `strategies/mcx/trend_spread.py` — **winner**
- EMA5/EMA20 crossover + spot side + RSI band (55–68 long / 32–45 short) + EMA gap ≥ 0.3% + ADX ≥ 20 + RVOL ≥ 1.3.
- Paper: **+₹103k, 49% win rate, 84 trades.** The one to build on.

### BreakoutSpread — `strategies/mcx/breakout_spread.py` — **to disable (B4.1)**
- 12-bar wick-range breakout + candle-close confirm + RSI>55 + EMA gap + ADX ≥ 20 + RVOL ≥ 1.5 + MACD>0.
- Paper: **−₹12k, 20% win rate.** Negative expectancy → disable until re-validated.

### RSIReversalSpread — `strategies/mcx/rsi_reversal.py` — **to disable (B4.1)**
- Fade RSI extremes (<32 / >68) only while EMA trend intact.
- Paper: **−₹19k, 33% win rate.** Negative expectancy → disable until re-validated.

---

## How to read performance (and why we can't trust it yet)

Current win rates/P&L are **paper + partly estimated**. Before any of these decides real money they
must clear the §B3 gate in PROJECT_PLAN: out-of-sample walk-forward backtest with real costs, producing
true win rate, expectancy, trades/day, and risk-of-ruin. See the ₹1,000/day expectancy math there.
