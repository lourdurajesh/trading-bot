"""
learning_engine.py
──────────────────
Runs the learning strategy bake-off (NSE equity + index options) through the
SAME shared pipeline production uses — Strategy.evaluate() -> Signal ->
RiskManager -> OrderManager -> PortfolioTracker -> PositionManager — just with
this book's own instances (segment "nse", book "LEARNING"): its own risk state
(kill-switch/daily-P&L never shared with LIVE), its own tracker, its own exit
engine. No separate entry path, no separate exit loop (see the trading-
architecture guardrail + memory `one-engine-never-compromise`).

What it does every cycle:
  1. Manages open positions via the shared PositionManager (STOP/target/
     structural-exit/Chandelier-trail/underlying-point-trail/EOD/DTE — the
     same decisions the live book gets, plus MAE/MFE instrumentation).
  2. Evaluates every enabled equity + index-options strategy on the learning
     watchlist and submits signals through RiskManager/OrderManager.
  3. Everything lands in the unified ledger's "nse" segment (learning_trades
     compat VIEW) for review.

Access results via:
  GET /learning/trades   — all trades (open + closed)
  GET /learning/stats    — win rate, avg R, top patterns
  GET /learning/review   — grouped by outcome with metadata
"""

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST    = ZoneInfo("Asia/Kolkata")
from config.settings import DB_PATH, TOTAL_CAPITAL, RISK_PER_TRADE_PCT  # single source for paths/sizing
from execution import fees as txn_fees  # single source for transaction costs (cost_model facade)
from config import strategy_toggles      # single source for per-strategy on/off (UI)

# Equity learning trades carry no lot size, so monetary P&L = pnl_pts × qty where qty is
# sized to a fixed risk-per-trade (₹). This makes ₹ P&L track R (pnl_pts×qty == pnl_r×risk),
# consistent with the lab's R metric and the system's fixed-fractional risk sizing. Used only
# as the read-API's last-resort qty estimate for legacy rows with no stored position_size.
LEARNING_RISK_RUPEES = float(os.getenv("LEARNING_RISK_RUPEES",
                                       str(TOTAL_CAPITAL * RISK_PER_TRADE_PCT / 100)))

# New-pipeline trades (since slice 6c) live in their OWN file, using the same
# schema PortfolioTracker/Position already write (segment "live" — the schema
# name, not the book identity; book="LEARNING" is what keeps this book's risk
# state/exits isolated). The OLD "nse" segment in trades.db has a genuinely
# different, learning-only schema (target/pnl_pts/pnl_r/metadata/mae_pts/mfe_pts/
# fees) that Position was never built to populate — writing new rows there left
# them with empty metadata and zero pnl_pts/pnl_r (verified via smoke test). A
# fresh isolated file sidesteps the schema mismatch entirely; get_trades() below
# unions historical "nse" rows with this file's so nothing already recorded
# disappears from the dashboard.
LEARNING_DB_PATH = os.path.join(os.path.dirname(DB_PATH) or "db", "learning.db")

logger = logging.getLogger(__name__)

# Swing strategies are NOT forced to close at EOD by the strategy's own design intent —
# used only to resolve Signal.hold_type when a strategy object doesn't expose the
# attribute directly (index-options strategies below). The shared PositionManager's
# own MAX_HOLDING_DAYS (20 calendar days) governs how long a swing position may run.
_SWING_STRATEGIES = {"TrendFollow"}


def _sanitize_for_json(obj):
    """
    Recursively replace NaN/Inf floats with 0.0 throughout a dict/list tree.

    A shallow loop on the top-level trade dict misses floats nested inside the
    'metadata' dict (e.g. rvol=nan, atr=inf from bad calculations).  orjson —
    FastAPI's default serialiser — rejects NaN/Inf and raises a TypeError that
    propagates as HTTP 500 because it happens *after* the endpoint returns, i.e.
    outside the endpoint's try/except block.
    """
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(i) for i in obj]
    return obj


class LearningEngine:

    def __init__(self):
        from risk.risk_manager import RiskManager
        from risk.portfolio_tracker import PortfolioTracker
        from execution.position_manager import PositionManager
        from data.data_store import store

        self._cooldowns: dict[str, datetime] = {}

        # This book's own risk state, tracker and exit engine — never the LIVE
        # globals (per-book instantiable since slices 0b/1b/6a/6c-1/6c-2).
        # segment="live": a fresh, isolated file using the SAME schema Position/
        # PortfolioTracker already write (see LEARNING_DB_PATH's comment above
        # for why the legacy "nse" segment in trades.db can't be reused here).
        # ledger_ MUST be an explicit Ledger(LEARNING_DB_PATH) instance --
        # PortfolioTracker defaults self._ledger to the GLOBAL ledger (bound to
        # the main trades.db) whenever ledger_ is omitted, even if db_path is
        # set; db_path alone only affects this tracker's own raw-sqlite helpers,
        # not where _save_position/_load_open_positions actually read/write.
        # Passing db_path without ledger_ would have written every new learning
        # trade into the LIVE segment of trades.db -- the production book's
        # own data.
        from execution.ledger import Ledger
        self._nse_risk_manager = RiskManager(book="LEARNING")
        self._nse_tracker = PortfolioTracker(
            ledger_=Ledger(LEARNING_DB_PATH), db_path=LEARNING_DB_PATH,
            segment="live", book="LEARNING", risk_manager_=self._nse_risk_manager,
        )
        self._nse_position_manager = PositionManager(
            tracker=self._nse_tracker, store_=store, book="LEARNING",
            on_close=self._apply_cooldown,
        )

        self._init_db()
        self._load_cooldowns()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC — called from main loop
    # ─────────────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """Evaluate all strategies and submit entries. Exits run separately, on
        the orchestrator's fast-monitor tick (NSELearningAdapter.fast_monitor →
        self._nse_position_manager.check_all()) — same cadence the production
        book gets, not tied to this 60s generation cycle."""
        from config.learning_watchlist import (
            LEARNING_NSE_EQUITIES, LEARNING_NSE_INDICES,
        )
        from strategies.trend_follow    import TrendFollowStrategy
        from strategies.mean_reversion  import MeanReversionStrategy
        from strategies.simple_momentum import SimpleMomentumStrategy
        from strategies.simple_rsi      import SimpleRSIStrategy

        equity_strategies = [
            TrendFollowStrategy(),
            MeanReversionStrategy(),
            SimpleMomentumStrategy(),
            SimpleRSIStrategy(),
        ]

        # ── 1. Equity entries — 1 trade per symbol per strategy ──
        # MCX commodities are NOT traded here — they trade exclusively as
        # options via commodity_options_learning.py (the MCX tab).
        for symbol in LEARNING_NSE_EQUITIES:
            if "-INDEX" in symbol:
                continue  # safety: indices must never go through equity strategies
            if self._is_on_cooldown(symbol):
                continue
            if self._is_earnings_blocked(symbol):
                continue

            for strat in equity_strategies:
                if not strategy_toggles.is_enabled(strat.name):
                    continue  # disabled from the dashboard (single source)
                if self._has_open(symbol, strat.name):
                    continue  # already in this strategy's trade for this symbol
                try:
                    signal = strat.evaluate(symbol)
                except Exception as exc:
                    logger.debug(f"[Learning] {strat.name}/{symbol} error: {exc}")
                    continue
                if signal and signal.is_valid():
                    self._submit(signal, strat.name, strat)
                elif signal:
                    logger.warning(f"[Learning] {strat.name}/{symbol}: signal failed is_valid() template check — skipping")

        # ── 2. Index options entries ──────────────────────────────
        self._run_index_options_learning(LEARNING_NSE_INDICES)

    def _run_index_options_learning(self, index_symbols: list) -> None:
        """
        Evaluate DirectionalOptions / InstitutionalMomentum / Reversal5m(+3m) on
        NIFTY/BANKNIFTY/FINNIFTY. Entries route through the same RiskManager/
        OrderManager as equity; exits are the shared PositionManager's options
        path (premium STOP/TARGET, or the underlying point-trail for strategies
        that opt into options_meta["exit_mode"]="underlying_trail").
        """
        try:
            from strategies.directional_options    import DirectionalOptionsStrategy
            from strategies.institutional_momentum import InstitutionalMomentumStrategy
            from strategies.reversal_5m            import Reversal5mStrategy
            index_strategies = [
                DirectionalOptionsStrategy(),
                InstitutionalMomentumStrategy(),
                Reversal5mStrategy(),    # 5m red→green reclaim, ATM call, per-index exit
                # 3m NIFTY/FINNIFTY trailing variant — A/B against the 5m version.
                Reversal5mStrategy(
                    timeframe="3m", name="Reversal3m",
                    allowed={"NSE:NIFTY50-INDEX", "NSE:FINNIFTY-INDEX"},
                    force_exit_mode="underlying_trail",
                ),
            ]
            for symbol in index_symbols:
                if self._is_on_cooldown(symbol):
                    continue
                for strat in index_strategies:
                    if not strategy_toggles.is_enabled(strat.name):
                        continue  # disabled from the dashboard (single source)
                    strat_name = f"{strat.name}_LRN"
                    if self._has_open(symbol, strat_name):
                        continue
                    try:
                        sig = strat.evaluate(symbol)
                    except Exception as exc:
                        logger.debug(f"[Learning/IndexOptions] {strat.name}/{symbol}: {exc}")
                        continue
                    if sig and sig.is_valid():
                        self._submit(sig, strat_name, strat)
                    elif sig:
                        logger.warning(f"[Learning/IndexOptions] {strat.name}/{symbol}: signal failed is_valid() template check — skipping")
        except Exception as e:
            logger.debug(f"[Learning/IndexOptions] setup error: {e}")

    # ─────────────────────────────────────────────────────────────
    # TRADE ENTRY — the ONE shared pipeline (RiskManager -> OrderManager -> tracker)
    # ─────────────────────────────────────────────────────────────

    def _has_open(self, symbol: str, strategy: str) -> bool:
        """Whether THIS strategy already holds a position on this symbol. Learning
        allows several strategies on the same symbol at once (the bake-off) —
        risk_manager's duplicate-symbol check is skipped for this book precisely
        so this per-(symbol,strategy) check is the only dedup that applies."""
        return any(p["symbol"] == symbol and p["strategy"] == strategy
                   for p in self._nse_tracker.get_open_positions())

    def _resolve_hold_type(self, strategy_name: str, strat=None) -> str:
        """Read hold_type from the strategy object itself when it declares one;
        otherwise fall back to the swing-strategy allowlist."""
        if strat is not None and hasattr(strat, "hold_type"):
            return strat.hold_type
        base_name = strategy_name.replace("_LRN", "")
        return "swing" if base_name in _SWING_STRATEGIES else "intraday"

    def _submit(self, sig, strategy_name: str, strat=None) -> None:
        """Route a learning Signal through the SAME shared pipeline production
        uses: this book's own RiskManager (sizing + risk gates, own kill-switch)
        -> OrderManager (record-only, LEARNING context) -> PortfolioTracker.
        No separate learning entry path, no separate sizing."""
        sig.strategy  = strategy_name   # preserve the _LRN-suffixed name for reporting
        sig.hold_type = self._resolve_hold_type(strategy_name, strat)

        from execution.order_manager import order_manager
        from execution.run_context import learning_context
        order_manager.submit(
            sig, ctx=learning_context(),
            tracker=self._nse_tracker, risk_manager_=self._nse_risk_manager,
        )

    # ─────────────────────────────────────────────────────────────
    # MANUAL CLOSE (dashboard)
    # ─────────────────────────────────────────────────────────────

    def manual_close(self, trade_id: str, reason: str = "MANUAL") -> bool:
        """Close an OPEN learning trade now (dashboard manual close). Trade-id
        based — a symbol can have several open positions at once (the bake-off),
        so a symbol-keyed close would be ambiguous."""
        return self._nse_tracker.force_close_by_id(trade_id, reason)

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
        # Trades live in the unified ledger (PortfolioTracker.__init__ already
        # calls ledger.init()); only the cooldowns table is learning-specific.
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_cooldowns (
                    symbol     TEXT PRIMARY KEY,
                    expires_at TEXT
                )
            """)
        logger.info("[Learning] DB tables ready")

    # ─────────────────────────────────────────────────────────────
    # READ API — used by dashboard
    # ─────────────────────────────────────────────────────────────

    def _normalize_new_row(self, row: dict) -> dict:
        """Translate a new-pipeline row (the shared PortfolioTracker/Position schema,
        from LEARNING_DB_PATH) into the OLD learning_trades output shape (target/
        pnl_pts/pnl_r/metadata/mae_pts/mfe_pts/fees) so historical and new-pipeline
        trades render identically through the rest of this read API."""
        options_meta = row.get("options_meta") or {}
        if isinstance(options_meta, str):
            # Ledger.get_rows() parses the outer payload once; options_meta is
            # stored double-encoded (json.dumps'd into a string value inside
            # that payload by _save_position), so it's still a raw string here.
            try:
                options_meta = json.loads(options_meta)
            except Exception:
                options_meta = {}
        if not isinstance(options_meta, dict):
            options_meta = {}
        meta = dict(options_meta)
        is_options = row.get("signal_type") == "OPTIONS"
        if is_options:
            meta.setdefault("instrument_type", "nse_options")
        meta.setdefault("hold_type", row.get("hold_type") or "intraday")

        entry     = float(row.get("entry_price") or 0)
        exitp     = float(row.get("exit_price") or 0)
        direction = row.get("direction", "LONG")
        status    = row.get("status")

        pnl_pts = pnl_r = 0.0
        fees    = 0.0
        if status == "CLOSED" and entry > 0 and exitp > 0:
            pts  = (exitp - entry) if (is_options or direction == "LONG") else (entry - exitp)
            risk = abs(entry - float(row.get("original_stop_loss") or row.get("stop_loss") or 0))
            pnl_pts = round(pts, 2)
            pnl_r   = round(pts / risk, 2) if risk > 0 else 0.0
            qty     = int(row.get("position_size") or 0)
            realised = row.get("realised_pnl")
            if qty and realised is not None:
                fees = round(pts * qty - float(realised), 2)

        return {
            "id":            row.get("id"),
            "symbol":        row.get("symbol"),
            "strategy":      row.get("strategy"),
            "direction":     direction,
            "entry_price":   entry,
            "exit_price":    exitp,
            "stop_loss":     row.get("stop_loss"),
            "target":        row.get("target_1"),
            "rr_planned":    None,
            "pnl_pts":       pnl_pts,
            "pnl_r":         pnl_r,
            "status":        status,
            "exit_reason":   row.get("exit_reason"),
            "entry_time":    row.get("entry_time"),
            "exit_time":     row.get("exit_time"),
            "metadata":      json.dumps(meta),
            "mae_pts":       float(row.get("mae_pts") or 0),
            "mfe_pts":       float(row.get("mfe_pts") or 0),
            "fees":          fees,
            "position_size": row.get("position_size"),   # real qty — _real_qty() checks this first
        }

    def get_trades(self, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        # Historical rows (pre-slice-6c, legacy "nse" segment schema).
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                legacy_rows = conn.execute(
                    "SELECT * FROM learning_trades WHERE status=? ORDER BY entry_time DESC LIMIT ?",
                    (status.upper(), limit),
                ).fetchall()
            else:
                legacy_rows = conn.execute(
                    "SELECT * FROM learning_trades ORDER BY entry_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        rows = [dict(r) for r in legacy_rows]

        # New-pipeline rows (since slice 6c, isolated file + shared schema) —
        # normalized to the same shape, then merged and re-sorted.
        try:
            new_rows = self._nse_tracker._ledger.get_rows("live", status=status, limit=limit)
            rows += [self._normalize_new_row(r) for r in new_rows]
        except Exception as e:
            logger.debug(f"[Learning] Could not read new-pipeline trades: {e}")
        rows.sort(key=lambda d: d.get("entry_time") or "", reverse=True)
        rows = rows[:limit]

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            # Monetary P&L = (per-unit move) × ACTUAL executed qty − fees, for both
            # options (lots × lot_size) and equity (shares). The qty is the real order
            # size — same number the Paper Positions widget and live portfolio use,
            # never a risk-amount proxy.
            _pts = float(d.get("pnl_pts") or 0)
            fees = float(d.get("fees") or 0)
            qty  = self._real_qty(d)
            d["qty"]     = qty
            d["pnl_inr"] = round(_pts * qty - fees, 2) if qty else None
            # Live mark-to-market for OPEN trades so the dashboard summary reflects
            # unrealized P&L instead of only updating when a trade closes.
            if d.get("status") == "OPEN":
                u = self._unrealized(d)
                if u:
                    d["live_ltp"]       = u["ltp"]
                    d["unrealized_r"]   = u["pnl_r"]
                    d["unrealized_inr"] = u["pnl_inr"]
            # SQLite can store NaN/Inf from edge-case calculations; JSON cannot.
            # Apply recursively so nested metadata values (e.g. rvol=nan) are
            # also sanitised — a shallow loop misses dict/list nesting and
            # causes orjson / FastAPI to throw a 500 Internal Server Error.
            d = _sanitize_for_json(d)
            result.append(d)
        return result

    def _paper_position_size(self, trade_id: str) -> int:
        """Read the REAL executed share qty from the mirrored paper trade
        (PAPER-LRN-<id>), for legacy trades that predate risk_manager-based
        sizing (position_size stored on the row itself, see _real_qty)."""
        try:
            paper_id = f"PAPER-LRN-{trade_id[-8:]}"
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT position_size FROM paper_trades WHERE id=?", (paper_id,)
                ).fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception:
            return 0

    def _real_qty(self, trade: dict) -> int:
        """The actual executed quantity P&L is computed against. Priority:
          1. row's own position_size — set by RiskManager/PortfolioTracker.open_position()
             for every trade going through the shared pipeline (the real order size).
          2. metadata["position_size"] / paper_trades lookup — legacy trades from
             before this book was routed through the shared pipeline.
          3. fallback — options: lot_size (1 lot); equity: LEARNING_RISK_RUPEES / risk_pts
        """
        qty = int(trade.get("position_size") or 0)
        if qty:
            return qty
        meta = trade.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        qty = int(meta.get("position_size") or 0)
        if qty:
            return qty
        qty = self._paper_position_size(str(trade.get("id") or ""))
        if qty:
            return qty
        if meta.get("instrument_type") == "nse_options":
            # Legacy option trade with no stored size — fall back to one lot.
            return int(meta.get("lot_size") or 1) or 1
        # Equity last resort: size to fixed per-trade risk on the ORIGINAL stop.
        _pts = float(trade.get("pnl_pts") or 0)
        _r   = float(trade.get("pnl_r") or 0)
        rps = float(meta.get("risk_pts") or 0)
        if not rps and _pts != 0 and _r != 0:
            rps = abs(_pts / _r)
        if not rps:
            rps = abs(float(trade.get("entry_price") or 0) - float(trade.get("stop_loss") or 0))
        return round(LEARNING_RISK_RUPEES / rps) if rps > 0 else 0

    def _unrealized(self, trade: dict) -> Optional[dict]:
        """
        Mark an OPEN trade to market using the live LTP (option contract or
        equity symbol). Returns {ltp, pnl_pts, pnl_r, pnl_inr} or None when no
        live price is available.
        """
        try:
            from data.data_store import store
            meta  = trade.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            entry = float(trade.get("entry_price") or 0)
            stop  = float(trade.get("stop_loss") or 0)
            if entry <= 0:
                return None

            if meta.get("instrument_type") == "nse_options":
                # Display price: live LTP, else last candle close (survives market close / restart)
                ltp = store.get_last_price(meta.get("nfo_symbol"))
                if not ltp or ltp <= 0:
                    return None
                risk    = entry - stop          # long premium: stop < entry
                pnl_pts = ltp - entry           # premium move per unit
                # ₹ P&L uses the ACTUAL executed qty (lots × lot_size), same as Paper.
                # Open position: only the entry-leg cost has been incurred (single source).
                qty     = self._real_qty(trade)
                fee     = txn_fees.open_leg("OPTIONS", entry, qty)
                pnl_inr = round(pnl_pts * qty - fee, 2) if qty else None
            else:
                ltp = store.get_last_price(trade.get("symbol"))
                if not ltp or ltp <= 0:
                    return None
                if trade.get("direction") == "LONG":
                    pnl_pts = ltp - entry
                else:
                    pnl_pts = entry - ltp
                # ₹ P&L uses the ACTUAL executed share qty (same as Paper widget);
                # R is still risk-based, so keep risk from the original stop distance.
                # Open position: only the entry-leg cost has been incurred (single source).
                risk = float(meta.get("risk_pts") or 0) or abs(entry - stop)
                qty     = self._real_qty(trade)
                fee     = txn_fees.open_leg("EQUITY", entry, qty)
                pnl_inr = round(pnl_pts * qty - fee, 2) if qty else None

            pnl_r = round(pnl_pts / risk, 2) if risk and risk > 0 else 0.0
            return {"ltp": round(ltp, 2), "pnl_pts": round(pnl_pts, 2),
                    "pnl_r": pnl_r, "pnl_inr": pnl_inr}
        except Exception as exc:
            logger.debug(f"[Learning] _unrealized error for {trade.get('id')}: {exc}")
            return None

    def _open_summary(self) -> dict:
        """Live unrealized P&L across all OPEN learning trades (marked to market)."""
        open_trades = self.get_trades(status="OPEN", limit=500)
        marked = 0
        unreal_r = 0.0
        unreal_inr = 0.0
        for t in open_trades:
            if t.get("unrealized_r") is not None:
                marked += 1
                unreal_r += float(t.get("unrealized_r") or 0)
                unreal_inr += float(t.get("unrealized_inr") or 0)
        return {
            "count":          len(open_trades),
            "marked":         marked,        # how many had a live price this cycle
            "unrealized_r":   round(unreal_r, 2),
            "unrealized_inr": round(unreal_inr, 2),
        }

    def get_stats(self) -> dict:
        """Win rate, avg R, best/worst trades, breakdown by strategy + live open P&L."""
        open_block = self._open_summary()
        trades = self.get_trades(status="CLOSED", limit=1000)
        if not trades:
            return {"total_closed": 0, "open": open_block,
                    "message": "No closed learning trades yet."}

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
            "total_open":    open_block["count"],
            "open":          open_block,
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
