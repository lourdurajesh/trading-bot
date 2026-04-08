"""
backtest_engine.py
──────────────────
Runs trading strategies against 3 years of historical OHLCV data.
Simulates realistic execution: slippage, brokerage, partial fills.

Usage:
    engine  = BacktestEngine()
    results = engine.run("NSE:RELIANCE-EQ", df_historical, TrendFollowStrategy())
    print(results.summary())
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from config.settings import RISK_PER_TRADE_PCT, TOTAL_CAPITAL

logger = logging.getLogger(__name__)

# Realistic execution assumptions
SLIPPAGE_PCT    = 0.05    # 0.05% slippage on entry and exit
BROKERAGE_PCT   = 0.03    # Zerodha-style flat brokerage per leg
STT_PCT         = 0.025   # Securities Transaction Tax on sell side
INITIAL_CAPITAL = TOTAL_CAPITAL


@dataclass
class Trade:
    symbol:        str
    direction:     str         # LONG / SHORT
    entry_date:    datetime
    entry_price:   float
    exit_date:     Optional[datetime] = None
    exit_price:    float              = 0.0
    stop_loss:     float              = 0.0
    target_1:      float              = 0.0
    target_2:      float              = 0.0
    position_size: int                = 0
    pnl:           float              = 0.0
    pnl_pct:       float              = 0.0
    exit_reason:   str                = ""   # TARGET1 | TARGET2 | BREAKEVEN | STOP | TIMEOUT
    holding_days:  int                = 0
    regime:        str                = ""
    # Partial-fill state — set internally after T1 is hit
    _t1_hit:       bool               = False


@dataclass
class BacktestResult:
    symbol:           str
    strategy:         str
    timeframe:        str
    start_date:       str
    end_date:         str
    trades:           list[Trade]     = field(default_factory=list)

    # Performance metrics (computed by performance.py)
    total_return_pct: float = 0.0
    win_rate:         float = 0.0
    profit_factor:    float = 0.0
    sharpe_ratio:     float = 0.0
    max_drawdown_pct: float = 0.0
    avg_holding_days: float = 0.0
    total_trades:     int   = 0
    winning_trades:   int   = 0
    losing_trades:    int   = 0
    avg_winner:       float = 0.0
    avg_loser:        float = 0.0
    expectancy:       float = 0.0     # avg P&L per trade

    def summary(self) -> str:
        return (
            f"{self.symbol} [{self.strategy}] | "
            f"Trades: {self.total_trades} | "
            f"Win: {self.win_rate:.0%} | "
            f"PF: {self.profit_factor:.2f} | "
            f"Sharpe: {self.sharpe_ratio:.2f} | "
            f"MaxDD: {self.max_drawdown_pct:.1f}% | "
            f"Return: {self.total_return_pct:+.1f}%"
        )


class BacktestEngine:
    """
    Runs a strategy against historical OHLCV data.

    The engine replays history bar by bar, calling strategy.evaluate()
    with only data available up to that point (no lookahead bias).
    """

    def __init__(self, initial_capital: float = INITIAL_CAPITAL):
        self.initial_capital = initial_capital

    def run(
        self,
        symbol:     str,
        df:         pd.DataFrame,
        strategy,
        timeframe:  str = "1D",
        warmup_bars: int = 60,   # bars needed before strategy can fire
    ) -> BacktestResult:
        """
        Run strategy on historical data.

        df: DataFrame with timestamp, open, high, low, close, volume
        strategy: instance of BaseStrategy subclass
        warmup_bars: minimum bars before evaluation starts
        """
        from data.data_store import DataStore
        import threading

        if len(df) < warmup_bars + 10:
            logger.warning(f"[Backtest] Insufficient data for {symbol}: {len(df)} rows")
            return BacktestResult(symbol=symbol, strategy=strategy.name, timeframe=timeframe,
                                  start_date="", end_date="")

        result = BacktestResult(
            symbol     = symbol,
            strategy   = strategy.name,
            timeframe  = timeframe,
            start_date = str(df["timestamp"].iloc[0].date()),
            end_date   = str(df["timestamp"].iloc[-1].date()),
        )

        trades        = []
        open_trade:   Optional[Trade] = None
        capital       = self.initial_capital
        equity_curve  = [capital]

        # Create isolated DataStore for backtesting
        bt_store = DataStore()

        # Determine which extra timeframes the strategy needs so the regime
        # detector and confirmation filters don't abort due to missing data.
        extra_tfs = set()
        if hasattr(strategy, "timeframe"):
            extra_tfs.add(strategy.timeframe)
        if hasattr(strategy, "confirm_tf"):
            extra_tfs.add(strategy.confirm_tf)
        extra_tfs.discard(timeframe)   # already populated below

        for i in range(warmup_bars, len(df)):
            # Feed history up to bar i into isolated store (no lookahead)
            window = df.iloc[:i+1].copy()
            records = window.to_dict("records")
            bt_store._candles[symbol][timeframe] = records
            # Also populate any extra timeframes the strategy requests so
            # regime_detector / get_ohlcv don't abort with missing data.
            for tf in extra_tfs:
                bt_store._candles[symbol][tf] = records
            bt_store._ltp[symbol] = float(window["close"].iloc[-1])

            bar        = df.iloc[i]
            bar_high   = float(bar["high"])
            bar_low    = float(bar["low"])
            bar_close  = float(bar["close"])
            bar_date   = bar["timestamp"]

            # ── Manage open trade ─────────────────────────────────
            # Two-phase partial exit:
            #   Phase 1 (full size): stop → STOP, T1 → exit half, trail stop to breakeven
            #   Phase 2 (half size): breakeven stop → BREAKEVEN, T2 → TARGET2
            if open_trade:
                bars_held = i - self._find_entry_bar(df, open_trade.entry_date)

                if not open_trade._t1_hit:
                    # ── Phase 1: full position ────────────────────────
                    if open_trade.direction == "LONG" and bar_low <= open_trade.stop_loss:
                        exit_price = open_trade.stop_loss * (1 - SLIPPAGE_PCT / 100)
                        open_trade = self._close_trade(open_trade, exit_price, bar_date, "STOP", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                    elif open_trade.direction == "LONG" and bar_high >= open_trade.target_1:
                        # Exit half at T1, keep half riding to T2
                        half    = open_trade.position_size // 2 if open_trade.position_size >= 2 else 0
                        t1_exit = open_trade.target_1 * (1 - SLIPPAGE_PCT / 100)
                        if half == 0:
                            # Size-1 trade: exit full position at T1, no T2 leg
                            open_trade = self._close_trade(open_trade, t1_exit, bar_date, "TARGET1", trades)
                            capital   += open_trade.pnl
                            open_trade = None
                        else:
                            t1_trade = Trade(
                                symbol        = open_trade.symbol,
                                direction     = open_trade.direction,
                                entry_date    = open_trade.entry_date,
                                entry_price   = open_trade.entry_price,
                                stop_loss     = open_trade.stop_loss,
                                target_1      = open_trade.target_1,
                                target_2      = open_trade.target_2,
                                position_size = half,
                            )
                            t1_trade = self._close_trade(t1_trade, t1_exit, bar_date, "TARGET1", trades)
                            capital += t1_trade.pnl
                            # Remaining half: trail stop to entry (breakeven)
                            open_trade.position_size = open_trade.position_size - half
                            open_trade.stop_loss     = open_trade.entry_price
                            open_trade._t1_hit       = True

                    elif open_trade.direction == "SHORT" and bar_high >= open_trade.stop_loss:
                        exit_price = open_trade.stop_loss * (1 + SLIPPAGE_PCT / 100)
                        open_trade = self._close_trade(open_trade, exit_price, bar_date, "STOP", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                    elif open_trade.direction == "SHORT" and bar_low <= open_trade.target_1:
                        half    = open_trade.position_size // 2 if open_trade.position_size >= 2 else 0
                        t1_exit = open_trade.target_1 * (1 + SLIPPAGE_PCT / 100)
                        if half == 0:
                            open_trade = self._close_trade(open_trade, t1_exit, bar_date, "TARGET1", trades)
                            capital   += open_trade.pnl
                            open_trade = None
                        else:
                            t1_trade = Trade(
                                symbol        = open_trade.symbol,
                                direction     = open_trade.direction,
                                entry_date    = open_trade.entry_date,
                                entry_price   = open_trade.entry_price,
                                stop_loss     = open_trade.stop_loss,
                                target_1      = open_trade.target_1,
                                target_2      = open_trade.target_2,
                                position_size = half,
                            )
                            t1_trade = self._close_trade(t1_trade, t1_exit, bar_date, "TARGET1", trades)
                            capital += t1_trade.pnl
                            open_trade.position_size = open_trade.position_size - half
                            open_trade.stop_loss     = open_trade.entry_price
                            open_trade._t1_hit       = True

                    elif bars_held > 20:
                        open_trade = self._close_trade(open_trade, bar_close, bar_date, "TIMEOUT", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                else:
                    # ── Phase 2: half position, stop at breakeven ─────
                    if open_trade.direction == "LONG" and bar_low <= open_trade.stop_loss:
                        # Stop trailed to entry — exits at breakeven
                        be_exit = open_trade.stop_loss * (1 - SLIPPAGE_PCT / 100)
                        open_trade = self._close_trade(open_trade, be_exit, bar_date, "BREAKEVEN", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                    elif open_trade.direction == "LONG" and bar_high >= open_trade.target_2:
                        t2_exit = open_trade.target_2 * (1 - SLIPPAGE_PCT / 100)
                        open_trade = self._close_trade(open_trade, t2_exit, bar_date, "TARGET2", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                    elif open_trade.direction == "SHORT" and bar_high >= open_trade.stop_loss:
                        be_exit = open_trade.stop_loss * (1 + SLIPPAGE_PCT / 100)
                        open_trade = self._close_trade(open_trade, be_exit, bar_date, "BREAKEVEN", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                    elif open_trade.direction == "SHORT" and bar_low <= open_trade.target_2:
                        t2_exit = open_trade.target_2 * (1 + SLIPPAGE_PCT / 100)
                        open_trade = self._close_trade(open_trade, t2_exit, bar_date, "TARGET2", trades)
                        capital   += open_trade.pnl
                        open_trade = None

                    elif bars_held > 40:   # give T2 leg double the time
                        open_trade = self._close_trade(open_trade, bar_close, bar_date, "TIMEOUT", trades)
                        capital   += open_trade.pnl
                        open_trade = None

            # ── Evaluate new signal ───────────────────────────────
            if open_trade is None:
                try:
                    # Patch store in every module that imported it directly.
                    # `from data.data_store import store` creates a local name
                    # that won't follow ds_module.store reassignment, so we
                    # must patch each consumer module individually.
                    import data.data_store as ds_module
                    import strategies.base_strategy as base_strat_module
                    import analysis.regime_detector as regime_module

                    original_store          = ds_module.store
                    ds_module.store         = bt_store
                    base_strat_module.store = bt_store
                    regime_module.store     = bt_store

                    # Enable backtest mode so strategies skip live-only guards
                    # (opening blackout time check, strict regime filter, etc.)
                    strategy.backtest_mode = True
                    signal = strategy.evaluate(symbol)
                    strategy.backtest_mode = False

                    # Restore all patched references
                    ds_module.store         = original_store
                    base_strat_module.store = original_store
                    regime_module.store     = original_store

                    if signal and signal.is_valid():
                        # Apply slippage to entry
                        entry_price = signal.entry * (
                            1 + SLIPPAGE_PCT / 100
                            if signal.direction.value == "LONG"
                            else 1 - SLIPPAGE_PCT / 100
                        )
                        # Position sizing — scale by signal confidence so
                        # marginal RSI 60-70 setups get smaller size than
                        # high-conviction RSI 75+ setups.
                        confidence_scalar = getattr(signal, "confidence", 1.0)
                        risk_amount   = capital * (RISK_PER_TRADE_PCT / 100) * confidence_scalar
                        risk_per_unit = abs(entry_price - signal.stop_loss)
                        min_risk      = entry_price * 0.001
                        if risk_per_unit < min_risk:
                            continue
                        size = int(risk_amount / risk_per_unit)
                        if size <= 0:
                            continue

                        open_trade = Trade(
                            symbol        = symbol,
                            direction     = signal.direction.value,
                            entry_date    = bar_date,
                            entry_price   = entry_price,
                            stop_loss     = signal.stop_loss,
                            target_1      = signal.target_1,
                            target_2      = getattr(signal, "target_2", 0.0),
                            position_size = size,
                        )

                except Exception as e:
                    strategy.backtest_mode = False
                    if original_store:
                        import data.data_store as ds_module
                        import strategies.base_strategy as base_strat_module
                        import analysis.regime_detector as regime_module
                        ds_module.store         = original_store
                        base_strat_module.store = original_store
                        regime_module.store     = original_store
                    logger.debug(f"[Backtest] Signal eval error on bar {i}: {e}")

            equity_curve.append(capital)

        # Close any open trade at end of data
        if open_trade and len(df) > 0:
            last_close = float(df["close"].iloc[-1])
            open_trade = self._close_trade(
                open_trade, last_close, df["timestamp"].iloc[-1], "EOD", trades
            )
            trades.append(open_trade)

        result.trades = trades
        return result

    def run_multi(
        self,
        symbol:     str,
        data:       dict[str, pd.DataFrame],
        strategy,
    ) -> BacktestResult:
        """
        Run strategy using multi-timeframe data.
        data: dict of timeframe → DataFrame
        Uses daily as primary, hourly for entry timing.
        """
        primary_tf = "1D"
        df = data.get(primary_tf)
        if df is None:
            logger.warning(f"[Backtest] No daily data for {symbol}")
            return BacktestResult(symbol=symbol, strategy=strategy.name, timeframe=primary_tf,
                                  start_date="", end_date="")
        return self.run(symbol, df, strategy, primary_tf)

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _close_trade(
        self,
        trade:      Trade,
        exit_price: float,
        exit_date:  datetime,
        reason:     str,
        trades:     list,
    ) -> Trade:
        """Calculate P&L and close a trade."""
        # Brokerage and taxes
        brokerage = (trade.entry_price + exit_price) * trade.position_size * BROKERAGE_PCT / 100
        stt       = exit_price * trade.position_size * STT_PCT / 100

        if trade.direction == "LONG":
            gross_pnl = (exit_price - trade.entry_price) * trade.position_size
        else:
            gross_pnl = (trade.entry_price - exit_price) * trade.position_size

        trade.pnl          = round(gross_pnl - brokerage - stt, 2)
        trade.exit_price   = exit_price
        trade.exit_date    = exit_date
        trade.exit_reason  = reason
        trade.holding_days = max(1, (exit_date - trade.entry_date).days)
        cost = trade.entry_price * trade.position_size
        trade.pnl_pct = round(trade.pnl / cost * 100, 2) if cost else 0.0
        trades.append(trade)
        return trade

    def _find_entry_bar(self, df: pd.DataFrame, entry_date) -> int:
        """Find bar index matching entry date."""
        matches = df.index[df["timestamp"] == entry_date].tolist()
        return matches[0] if matches else 0
