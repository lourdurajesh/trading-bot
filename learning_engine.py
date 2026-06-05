"""
learning_engine.py
──────────────────
Runs simple learning paper trades independently of the main strategy loop.

What it does every cycle:
  1. Evaluates SimpleRSI + SimpleMomentum on the learning watchlist
  2. Opens paper positions when a signal fires (if not already in one)
  3. Monitors open positions against stop/target
  4. Logs everything to learning_trades table (rich metadata for review)

The learning trades are PAPER ONLY and completely isolated from the
production risk manager and order manager.

Access results via:
  GET /learning/trades   — all trades (open + closed)
  GET /learning/stats    — win rate, avg R, top patterns
  GET /learning/review   — grouped by outcome with metadata
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST    = ZoneInfo("Asia/Kolkata")
DB_PATH = "db/trades.db"

logger = logging.getLogger(__name__)

# ── Slippage model ────────────────────────────────────────────────
# Applied at entry (worse fill) and exit (worse fill) to make P&L realistic.
SLIPPAGE_EQUITY  = 0.0005   # 0.05% — liquid large-caps / MCX futures
SLIPPAGE_OPTIONS = 0.003    # 0.30% — accounts for bid-ask spread on options

# ── Trading fees (round trip) ─────────────────────────────────────
# Fyers: ₹20 flat per order (not per lot) for all segments.
# Round trip = entry order + exit order = ₹40 flat regardless of lot count.
FEES_EQUITY_FLAT   = 40.0   # ₹40 per equity/futures round trip
FEES_OPTIONS_FLAT  = 40.0   # ₹40 per options round trip (₹20 × 2 orders, flat)

# ── Swing vs intraday hold classification ─────────────────────────
# Swing strategies are NOT forced to close at EOD; they run until
# stop/target hit or SWING_MAX_HOLD_DAYS trading days have elapsed.
_SWING_STRATEGIES = {"TrendFollow"}
SWING_MAX_HOLD_DAYS  = 5
CHANDELIER_MULT      = 2.5   # ATR multiplier for Chandelier trailing stop
EOD_TIGHT_HOUR       = 14    # after 2pm, tighten stop to protect 50% of intraday gain
EOD_TIGHT_MINUTE     = 0
MAX_BARS_INTRADAY    = 10    # 10 × 15min = 2.5hr max; RSI bounces resolve or fail within 2hr
VOLATILITY_CONTRACTION = 0.50  # exit if current ATR < 50% of entry ATR (momentum gone)


class LearningEngine:

    def __init__(self):
        self._open_positions: dict[str, dict] = {}  # symbol+strategy → trade
        self._cooldowns: dict[str, datetime] = {}
        self._init_db()
        self._restore_open_positions()
        self._load_cooldowns()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC — called from main loop
    # ─────────────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """Evaluate all strategies and manage open positions."""
        from config.learning_watchlist import (
            LEARNING_NSE_EQUITIES, LEARNING_NSE_INDICES, get_mcx_learning_symbols,
        )
        from strategies.trend_follow    import TrendFollowStrategy
        from strategies.mean_reversion  import MeanReversionStrategy
        from strategies.simple_momentum import SimpleMomentumStrategy
        from strategies.simple_rsi      import SimpleRSIStrategy
        from data.data_store            import store

        # Compute current front-month contract codes each cycle — auto-rolls on expiry
        mcx_symbols = get_mcx_learning_symbols()

        equity_strategies = [
            TrendFollowStrategy(),
            MeanReversionStrategy(),
            SimpleMomentumStrategy(),
            SimpleRSIStrategy(),
        ]

        # ── 1. Monitor existing open positions ───────────────────
        self._check_exits(store)

        # ── 2. Equity + commodity entries — 1 trade per symbol per strategy ──
        for symbol in LEARNING_NSE_EQUITIES + mcx_symbols:
            if "-INDEX" in symbol:
                continue  # safety: indices must never go through equity strategies
            if self._is_on_cooldown(symbol):
                continue
            if self._is_earnings_blocked(symbol):
                continue

            for strat in equity_strategies:
                if f"{symbol}:{strat.name}" in self._open_positions:
                    continue  # already in this strategy's trade for this symbol
                try:
                    signal = strat.evaluate(symbol)
                except Exception as exc:
                    logger.debug(f"[Learning] {strat.name}/{symbol} error: {exc}")
                    continue
                if signal:
                    self._open_trade(self._sig_to_learning_dict(signal, strat.name, strat))

        # ── 3. Index options entries ──────────────────────────────
        self._run_index_options_learning(LEARNING_NSE_INDICES)

    def _sig_to_learning_dict(self, sig, strategy_name: str, strat=None) -> dict:
        """Convert a Signal object or legacy dict to the learning trade dict format."""
        # Read hold_type from the strategy object itself — each strategy declares
        # its own type. Fall back to _SWING_STRATEGIES list for any strategy that
        # hasn't been updated yet.
        if strat is not None and hasattr(strat, "hold_type"):
            hold_type = strat.hold_type
        else:
            base_name = strategy_name.replace("_LRN", "")
            hold_type = "swing" if base_name in _SWING_STRATEGIES else "intraday"

        if isinstance(sig, dict):
            # SimpleRSI / SimpleMomentum return dicts — just inject hold_type
            d = sig.copy()
            d.setdefault("metadata", {})["hold_type"] = hold_type
            return d

        entry = sig.entry
        stop  = sig.stop_loss
        tgt   = sig.target_1
        rr    = sig.risk_reward or (
            round(abs(tgt - entry) / abs(entry - stop), 2)
            if abs(entry - stop) > 0 else 0
        )
        meta = {
            "regime":      sig.regime,
            "reason":      sig.reason,
            "confidence":  sig.confidence,
            "signal_type": sig.signal_type.value if hasattr(sig.signal_type, "value") else str(sig.signal_type),
            "hold_type":   hold_type,
        }
        if sig.options_meta:
            meta.update(sig.options_meta)
        if getattr(sig, "signal_type", None) and sig.signal_type.value == "OPTIONS":
            meta["instrument_type"] = "nse_options"
        return {
            "strategy":    strategy_name,
            "symbol":      sig.symbol,
            "direction":   sig.direction.value,
            "entry_price": entry,
            "stop_loss":   stop,
            "target":      tgt,
            "rr":          rr,
            "metadata":    meta,
        }

    def _run_index_options_learning(self, index_symbols: list) -> None:
        """
        Evaluate DirectionalOptions and InstitutionalMomentum on NIFTY/BANKNIFTY/FINNIFTY.
        Tracks using actual option premium (entry=debit cost, stop=50% loss, target=max_profit).
        _check_exits() uses nfo_symbol LTP for premium-based P&L in rupees.
        Paper wallet mirrors use lot-based sizing (lots × lot_size × premium).
        """
        try:
            from strategies.directional_options    import DirectionalOptionsStrategy
            from strategies.institutional_momentum import InstitutionalMomentumStrategy
            index_strategies = [
                DirectionalOptionsStrategy(),
                InstitutionalMomentumStrategy(),
            ]
            for symbol in index_symbols:
                if self._is_on_cooldown(symbol):
                    continue
                for strat in index_strategies:
                    strat_name = f"{strat.name}_LRN"
                    if f"{symbol}:{strat_name}" in self._open_positions:
                        continue
                    try:
                        sig = strat.evaluate(symbol)
                    except Exception as exc:
                        logger.debug(f"[Learning/IndexOptions] {strat.name}/{symbol}: {exc}")
                        continue
                    if sig:
                        self._open_trade(self._sig_to_learning_dict(sig, strat_name))
        except Exception as e:
            logger.debug(f"[Learning/IndexOptions] setup error: {e}")

    # ─────────────────────────────────────────────────────────────
    # TRADE MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    def _open_trade(self, signal: dict) -> None:
        symbol   = signal["symbol"]
        strategy = signal["strategy"]
        meta     = signal.get("metadata") or {}
        direction        = signal["direction"]
        instrument_type  = meta.get("instrument_type", "")

        # Refuse to open an options trade without a real contract symbol — it cannot be monitored
        if instrument_type == "nse_options" and not meta.get("nfo_symbol"):
            logger.warning(f"[Learning] Skipping {strategy} {symbol} — options trade has no nfo_symbol (no chain data)")
            return

        # Apply entry slippage: buyer pays more, seller receives less
        slip     = SLIPPAGE_OPTIONS if instrument_type == "nse_options" else SLIPPAGE_EQUITY
        raw_entry = float(signal["entry_price"])
        if direction == "LONG" or instrument_type == "nse_options":
            entry_price = round(raw_entry * (1 + slip), 2)
        else:
            entry_price = round(raw_entry * (1 - slip), 2)

        # Store risk context for trailing stop — equity/futures only
        if instrument_type != "nse_options":
            risk_pts = round(abs(entry_price - float(signal["stop_loss"])), 2)
            meta["risk_pts"]      = risk_pts
            meta["original_stop"] = float(signal["stop_loss"])
            # Persist target_2 in metadata so it survives bot restarts (no DB column)
            if signal.get("target_2"):
                meta["target_2"] = float(signal["target_2"])

        trade_id = f"LRN-{uuid.uuid4().hex[:8].upper()}"
        now_str  = datetime.now(tz=IST).isoformat()

        trade = {
            "id":           trade_id,
            "symbol":       symbol,
            "strategy":     strategy,
            "direction":    direction,
            "entry_price":  entry_price,
            "stop_loss":    signal["stop_loss"],
            "target":       signal["target"],
            "target_2":     float(signal.get("target_2") or 0),
            "rr":           signal["rr"],
            "metadata":     meta,
            "entry_time":   now_str,
            "status":       "OPEN",
            "mae_pts":      0.0,
            "mfe_pts":      0.0,
        }

        self._open_positions[f"{symbol}:{strategy}"] = trade
        self._db_insert(trade)
        logger.info(
            f"[Learning] OPEN {trade_id} | {strategy} {signal['direction']} {symbol} "
            f"@ {signal['entry_price']:.2f} | SL {signal['stop_loss']:.2f} "
            f"T {signal['target']:.2f} | R:R {signal['rr']:.1f}"
        )

        # Mirror to paper wallet — capital-limited; options use lot-based sizing
        try:
            from paper_trading import paper_trading_engine
            paper_trading_engine.mirror_learning_open(trade)
        except Exception as exc:
            logger.debug(f"[Learning] Paper mirror open error: {exc}")

        # Subscribe NFO option contract for live tick monitoring
        if instrument_type == "nse_options":
            syms = []
            nfo = meta.get("nfo_symbol")
            if nfo:
                syms.append(nfo)
            for leg in meta.get("entry_legs", []):
                s = leg.get("symbol")
                if s and s not in syms:
                    syms.append(s)
            if syms:
                try:
                    from data.fyers_stream import fyers_stream
                    fyers_stream.subscribe_extra(syms)
                except Exception as e:
                    logger.error(f"[Learning] Failed to subscribe options ticks for {symbol}: {e}")

    def _check_exits(self, store) -> None:
        from datetime import time as dtime
        closed_keys = []

        for key, trade in list(self._open_positions.items()):
            symbol   = trade["symbol"]
            metadata = trade.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            instrument_type = metadata.get("instrument_type", "")
            nfo_symbol      = metadata.get("nfo_symbol")
            lot_size        = int(metadata.get("lot_size", 1)) or 1

            # Options trades without a real contract symbol can't be monitored —
            # force EOD-close them rather than leaving them open forever
            if instrument_type == "nse_options" and not nfo_symbol:
                now_time = datetime.now(tz=IST).time()
                if now_time >= dtime(15, 20):
                    self._db_close(trade["id"], trade["entry_price"], "EOD_NO_CONTRACT", 0.0, 0.0, 0.0, 0.0)
                    closed_keys.append(key)
                    logger.warning(f"[Learning] EOD-closed {trade['id']} — options trade had no nfo_symbol")
                    try:
                        from paper_trading import paper_trading_engine
                        paper_trading_engine.mirror_learning_close(trade["id"], trade["entry_price"], "EOD_NO_CONTRACT")
                    except Exception:
                        pass
                continue

            # Options must use nfo_symbol LTP — never fall back to index LTP
            # (index at 25,000 against premium stop at ₹100 would never trigger)
            if instrument_type == "nse_options":
                ltp = store.get_ltp(nfo_symbol)
                _eod_now = datetime.now(tz=IST).time() >= dtime(15, 20)
                if (not ltp or ltp <= 0) and not _eod_now:
                    continue  # tick data unavailable — wait for next cycle
                if not ltp or ltp <= 0:
                    ltp = trade["entry_price"]  # EOD fallback: record exit at entry (P&L = fees)
            else:
                ltp = store.get_ltp(symbol)
            if not ltp:
                continue

            direction = trade["direction"]
            entry     = trade["entry_price"]
            hold_type = metadata.get("hold_type", "intraday")

            # MAE/MFE — update first; Chandelier derives peak_ltp from mfe_pts
            if instrument_type == "nse_options":
                adverse    = entry - ltp
                favourable = ltp - entry
            elif direction == "LONG":
                adverse    = entry - ltp
                favourable = ltp - entry
            else:
                adverse    = ltp - entry
                favourable = entry - ltp
            trade["mae_pts"] = max(trade["mae_pts"], adverse)
            trade["mfe_pts"] = max(trade["mfe_pts"], favourable)

            # Trail, protect, and partial-book (equity/futures only)
            if instrument_type != "nse_options":
                self._update_chandelier_stop(trade, ltp)
                self._apply_time_tightening(trade, ltp)
                self._check_partial_booking(trade, ltp)

            # Read stop/target AFTER all trail updates
            stop        = trade["stop_loss"]
            target      = trade["target"]
            target_2    = trade.get("target_2", 0)
            # Use T2 as final exit when present, else T1
            final_target = target_2 if target_2 > 0 else target
            final_reason = "TARGET2" if target_2 > 0 else "TARGET"
            exit_reason = None
            exit_price  = None

            # Stop / target exits
            if instrument_type == "nse_options":
                if ltp <= stop:
                    exit_reason, exit_price = "STOP",   ltp
                elif ltp >= target:
                    exit_reason, exit_price = "TARGET", ltp
            elif direction == "LONG":
                if ltp <= stop:
                    original_stop = metadata.get("original_stop", stop)
                    exit_reason = "TRAIL_STOP" if abs(stop - original_stop) > 0.01 else "STOP"
                    exit_price  = ltp
                elif ltp >= final_target:
                    exit_reason, exit_price = final_reason, ltp
            else:
                if ltp >= stop:
                    original_stop = metadata.get("original_stop", stop)
                    exit_reason = "TRAIL_STOP" if abs(stop - original_stop) > 0.01 else "STOP"
                    exit_price  = ltp
                elif ltp <= final_target:
                    exit_reason, exit_price = final_reason, ltp

            # Signal reversal exit — only when profitable; let stop handle losses.
            # Firing on an unprofitable trade exits at a tiny gain while the stop was
            # still 1.5×ATR away — terrible risk/reward.
            if exit_reason is None and instrument_type != "nse_options":
                is_profitable = (
                    (direction == "LONG"  and ltp > entry) or
                    (direction == "SHORT" and ltp < entry)
                )
                if is_profitable and self._check_signal_reversal(trade, store):
                    exit_reason = "SIGNAL_REVERSAL"
                    exit_price  = ltp

            # BB middle cross — price returned through centre band, bounce failed
            if exit_reason is None and instrument_type != "nse_options":
                bb_exit = self._check_bb_middle_exit(trade, ltp, store)
                if bb_exit:
                    exit_reason = bb_exit
                    exit_price  = ltp

            # Time-based exit — max N bars to prevent dead-money trades lingering all day
            if exit_reason is None and trade.get("strategy") == "SimpleRSI":
                try:
                    entry_dt     = datetime.fromisoformat(trade["entry_time"]).astimezone(IST)
                    elapsed_secs = (datetime.now(tz=IST) - entry_dt).total_seconds()
                    bars_elapsed = int(elapsed_secs // (15 * 60))
                    if bars_elapsed >= MAX_BARS_INTRADAY:
                        exit_reason = "TIME_EXIT"
                        exit_price  = ltp
                except Exception:
                    pass

            # Volatility contraction — ATR collapsed, momentum is gone
            if exit_reason is None and instrument_type != "nse_options":
                if self._check_volatility_exit(trade, store):
                    exit_reason = "VOLATILITY_EXIT"
                    exit_price  = ltp

            # EOD close only for intraday strategies
            # Swing strategies run across sessions until stop/target or max hold
            now_time = datetime.now(tz=IST).time()
            if now_time >= dtime(15, 20) and exit_reason is None:
                if hold_type == "intraday":
                    exit_reason = "EOD"
                    exit_price  = ltp
                else:
                    try:
                        entry_dt  = datetime.fromisoformat(trade["entry_time"]).astimezone(IST)
                        days_held = (datetime.now(tz=IST) - entry_dt).days
                        if days_held >= SWING_MAX_HOLD_DAYS:
                            exit_reason = "MAX_HOLD"
                            exit_price  = ltp
                    except Exception:
                        pass

            if exit_reason:
                # Gap 3: apply exit slippage (exit at worse price)
                slip = SLIPPAGE_OPTIONS if instrument_type == "nse_options" else SLIPPAGE_EQUITY
                if direction == "LONG" or instrument_type == "nse_options":
                    eff_exit = round(exit_price * (1 - slip), 2)  # sell at bid
                else:
                    eff_exit = round(exit_price * (1 + slip), 2)  # buy back at ask

                if instrument_type == "nse_options":
                    # pnl_pts is in ₹ (premium diff × lot_size) — fees deduct directly
                    fees    = FEES_OPTIONS_FLAT
                    pnl_pts = round((eff_exit - entry) * lot_size - fees, 2)
                    pnl_r   = round((eff_exit - entry) / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0
                else:
                    fees = FEES_EQUITY_FLAT
                    # Partial booking: 50% was booked at 1R; blend with final exit
                    if metadata.get("partial_booked") and metadata.get("partial_price"):
                        half_price = float(metadata["partial_price"])
                        half1 = (half_price - entry) if direction == "LONG" else (entry - half_price)
                        half2 = (eff_exit  - entry) if direction == "LONG" else (entry - eff_exit)
                        pnl_pts = round((half1 + half2) / 2, 2)
                    else:
                        pnl_pts = round((eff_exit - entry) if direction == "LONG" else (entry - eff_exit), 2)
                    # pnl_r against original risk (not trailed stop) for consistent R reporting
                    orig_risk = abs(entry - float(metadata.get("original_stop", stop)))
                    pnl_r = round(pnl_pts / orig_risk, 2) if orig_risk > 0 else 0

                exit_price = eff_exit  # store slippage-adjusted price

                self._db_close(
                    trade["id"], exit_price, exit_reason, pnl_pts, pnl_r,
                    trade["mae_pts"], trade["mfe_pts"], fees,
                )
                closed_keys.append(key)
                logger.info(
                    f"[Learning] CLOSE {trade['id']} | {exit_reason} @ {exit_price:.2f} "
                    f"| PnL {pnl_pts:+.2f} ({'₹' if instrument_type=='nse_options' else 'pts'}) "
                    f"fees ₹{fees:.0f} ({pnl_r:+.1f}R)"
                )

                win_exits = {"TARGET", "TARGET1", "TARGET2", "TRAIL_STOP", "SIGNAL_REVERSAL",
                             "BB_MID_CROSS", "TIME_EXIT", "VOLATILITY_EXIT"}
                if exit_reason in win_exits:
                    self._apply_cooldown(symbol, minutes=30)
                else:
                    self._apply_cooldown(symbol, minutes=60)

                try:
                    from paper_trading import paper_trading_engine
                    paper_trading_engine.mirror_learning_close(
                        trade["id"], exit_price, exit_reason
                    )
                except Exception as exc:
                    logger.debug(f"[Learning] Paper mirror close error: {exc}")

        for k in closed_keys:
            del self._open_positions[k]

    def _update_chandelier_stop(self, trade: dict, ltp: float) -> None:
        """Chandelier Exit: trail stop at N×ATR from the highest high (LONG) or lowest low (SHORT).

        Adapts to volatility — stays loose in a smooth trend, tightens when ATR expands.
        Uses ATR from entry metadata; peak_ltp is derived from mfe_pts on first call after restart.
        """
        metadata     = trade.get("metadata") or {}
        direction    = trade["direction"]
        entry        = trade["entry_price"]
        atr_val      = float(metadata.get("atr", 0) or 0)
        if atr_val <= 0:
            return  # strategy didn't compute ATR — skip

        # Derive peak/trough from mfe_pts if not yet tracked this session
        if "peak_ltp" not in trade:
            mfe = trade.get("mfe_pts", 0)
            trade["peak_ltp"] = (entry + mfe) if direction == "LONG" else (entry - mfe)

        # Mean-reversion strategies (SimpleRSI) need a tight 1×ATR trail so the
        # SL ratchets up quickly. Trend-following strategies keep the loose 2.5× trail.
        mult = 1.0 if trade.get("strategy") == "SimpleRSI" else CHANDELIER_MULT

        current_stop = trade["stop_loss"]

        if direction == "LONG":
            trade["peak_ltp"] = max(trade["peak_ltp"], ltp)
            chandelier = round(trade["peak_ltp"] - mult * atr_val, 2)
            if chandelier <= current_stop:
                return  # don't loosen the stop
            trade["stop_loss"] = chandelier
        else:  # SHORT
            trade["peak_ltp"] = min(trade["peak_ltp"], ltp)
            chandelier = round(trade["peak_ltp"] + mult * atr_val, 2)
            if chandelier >= current_stop:
                return  # don't loosen the stop
            trade["stop_loss"] = chandelier

        logger.info(
            f"[Learning] CHANDELIER {trade['id']} | "
            f"peak {trade['peak_ltp']:.2f} ± {mult}×ATR({atr_val:.2f}) "
            f"= stop {current_stop:.2f} → {trade['stop_loss']:.2f}"
        )
        self._db_update_stop(trade["id"], trade["stop_loss"])

    def _apply_time_tightening(self, trade: dict, ltp: float) -> None:
        """After 14:00, tighten stop to protect at least 50% of any open intraday gain.

        Prevents giving back profit at EOD when there's no time to reach the target.
        """
        from datetime import time as dtime
        if datetime.now(tz=IST).time() < dtime(EOD_TIGHT_HOUR, EOD_TIGHT_MINUTE):
            return

        direction    = trade["direction"]
        entry        = trade["entry_price"]
        current_stop = trade["stop_loss"]

        if direction == "LONG":
            profit_pts = ltp - entry
            if profit_pts <= 0:
                return
            protective = round(entry + 0.5 * profit_pts, 2)
            if protective <= current_stop:
                return  # already protected at least this much
            trade["stop_loss"] = protective
        else:  # SHORT
            profit_pts = entry - ltp
            if profit_pts <= 0:
                return
            protective = round(entry - 0.5 * profit_pts, 2)
            if protective >= current_stop:
                return
            trade["stop_loss"] = protective

        logger.info(
            f"[Learning] EOD-TIGHT {trade['id']} | "
            f"protecting 50% of {profit_pts:.1f} pts | "
            f"stop {current_stop:.2f} → {trade['stop_loss']:.2f}"
        )
        self._db_update_stop(trade["id"], trade["stop_loss"])

    def _check_partial_booking(self, trade: dict, ltp: float) -> None:
        """Simulate booking 50% of the position when 1R profit is first reached.

        Records partial_booked and partial_price in metadata so the final P&L
        calculation blends the booked half with the remaining position's outcome.
        """
        metadata = trade.get("metadata") or {}
        if metadata.get("partial_booked"):
            return  # already recorded

        direction = trade["direction"]
        entry     = trade["entry_price"]
        risk_pts  = float(metadata.get("risk_pts", 0) or 0)
        if risk_pts <= 0:
            return

        reached_1r = (ltp >= entry + risk_pts) if direction == "LONG" else (ltp <= entry - risk_pts)
        if not reached_1r:
            return

        metadata["partial_booked"] = True
        metadata["partial_price"]  = round(ltp, 2)
        trade["metadata"] = metadata
        logger.info(
            f"[Learning] PARTIAL BOOK {trade['id']} | "
            f"50% @ {ltp:.2f} (+{risk_pts:.1f} pts, 1R)"
        )
        self._db_update_metadata(trade["id"], metadata)

        # Move SL to breakeven — remaining 50% rides for free.
        # Only tighten: if Chandelier already pushed SL above entry, don't move it back.
        current_sl = trade.get("stop_loss", entry)
        if direction == "LONG" and current_sl < entry:
            trade["stop_loss"] = entry
            self._db_update_stop(trade["id"], entry)
            logger.info(f"[Learning] BREAKEVEN {trade['id']} | SL → ₹{entry:.2f} (partial booked)")
        elif direction == "SHORT" and current_sl > entry:
            trade["stop_loss"] = entry
            self._db_update_stop(trade["id"], entry)
            logger.info(f"[Learning] BREAKEVEN {trade['id']} | SL → ₹{entry:.2f} (partial booked)")

    def _check_signal_reversal(self, trade: dict, store) -> bool:
        """Return True when the condition that triggered entry has reversed.

        SimpleMomentum: EMA9/21 crossed back the other way.
        SimpleRSI: RSI returned past the 50 midpoint (bounce/reversion complete).
        """
        strategy  = trade.get("strategy", "")
        symbol    = trade["symbol"]
        direction = trade["direction"]
        try:
            if strategy == "SimpleMomentum":
                from analysis.indicators import ema
                df = store.get_ohlcv(symbol, "1H", n=30)
                if df is None or len(df) < 5:
                    return False
                e9   = ema(df["close"], 9)
                e21  = ema(df["close"], 21)
                diff = e9.iloc[-1] - e21.iloc[-1]
                return (direction == "LONG" and diff < 0) or (direction == "SHORT" and diff > 0)

            if strategy == "SimpleRSI":
                from analysis.indicators import rsi
                df = store.get_ohlcv(symbol, "15m", n=30)
                if df is None or len(df) < 5:
                    return False
                rsi_val = rsi(df["close"]).iloc[-1]
                # 50 is too close to the midline — RSI oscillates through it on every normal
                # candle. Use 55/45 to confirm momentum is genuinely lost, not just a wobble.
                return (direction == "LONG" and rsi_val > 55) or (direction == "SHORT" and rsi_val < 45)

        except Exception as e:
            logger.debug(f"[Learning] Signal reversal check error for {trade['id']}: {e}")
        return False

    def _check_bb_middle_exit(self, trade: dict, ltp: float, store) -> Optional[str]:
        """
        Exit when price crosses BACK through the BB middle after having crossed it first.

        At entry, oversold LONG is below BB mid and overbought SHORT is above BB mid.
        Firing immediately would exit every trade on the first cycle — so we guard with
        mfe_pts: the trade must have moved past the BB midline before the exit can fire.
        """
        if trade.get("strategy") != "SimpleRSI":
            return None
        direction = trade["direction"]
        entry     = trade["entry_price"]
        metadata  = trade.get("metadata") or {}
        try:
            from analysis.indicators import bollinger_bands
            df = store.get_ohlcv(trade["symbol"], "15m", n=30)
            if df is None or len(df) < 20:
                return None
            _, middle, _ = bollinger_bands(df["close"])
            bb_mid = middle.iloc[-1]

            # Guard: price must have crossed the BB midline from entry before we use this exit.
            # bb_middle stored at entry tells us how far price needed to travel.
            bb_mid_at_entry = float(metadata.get("bb_middle", 0))
            if bb_mid_at_entry > 0:
                required_mfe = abs(entry - bb_mid_at_entry)
                if trade.get("mfe_pts", 0) < required_mfe:
                    return None  # price never crossed BB midline — not a valid reversal

            if direction == "LONG" and ltp < bb_mid:
                return "BB_MID_CROSS"
            if direction == "SHORT" and ltp > bb_mid:
                return "BB_MID_CROSS"
        except Exception as e:
            logger.debug(f"[Learning] BB middle exit check error for {trade['id']}: {e}")
        return None

    def _check_volatility_exit(self, trade: dict, store) -> bool:
        """
        Exit if current ATR has dropped below 50% of entry ATR — momentum has dried up
        and the trade is unlikely to reach its target. Recycle capital elsewhere.
        Only applied to SimpleRSI trades (entry_atr stored in metadata).
        """
        if trade.get("strategy") != "SimpleRSI":
            return False
        metadata  = trade.get("metadata") or {}
        entry_atr = float(metadata.get("entry_atr") or 0)
        if entry_atr <= 0:
            return False
        try:
            from analysis.indicators import atr
            df = store.get_ohlcv(trade["symbol"], "15m", n=30)
            if df is None or len(df) < 15:
                return False
            current_atr = atr(df).iloc[-1]
            return current_atr < VOLATILITY_CONTRACTION * entry_atr
        except Exception as e:
            logger.debug(f"[Learning] Volatility exit check error for {trade['id']}: {e}")
        return False

    def _db_update_stop(self, trade_id: str, new_stop: float) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE learning_trades SET stop_loss=? WHERE id=?",
                    (new_stop, trade_id),
                )
        except Exception as e:
            logger.debug(f"[Learning] Could not persist trailing stop for {trade_id}: {e}")

    def _db_update_metadata(self, trade_id: str, metadata: dict) -> None:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE learning_trades SET metadata=? WHERE id=?",
                    (json.dumps(metadata), trade_id),
                )
        except Exception as e:
            logger.debug(f"[Learning] Could not persist metadata for {trade_id}: {e}")

    # ─────────────────────────────────────────────────────────────
    # COOLDOWN + EARNINGS VETO
    # ─────────────────────────────────────────────────────────────

    def _apply_cooldown(self, symbol: str, minutes: int) -> None:
        expires_at = datetime.now(tz=IST) + timedelta(minutes=minutes)
        self._cooldowns[symbol] = expires_at
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO learning_cooldowns (symbol, expires_at) VALUES (?, ?)",
                    (symbol, expires_at.isoformat()),
                )
        except Exception as e:
            logger.debug(f"[Learning] Could not persist cooldown for {symbol}: {e}")

    def _is_on_cooldown(self, symbol: str) -> bool:
        expiry = self._cooldowns.get(symbol)
        if not expiry:
            return False
        if datetime.now(tz=IST) < expiry:
            return True
        del self._cooldowns[symbol]
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("DELETE FROM learning_cooldowns WHERE symbol=?", (symbol,))
        except Exception:
            pass
        return False

    def _load_cooldowns(self) -> None:
        """Load non-expired cooldowns from DB on startup — survives restarts."""
        try:
            now_str = datetime.now(tz=IST).isoformat()
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT symbol, expires_at FROM learning_cooldowns WHERE expires_at > ?",
                    (now_str,),
                ).fetchall()
            for symbol, expires_str in rows:
                try:
                    expires_at = datetime.fromisoformat(expires_str)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=IST)
                    self._cooldowns[symbol] = expires_at
                except Exception:
                    pass
            if self._cooldowns:
                logger.info(
                    f"[Learning] Restored {len(self._cooldowns)} cooldown(s) from DB: "
                    f"{list(self._cooldowns.keys())}"
                )
        except Exception as e:
            logger.warning(f"[Learning] Could not load cooldowns from DB: {e}")

    def _is_earnings_blocked(self, symbol: str) -> bool:
        """Return True if symbol has earnings within 5 days (skip to avoid pre-results noise)."""
        try:
            from intelligence.fundamental_guard import fundamental_guard
            result = fundamental_guard.check(symbol)
            return result.veto
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────
    # DB
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_trades (
                    id           TEXT PRIMARY KEY,
                    symbol       TEXT,
                    strategy     TEXT,
                    direction    TEXT,
                    entry_price  REAL,
                    exit_price   REAL DEFAULT 0,
                    stop_loss    REAL,
                    target       REAL,
                    rr_planned   REAL,
                    pnl_pts      REAL DEFAULT 0,
                    pnl_r        REAL DEFAULT 0,
                    status       TEXT DEFAULT 'OPEN',
                    exit_reason  TEXT DEFAULT '',
                    entry_time   TEXT,
                    exit_time    TEXT DEFAULT '',
                    metadata     TEXT DEFAULT '{}',
                    mae_pts      REAL DEFAULT 0,
                    mfe_pts      REAL DEFAULT 0
                )
            """)
            # Safe migration for pre-existing tables
            for col, default in [("mae_pts", 0), ("mfe_pts", 0), ("fees", 0)]:
                try:
                    conn.execute(f"ALTER TABLE learning_trades ADD COLUMN {col} REAL DEFAULT {default}")
                except Exception:
                    pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_cooldowns (
                    symbol     TEXT PRIMARY KEY,
                    expires_at TEXT
                )
            """)
        logger.info("[Learning] DB tables ready")

    def _restore_open_positions(self) -> None:
        """Reload OPEN positions from DB into memory — prevents duplicates across restarts.
        Trades from a previous day are immediately marked STALE (they missed EOD close)."""
        today = datetime.now(tz=IST).date()

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM learning_trades WHERE status='OPEN'"
            ).fetchall()

        restored = 0
        stale    = 0
        for r in rows:
            trade = dict(r)
            try:
                trade["metadata"] = json.loads(trade.get("metadata") or "{}")
            except Exception:
                trade["metadata"] = {}
            trade["mae_pts"]  = trade.get("mae_pts") or 0.0
            trade["mfe_pts"]  = trade.get("mfe_pts") or 0.0
            trade["target_2"] = float(trade.get("metadata", {}).get("target_2") or 0)

            # Trades from a prior day missed their EOD close — mark STALE
            try:
                entry_date = datetime.fromisoformat(trade["entry_time"]).astimezone(IST).date()
            except Exception:
                entry_date = today

            if entry_date < today:
                hold_type = trade.get("metadata", {}).get("hold_type", "intraday")
                days_held = (today - entry_date).days
                # Swing trades within max hold window resume normally
                if hold_type == "swing" and days_held <= SWING_MAX_HOLD_DAYS:
                    self._open_positions[f"{trade['symbol']}:{trade['strategy']}"] = trade
                    restored += 1
                    logger.info(
                        f"[Learning] RESUME swing {trade['id']} | {trade['strategy']} "
                        f"{trade['symbol']} — day {days_held}/{SWING_MAX_HOLD_DAYS}"
                    )
                    continue
                self._db_close(
                    trade["id"], trade["entry_price"], "STALE",
                    0.0, 0.0, trade["mae_pts"], trade["mfe_pts"],
                )
                stale += 1
                logger.info(f"[Learning] STALE {trade['id']} | {trade['strategy']} {trade['symbol']} — missed EOD close on {entry_date}")
                try:
                    from paper_trading import paper_trading_engine
                    paper_trading_engine.mirror_learning_close(
                        trade["id"], trade["entry_price"], "STALE"
                    )
                except Exception:
                    pass
                continue

            self._open_positions[f"{trade['symbol']}:{trade['strategy']}"] = trade
            restored += 1

        if stale:
            logger.info(f"[Learning] Closed {stale} stale position(s) from previous day(s)")
        if restored:
            logger.info(f"[Learning] Restored {restored} open position(s) from DB")

    def _db_insert(self, trade: dict) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO learning_trades
                (id, symbol, strategy, direction, entry_price, stop_loss,
                 target, rr_planned, status, entry_time, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade["id"], trade["symbol"], trade["strategy"],
                trade["direction"], trade["entry_price"], trade["stop_loss"],
                trade["target"], trade["rr"], trade["status"],
                trade["entry_time"],
                json.dumps(trade.get("metadata", {})),
            ))

    def _db_close(
        self, trade_id: str, exit_price: float,
        exit_reason: str, pnl_pts: float, pnl_r: float,
        mae_pts: float = 0.0, mfe_pts: float = 0.0,
        fees: float = 0.0,
    ) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                UPDATE learning_trades
                SET exit_price=?, exit_reason=?, pnl_pts=?, pnl_r=?,
                    status='CLOSED', exit_time=?, mae_pts=?, mfe_pts=?, fees=?
                WHERE id=?
            """, (
                exit_price, exit_reason,
                round(pnl_pts, 2), round(pnl_r, 2),
                datetime.now(tz=IST).isoformat(),
                round(mae_pts, 2), round(mfe_pts, 2),
                round(fees, 2),
                trade_id,
            ))

    # ─────────────────────────────────────────────────────────────
    # READ API — used by dashboard
    # ─────────────────────────────────────────────────────────────

    def get_trades(self, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM learning_trades WHERE status=? ORDER BY entry_time DESC LIMIT ?",
                    (status.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM learning_trades ORDER BY entry_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            result.append(d)
        return result

    def get_stats(self) -> dict:
        """Win rate, avg R, best/worst trades, breakdown by strategy."""
        trades = self.get_trades(status="CLOSED", limit=1000)
        if not trades:
            return {"total_closed": 0, "message": "No closed learning trades yet."}

        wins   = [t for t in trades if t["pnl_r"] > 0]
        losses = [t for t in trades if t["pnl_r"] <= 0]

        by_strategy: dict[str, dict] = {}
        for t in trades:
            s = t["strategy"]
            if s not in by_strategy:
                by_strategy[s] = {"total": 0, "wins": 0, "total_r": 0.0, "total_fees": 0.0}
            by_strategy[s]["total"]      += 1
            by_strategy[s]["total_r"]    += t["pnl_r"]
            by_strategy[s]["total_fees"] += t.get("fees", 0.0) or 0.0
            if t["pnl_r"] > 0:
                by_strategy[s]["wins"] += 1

        for s, d in by_strategy.items():
            d["win_rate"]   = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0
            d["avg_r"]      = round(d["total_r"] / d["total"], 2) if d["total"] else 0
            d["total_fees"] = round(d["total_fees"], 2)

        all_r     = [t["pnl_r"] for t in trades]
        total_fees = round(sum(t.get("fees", 0.0) or 0.0 for t in trades), 2)
        return {
            "total_closed":  len(trades),
            "total_open":    len(self.get_trades(status="OPEN")),
            "win_rate_pct":  round(len(wins) / len(trades) * 100, 1),
            "avg_r":         round(sum(all_r) / len(all_r), 2),
            "total_r":       round(sum(all_r), 2),
            "best_trade_r":  round(max(all_r), 2),
            "worst_trade_r": round(min(all_r), 2),
            "total_fees":    total_fees,
            "by_strategy":   by_strategy,
            "exit_reasons":  _count_field(trades, "exit_reason"),
            "directions":    _count_field(trades, "direction"),
        }

    def get_review(self, strategy: Optional[str] = None) -> list[dict]:
        """Returns closed trades grouped by outcome bucket for review."""
        trades = self.get_trades(status="CLOSED", limit=500)
        if strategy:
            trades = [t for t in trades if t["strategy"] == strategy]

        def bucket(r):
            if r >= 2.0:   return "strong_win"
            if r > 0:      return "small_win"
            if r >= -0.5:  return "scratch"
            if r >= -1.0:  return "small_loss"
            return "large_loss"

        for t in trades:
            t["outcome_bucket"] = bucket(t["pnl_r"])
        return trades


def _count_field(trades: list[dict], field: str) -> dict:
    c: dict = {}
    for t in trades:
        v = t.get(field, "unknown")
        c[v] = c.get(v, 0) + 1
    return c


# Module-level singleton
learning_engine = LearningEngine()
