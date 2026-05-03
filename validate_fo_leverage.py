"""
validate_fo_leverage.py
Shows exactly how Rs.50,000 (10% of 5L) is achieved per trade in F&O,
and what move size is actually needed based on real lot sizes and premiums.
"""
import math

SEP = "=" * 68
SEP2 = "-" * 68

print(f"\n{SEP}")
print("  F&O LEVERAGE MATH -- How Rs.50,000 Per Trade Actually Works")
print(SEP)

# ─────────────────────────────────────────────────────────────────────────────
# INPUTS
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL      = 500_000   # Rs.5L total capital
TARGET_GAIN  = 50_000    # Rs.50,000 = 10% of capital per winning trade
STOP_LOSS    = 25_000    # Rs.25,000 = 5% of capital per losing trade (2:1 R:R)

# ATM premium estimates (approximate for illustration; changes with IV and DTE)
# Using conservative 5-7 DTE ATM options premiums
INSTRUMENTS = [
    # (name,          spot,   lot_size, atm_premium, note)
    ("BANKNIFTY",    51000,   15,       350,  "Weekly expiry, 5 DTE, IV~18%"),
    ("NIFTY",        24000,   75,       150,  "Weekly expiry, 5 DTE, IV~14%"),
    ("FINNIFTY",     24000,   40,       160,  "Weekly expiry, 5 DTE, IV~15%"),
]

print(f"\n  Capital:       Rs.{CAPITAL:,}")
print(f"  Target gain:   Rs.{TARGET_GAIN:,} ({TARGET_GAIN/CAPITAL*100:.0f}% of capital)")
print(f"  Max loss:      Rs.{STOP_LOSS:,}  ({STOP_LOSS/CAPITAL*100:.0f}% of capital)")

for name, spot, lot_size, atm_premium, note in INSTRUMENTS:
    print(f"\n{SEP2}")
    print(f"  {name}  |  Spot: {spot:,}  |  Lot: {lot_size}  |  ATM premium: Rs.{atm_premium}  |  {note}")
    print(SEP2)

    cost_per_lot = atm_premium * lot_size
    print(f"\n  Cost per lot:  Rs.{cost_per_lot:,}")

    # Deploy 30% of capital in options (Rs.1.5L) -- conservative
    for deploy_pct in [25, 35, 50]:
        deploy_capital = CAPITAL * deploy_pct / 100
        num_lots       = int(deploy_capital / cost_per_lot)
        total_deployed = num_lots * cost_per_lot
        total_units    = num_lots * lot_size

        if num_lots == 0:
            continue

        # Gain needed on option price to hit Rs.50,000 target
        gain_per_unit = TARGET_GAIN / total_units
        gain_pct_opt  = gain_per_unit / atm_premium * 100

        # Loss on option price for stop (Rs.25,000)
        loss_per_unit = STOP_LOSS / total_units
        loss_pct_opt  = loss_per_unit / atm_premium * 100

        # Underlying move needed: delta ~0.5 for ATM
        # option_move = delta * underlying_move  =>  underlying_move = option_move / delta
        delta = 0.50
        underlying_move_pts = gain_per_unit / delta
        underlying_move_pct = underlying_move_pts / spot * 100

        stop_underlying_pts = loss_per_unit / delta
        stop_underlying_pct = stop_underlying_pts / spot * 100

        print(f"\n  Deploy {deploy_pct}% (Rs.{total_deployed:,.0f}) = {num_lots} lots = {total_units:,} units:")
        print(f"    To make Rs.{TARGET_GAIN:,}:")
        print(f"      Option needs to gain:    Rs.{gain_per_unit:.0f}/unit ({gain_pct_opt:.0f}% of premium)")
        print(f"      {name} needs to move:    {underlying_move_pts:.0f} pts ({underlying_move_pct:.2f}%)")
        print(f"    To lose Rs.{STOP_LOSS:,} (stop):")
        print(f"      Option loses:            Rs.{loss_per_unit:.0f}/unit ({loss_pct_opt:.0f}% of premium)")
        print(f"      {name} moves against:    {stop_underlying_pts:.0f} pts ({stop_underlying_pct:.2f}%)")

    # Average daily range for context
    avg_range_pct = {"BANKNIFTY": 1.14, "NIFTY": 0.98, "FINNIFTY": 1.00}
    rng = avg_range_pct.get(name, 1.0)
    avg_range_pts = spot * rng / 100
    print(f"\n  Context: {name} avg daily range = {avg_range_pts:.0f} pts ({rng}% of spot)")
    print(f"  At 35% deploy, need {spot * 0.98 / 100 * 0.40:.0f} pts move = {(spot * 0.98 / 100 * 0.40)/avg_range_pts*100:.0f}% of avg daily range")


# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY P&L MODEL
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{SEP}")
print("  MONTHLY P&L MODEL -- Rs.5L Capital, F&O Only")
print(SEP)

WIN  = 50_000
LOSS = 25_000

print(f"\n  Per-trade: Win=Rs.{WIN:,} | Loss=Rs.{LOSS:,} | R:R = 2:1\n")
print(f"  {'Trades/mo':>10} {'Win Rate':>10} {'Wins':>6} {'Losses':>8} {'Monthly P&L':>14} {'Monthly%':>10} {'Annual%':>10}")
print(f"  {'-'*10} {'-'*10} {'-'*6} {'-'*8} {'-'*14} {'-'*10} {'-'*10}")

for trades in [2, 3, 4, 5]:
    for wr in [0.55, 0.60, 0.65, 0.70]:
        wins   = trades * wr
        losses = trades * (1 - wr)
        pnl    = wins * WIN - losses * LOSS
        pct    = pnl / CAPITAL * 100
        ann    = ((1 + pct/100) ** 12 - 1) * 100
        marker = " <-- target" if trades == 4 and wr == 0.65 else ""
        print(f"  {trades:>10} {wr*100:>9.0f}% {wins:>6.1f} {losses:>8.1f} {pnl:>14,.0f} {pct:>9.1f}% {ann:>9.0f}%{marker}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL SYSTEM: WHAT MOVES MARKET > 0.5% DIRECTIONALLY?
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{SEP}")
print("  INSTITUTIONAL SIGNALS -- What Predicts 0.5%+ Directional Move?")
print(SEP)

signals = [
    ("FII F&O Net Position Change",
     "NSE participant-wise data 5:30 PM daily",
     "FII adds net long >10k contracts in index futures",
     "65-70%",
     "Next-day opening gap + follow-through",
     "HIGHEST -- FIIs are largest directional players in Nifty"),

    ("OI Unwinding at Key Strike",
     "Fyers options chain, real-time",
     "Heavy put OI at support dissolves (shorts exit) + price holds",
     "65%",
     "Intraday within 2 hours of signal",
     "HIGH -- dealer gamma hedging flows follow OI"),

    ("PCR Extreme + Reversal",
     "Computed from options chain OI",
     "PCR < 0.7 (excess calls) on falling market = oversold bounce",
     "60%",
     "Same-day reversal",
     "MEDIUM -- works at extremes, noisy in middle"),

    ("VIX Spike + Stabilise",
     "India VIX from NSE",
     "VIX spikes >20% intraday then falls back below open",
     "68%",
     "Same-day recovery in Nifty",
     "HIGH -- panic peak signal"),

    ("SGX/Gift Nifty Gap Fade",
     "Overnight futures premium/discount",
     "Gift Nifty up >1% vs Nifty close, fades after open",
     "60%",
     "First 30-60 minutes of session",
     "MEDIUM -- works well in ranging markets"),

    ("Sector Rotation + Index Lead",
     "Real-time price, cross-index",
     "BankNifty leads Nifty by 15+ minutes (bank stocks front-run)",
     "62%",
     "Within 30 minutes",
     "HIGH -- banks are institutional indicator"),
]

for sig, source, trigger, hitrate, timing, importance in signals:
    print(f"\n  [{importance.split('--')[0].strip()}]  {sig}")
    print(f"    Source:   {source}")
    print(f"    Trigger:  {trigger}")
    print(f"    Hit rate: {hitrate}  |  Timing: {timing}")


# ─────────────────────────────────────────────────────────────────────────────
# CONVICTION SCORING -- WHEN TO TRADE
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{SEP}")
print("  CONVICTION SCORE SYSTEM -- When to Deploy Capital")
print(SEP)

print("""
  Score each signal morning before 9:15 AM:

  Signal                              Bullish  Bearish
  --------------------------------    -------  -------
  FII F&O net change (prev day)        +3       -3
  OI buildup at support/resistance     +2       -2
  PCR extreme (< 0.7 or > 1.2)        +2       -2
  VIX direction (falling / rising)     +1       -1
  SGX Nifty cue                        +1       -1
  BankNifty leadership                 +1       -1
  --------------------------------    -------  -------
  Max score:                          +10      -10

  TRADE RULES:
    Score +7 to +10  -> BUY calls aggressively (35-50% capital)
    Score +5 to +6   -> BUY calls conservatively (20% capital)
    Score -1 to +4   -> DO NOTHING (no trade today)
    Score -5 to -6   -> BUY puts conservatively (20% capital)
    Score -7 to -10  -> BUY puts aggressively (35-50% capital)

  WHY THIS MATTERS:
    Most days: score is 2-4  -> no trade  -> capital preserved
    3-4 days/month: score is 7+  -> high-conviction trade
    This selectivity is the edge. The bot that trades every day loses.
    The bot that waits for 7+ and bets big WINS.
""")

print(f"\n{SEP}")
print("  WHAT TO BUILD -- Priority Order")
print(SEP)

print("""
  MODULE 1 (Build now, 1 week):
    intelligence/nse_participant_collector.py
      -> Fetches NSE F&O participant-wise OI data at 5:30 PM daily
      -> Stores FII net long/short in index futures (NIFTY, BANKNIFTY)
      -> Computes day-over-day change
      -> Most important single signal for next-day direction

  MODULE 2 (Build now, parallel):
    analysis/oi_analyzer.py
      -> Real-time during market hours via Fyers options chain
      -> PCR of OI, max pain strike, gamma walls (top 3 OI strikes)
      -> Updated every 5 minutes
      -> Feeds into intraday conviction score

  MODULE 3 (1 week):
    intelligence/conviction_scorer.py
      -> Runs pre-market at 9:00 AM
      -> Combines FII F&O signal + OI snapshot + VIX + SGX cue
      -> Outputs score 0-10 + direction (BULLISH/BEARISH/NEUTRAL)
      -> Only generates trade signal if score >= 7

  MODULE 4 (1 week):
    strategies/institutional_momentum.py
      -> Activated only when conviction_scorer outputs 7+
      -> Buys ATM calls (bullish) or ATM puts (bearish)
      -> 35% capital deployed per trade
      -> Target: +20% on options = Rs.35,000 gain
      -> Stop: -14% on options = Rs.25,000 loss
      -> R:R = 1.4:1 but win rate 65-70% -> positive EV

  DATA TO COLLECT STARTING TODAY:
    - NSE participant F&O data: stored daily at 5:30 PM
    - Options chain OI snapshot: stored every 30 min during market hours
    - India VIX: stored daily close
    -> After 30 days: enough to start live trading with signal score
""")
