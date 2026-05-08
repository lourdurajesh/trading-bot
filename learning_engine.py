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
_SWING_STRATEGIES = {"TrendFollow", "MeanReversion"}
SWING_MAX_HOLD_DAYS = 5


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
            LEARNING_NSE_EQUITIES, LEARNING_NSE_INDICES, LEARNING_MCX_COMMODITIES,
        )
        from strategies.trend_follow    import TrendFollowStrategy
        from strategies.mean_reversion  import MeanReversionStrategy
        from strategies.simple_momentum import SimpleMomentumStrategy
        from strategies.simple_rsi      import SimpleRSIStrategy
        from data.data_store            import store

        equity_strategies = [
            TrendFollowStrategy(),
            MeanReversionStrategy(),
            SimpleMomentumStrategy(),
            SimpleRSIStrategy(),
        ]

        # ── 1. Monitor existing open positions ───────────────────
        self._check_exits(store)

        # ── 2. Equity + commodity entries — 1 trade per symbol per strategy ──
        for symbol in LEARNING_NSE_EQUITIES + LEARNING_MCX_COMMODITIES:
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
                    self._open_trade(self._sig_to_learning_dict(signal, strat.name))

        # ── 3. Index options entries ──────────────────────────────
        self._run_index_options_learning(LEARNING_NSE_INDICES)

    def _sig_to_learning_dict(self, sig, strategy_name: str) -> dict:
        """Convert a Signal object or legacy dict to the learning trade dict format."""
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
                if not ltp or ltp <= 0:
                    continue  # chain data unavailable this cycle — wait for next
            else:
                ltp = store.get_ltp(symbol)
            if not ltp:
                continue

            direction   = trade["direction"]
            stop        = trade["stop_loss"]
            target      = trade["target"]
            entry       = trade["entry_price"]
            hold_type   = metadata.get("hold_type", "intraday")
            exit_reason = None
            exit_price  = None

            # MAE/MFE — options always "long" the contract
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

            # Stop / target exits
            if instrument_type == "nse_options":
                if ltp <= stop:
                    exit_reason, exit_price = "STOP",   ltp
                elif ltp >= target:
                    exit_reason, exit_price = "TARGET", ltp
            elif direction == "LONG":
                if ltp <= stop:
                    exit_reason, exit_price = "STOP",   ltp
                elif ltp >= target:
                    exit_reason, exit_price = "TARGET", ltp
            else:
                if ltp >= stop:
                    exit_reason, exit_price = "STOP",   ltp
                elif ltp <= target:
                    exit_reason, exit_price = "TARGET", ltp

            # Gap 1: EOD close only for intraday strategies
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
                    # pnl_pts is in price points; fees stored separately (no qty to convert)
                    fees    = FEES_EQUITY_FLAT
                    pnl_pts = round((eff_exit - entry) if direction == "LONG" else (entry - eff_exit), 2)
                    pnl_r   = round(pnl_pts / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0

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

                win_exits = {"TARGET", "TARGET1", "TARGET2"}
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
            return not result.allowed
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
            trade["mae_pts"] = trade.get("mae_pts") or 0.0
            trade["mfe_pts"] = trade.get("mfe_pts") or 0.0

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
