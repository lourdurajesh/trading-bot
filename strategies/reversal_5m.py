"""
reversal_5m.py
──────────────
Custom intraday bullish-reversal strategy for INDEX symbols on 5-minute candles.

Pattern (user spec)
───────────────────
  1. A red candle forms                      (close < open)
  2. Immediately followed by a green candle   (close > open)
  3. The green candle CLOSES above the red candle's OPEN
     (i.e. the green bar reclaims the whole red body), AND
     RSI is inside the desired band 30–70 AND RISING (rsi now > rsi prev)
  4. Enough volume on the green candle        (relative volume ≥ MIN_RVOL)
  → fire a LONG signal.

Notes
─────
  • Indices are not directly tradeable — live, a LONG index view is expressed by
    buying a call (DirectionalOptions / InstitutionalMomentum already do this).
    This strategy emits a DIRECTIONAL long Signal on the index price so the
    pattern's edge can be measured by the backtest engine first (the user asked
    to backtest before going live). Live options-wiring is a follow-up once the
    backtest validates the edge.
  • Fyers DOES provide volume for index 5m candles, so the volume gate is real.
    If a feed ever returns all-zero volume, the gate fails open (skipped) rather
    than blocking every signal — consistent with the MCX engine's handling.

All thresholds are module constants (same style as mean_reversion.py).
"""

import logging
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from analysis.indicators import atr, relative_volume, rsi
from strategies.base_strategy import BaseStrategy, Direction, Signal, SignalType

logger = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────
RSI_BAND_LOW    = 30.0    # RSI must be inside (30, 70) …
RSI_BAND_HIGH   = 70.0
MIN_RVOL        = 1.2     # green candle must have above-average volume
ATR_STOP_BUFFER = 0.5     # stop = pattern low − ATR×buffer
MIN_STOP_PCT    = 0.0015  # floor: stop at least 0.15% below entry (5m index noise)
TARGET_R1       = 1.5     # T1 — partial booking trigger
TARGET_R2       = 2.5     # T2 — final exit
MIN_BARS        = 30      # need ≥ RSI(14) warmup + the 2 pattern candles

# Opening blackout: skip the first 10 minutes (gap/auction noise). Live only —
# in backtest we replay by bar index, not wall-clock, so this is bypassed.
OPENING_BLACKOUT_END = dtime(9, 25)


class Reversal5mStrategy(BaseStrategy):
    """Bullish reversal (red→green reclaim) on 5m index candles."""

    def __init__(self):
        super().__init__()
        self.name       = "Reversal5m"
        self.hold_type  = "intraday"
        self.timeframe  = "5m"

    def evaluate(self, symbol: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        # Index-only strategy.
        if "-INDEX" not in symbol:
            return None

        # Opening blackout (live only).
        if not self.backtest_mode:
            from datetime import datetime
            if datetime.now(tz=IST).time() < OPENING_BLACKOUT_END:
                self.log_skip(symbol, "Opening blackout — waiting for 09:25 to avoid auction/gap noise")
                return None

        df = self.get_ohlcv(symbol, self.timeframe)
        if df is None or len(df) < MIN_BARS:
            self.log_skip(symbol, "Insufficient 5m data")
            return None

        ltp = self.get_ltp(symbol)
        if not ltp or ltp <= 0:
            return None

        close = df["close"]

        # ── Last two CLOSED candles: red (i-1) then green (i) ─────
        red_open,   red_close   = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
        green_open, green_close = float(df["open"].iloc[-1]), float(df["close"].iloc[-1])

        is_red    = red_close < red_open
        is_green  = green_close > green_open
        reclaims  = green_close > red_open          # green closes above red's open
        if not (is_red and is_green and reclaims):
            self.log_skip(symbol, "No red→green reclaim pattern on last 2 candles")
            return None

        # ── RSI inside band AND rising ────────────────────────────
        rsi_series = rsi(close)
        rsi_now, rsi_prev = float(rsi_series.iloc[-1]), float(rsi_series.iloc[-2])
        rsi_in_band = RSI_BAND_LOW < rsi_now < RSI_BAND_HIGH
        rsi_rising  = rsi_now > rsi_prev
        if not (rsi_in_band and rsi_rising):
            self.log_skip(
                symbol,
                f"RSI gate failed: {rsi_now:.1f} (band {RSI_BAND_LOW:.0f}-{RSI_BAND_HIGH:.0f}, "
                f"rising={rsi_rising})"
            )
            return None

        # ── Volume gate (fail-open when no volume feed) ───────────
        volume_available = df["volume"].sum() > 0
        rvol = float(relative_volume(df).iloc[-1])
        if volume_available and rvol < MIN_RVOL:
            self.log_skip(symbol, f"RVOL {rvol:.2f} below {MIN_RVOL} — weak participation")
            return None
        rvol_label = f"RVOL={rvol:.2f}x" if volume_available else "RVOL=N/A(no feed)"

        # ── Build the LONG signal ─────────────────────────────────
        entry   = green_close
        atr_val = float(atr(df).iloc[-1])
        pattern_low = min(float(df["low"].iloc[-2]), float(df["low"].iloc[-1]))
        stop = pattern_low - ATR_STOP_BUFFER * atr_val
        # Floor: don't let a tight pattern produce a micro-stop that noise blows through.
        stop = min(stop, entry * (1 - MIN_STOP_PCT))

        risk = entry - stop
        if risk <= 0:
            return None

        target_1 = entry + TARGET_R1 * risk
        target_2 = entry + TARGET_R2 * risk

        # Confidence: base for passing all gates + a bump for stronger RSI momentum.
        confidence = 0.55 + min(0.20, (rsi_now - rsi_prev) / 100.0 * 4)
        confidence = round(min(confidence, 0.90), 2)

        reason = (
            f"Red→green reclaim (green close {green_close:.1f} > red open {red_open:.1f}) | "
            f"RSI {rsi_prev:.0f}→{rsi_now:.0f} rising in band | {rvol_label}"
        )
        ctx = {
            "red_open":    round(red_open, 2),
            "red_close":   round(red_close, 2),
            "green_open":  round(green_open, 2),
            "green_close": round(green_close, 2),
            "rsi_now":     round(rsi_now, 1),
            "rsi_prev":    round(rsi_prev, 1),
            "rvol":        round(rvol, 2),
            "atr":         round(atr_val, 2),
        }

        # ── Backtest: directional index Signal (engine measures the underlying move) ──
        if self.backtest_mode:
            signal = Signal(
                symbol      = symbol,
                strategy    = self.name,
                direction   = Direction.LONG,
                signal_type = SignalType.EQUITY,
                entry       = round(entry, 2),
                stop_loss   = round(stop, 2),
                target_1    = round(target_1, 2),
                target_2    = round(target_2, 2),
                confidence  = confidence,
                timeframe   = self.timeframe,
                reason      = reason,
                meta        = ctx,
            )
            signal.calculate_rr()
            self.log_signal(signal)
            return signal

        # ── Live/paper: buy an ATM weekly call; exit via the underlying index-point
        # trailing stop (config-driven per index). Indices aren't directly tradeable. ──
        return self._build_options_signal(symbol, ltp, confidence, reason, ctx)

    def _build_options_signal(self, symbol, spot, confidence, reason, ctx) -> Optional[Signal]:
        from execution.options_executor import options_executor

        tp = options_executor.get_trail_points(symbol)
        if not tp:
            self.log_skip(symbol, "no index_trail_points config for this index")
            return None
        sl_pts, trail_pts = tp

        opt = options_executor.get_best_option(
            underlying=symbol, option_type="call", target_delta=0.50,
            min_dte=1, max_dte=7,
        )
        if not opt or opt.is_simulated or not opt.symbol:
            self.log_skip(symbol, "no live ATM call chain — skipping")
            return None
        entry_prem = float(opt.ltp or 0)
        if entry_prem < 5.0:
            self.log_skip(symbol, f"ATM premium ₹{entry_prem} too low")
            return None

        # Nominal premium stop for Signal.is_valid() + R reporting only — the REAL
        # exit is the underlying index-point trail handled by the exit manager.
        delta = abs(opt.delta) or 0.5
        stop_prem = round(entry_prem - delta * sl_pts, 2)
        if stop_prem >= entry_prem or stop_prem <= 0:
            stop_prem = round(entry_prem * 0.7, 2)
        nfo = opt.symbol

        sig = Signal(
            symbol         = symbol,
            strategy       = self.name,
            direction      = Direction.LONG,
            signal_type    = SignalType.OPTIONS,
            entry          = round(entry_prem, 2),
            stop_loss      = stop_prem,
            target_1       = round(entry_prem * 2.0, 2),   # nominal; trail governs exit
            confidence     = confidence,
            timeframe      = self.timeframe,
            reason         = f"{reason} | ATM CALL {nfo} ₹{entry_prem:.1f} | trail {sl_pts:.0f}/{trail_pts:.0f}pts",
            monitor_symbol = nfo,
            options_meta   = {
                "strategy":        "reversal_5m",
                "option_type":     "call",
                "nfo_symbol":      nfo,
                "lot_size":        opt.lot_size,
                "dte":             opt.dte,
                "iv":              opt.iv,
                "exit_mode":       options_executor.get_exit_mode(symbol),
                "underlying":      symbol,
                "entry_spot":      round(spot, 2),
                "sl_pts":          sl_pts,
                "trail_pts":       trail_pts,
                "peak_spot":       round(spot, 2),
                "trail_stop_spot": round(spot - sl_pts, 2),
                "entry_legs":      [{"symbol": nfo, "direction": "LONG"}],
                "exit_legs":       [{"symbol": nfo, "direction": "SHORT"}],
                **ctx,
            },
        )
        sig.calculate_rr()
        self.log_signal(sig)
        return sig
