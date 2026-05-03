"""
run_full_backtest.py
--------------------
Comprehensive backtest covering ALL 6 strategies:
  - TrendFollow, MeanReversion, SimpleMomentum, SimpleRSI (equity — bar-by-bar engine)
  - DirectionalOptions, IronCondor (options — signal simulation with P&L model)

Outputs:
  1. Per-strategy aggregate metrics table
  2. April 2026 isolated results
  3. MIN_RISK_REWARD parameter sweep [0.5, 0.75, 1.0, 1.25, 1.5]
  4. IronCondor profit_target x stop_mult sweep
  5. Final recommendations

Run from: d:\\Tech\\trading-bot
  python run_full_backtest.py
"""

import logging
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# -- Suppress noisy logs -------------------------------------------
logging.basicConfig(level=logging.WARNING)
warnings.filterwarnings("ignore")

# -- Ensure project root is on path -------------------------------
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

IST = ZoneInfo("Asia/Kolkata")

import numpy as np
import pandas as pd

# -- Internal imports ----------------------------------------------
from backtesting.backtest_engine import BacktestEngine, Trade
from backtesting.performance import compute_metrics
from config.settings import TOTAL_CAPITAL


# -----------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------

CACHE_DIR = ROOT / "db" / "historical"
APRIL_START = pd.Timestamp("2026-04-01", tz="Asia/Kolkata")
APRIL_END   = pd.Timestamp("2026-04-30", tz="Asia/Kolkata")


def load_csv(symbol: str, timeframe: str = "1D") -> pd.DataFrame | None:
    """Load historical CSV from cache. Returns None if missing."""
    safe = symbol.replace(":", "_").replace("-", "_")
    path = CACHE_DIR / f"{safe}_{timeframe}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
        df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)
        df = df.dropna(subset=["open", "high", "low", "close"])
        return df
    except Exception as e:
        logging.warning(f"Could not load {path}: {e}")
        return None


def symbols_from_csv() -> list[str]:
    """Discover symbols from CSV filenames (1D only, exclude indices for equity)."""
    syms = []
    for p in sorted(CACHE_DIR.glob("*_1D.csv")):
        name = p.stem  # e.g. NSE_RELIANCE_EQ_1D
        name = name.replace("_1D", "")
        # Reconstruct fyers symbol
        if name.endswith("_INDEX"):
            base = name[4:].replace("_", "")  # NSE_ prefix + _INDEX suffix
            # Careful reconstruction
            raw = p.stem.replace("_1D", "")   # NSE_NIFTY50_INDEX
            parts = raw.split("_")             # ['NSE', 'NIFTY50', 'INDEX']
            sym = f"{parts[0]}:{parts[1]}-{parts[2]}"   # NSE:NIFTY50-INDEX
        elif name.endswith("_EQ"):
            raw = p.stem.replace("_1D", "")
            parts = raw.split("_")
            sym = f"{parts[0]}:{parts[1]}-{parts[2]}"   # NSE:RELIANCE-EQ
        else:
            continue
        syms.append(sym)
    return syms


def april_trades(trades: list[Trade]) -> list[Trade]:
    """Filter trades that had entry in April 2026."""
    result = []
    for t in trades:
        ts = t.entry_date
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        elif hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.astimezone(IST)
        if APRIL_START <= ts <= APRIL_END:
            result.append(t)
    return result


def aggregate_results(results: list) -> dict:
    """Aggregate BacktestResult list into summary dict."""
    traded = [r for r in results if r.total_trades > 0]
    if not traded:
        return {
            "symbols": len(results), "traded_symbols": 0, "total_trades": 0,
            "win_rate": 0, "profit_factor": 0, "sharpe": 0,
            "max_drawdown": 0, "total_return": 0, "expectancy": 0,
        }
    all_trades = []
    for r in traded:
        all_trades.extend(r.trades)
    n = len(all_trades)
    winners = [t for t in all_trades if t.pnl > 0]
    losers  = [t for t in all_trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in winners)
    gross_loss   = abs(sum(t.pnl for t in losers))
    pf = gross_profit / gross_loss if gross_loss > 0 else 99.0
    wr = len(winners) / n if n else 0
    exp = sum(t.pnl for t in all_trades) / n if n else 0
    avg_sharpe = sum(r.sharpe_ratio for r in traded) / len(traded)
    avg_dd     = sum(r.max_drawdown_pct for r in traded) / len(traded)
    avg_ret    = sum(r.total_return_pct for r in traded) / len(traded)
    return {
        "symbols": len(results),
        "traded_symbols": len(traded),
        "total_trades": n,
        "win_rate": wr,
        "profit_factor": round(pf, 2),
        "sharpe": round(avg_sharpe, 2),
        "max_drawdown": round(avg_dd, 1),
        "total_return": round(avg_ret, 1),
        "expectancy": round(exp, 0),
    }


def aggregate_trades_direct(all_trades: list[Trade], label: str = "") -> dict:
    """Aggregate a flat list of trades into a summary dict."""
    n = len(all_trades)
    if n == 0:
        return {"total_trades": 0, "win_rate": 0, "profit_factor": 0,
                "expectancy": 0, "total_pnl": 0}
    winners    = [t for t in all_trades if t.pnl > 0]
    losers     = [t for t in all_trades if t.pnl <= 0]
    gp         = sum(t.pnl for t in winners)
    gl         = abs(sum(t.pnl for t in losers))
    pf         = gp / gl if gl > 0 else 99.0
    wr         = len(winners) / n
    exp        = sum(t.pnl for t in all_trades) / n
    total_pnl  = sum(t.pnl for t in all_trades)
    return {
        "total_trades": n, "win_rate": round(wr, 3),
        "profit_factor": round(pf, 2), "expectancy": round(exp, 0),
        "total_pnl": round(total_pnl, 0),
    }


# -----------------------------------------------------------------
# EQUITY BACKTESTS (bar-by-bar engine)
# -----------------------------------------------------------------

def run_equity_strategy(strategy_cls, equity_symbols: list[str]) -> tuple[list, list]:
    """
    Run an equity strategy using BacktestEngine on all equity_symbols.
    Returns (all_results, april_trades_flat).
    """
    from strategies.base_strategy import BaseStrategy
    engine   = BacktestEngine()
    strategy = strategy_cls()
    results  = []
    april_flat = []

    for symbol in equity_symbols:
        df = load_csv(symbol)
        if df is None or len(df) < 80:
            continue
        try:
            r = engine.run(symbol, df, strategy, "1D", warmup_bars=60)
            r = compute_metrics(r)
            results.append(r)
            april_flat.extend(april_trades(r.trades))
        except Exception as e:
            logging.debug(f"[{strategy.name}] {symbol} failed: {e}")

    return results, april_flat


# -----------------------------------------------------------------
# OPTIONS SIMULATION
# -----------------------------------------------------------------

def simulate_directional_options(index_symbols: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Replay DirectionalOptions signals in simulation mode.

    For each daily bar on each index:
      - Simulate signal conditions using EMA alignment + RSI on daily data
      - If signal would fire: simulate debit spread P&L over next 14 bars
        * Underlying moves >= 2% in direction within 14 days → profit (max_profit)
        * Underlying moves >= 1.5% against direction within 14 days → loss (50% debit)
        * Otherwise → small time-decay loss (25% debit)

    Returns (all_simulated_trades, april_simulated_trades)
    """
    try:
        from analysis.indicators import ema, rsi, atr
    except Exception as e:
        print(f"  WARNING: indicator import failed: {e}")
        return [], []

    iv_assumed = 0.18
    all_trades   = []
    april_trades_list = []

    for symbol in index_symbols:
        df = load_csv(symbol)
        if df is None or len(df) < 80:
            continue

        close = df["close"]

        for i in range(60, len(df) - 15):
            window = df.iloc[:i+1]
            wc     = window["close"]

            # EMA alignment
            e9  = ema(wc, 9).iloc[-1]
            e21 = ema(wc, 21).iloc[-1]
            e50 = ema(wc, 50).iloc[-1]
            rsi_val = rsi(wc).iloc[-1]
            atr_val = atr(window).iloc[-1]

            bullish = (e9 > e21 > e50) and rsi_val > 50
            bearish = (e9 < e21 < e50) and rsi_val < 50

            if not (bullish or bearish):
                continue

            direction = "LONG" if bullish else "SHORT"
            spot      = float(window["close"].iloc[-1])
            bar_date  = window["timestamp"].iloc[-1]

            # Simulate debit spread pricing
            debit_cost  = spot * iv_assumed * 0.015  # ~0.27% of spot
            max_profit  = spot * iv_assumed * 0.025  # ~0.45% of spot
            stop_loss   = debit_cost * 0.5           # exit at 50% premium loss

            # Simulate outcome over next 14 bars
            future = df.iloc[i+1 : i+15]
            if len(future) < 5:
                continue

            future_highs = future["high"].values
            future_lows  = future["low"].values

            hit_profit = False
            hit_stop   = False

            for j, (fh, fl) in enumerate(zip(future_highs, future_lows)):
                pct_up   = (fh - spot) / spot
                pct_down = (spot - fl) / spot
                if direction == "LONG":
                    if pct_up >= 0.02:
                        hit_profit = True
                        break
                    if pct_down >= 0.015:
                        hit_stop = True
                        break
                else:
                    if pct_down >= 0.02:
                        hit_profit = True
                        break
                    if pct_up >= 0.015:
                        hit_stop = True
                        break

            if hit_profit:
                pnl = max_profit * 50  # assume 50 lot-equivalent notional
            elif hit_stop:
                pnl = -stop_loss * 50
            else:
                pnl = -debit_cost * 0.25 * 50  # time decay

            rr = max_profit / stop_loss if stop_loss > 0 else 0

            trade_rec = {
                "symbol":    symbol,
                "strategy":  "DirectionalOptions",
                "date":      bar_date,
                "direction": direction,
                "entry":     round(debit_cost, 2),
                "stop":      round(stop_loss, 2),
                "target":    round(max_profit, 2),
                "rr":        round(rr, 2),
                "pnl":       round(pnl, 2),
                "outcome":   "profit" if hit_profit else ("stop" if hit_stop else "decay"),
            }
            all_trades.append(trade_rec)

            ts = bar_date
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            else:
                ts = ts.astimezone(IST)
            if APRIL_START <= ts <= APRIL_END:
                april_trades_list.append(trade_rec)

    return all_trades, april_trades_list


def simulate_iron_condor(
    index_symbols: list[str],
    profit_target: float = 0.50,
    stop_mult: float = 2.0,
) -> tuple[list[dict], list[dict]]:
    """
    Simulate IronCondor signals on index data.

    Entry conditions (simplified from live strategy):
      - RSI between 40-60 (ranging)
      - Price within 1% of 50-EMA (low momentum)

    Outcome simulation over 30 bars:
      - Underlying stays within ±2% of entry → profit (net_credit x profit_target)
      - Underlying breaks ±2% → loss (net_credit x stop_mult)

    Returns (all_trades, april_trades)
    """
    try:
        from analysis.indicators import ema, rsi, atr, bollinger_bands
    except Exception as e:
        print(f"  WARNING: indicator import failed: {e}")
        return [], []

    iv_assumed = 0.18
    all_trades   = []
    april_trades_list = []

    for symbol in index_symbols:
        df = load_csv(symbol)
        if df is None or len(df) < 80:
            continue

        for i in range(60, len(df) - 32):
            window   = df.iloc[:i+1]
            wc       = window["close"]
            spot     = float(wc.iloc[-1])
            bar_date = window["timestamp"].iloc[-1]

            rsi_val = rsi(wc).iloc[-1]
            e50     = ema(wc, 50).iloc[-1]

            # Ranging conditions: RSI near neutral + price near EMA50
            if not (35 <= rsi_val <= 65):
                continue
            pct_from_ema = abs(spot - e50) / e50
            if pct_from_ema > 0.015:
                continue

            # Simulate IC pricing
            dte         = 28
            T           = dte / 365
            wing_pct    = 0.02   # 2% of spot
            wing_width  = spot * wing_pct

            # Net credit via rough Black-Scholes approximation
            # Short 0.20-delta strangle nets ~2x weekly premium ≈ iv x sqrt(T) x 0.20 x spot
            net_credit = round(spot * iv_assumed * (T ** 0.5) * 0.18, 2)
            max_loss   = round(wing_width - net_credit, 2)
            if net_credit <= 0 or max_loss <= 0:
                continue

            profit_amt = round(net_credit * profit_target, 2)
            stop_amt   = round(net_credit * stop_mult, 2)
            rr_val     = profit_amt / stop_amt if stop_amt > 0 else 0

            # Simulate outcome: does spot breach ±2% within 30 bars?
            future = df.iloc[i+1 : i+32]
            if len(future) < 10:
                continue

            breached = False
            for _, row in future.iterrows():
                if abs(row["high"] - spot) / spot > wing_pct or \
                   abs(spot - row["low"])  / spot > wing_pct:
                    breached = True
                    break

            if breached:
                pnl = -stop_amt * 25   # 25 lots equivalent
            else:
                pnl = profit_amt * 25

            trade_rec = {
                "symbol":         symbol,
                "strategy":       "IronCondor",
                "date":           bar_date,
                "direction":      "NEUTRAL",
                "net_credit":     net_credit,
                "profit_target":  profit_target,
                "stop_mult":      stop_mult,
                "rr":             round(rr_val, 2),
                "pnl":            round(pnl, 2),
                "outcome":        "profit" if not breached else "loss",
            }
            all_trades.append(trade_rec)

            ts = bar_date
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            else:
                ts = ts.astimezone(IST)
            if APRIL_START <= ts <= APRIL_END:
                april_trades_list.append(trade_rec)

    return all_trades, april_trades_list


# -----------------------------------------------------------------
# R:R PARAMETER SWEEP (equity signals)
# -----------------------------------------------------------------

def rr_sweep_equity(all_equity_results: dict[str, list]) -> dict:
    """
    For each equity strategy, collect all generated signal R:Rs and count
    pass/fail at each MIN_RISK_REWARD threshold.
    Signal R:R is inferred from trade stop/entry/target ratios.
    """
    thresholds = [0.5, 0.75, 1.0, 1.25, 1.5]
    result = {}

    # Gather all trades with their implied R:R from backtest results
    all_trade_rrs = []
    for strat_name, results in all_equity_results.items():
        for r in results:
            for t in r.trades:
                # Reconstruct R:R from trade data
                risk   = abs(t.entry_price - t.stop_loss)
                reward = abs(t.target_1    - t.entry_price)
                if risk > 0 and reward > 0:
                    rr = reward / risk
                    all_trade_rrs.append({
                        "strategy": strat_name,
                        "rr": rr,
                    })

    total = len(all_trade_rrs)
    result["total_signals"] = total

    for thresh in thresholds:
        passing = [s for s in all_trade_rrs if s["rr"] >= thresh]
        failing = [s for s in all_trade_rrs if s["rr"] < thresh]
        result[thresh] = {
            "pass": len(passing),
            "fail": len(failing),
            "pass_pct": round(len(passing) / total * 100, 1) if total > 0 else 0,
        }

        # Per-strategy breakdown
        per_strat = {}
        for strat in set(s["strategy"] for s in all_trade_rrs):
            strat_signals = [s for s in all_trade_rrs if s["strategy"] == strat]
            strat_pass    = [s for s in strat_signals if s["rr"] >= thresh]
            per_strat[strat] = {
                "total":    len(strat_signals),
                "pass":     len(strat_pass),
                "pass_pct": round(len(strat_pass) / len(strat_signals) * 100, 1) if strat_signals else 0,
            }
        result[thresh]["per_strategy"] = per_strat

    return result


def rr_sweep_options(do_trades: list[dict], ic_trades: list[dict]) -> dict:
    """Compute R:R pass rates for options simulated trades."""
    thresholds = [0.5, 0.75, 1.0, 1.25, 1.5]
    result = {}
    all_opts = [(t["rr"], "DirectionalOptions") for t in do_trades] + \
               [(t["rr"], "IronCondor")         for t in ic_trades]
    total = len(all_opts)
    result["total_signals"] = total

    for thresh in thresholds:
        passing = [(rr, s) for rr, s in all_opts if rr >= thresh]
        result[thresh] = {
            "pass":     len(passing),
            "fail":     total - len(passing),
            "pass_pct": round(len(passing) / total * 100, 1) if total > 0 else 0,
        }
    return result


# -----------------------------------------------------------------
# IRON CONDOR PARAMETER SWEEP
# -----------------------------------------------------------------

def ic_param_sweep(index_symbols: list[str]) -> list[dict]:
    """Test IC across profit_target x stop_mult combinations."""
    profit_targets = [0.30, 0.40, 0.50]
    stop_mults     = [1.5,  2.0,  2.5]
    results = []

    for pt in profit_targets:
        for sm in stop_mults:
            trades, _ = simulate_iron_condor(index_symbols, profit_target=pt, stop_mult=sm)
            if not trades:
                results.append({
                    "profit_target": pt, "stop_mult": sm,
                    "total_trades": 0, "win_rate": 0,
                    "profit_factor": 0, "expectancy": 0,
                    "implied_rr": round(pt / sm, 2),
                })
                continue
            n       = len(trades)
            wins    = [t for t in trades if t["outcome"] == "profit"]
            losses  = [t for t in trades if t["outcome"] == "loss"]
            gp      = sum(t["pnl"] for t in wins)
            gl      = abs(sum(t["pnl"] for t in losses))
            pf      = gp / gl if gl > 0 else 99.0
            wr      = len(wins) / n
            exp     = sum(t["pnl"] for t in trades) / n
            rr_val  = round(pt / sm, 2)   # R:R of the strategy structure
            results.append({
                "profit_target":  pt,
                "stop_mult":      sm,
                "total_trades":   n,
                "win_rate":       round(wr, 3),
                "profit_factor":  round(pf, 2),
                "expectancy":     round(exp, 0),
                "implied_rr":     rr_val,
            })

    return results


# -----------------------------------------------------------------
# PRINT HELPERS
# -----------------------------------------------------------------

def print_header(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_table(headers: list[str], rows: list[list], widths: list[int] = None):
    """Print a simple ASCII table."""
    if widths is None:
        widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                  for i, h in enumerate(headers)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    sep = "  " + "  ".join("-" * w for w in widths)
    print(fmt.format(*headers))
    print(sep)
    for row in rows:
        print(fmt.format(*row))


# -----------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------

def main():
    print_header("FULL STRATEGY BACKTEST — NSE 2023-2026")
    print(f"  Capital: Rs.{TOTAL_CAPITAL:,.0f}  |  Data through 2026-04-30")

    # -- Symbol lists ----------------------------------------------
    from config.watchlist import NSE_EQUITIES, NSE_MIDCAP, NSE_INDICES
    equity_symbols = NSE_EQUITIES + NSE_MIDCAP
    index_symbols  = NSE_INDICES

    # Only keep symbols we have CSV data for
    equity_symbols = [s for s in equity_symbols if load_csv(s) is not None]
    index_symbols  = [s for s in index_symbols  if load_csv(s) is not None]

    print(f"\n  Equity symbols available : {len(equity_symbols)}")
    print(f"  Index symbols available  : {len(index_symbols)}")

    # -- Import equity strategy classes ----------------------------
    from strategies.trend_follow     import TrendFollowStrategy
    from strategies.mean_reversion   import MeanReversionStrategy

    # SimpleMomentum and SimpleRSI use store.get_ohlcv / store.get_ltp
    # which are patched by the engine, but they return dicts not Signals.
    # We handle them with a wrapper engine approach — see below.

    # -------------------------------------------------------------
    # SECTION 1: Equity strategy backtests
    # -------------------------------------------------------------
    print_header("SECTION 1: EQUITY STRATEGY BACKTESTS")

    equity_strategy_results: dict[str, list] = {}
    equity_april: dict[str, list] = {}

    EQUITY_STRATEGIES = [
        ("TrendFollow",    TrendFollowStrategy),
        ("MeanReversion",  MeanReversionStrategy),
    ]

    for strat_name, strat_cls in EQUITY_STRATEGIES:
        print(f"\n  Running {strat_name} on {len(equity_symbols)} symbols...", end="", flush=True)
        results, apr = run_equity_strategy(strat_cls, equity_symbols)
        equity_strategy_results[strat_name] = results
        equity_april[strat_name]            = apr
        traded = [r for r in results if r.total_trades > 0]
        total_t = sum(r.total_trades for r in results)
        print(f" done. {len(traded)} symbols traded, {total_t} trades total.")

    # SimpleMomentum and SimpleRSI return plain dicts (not Signal objects)
    # and use 1H / 15m timeframes. In backtest mode with daily CSV we run
    # them with daily data fed into the engine — the engine patches the store
    # so strategy.evaluate() sees 1D bars but the cross-over logic still works.
    # We run them through the engine exactly the same way as TrendFollow.

    # Wrap the dict-returning strategies so the engine can handle them.
    # The engine calls signal.is_valid() and signal.direction.value —
    # we need to make these strategies return Signal objects during backtest.
    # Since SimpleMomentum/SimpleRSI are standalone (not BaseStrategy subclasses),
    # we create thin adapter wrappers.

    class _SimpleMomentumAdapter:
        """Adapter: wraps SimpleMomentumStrategy and produces Signal objects."""
        name          = "SimpleMomentum"
        timeframe     = "1D"
        backtest_mode = False
        enabled       = True

        def __init__(self):
            from strategies.base_strategy import Direction, Signal, SignalType
            self._Direction   = Direction
            self._Signal      = Signal
            self._SignalType  = SignalType

        def evaluate(self, symbol: str):
            from data.data_store import store
            from analysis.indicators import ema, rsi, atr

            df = store.get_ohlcv(symbol, "1D", n=100)
            if df is None or len(df) < 30:
                return None
            ltp = store.get_ltp(symbol)
            if not ltp or ltp <= 0:
                return None

            close = df["close"]
            ema9  = ema(close, 9)
            ema21 = ema(close, 21)
            rsi_val = rsi(close).iloc[-1]
            atr_val = atr(df).iloc[-1]

            if len(ema9) < 2 or len(ema21) < 2:
                return None

            curr_diff = ema9.iloc[-1] - ema21.iloc[-1]
            prev_diff = ema9.iloc[-2] - ema21.iloc[-2]

            crossed_up   = prev_diff <= 0 and curr_diff > 0
            crossed_down = prev_diff >= 0 and curr_diff < 0

            if crossed_up and rsi_val > 50:
                direction = self._Direction.LONG
            elif crossed_down and rsi_val < 50:
                direction = self._Direction.SHORT
            else:
                return None

            ATR_MULT = 1.5
            TARGET_R = 3.0
            if direction == self._Direction.LONG:
                stop   = ltp - ATR_MULT * atr_val
                target = ltp + TARGET_R * (ltp - stop)
            else:
                stop   = ltp + ATR_MULT * atr_val
                target = ltp - TARGET_R * (stop - ltp)

            risk = abs(ltp - stop)
            if risk <= 0:
                return None

            signal = self._Signal(
                symbol      = symbol,
                strategy    = self.name,
                direction   = direction,
                signal_type = self._SignalType.EQUITY,
                entry       = round(ltp, 2),
                stop_loss   = round(stop, 2),
                target_1    = round(target, 2),
                confidence  = 0.70,
                timeframe   = "1D",
            )
            signal.calculate_rr()
            return signal

    class _SimpleRSIAdapter:
        """Adapter: wraps SimpleRSIStrategy and produces Signal objects."""
        name          = "SimpleRSI"
        timeframe     = "1D"
        backtest_mode = False
        enabled       = True

        def __init__(self):
            from strategies.base_strategy import Direction, Signal, SignalType
            self._Direction  = Direction
            self._Signal     = Signal
            self._SignalType = SignalType

        def evaluate(self, symbol: str):
            from data.data_store import store
            from analysis.indicators import rsi, atr

            df = store.get_ohlcv(symbol, "1D", n=100)
            if df is None or len(df) < 30:
                return None
            ltp = store.get_ltp(symbol)
            if not ltp or ltp <= 0:
                return None

            close   = df["close"]
            rsi_val = rsi(close).iloc[-1]
            atr_val = atr(df).iloc[-1]

            RSI_OVERSOLD   = 35
            RSI_OVERBOUGHT = 65
            ATR_MULT       = 1.5
            TARGET_R       = 2.0

            if rsi_val < RSI_OVERSOLD:
                direction = self._Direction.LONG
            elif rsi_val > RSI_OVERBOUGHT:
                direction = self._Direction.SHORT
            else:
                return None

            if direction == self._Direction.LONG:
                stop   = ltp - ATR_MULT * atr_val
                target = ltp + TARGET_R * (ltp - stop)
            else:
                stop   = ltp + ATR_MULT * atr_val
                target = ltp - TARGET_R * (stop - ltp)

            risk = abs(ltp - stop)
            if risk <= 0:
                return None

            signal = self._Signal(
                symbol      = symbol,
                strategy    = self.name,
                direction   = direction,
                signal_type = self._SignalType.EQUITY,
                entry       = round(ltp, 2),
                stop_loss   = round(stop, 2),
                target_1    = round(target, 2),
                confidence  = 0.70,
                timeframe   = "1D",
            )
            signal.calculate_rr()
            return signal

    for strat_name, adapter_cls in [("SimpleMomentum", _SimpleMomentumAdapter),
                                     ("SimpleRSI",      _SimpleRSIAdapter)]:
        print(f"\n  Running {strat_name} on {len(equity_symbols)} symbols...", end="", flush=True)
        engine   = BacktestEngine()
        strategy = adapter_cls()
        results  = []
        apr_list = []

        for symbol in equity_symbols:
            df = load_csv(symbol)
            if df is None or len(df) < 80:
                continue
            try:
                r = engine.run(symbol, df, strategy, "1D", warmup_bars=60)
                r = compute_metrics(r)
                results.append(r)
                apr_list.extend(april_trades(r.trades))
            except Exception as e:
                logging.debug(f"[{strat_name}] {symbol} error: {e}")

        equity_strategy_results[strat_name] = results
        equity_april[strat_name]            = apr_list
        traded = [r for r in results if r.total_trades > 0]
        total_t = sum(r.total_trades for r in results)
        print(f" done. {len(traded)} symbols traded, {total_t} trades total.")

    # -- Print equity summary table --------------------------------
    print("\n")
    print_header("EQUITY STRATEGY AGGREGATE RESULTS (3-year full history)")
    headers = ["Strategy", "Symbols", "Trades", "WinRate", "PF", "Sharpe",
               "MaxDD%", "AvgRet%", "Expectancy(Rs.)", "Viable?"]
    widths  = [14, 7, 6, 8, 6, 7, 7, 8, 14, 7]
    rows = []
    for strat_name in ["TrendFollow", "MeanReversion", "SimpleMomentum", "SimpleRSI"]:
        agg = aggregate_results(equity_strategy_results[strat_name])
        viable = "YES" if agg["profit_factor"] > 1.0 else "NO"
        rows.append([
            strat_name,
            agg["traded_symbols"],
            agg["total_trades"],
            f"{agg['win_rate']:.1%}",
            f"{agg['profit_factor']:.2f}",
            f"{agg['sharpe']:.2f}",
            f"{agg['max_drawdown']:.1f}",
            f"{agg['total_return']:+.1f}",
            f"Rs.{agg['expectancy']:+,.0f}",
            viable,
        ])
    print_table(headers, rows, widths)

    # -- April 2026 equity results ---------------------------------
    print_header("EQUITY STRATEGIES — APRIL 2026 ISOLATION")
    headers_apr = ["Strategy", "Trades", "WinRate", "PF", "Expectancy(Rs.)", "TotalPnL(Rs.)"]
    widths_apr  = [14, 6, 8, 6, 14, 14]
    rows_apr = []
    for strat_name in ["TrendFollow", "MeanReversion", "SimpleMomentum", "SimpleRSI"]:
        apr_t = equity_april[strat_name]
        if not apr_t:
            rows_apr.append([strat_name, 0, "n/a", "n/a", "n/a", "n/a"])
            continue
        agg = aggregate_trades_direct(apr_t)
        rows_apr.append([
            strat_name,
            agg["total_trades"],
            f"{agg['win_rate']:.1%}",
            f"{agg['profit_factor']:.2f}",
            f"Rs.{agg['expectancy']:+,.0f}",
            f"Rs.{agg['total_pnl']:+,.0f}",
        ])
    print_table(headers_apr, rows_apr, widths_apr)

    # -------------------------------------------------------------
    # SECTION 2: Options simulation
    # -------------------------------------------------------------
    print_header("SECTION 2: OPTIONS STRATEGY SIMULATION")
    print(f"  Running DirectionalOptions simulation on {index_symbols}...")
    do_trades, do_april = simulate_directional_options(index_symbols)
    print(f"  DirectionalOptions: {len(do_trades)} simulated trades ({len(do_april)} in Apr 2026)")

    print(f"  Running IronCondor simulation (base params: PT=0.50, SM=2.0)...")
    ic_trades, ic_april = simulate_iron_condor(index_symbols, profit_target=0.50, stop_mult=2.0)
    print(f"  IronCondor:         {len(ic_trades)} simulated trades ({len(ic_april)} in Apr 2026)")

    def summarise_opt_trades(trades: list[dict]) -> dict:
        if not trades:
            return {"total_trades": 0, "win_rate": 0, "profit_factor": 0,
                    "expectancy": 0, "total_pnl": 0, "avg_rr": 0}
        n      = len(trades)
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gp     = sum(t["pnl"] for t in wins)
        gl     = abs(sum(t["pnl"] for t in losses))
        pf     = gp / gl if gl > 0 else 99.0
        wr     = len(wins) / n
        exp    = sum(t["pnl"] for t in trades) / n
        pnl    = sum(t["pnl"] for t in trades)
        avg_rr = sum(t["rr"] for t in trades) / n
        return {
            "total_trades": n, "win_rate": round(wr, 3),
            "profit_factor": round(pf, 2), "expectancy": round(exp, 0),
            "total_pnl": round(pnl, 0), "avg_rr": round(avg_rr, 2),
        }

    print("\n")
    print_header("OPTIONS SIMULATION AGGREGATE RESULTS (3-year)")
    headers_opt = ["Strategy", "Trades", "WinRate", "PF", "Avg R:R", "Expectancy(Rs.)", "TotalPnL(Rs.)", "Viable?"]
    widths_opt  = [18, 6, 8, 6, 8, 14, 14, 7]
    rows_opt = []
    for label, trades in [("DirectionalOptions", do_trades), ("IronCondor", ic_trades)]:
        s = summarise_opt_trades(trades)
        viable = "YES" if s["profit_factor"] > 1.0 else "NO"
        rows_opt.append([
            label, s["total_trades"], f"{s['win_rate']:.1%}",
            f"{s['profit_factor']:.2f}", f"{s['avg_rr']:.2f}",
            f"Rs.{s['expectancy']:+,.0f}", f"Rs.{s['total_pnl']:+,.0f}", viable,
        ])
    print_table(headers_opt, rows_opt, widths_opt)

    print_header("OPTIONS — APRIL 2026 ISOLATION")
    rows_opt_apr = []
    for label, trades in [("DirectionalOptions", do_april), ("IronCondor", ic_april)]:
        s = summarise_opt_trades(trades)
        rows_opt_apr.append([
            label, s["total_trades"], f"{s['win_rate']:.1%}",
            f"{s['profit_factor']:.2f}",
            f"Rs.{s['expectancy']:+,.0f}", f"Rs.{s['total_pnl']:+,.0f}",
        ])
    print_table(
        ["Strategy", "Trades", "WinRate", "PF", "Expectancy(Rs.)", "TotalPnL(Rs.)"],
        rows_opt_apr,
        [18, 6, 8, 6, 14, 14],
    )

    # -------------------------------------------------------------
    # SECTION 3: MIN_RISK_REWARD parameter sweep
    # -------------------------------------------------------------
    print_header("SECTION 3: MIN_RISK_REWARD PARAMETER SWEEP")
    print("  Counting signals that pass/fail each threshold across all strategies.\n")

    eq_sweep    = rr_sweep_equity(equity_strategy_results)
    opts_sweep  = rr_sweep_options(do_trades, ic_trades)

    thresholds = [0.5, 0.75, 1.0, 1.25, 1.5]

    print(f"  [EQUITY] Total backtest trades analysed: {eq_sweep['total_signals']}")
    headers_sw = ["MinRR", "Pass", "Fail", "Pass%", "TF-pass%", "MR-pass%", "SM-pass%", "SR-pass%"]
    widths_sw  = [6, 5, 5, 6, 9, 9, 9, 9]
    rows_sw = []
    for t in thresholds:
        info = eq_sweep.get(t, {})
        ps   = info.get("per_strategy", {})
        def pp(s):
            d = ps.get(s, {})
            return f"{d.get('pass_pct', 0):.0f}%"
        rows_sw.append([
            t,
            info.get("pass", 0), info.get("fail", 0),
            f"{info.get('pass_pct', 0):.0f}%",
            pp("TrendFollow"), pp("MeanReversion"),
            pp("SimpleMomentum"), pp("SimpleRSI"),
        ])
    print_table(headers_sw, rows_sw, widths_sw)

    print(f"\n  [OPTIONS] Total simulated option signals: {opts_sweep['total_signals']}")
    headers_sw2 = ["MinRR", "Pass", "Fail", "Pass%"]
    widths_sw2  = [6, 5, 5, 6]
    rows_sw2 = []
    for t in thresholds:
        info = opts_sweep.get(t, {})
        rows_sw2.append([
            t,
            info.get("pass", 0), info.get("fail", 0),
            f"{info.get('pass_pct', 0):.0f}%",
        ])
    print_table(headers_sw2, rows_sw2, widths_sw2)

    # -------------------------------------------------------------
    # SECTION 4: Iron Condor parameter sweep
    # -------------------------------------------------------------
    print_header("SECTION 4: IRON CONDOR profit_target x stop_mult SWEEP")
    print(f"  Running on {index_symbols}...\n")
    ic_sweep = ic_param_sweep(index_symbols)

    headers_ic = ["PT", "SM", "Implied R:R", "Trades", "WinRate%", "PF", "Expectancy(Rs.)"]
    widths_ic  = [5, 5, 11, 6, 10, 6, 14]
    rows_ic = []
    for s in ic_sweep:
        rows_ic.append([
            s["profit_target"], s["stop_mult"], s["implied_rr"],
            s["total_trades"], f"{s['win_rate']:.1%}",
            f"{s['profit_factor']:.2f}", f"Rs.{s['expectancy']:+,.0f}",
        ])
    print_table(headers_ic, rows_ic, widths_ic)

    # -------------------------------------------------------------
    # SECTION 5: Final recommendations
    # -------------------------------------------------------------
    print_header("SECTION 5: RECOMMENDATIONS")

    # Find best IC combo
    best_ic = max(ic_sweep, key=lambda s: (
        s["profit_factor"] if s["total_trades"] > 5 else 0
    ))
    # Find viable equity strategies
    viable_eq = []
    for strat_name in ["TrendFollow", "MeanReversion", "SimpleMomentum", "SimpleRSI"]:
        agg = aggregate_results(equity_strategy_results[strat_name])
        if agg["profit_factor"] > 1.0:
            viable_eq.append((strat_name, agg["profit_factor"], agg["win_rate"]))

    # Find optimal RR threshold for equity
    # Optimal = highest threshold where majority of signals still pass AND trades have pf>1
    # As a heuristic: find the threshold where pass% >= 50% for the best strategy
    best_eq_strat = max(viable_eq, key=lambda x: x[1])[0] if viable_eq else "TrendFollow"
    recommended_eq_rr = 1.0   # default
    for thresh in reversed(thresholds):
        info = eq_sweep.get(thresh, {})
        ps   = info.get("per_strategy", {})
        strat_info = ps.get(best_eq_strat, {})
        if strat_info.get("pass_pct", 0) >= 50:
            recommended_eq_rr = thresh
            break

    # Options RR: IronCondor implied R:R determines minimum viable threshold
    ic_rr_at_best = best_ic["implied_rr"]
    # For DirectionalOptions: avg R:R from simulation
    do_summary = summarise_opt_trades(do_trades)
    do_avg_rr  = do_summary["avg_rr"]

    print(f"""
  EQUITY STRATEGIES
  -----------------
  Viable strategies (PF > 1.0):""")
    if viable_eq:
        for name, pf, wr in sorted(viable_eq, key=lambda x: -x[1]):
            print(f"    [OK] {name:18s}  PF={pf:.2f}  WinRate={wr:.1%}")
    else:
        print("    None — all strategies below PF=1.0 on this dataset")

    print(f"""
  Recommended MIN_RISK_REWARD for EQUITY signals : {recommended_eq_rr}
    (At RR>={recommended_eq_rr}: {eq_sweep.get(recommended_eq_rr, {}).get('pass_pct', 0):.0f}% of historical signals pass)

  OPTIONS STRATEGIES
  ------------------
  DirectionalOptions avg simulated R:R : {do_avg_rr:.2f}
  IronCondor         avg simulated R:R : {summarise_opt_trades(ic_trades)['avg_rr']:.2f}

  Recommended MIN_RISK_REWARD for OPTIONS signals:
    DirectionalOptions : {min(0.75, round(do_avg_rr * 0.8, 2))} (floor; options R:R is structural)
    IronCondor         : {min(0.5, round(ic_rr_at_best * 0.9, 2))} (credit spreads have R:R below 1 by design)

  IRON CONDOR BEST PARAMETERS
  ----------------------------
  Best combo found:
    profit_target = {best_ic['profit_target']}
    stop_mult     = {best_ic['stop_mult']}
    Implied R:R   = {best_ic['implied_rr']}
    Trades: {best_ic['total_trades']}  WinRate: {best_ic['win_rate']:.1%}  PF: {best_ic['profit_factor']:.2f}

  OVERALL CONFIG RECOMMENDATION
  ------------------------------
  # config/settings.py -- recommended values
  MIN_RISK_REWARD = {recommended_eq_rr}        # was 1.5 -- was blocking nearly all signals

  # .env -- recommended options overrides
  IC_PROFIT_TARGET  = {best_ic['profit_target']}
  IC_STOP_MULT      = {best_ic['stop_mult']}

  # Signal-type aware RR check (implement in risk_manager.py):
  #   EQUITY signals  : MIN_RISK_REWARD = {recommended_eq_rr}
  #   OPTIONS (debit) : MIN_RISK_REWARD = 0.75
  #   OPTIONS (credit): MIN_RISK_REWARD = 0.40  # iron condor / short premium
""")

    print("=" * 70)
    print("  Backtest complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
