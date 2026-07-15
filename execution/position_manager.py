"""
position_manager.py
───────────────────
Active position exit management — runs on every tick.
This is the most critical missing piece for autonomous trading.

Rules enforced:
  1. Stop loss hit        → exit immediately at market
  2. Target 1 hit         → exit 50%, move SL to breakeven
  3. Target 2 hit         → exit remaining 50% (skipped if dynamic target active)
  4. Trailing stop        → after 1R profit, trail by 1×ATR
  5. Breakeven move       → after T1 hit, SL moves to entry price
  6. EOD forced exit      → close all intraday positions at 3:25 PM IST (configurable via EOD_EXIT_TIME env)
  7. Max holding period   → force exit after 20 trading days
  8. Catastrophic gap     → if price gaps past SL by >3%, exit immediately
  9. Dynamic target       → after T1 hit, target extends by 1R at each milestone;
                            trailing stop becomes the sole exit mechanism

Called by main.py fast loop every 5 seconds.
"""

import logging
import math
import threading
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import OPTIONS_DTE_FORCE_EXIT, PAPER_TRADING, EOD_EXIT_TIME   # Bug 16: consistent source
from data.data_store import store
from risk.portfolio_tracker import portfolio_tracker, Position
from notifications.alert_service import alert_service
from execution.evaluator import Evaluator  # shared loop engine (Phase 5 — exit side)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Exit rules configuration — EOD_EXIT_TIME now sourced from config.settings (env: EOD_EXIT_TIME)
MAX_HOLDING_DAYS   = 20              # force exit after this many calendar days
BREAKEVEN_TRIGGER       = 1.0   # move SL to BE after 1R profit
TRAIL_TRIGGER           = 1.5   # start trailing after 1.5R profit
PARTIAL_EXIT_PCT        = 0.5   # exit 50% at T1
DYNAMIC_TARGET_START_R  = 3.0   # first dynamic milestone after T1 (~2R)
DYNAMIC_TARGET_STEP     = 1.0   # advance target by this many R per milestone
CHANDELIER_MULT         = 2.5   # ATR multiplier for the volatility-adaptive trail (TREND_TRAIL style)
CHANDELIER_TF           = "15m" # timeframe ATR is computed on

# Shadow-mode candidates for exit_signals.json's arm_profit_pct (2026-07 backtest: flat
# 0.15% underperformed both an ATR-relative arm AND doing nothing at all, on LONG; SHORT
# showed no such edge in the same window, likely a mild-uptrend regime effect). Logs ONLY
# — never changes real exit behavior. See _shadow_structural_exit_check.
SHADOW_ATR_MULTS        = (0.75, 1.0)




class PositionManager(Evaluator):
    """
    The shared ExitEvaluator (Phase 5 — exit side). Monitors all open positions on every
    tick and manages exits. Runs on the one Evaluator base: scope() = this book's open
    positions, evaluate() = the per-position exit check. Per-book instantiable (LIVE /
    LEARNING) so one exit engine serves every book.

    Usage:
        position_manager.check_all()   # called by fast loop every 5s
    """

    def __init__(self, tracker=None, store_=None, book: str = "PAPER", on_close=None):
        super().__init__(f"PositionManager[{book}]")
        # Per-book instantiable (library-safe). The portfolio engine uses the default
        # globals; a second runtime (the forward-test harness, slice 6) injects its own
        # tracker/store/book so one exit engine serves multiple books without globals.
        # NOTE: internal exit state is symbol-keyed today (correct for the portfolio book —
        # one position/symbol). The symbol→trade-id re-key lands with slice 6, paired with
        # the harness that actually needs multiple positions per symbol + its test.
        # on_close(symbol, minutes): optional cooldown callback for a second book — without
        # it, _apply_exit_cooldown falls back to the production strategy_selector's cooldown
        # store, which a non-LIVE book must NEVER write to (a learning trade closing must not
        # block production from re-entering that symbol).
        self._lock              = threading.Lock()
        self._book              = book
        self._on_close          = on_close
        self._tracker           = tracker if tracker is not None else portfolio_tracker
        self._store             = store_  if store_  is not None else store
        self._breakeven_applied: set[str] = set()   # symbols where SL moved to BE
        self._partial_exited:    set[str] = set()   # symbols where 50% already exited
        self._trailing_stops:    dict[str, float] = {}  # symbol → current trail SL
        self._dynamic_target_r:  dict[str, float] = {}  # symbol → next R-milestone target
        self._chandelier_atr:    dict[str, float] = {}  # pid → ATR captured near entry (TREND_TRAIL)
        self._chandelier_peak:   dict[str, float] = {}  # pid → running peak/trough for the chandelier calc
        self._mae_mfe:           dict[str, tuple] = {}  # pid → (max_adverse_pts, max_favourable_pts)
        self._shadow_logged:     set[tuple] = set()  # (pid, k) already logged — fire once per position

    def check_all(self) -> None:
        """Check all open positions against current prices (called every 5s from the
        fast loop). Delegates to the shared Evaluator loop; the hooks below are this
        book's exit scope + per-position check."""
        self.evaluate_once(datetime.now(tz=IST))   # always IST regardless of server tz

    # ── Evaluator hooks (exit side) ──────────────────────────────
    def scope(self, now):
        return self._tracker.get_open_positions()

    def evaluate(self, pos_dict, now):
        symbol = pos_dict.get("symbol", "")
        try:
            self._check_position(pos_dict, now)   # runs SL/target/trail/structural/EOD inline
        except Exception as e:
            logger.error(f"[PositionManager] Error checking {symbol}: {e}")
        return ()   # exits happen inside _check_position; no signal handed to on_signal

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — per-position check
    # ─────────────────────────────────────────────────────────────

    def _check_position(self, pos_dict: dict, now: datetime) -> None:
        symbol      = pos_dict.get("symbol", "")
        direction   = pos_dict.get("direction", "LONG")
        entry       = float(pos_dict.get("entry_price", 0))
        stop        = float(pos_dict.get("stop_loss", 0))
        target_1    = float(pos_dict.get("target_1", 0))
        target_2    = float(pos_dict.get("target_2", 0))
        entry_time  = pos_dict.get("entry_time", "")
        signal_type = pos_dict.get("signal_type", "EQUITY")
        options_meta = pos_dict.get("options_meta") or {}

        # ── OPTIONS positions — separate exit management ──────────
        if signal_type == "OPTIONS":
            pos = self._tracker.get_position_by_id(pos_dict.get("id")) or self._tracker.get_position(symbol)
            opt_size = pos.position_size if pos else 0
            opt_pid  = pos.id if pos else (pos_dict.get("id") or "")
            self._check_options_position(
                symbol, opt_pid, direction, entry, stop, target_1,
                opt_size, options_meta, now, pos_dict,
            )
            return

        ltp = self._store.get_ltp(symbol)
        if not ltp or ltp <= 0:
            return

        # The SPECIFIC position by trade-id (a book may hold several positions per symbol —
        # the learning bake-off). Exit state + tracker mutations are keyed by pos.id; market
        # data / orders / logging use pos.symbol. For the live book (one position/symbol)
        # id↔symbol is 1:1 → behaviour unchanged.
        pos = self._tracker.get_position_by_id(pos_dict.get("id")) or self._tracker.get_position(symbol)
        if not pos or pos.position_size <= 0:
            return
        pid = pos.id
        remaining_size = pos.position_size

        # Strategy-aware exit policy — the SAME shared source the learning engine uses
        # (execution/exit_policy + config). Mean-reversion takes a hard full exit at the
        # target; trend/momentum keeps partial + trail + T2 (+ the Chandelier ATR
        # ratchet below, for TREND_TRAIL specifically).
        from execution.exit_policy import exit_style, MEAN_REVERSION, TREND_TRAIL
        style        = exit_style(pos.strategy)
        is_mean_rev  = (style == MEAN_REVERSION)
        is_trend_trail = (style == TREND_TRAIL)

        # Trade-quality instrumentation (not an exit decision) — every book, every strategy.
        self._update_mae_mfe(pid, direction, entry, ltp)

        with self._lock:
            already_partial = pid in self._partial_exited
            already_be      = pid in self._breakeven_applied

        # ── Reconstruct in-memory state after a service restart ───
        # If none of our tracking dicts know about this position yet, infer from
        # the persisted stop_loss: if SL has already been moved to/past breakeven
        # it means T1 was hit in a previous session.
        with self._lock:
            untracked = (pid not in self._partial_exited
                         and pid not in self._breakeven_applied
                         and pid not in self._trailing_stops)
        if untracked:
            self._reconstruct_state_from_position(pos, direction, entry, stop, ltp)
            with self._lock:
                already_partial = pid in self._partial_exited
                already_be      = pid in self._breakeven_applied

        # Use trailing stop if set, else original stop
        effective_stop = self._trailing_stops.get(pid, stop)

        # ── 1. EOD forced exit (3:15 PM IST) ─────────────────────
        if now.time() >= EOD_EXIT_TIME:
            if pos.hold_type == "swing":
                # Swing trades hold overnight — skip EOD close, keep monitoring stops/targets.
                # Broker product is CNC so no auto-squareoff from exchange side either.
                return
            logger.info(f"[PositionManager] EOD exit: {symbol} × {remaining_size}")
            self._exit_position(pos, remaining_size, "EOD_FORCED", ltp)
            return

        # ── 2. Max holding period ─────────────────────────────────
        if entry_time:
            try:
                entry_dt  = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                days_held = (datetime.now(tz=IST) - entry_dt).days
                if days_held >= MAX_HOLDING_DAYS:
                    logger.info(f"[PositionManager] Max hold {days_held}d: {symbol}")
                    self._exit_position(pos, remaining_size, "MAX_HOLD", ltp)
                    return
            except Exception:
                pass

        # ── 3. LONG position management ───────────────────────────
        if direction == "LONG":
            # Use original_stop_loss for risk so trailing continues after breakeven move;
            # entry - stop = 0 once SL is at breakeven, which would freeze trailing (Bug 15).
            original_sl = (pos.original_stop_loss if pos.original_stop_loss else stop)
            risk = entry - original_sl
            if risk <= 0:
                return

            profit_r = (ltp - entry) / risk

            # Stop loss hit
            if ltp <= effective_stop:
                logger.info(f"[PositionManager] STOP HIT {symbol}: "
                            f"ltp={ltp:.2f} <= sl={effective_stop:.2f}")
                self._exit_position(pos, remaining_size, "STOP", ltp)
                return

            # Structural exit — close an in-profit trade reversing/stalling BEFORE it
            # round-trips to the stop (shared single source; same as the learning engine).
            sx = self._structural_exit_reason(symbol, direction, entry)
            self._shadow_structural_exit_check(symbol, direction, entry, symbol, pid, sx)
            if sx:
                logger.info(f"[PositionManager] STRUCTURAL EXIT {symbol}: {sx} @ {ltp:.2f}")
                self._exit_position(pos, remaining_size, sx, ltp)
                return

            # Mean-reversion: hard FULL exit at the target — no partial/trail (the
            # snap-back reverts). Same policy as the learning engine (shared exit_policy).
            if is_mean_rev:
                if target_1 > 0 and ltp >= target_1:
                    logger.info(f"[PositionManager] MEAN-REV TARGET {symbol}: "
                                f"ltp={ltp:.2f} >= t1={target_1:.2f} — full exit")
                    self._exit_position(pos, remaining_size, "TARGET", ltp)
                return

            # Dynamic target — advance milestone after T1 is hit
            if already_partial:
                self._update_dynamic_target(pid, symbol, profit_r, entry, risk, direction)

            # Target 2 hit — skipped when dynamic target is active (trailing stop exits instead)
            if target_2 > 0 and ltp >= target_2 and already_partial:
                if pid not in self._dynamic_target_r:
                    logger.info(f"[PositionManager] T2 HIT {symbol}: "
                                f"ltp={ltp:.2f} >= t2={target_2:.2f} — "
                                f"exiting remaining {remaining_size} shares")
                    self._exit_position(pos, remaining_size, "TARGET2", ltp)
                    return

            # Target 1 hit — partial exit + move SL to breakeven + activate dynamic target
            if target_1 > 0 and ltp >= target_1 and not already_partial:
                partial_size = max(1, int(remaining_size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT {symbol}: "
                            f"ltp={ltp:.2f} >= t1={target_1:.2f} — "
                            f"exiting {partial_size} shares")
                self._partial_exit(pos, partial_size, "TARGET1", ltp)
                self._tracker.mark_t1_hit(pid)   # persist so restart knows T1 fired
                with self._lock:
                    self._partial_exited.add(pid)
                    self._dynamic_target_r[pid] = DYNAMIC_TARGET_START_R
                if not already_be:
                    self._move_stop_to_breakeven(pid, symbol, entry)
                    with self._lock:
                        self._breakeven_applied.add(pid)
                return

            # Trailing stop — after 1.5R profit
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(pid, symbol, ltp, direction, risk)

            # Breakeven move — after 1R profit (if T1 not yet hit)
            elif profit_r >= BREAKEVEN_TRIGGER and not already_be:
                self._move_stop_to_breakeven(pid, symbol, entry)
                with self._lock:
                    self._breakeven_applied.add(pid)

            # Chandelier ATR trail — active from entry (no profit-R gate), only
            # for TREND_TRAIL-styled strategies. Tightens the stop above; never
            # overrides a tighter one already set.
            if is_trend_trail:
                self._chandelier_ratchet(pid, symbol, direction, ltp)

        # ── 4. SHORT position management ──────────────────────────
        elif direction == "SHORT":
            original_sl = (pos.original_stop_loss if pos.original_stop_loss else stop)
            risk = original_sl - entry   # Bug 15: use original SL, not current (moved) stop
            if risk <= 0:
                return

            profit_r = (entry - ltp) / risk

            # Stop loss hit
            if ltp >= effective_stop:
                logger.info(f"[PositionManager] STOP HIT SHORT {symbol}: "
                            f"ltp={ltp:.2f} >= sl={effective_stop:.2f}")
                self._exit_position(pos, remaining_size, "STOP", ltp)
                return

            # Structural exit — close an in-profit short reversing/stalling BEFORE it
            # round-trips to the stop (shared single source; same as the LONG path).
            sx = self._structural_exit_reason(symbol, direction, entry)
            self._shadow_structural_exit_check(symbol, direction, entry, symbol, pid, sx)
            if sx:
                logger.info(f"[PositionManager] STRUCTURAL EXIT SHORT {symbol}: {sx} @ {ltp:.2f}")
                self._exit_position(pos, remaining_size, sx, ltp)
                return

            # Mean-reversion: hard FULL exit at the target — no partial/trail (shared policy).
            if is_mean_rev:
                if target_1 > 0 and ltp <= target_1:
                    logger.info(f"[PositionManager] MEAN-REV TARGET SHORT {symbol}: "
                                f"ltp={ltp:.2f} <= t1={target_1:.2f} — full exit")
                    self._exit_position(pos, remaining_size, "TARGET", ltp)
                return

            # Dynamic target — advance milestone after T1 is hit
            if already_partial:
                self._update_dynamic_target(pid, symbol, profit_r, entry, risk, direction)

            # Target 2 hit — skipped when dynamic target is active (trailing stop exits instead)
            if target_2 > 0 and ltp <= target_2 and already_partial:
                if pid not in self._dynamic_target_r:
                    logger.info(f"[PositionManager] T2 HIT SHORT {symbol}: "
                                f"ltp={ltp:.2f} <= t2={target_2:.2f} — "
                                f"exiting remaining {remaining_size} shares")
                    self._exit_position(pos, remaining_size, "TARGET2", ltp)
                    return

            # Target 1 hit — partial exit + move SL to breakeven + activate dynamic target
            if target_1 > 0 and ltp <= target_1 and not already_partial:
                partial_size = max(1, int(remaining_size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT SHORT {symbol}")
                self._partial_exit(pos, partial_size, "TARGET1", ltp)
                self._tracker.mark_t1_hit(pid)   # persist so restart knows T1 fired
                with self._lock:
                    self._partial_exited.add(pid)
                    self._dynamic_target_r[pid] = DYNAMIC_TARGET_START_R
                if not already_be:
                    self._move_stop_to_breakeven(pid, symbol, entry)
                    with self._lock:
                        self._breakeven_applied.add(pid)
                return

            # Trailing stop for short
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(pid, symbol, ltp, direction, risk)

            # Chandelier ATR trail — same as the LONG path above.
            if is_trend_trail:
                self._chandelier_ratchet(pid, symbol, direction, ltp)

    # ─────────────────────────────────────────────────────────────
    # STRUCTURAL EXIT — shared single source (execution/exit_signals)
    # ─────────────────────────────────────────────────────────────

    def _structural_exit_reason(self, df_symbol: str, direction: str,
                                entry_ref: float) -> Optional[str]:
        """Structural (reversal / stall) exit on the underlying OHLCV — the SAME shared
        decision (execution/exit_signals.structural_exit) the learning engine uses, so the
        portfolio book closes a reversing winner identically. Keyed off the entry strategy
        via exit_signals config, never the segment. Self-gates to fire only once in profit.
        df_symbol: equity uses its own symbol; options pass the underlying index."""
        if entry_ref <= 0:
            return None
        try:
            from execution.exit_signals import structural_exit, config as _xs_cfg
            tf = _xs_cfg().get("timeframe", "5m")
            return structural_exit(self._store.get_ohlcv(df_symbol, tf), direction, entry_ref)
        except Exception as e:
            logger.error(f"[PositionManager] structural_exit error {df_symbol}: {e}")
            return None

    def _shadow_structural_exit_check(self, df_symbol: str, direction: str, entry_ref: float,
                                       symbol: str, pid: str, live_reason: Optional[str]) -> None:
        """SHADOW MODE ONLY — logs what an ATR-relative arm gate would have decided,
        alongside the real (flat arm_profit_pct) decision already computed by
        _structural_exit_reason. Never changes behavior: no return value is consumed,
        exceptions are swallowed, and it costs nothing once a (pid, k) has fired once.

        Why this exists instead of just changing exit_signals.json: the 2026-07 backtest
        (41 days of NIFTY 5m closed-bar data) showed the flat gate underperforms both an
        ATR-relative arm AND doing nothing at all on LONG, but showed no such edge on
        SHORT — and a closed-bar offline replay doesn't exactly match how the live system
        reacts to a forming candle. This validates the candidate against genuine live tick
        data before arm_profit_pct itself is touched — one shared exit_signals.structural_exit
        call, same as the real decision, just fed a different (ungated) cfg.
        """
        if entry_ref <= 0 or not pid:
            return
        try:
            from execution.exit_signals import structural_exit, config as _xs_cfg
            from analysis.indicators import atr as _atr
            base_cfg = _xs_cfg()
            df = self._store.get_ohlcv(df_symbol, base_cfg.get("timeframe", "5m"))
            if df is None or len(df) < int(base_cfg.get("min_bars", 20)):
                return
            price = float(df["close"].iloc[-1])
            atr_now = float(_atr(df, 14).iloc[-1])
            if atr_now <= 0:
                return
            moved = (price - entry_ref) if direction == "LONG" else (entry_ref - price)
            for k in SHADOW_ATR_MULTS:
                key = (pid, k)
                if key in self._shadow_logged:
                    continue
                if moved < k * atr_now:
                    continue
                cfg2 = dict(base_cfg)
                cfg2["arm_profit_pct"] = 0.0
                shadow_reason = structural_exit(df, direction, entry_ref, cfg=cfg2)
                if shadow_reason:
                    self._shadow_logged.add(key)
                    logger.info(
                        f"[ShadowExit] {symbol} pid={pid} k={k} ATR-relative would exit "
                        f"NOW via {shadow_reason} (live gate: {live_reason or 'holding'}) "
                        f"price={price:.2f} entry_ref={entry_ref:.2f} atr={atr_now:.2f}"
                    )
        except Exception as e:
            logger.debug(f"[PositionManager] shadow_structural_exit error {symbol}: {e}")

    def _underlying_point_trail(self, symbol: str, pid: str, options_meta: dict) -> Optional[str]:
        """Underlying index-point trailing stop for an option-BUYING trade — the
        proven learning-engine mechanism (InstitutionalMomentum, Reversal5m/3m),
        now shared. Trails the UNDERLYING's spot (clean price action) instead of
        the noisy option premium: initial stop = entry_spot − sl_pts, ratcheted up
        to peak_spot − trail_pts as the index makes new highs. Exits at the
        option's current premium when the index trades at/through the trailed
        stop. peak_spot/trail_stop_spot persist in options_meta across ticks
        (same fields institutional_momentum.py/reversal_5m.py already seed at
        entry). LONG-underlying only, matching the original — these strategies
        only ever emit long-call signals today.
        """
        entry_spot = float(options_meta.get("entry_spot") or 0)
        if entry_spot <= 0:
            return None
        spot = self._store.get_ltp(symbol)   # the option position's symbol IS the underlying
        if not spot or spot <= 0:
            return None
        sl_pts    = float(options_meta.get("sl_pts") or 0)
        trail_pts = float(options_meta.get("trail_pts") or 0)
        peak      = float(options_meta.get("peak_spot") or entry_spot)
        stop      = float(options_meta.get("trail_stop_spot") or (entry_spot - sl_pts))

        from execution.exit_rules import underlying_exit
        reason, new_peak, new_stop = underlying_exit(spot, entry_spot, sl_pts, trail_pts, peak, stop, pct=False)

        if new_peak != peak or new_stop != stop:
            options_meta["peak_spot"]       = new_peak
            options_meta["trail_stop_spot"] = new_stop
            self._tracker.update_options_meta(pid, options_meta)

        return reason

    # ─────────────────────────────────────────────────────────────
    # OPTIONS EXIT MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    def _check_options_position(
        self,
        symbol: str,
        pid: str,
        direction: str,
        entry: float,
        stop: float,
        target_1: float,
        size: int,
        options_meta: dict,
        now: datetime,
        pos_dict: dict,
    ) -> None:
        """
        Unified options position monitoring.

        All exit rules are data-driven from the position record — no strategy
        name branching. Each strategy must declare:
          - monitor_symbol (or monitor_symbols + monitor_combine="sum")
          - stop_loss as an absolute price/premium level
          - target_1 as an absolute price/premium level
          - time_stop in options_meta if a time-based exit is needed
        """
        # ── 1. DTE-based forced exit ──────────────────────────────
        expiry_str = options_meta.get("expiry")
        if not expiry_str:
            nfo = options_meta.get("nfo_symbol") or options_meta.get("nfo_call")
            if nfo:
                try:
                    from risk.options_risk import options_risk_gate
                    expiry_str = options_risk_gate._parse_expiry_from_symbol(nfo)
                except Exception:
                    pass

        if expiry_str:
            try:
                expiry_dt = date.fromisoformat(expiry_str)
                dte = (expiry_dt - datetime.now(tz=IST).date()).days
                if dte <= OPTIONS_DTE_FORCE_EXIT:
                    logger.warning(
                        f"[PositionManager] OPTIONS DTE EXIT {symbol}: "
                        f"{dte} days to expiry — closing to avoid expiry risk"
                    )
                    self._exit_options_position(symbol, pid, size, "DTE_FORCED", options_meta)
                    return
            except Exception:
                pass

        # ── 2. EOD forced exit (3:15 PM) ─────────────────────────
        # Must run BEFORE the LTP check so EOD exits fire even when tick data
        # is temporarily unavailable (e.g. WS reconnect gap, subscription delay).
        if now.time() >= EOD_EXIT_TIME:
            opt_pos = self._tracker.get_position(symbol)
            if opt_pos and opt_pos.hold_type == "swing":
                return
            logger.info(f"[PositionManager] EOD OPTIONS exit: {symbol}")
            self._exit_options_position(symbol, pid, size, "EOD_FORCED", options_meta)
            return

        # ── 3. Get current monitor LTP ────────────────────────────
        option_ltp = self._get_monitor_ltp(pos_dict)
        if option_ltp is None:
            logger.warning(f"[PositionManager] {symbol}: monitor LTP unavailable — stop/target checks skipped (WS subscription may be pending)")
            return

        # Trade-quality instrumentation — premium excursion (buying options is
        # always long-premium cost basis, so adverse/favourable don't mirror by
        # direction here, same as the equity path).
        self._update_mae_mfe(pid, "LONG", entry, option_ltp)

        # ── 4. Time stop (optional — set in options_meta["time_stop"]) ───
        time_stop_str = options_meta.get("time_stop")
        if time_stop_str:
            try:
                h, m = map(int, time_stop_str.split(":"))
                if now.time() >= time(h, m):
                    logger.info(f"[PositionManager] TIME STOP {symbol}: {time_stop_str} — exiting")
                    self._exit_options_position(symbol, pid, size, "TIME_STOP", options_meta)
                    return
            except Exception:
                pass

        # ── 5/6. Underlying point-trail OR premium STOP/TARGET ──────────────
        # Strategies that opt into options_meta["exit_mode"]="underlying_trail"
        # (InstitutionalMomentum, Reversal5m/3m) ride the underlying's clean price
        # action instead of the noisy option premium — their premium target_1 is
        # intentionally nominal ("trail governs exit"), so the point-trail REPLACES
        # the premium check for them, exactly like the learning engine. Everyone
        # else keeps the premium STOP/TARGET decision (execution/exit_rules).
        if options_meta.get("exit_mode") == "underlying_trail":
            _ut_reason = self._underlying_point_trail(symbol, pid, options_meta)
            if _ut_reason:
                logger.info(f"[PositionManager] UNDERLYING TRAIL {symbol}: {_ut_reason} @ {option_ltp:.2f}")
                self._exit_options_position(symbol, pid, size, _ut_reason, options_meta)
                return
        else:
            # LONG (buying): STOP when premium ≤ stop, TARGET when ≥ target.
            # SHORT (selling/strangle): mirrored. One predicate, no LONG/SHORT fork here.
            from execution.exit_rules import premium_exit
            _pdec = premium_exit(option_ltp, stop, target_1, direction)
            if _pdec == "STOP" and stop > 0:
                logger.warning(
                    f"[PositionManager] OPTIONS STOP {symbol}: "
                    f"{'value' if direction == 'SHORT' else 'premium'} {option_ltp:.2f} "
                    f"{'≥' if direction == 'SHORT' else '≤'} stop {stop:.2f}"
                )
                self._exit_options_position(symbol, pid, size, "STOP", options_meta)
                return
            if _pdec == "TARGET" and target_1 > 0 and pid not in self._partial_exited:
                logger.info(
                    f"[PositionManager] OPTIONS TARGET {symbol}: "
                    f"ltp {option_ltp:.2f} {'≥' if direction == 'LONG' else '≤'} target {target_1:.2f}"
                )
                self._exit_options_position(symbol, pid, size, "TARGET", options_meta)
                self._partial_exited.add(pid)
                return

        # ── 7. Structural exit on the UNDERLYING — shared single source ──────────
        # Close a reversing/stalling option winner before it round-trips, using the SAME
        # exit_signals decision the learning engine applies. The option position's `symbol`
        # IS the underlying index; map direction from option type (CE→LONG, PE→SHORT). Gated
        # on underlying profit via the entry spot captured at open. This is the option
        # "trend reversed → exit" path that was missing (NIFTY CE rode 144→112 to EOD).
        entry_spot = float(options_meta.get("entry_spot") or 0)
        if entry_spot > 0:
            opt_type = (options_meta.get("option_type") or "").upper()
            nfo = (options_meta.get("nfo_symbol") or pos_dict.get("monitor_symbol") or "").upper()
            und_dir = "SHORT" if (opt_type == "PE" or "PE" in nfo) else "LONG"
            sx = self._structural_exit_reason(symbol, und_dir, entry_spot)
            self._shadow_structural_exit_check(symbol, und_dir, entry_spot, symbol, pid, sx)
            if sx:
                logger.info(
                    f"[PositionManager] OPTIONS STRUCTURAL EXIT {symbol}: {sx} "
                    f"(underlying {und_dir}, entry_spot {entry_spot:.2f})"
                )
                self._exit_options_position(symbol, pid, size, sx, options_meta)
                return

    def _get_monitor_ltp(self, pos_dict: dict) -> Optional[float]:
        """
        Return the live LTP of the position's monitor instrument.

        Uses monitor_symbol for single-leg positions.
        Uses options_meta["monitor_symbols"] + monitor_combine="sum" for multi-leg.
        Returns None if the symbol has no data — caller skips the check.
        Never falls back to the underlying index price.
        """
        try:
            options_meta = pos_dict.get("options_meta") or {}
            combine = options_meta.get("monitor_combine", "single")

            if combine == "sum":
                syms = options_meta.get("monitor_symbols", [])
                ltps = [self._store.get_ltp(s) for s in syms if s]
                valid = [float(v) for v in ltps if v and v > 0]
                return sum(valid) if valid and len(valid) == len(syms) else None
            else:
                monitor_sym = pos_dict.get("monitor_symbol") or ""
                if not monitor_sym:
                    return None
                ltp = self._store.get_ltp(monitor_sym)
                return float(ltp) if ltp and ltp > 0 else None
        except Exception:
            return None

    def _exit_options_position(
        self, symbol: str, pid: str, size: int, reason: str, options_meta: dict
    ) -> None:
        """
        Exit a SPECIFIC options position (by trade-id — multi-position-safe).
        Closes all legs (call + put for strangles, single leg for debit spread).
        Routes to paper engine in paper mode, else Fyers NFO.
        """

        pos = self._tracker.get_position_by_id(pid) or self._tracker.get_position(symbol)
        if not pos:
            logger.warning(f"[PositionManager] Options exit: no position found for {symbol}")
            return
        pid = pos.id

        logger.info(
            f"[PositionManager] OPTIONS EXIT {symbol} × {size} — {reason}"
        )

        # Resolve the option's OWN premium for an accurate exit price (never the index spot)
        pos_dict_for_ltp = {
            "monitor_symbol": pos.monitor_symbol or pos.symbol,
            "options_meta":   options_meta,
        }
        exit_ltp   = self._get_monitor_ltp(pos_dict_for_ltp)
        exit_price = exit_ltp if exit_ltp and exit_ltp > 0 else pos.entry_price

        if self._book == "LEARNING":
            # Same reasoning as _exit_position: no real order, no paper_trading_engine
            # call (separate symbol-keyed wallet mirror, owned by learning_engine).
            order_id = "LEARNING-EXIT"
        elif PAPER_TRADING:
            from paper_trading import paper_trading_engine
            paper_trading_engine.close_order(
                symbol     = symbol,
                qty        = size,
                direction  = pos.direction,
                reason     = reason,
                exit_price = exit_price,   # option premium, not index spot
            )
            order_id = "PAPER-OPT-EXIT"
        else:
            order_id = self._place_options_exit_orders(pos, size, options_meta)

        if order_id:
            # exit_price (option premium) already resolved above — reuse it.
            closed = self._tracker.close_position_by_id(pid, exit_price, reason)
            if closed:
                # Notify options risk gate of the P&L
                try:
                    from risk.options_risk import options_risk_gate
                    from config.settings import TOTAL_CAPITAL
                    options_risk_gate.update_daily_pnl(closed.realised_pnl, TOTAL_CAPITAL)
                except Exception:
                    pass
                alert_service.trade_closed(symbol, closed.realised_pnl, reason)
                self._apply_exit_cooldown(symbol, reason)
            with self._lock:
                self._breakeven_applied.discard(pid)
                self._partial_exited.discard(pid)
                self._trailing_stops.pop(pid, None)
                self._dynamic_target_r.pop(pid, None)
        else:
            logger.error(
                f"[PositionManager] OPTIONS EXIT ORDER FAILED for {symbol} — "
                f"MANUAL INTERVENTION REQUIRED"
            )
            alert_service.info(
                f"🚨 OPTIONS EXIT FAILED: {symbol}\n"
                f"Reason: {reason}\nMANUAL EXIT REQUIRED"
            )

    def _place_options_exit_orders(self, pos, size: int, options_meta: dict) -> Optional[str]:
        """
        Place exit orders for all legs using options_meta["exit_legs"].
        Data-driven — no strategy name branching.
        """
        try:
            from execution.order_router import order_router
            exit_legs = options_meta.get("exit_legs", [])
            lot_size  = int(options_meta.get("lot_size", 1))
            ids = []
            for leg in exit_legs:
                oid = order_router.place(
                    symbol=leg["symbol"], direction=leg["direction"],
                    qty=lot_size, order_type="MARKET",
                )
                if oid:
                    ids.append(oid)
            return ids[0] if ids else None
        except Exception as e:
            logger.error(f"[PositionManager] Options exit order error: {e}")
        return None

    # ─────────────────────────────────────────────────────────────
    # EXIT OPERATIONS
    # ─────────────────────────────────────────────────────────────

    def _exit_position(self, pos, size: int, reason: str, price: float) -> None:
        """Full exit of a SPECIFIC position (identified by the Position object, not symbol),
        so a book holding several positions per symbol exits the right one. State + tracker
        ops route by pos.id; orders/logging use pos.symbol."""
        if not pos:
            return
        pid    = pos.id
        symbol = pos.symbol

        logger.info(f"[PositionManager] EXITING {symbol} × {size} @ {price:.2f} — {reason}")

        if self._book == "LEARNING":
            # No real order, and no paper_trading_engine call — that engine's
            # ₹5L wallet mirror is a SEPARATE bookkeeping system keyed by its own
            # trade-id (learning_engine's mirror_learning_open/close); calling
            # close_order() here by SYMBOL could match and corrupt the wrong row.
            order_id = "LEARNING-EXIT"
        elif PAPER_TRADING:
            # Paper mode — simulate exit via paper trading engine
            from paper_trading import paper_trading_engine
            paper_trading_engine.close_order(
                symbol    = symbol,
                qty       = size,
                direction = pos.direction,
                reason    = reason,
            )
            order_id = f"PAPER-EXIT"
        else:
            from execution.order_router import order_router
            exit_direction = "SHORT" if pos.direction == "LONG" else "LONG"
            order_id = order_router.place(
                symbol     = symbol,
                direction  = exit_direction,
                qty        = size,
                order_type = "MARKET",
            )

        # Mark PENDING_CLOSE BEFORE placing the order so that a crash between
        # the broker call and close_position() is detectable on restart (Bug 1).
        self._tracker.set_pending_close(pid)

        if order_id:
            # Close in portfolio tracker (by trade-id — multi-position-safe)
            closed = self._tracker.close_position_by_id(pid, price, reason)
            if closed:
                alert_service.trade_closed(symbol, closed.realised_pnl, reason)
                self._apply_exit_cooldown(symbol, reason)

            # Clean up tracking sets (locked)
            with self._lock:
                self._breakeven_applied.discard(pid)
                self._partial_exited.discard(pid)
                self._trailing_stops.pop(pid, None)
                self._dynamic_target_r.pop(pid, None)
        else:
            # Revert PENDING_CLOSE → OPEN so position monitoring continues
            pos.status = "OPEN"
            self._tracker._update_position_db(pos)
            logger.error(f"[PositionManager] EXIT ORDER FAILED for {symbol} — "
                         f"MANUAL INTERVENTION REQUIRED")
            alert_service.info(
                f"🚨 EXIT FAILED for {symbol}\n"
                f"Reason: {reason}\nPrice: ₹{price:.2f}\n"
                f"MANUAL EXIT REQUIRED IMMEDIATELY"
            )

    def _partial_exit(self, pos, size: int, reason: str, price: float) -> None:
        """Exit part of a SPECIFIC position (by Position object). update_position_size routes by id."""
        if not pos:
            return
        symbol = pos.symbol

        logger.info(f"[PositionManager] PARTIAL EXIT {symbol} × {size} @ {price:.2f}")

        if PAPER_TRADING:
            from paper_trading import paper_trading_engine
            order_id = paper_trading_engine.close_order(
                symbol    = symbol,
                qty       = size,
                direction = pos.direction,
                reason    = f"PARTIAL_{reason}",
            )
        else:
            from execution.order_router import order_router
            exit_direction = "SHORT" if pos.direction == "LONG" else "LONG"
            order_id = order_router.place(
                symbol     = symbol,
                direction  = exit_direction,
                qty        = size,
                order_type = "MARKET",
            )

        if order_id:
            # Persist reduced size to DB immediately so restart sees correct qty (Bug 5)
            new_size = pos.position_size - size
            self._tracker.update_position_size(pos.id, new_size)
            pos.position_size = new_size

            partial_pnl = (price - pos.entry_price) * size
            if pos.direction == "SHORT":
                partial_pnl = (pos.entry_price - price) * size
            logger.info(f"[PositionManager] Partial P&L: ₹{partial_pnl:+,.0f}")
            alert_service.info(
                f"📊 Partial exit: {symbol.replace('NSE:','').replace('-EQ','')}\n"
                f"Sold {size} shares @ ₹{price:.2f}\n"
                f"P&L: ₹{partial_pnl:+,.0f}\nSL moved to breakeven"
            )
        else:
            logger.error(f"[PositionManager] Partial exit order failed for {symbol}")

    def _move_stop_to_breakeven(self, pid: str, symbol: str, entry_price: float) -> None:
        """Move stop loss to breakeven (entry price). State + tracker by pid; broker/log by symbol."""
        self._trailing_stops[pid] = entry_price
        self._tracker.update_stop_loss(pid, entry_price)
        logger.info(f"[PositionManager] SL moved to breakeven: "
                    f"{symbol} → ₹{entry_price:.2f}")

        # Cancel old SL order on broker and place new one (live only; keyed by symbol)
        self._update_broker_sl(symbol, entry_price)

    def _update_trailing_stop(
        self, pid: str, symbol: str, ltp: float, direction: str, risk: float
    ) -> None:
        """Update trailing stop — trails by 1×ATR behind current price. State by pid."""
        # Use ATR as trail distance (approximated as original risk amount)
        trail_distance = risk * 0.8

        if direction == "LONG":
            new_sl = ltp - trail_distance
        else:
            new_sl = ltp + trail_distance

        current_sl = self._trailing_stops.get(pid, 0)

        # Only move stop in profitable direction (ratchet — never move backward)
        if direction == "LONG" and new_sl > current_sl:
            self._trailing_stops[pid] = round(new_sl, 2)
            self._tracker.update_stop_loss(pid, new_sl)
            logger.info(f"[PositionManager] Trail SL updated: {symbol} → ₹{new_sl:.2f}")
            self._update_broker_sl(symbol, new_sl)

        elif direction == "SHORT" and (current_sl == 0 or new_sl < current_sl):
            self._trailing_stops[pid] = round(new_sl, 2)
            self._tracker.update_stop_loss(pid, new_sl)
            logger.info(f"[PositionManager] Trail SL updated SHORT: {symbol} → ₹{new_sl:.2f}")
            self._update_broker_sl(symbol, new_sl)

    def _chandelier_ratchet(self, pid: str, symbol: str, direction: str, ltp: float) -> None:
        """Volatility-adaptive trail for TREND_TRAIL-styled strategies: ratchet the
        stop toward peak/trough ± CHANDELIER_MULT×ATR, tightening only, never
        loosening — the proven learning-engine Chandelier exit, now available to
        every book. ATR is captured once (first tick this position is seen) and
        held for the trade's life, same as the original. Feeds the SAME
        _trailing_stops dict _update_trailing_stop uses, so whichever trail is
        more protective wins — this never overrides a tighter existing stop.
        """
        atr_val = self._chandelier_atr.get(pid)
        if atr_val is None:
            try:
                from analysis.indicators import atr as _atr
                df = self._store.get_ohlcv(symbol, CHANDELIER_TF, n=60)
                atr_val = float(_atr(df, 14).iloc[-1]) if df is not None and len(df) >= 14 else 0.0
            except Exception:
                atr_val = 0.0
            self._chandelier_atr[pid] = atr_val
        if atr_val <= 0:
            return  # no ATR data yet — skip, fixed trail above still protects the trade

        peak = self._chandelier_peak.get(pid, ltp)
        current = self._trailing_stops.get(pid, 0)

        if direction == "LONG":
            peak = max(peak, ltp)
            self._chandelier_peak[pid] = peak
            chandelier = round(peak - CHANDELIER_MULT * atr_val, 2)
            if chandelier <= current:
                return
        else:
            peak = min(peak, ltp)
            self._chandelier_peak[pid] = peak
            chandelier = round(peak + CHANDELIER_MULT * atr_val, 2)
            if current != 0 and chandelier >= current:
                return

        self._trailing_stops[pid] = chandelier
        self._tracker.update_stop_loss(pid, chandelier)
        logger.info(
            f"[PositionManager] CHANDELIER {symbol}: peak {peak:.2f} "
            f"± {CHANDELIER_MULT}×ATR({atr_val:.2f}) → stop {chandelier:.2f}"
        )
        self._update_broker_sl(symbol, chandelier)

    def _update_mae_mfe(self, pid: str, direction: str, entry: float, ltp: float) -> None:
        """Track max adverse/favourable excursion in points — trade-quality
        instrumentation, not an exit decision. Persists only on a new high/low
        (same optimisation the learning engine used) via the tracker's generic
        update_fields (this book's own segment)."""
        adverse    = (entry - ltp) if direction == "LONG" else (ltp - entry)
        favourable = (ltp - entry) if direction == "LONG" else (entry - ltp)
        prev_mae, prev_mfe = self._mae_mfe.get(pid, (0.0, 0.0))
        mae = max(prev_mae, adverse)
        mfe = max(prev_mfe, favourable)
        if mae > prev_mae + 1e-9 or mfe > prev_mfe + 1e-9:
            self._mae_mfe[pid] = (mae, mfe)
            self._tracker.update_fields(pid, mae_pts=round(mae, 2), mfe_pts=round(mfe, 2))

    def _reconstruct_state_from_position(
        self, pos, direction: str, entry: float, original_stop: float, ltp: float
    ) -> None:
        """
        Rebuild in-memory tracking state from the persisted stop_loss after a restart.
        Keyed by pos.id (multi-position-safe).

        Uses original_stop_loss (frozen at entry) rather than the current stop_loss
        to compute risk, because if SL was moved to breakeven, stop_loss == entry,
        making risk = 0 and causing early return without restoring state (Bug 6).
        """
        if not pos:
            return
        pid    = pos.id
        symbol = pos.symbol

        persisted_sl = pos.stop_loss
        # Use the immutable original stop for risk — never use the current (moved) SL (Bug 6)
        true_original_stop = pos.original_stop_loss if pos.original_stop_loss else original_stop
        risk = abs(entry - true_original_stop)
        if risk <= 0:
            return

        # Use the persisted t1_hit flag as ground truth (exact).
        # Fallback to SL-position inference only for old positions that predate the column.
        if pos.t1_hit:
            t1_was_hit = True
        else:
            # Legacy inference: SL moved past breakeven implies T1 was hit.
            # This can produce a false positive (breakeven applied but T1 not yet hit),
            # but that's safe: worst case = T1 partial exit skipped, trailing active.
            t1_was_hit = (direction == "LONG"  and persisted_sl > entry) or \
                         (direction == "SHORT" and persisted_sl < entry)

        if not t1_was_hit:
            return

        if direction == "LONG":
            profit_r = (ltp - entry) / risk
        else:
            profit_r = (entry - ltp) / risk

        next_milestone = max(DYNAMIC_TARGET_START_R, math.ceil(profit_r) + 1)

        with self._lock:
            self._partial_exited.add(pid)
            self._breakeven_applied.add(pid)
            self._trailing_stops[pid]   = persisted_sl
            self._dynamic_target_r[pid] = next_milestone

        logger.info(
            f"[PositionManager] State restored after restart: {symbol} | "
            f"T1 hit | trail SL=₹{persisted_sl:.2f} | "
            f"profit={profit_r:.1f}R | next target={next_milestone:.0f}R"
        )

    def _update_dynamic_target(
        self, pid: str, symbol: str, profit_r: float, entry: float, risk: float, direction: str
    ) -> None:
        """
        Advance the dynamic target milestone each time the trade gains another R.
        Called every tick after T1 partial exit. Trailing stop remains the exit trigger.
        State keyed by pid; symbol used for logging only.
        """
        with self._lock:
            current_milestone = self._dynamic_target_r.get(pid)
        if current_milestone is None:
            return

        if profit_r >= current_milestone:
            next_milestone = current_milestone + DYNAMIC_TARGET_STEP
            with self._lock:
                self._dynamic_target_r[pid] = next_milestone

            if direction == "LONG":
                next_price = entry + next_milestone * risk
            else:
                next_price = entry - next_milestone * risk

            logger.info(
                f"[PositionManager] DYNAMIC TARGET {symbol}: "
                f"{current_milestone:.0f}R hit → target extended to "
                f"{next_milestone:.0f}R = ₹{next_price:.2f}"
            )
            alert_service.info(
                f"🎯 {symbol.replace('NSE:','').replace('-EQ','')}: "
                f"{current_milestone:.0f}R milestone hit\n"
                f"Target extended → {next_milestone:.0f}R = ₹{next_price:.2f}\n"
                f"Trailing stop riding the move"
            )

    def _update_broker_sl(self, symbol: str, new_sl: float) -> None:
        """
        Update stop loss order on broker.
        Cancels the EXISTING SL order first, then places the new one (Bug 2).
        Without cancellation, each update leaves an orphaned SL-M order on the broker;
        when price hits SL all orphaned orders fire simultaneously → over-sized exit.
        Skipped in paper trading mode.
        """
        if PAPER_TRADING:   # Bug 16: imported at top of module
            return
        try:
            from execution.order_router import order_router
            pos = self._tracker.get_position(symbol)
            if not pos:
                return
            remaining_size = pos.position_size
            if remaining_size <= 0:
                return

            # Cancel old SL order before placing the updated one (Bug 2)
            if pos.sl_order_id:
                try:
                    order_router.cancel(symbol, pos.sl_order_id)
                    logger.debug(f"[PositionManager] Cancelled old SL {pos.sl_order_id} for {symbol}")
                except Exception as ce:
                    logger.warning(f"[PositionManager] Could not cancel old SL {pos.sl_order_id}: {ce}")

            exit_dir = "SHORT" if pos.direction == "LONG" else "LONG"
            # Pass correct product so swing CNC SL isn't rejected (Bug 7)
            product = "CNC" if pos.hold_type == "swing" else "INTRADAY"
            new_sl_id = order_router.place(
                symbol     = symbol,
                direction  = exit_dir,
                qty        = remaining_size,
                order_type = "SL-M",
                trigger    = new_sl,
                product    = product,
            )
            if new_sl_id:
                self._tracker.update_sl_order_id(symbol, new_sl_id)
                logger.debug(f"[PositionManager] New SL order {new_sl_id} @ ₹{new_sl:.2f} for {symbol}")
        except Exception as e:
            logger.warning(f"[PositionManager] Broker SL update failed (non-fatal): {e}")

    def _apply_exit_cooldown(self, symbol: str, reason: str) -> None:
        """
        Apply a re-entry cooldown after any position close.
        Persisted to DB so bot restarts don't lose the cooldown.

        Loss/forced exits  → full SYMBOL_COOLDOWN_MINUTES (default 60 min)
        Target/win exits   → 30 min (prevent immediate same-day re-entry)
        """
        from config.settings import SYMBOL_COOLDOWN_MINUTES
        win_reasons  = {"TARGET1", "TARGET2", "TARGET1_PARTIAL", "TARGET_50PCT_CREDIT"}
        loss_reasons = {"STOP", "EOD_FORCED", "MAX_HOLD", "DTE_FORCED",
                        "STOP_50PCT_PREMIUM", "STOP_2X_CREDIT", "SL_PLACEMENT_FAILED"}
        if reason in win_reasons:
            minutes = 30
        elif reason in loss_reasons:
            minutes = SYMBOL_COOLDOWN_MINUTES
        else:
            minutes = 30   # unknown reason — short cooldown as safety net

        if self._on_close is not None:
            # A second book's own cooldown store (e.g. learning_engine._apply_cooldown) —
            # never the production strategy_selector.
            try:
                self._on_close(symbol, minutes)
            except Exception as e:
                logger.warning(f"[PositionManager] on_close callback error for {symbol}: {e}")
            return

        try:
            from strategies.strategy_selector import strategy_selector
            strategy_selector.apply_cooldown(symbol, minutes=minutes)
        except Exception as e:
            logger.warning(f"[PositionManager] Could not apply cooldown for {symbol}: {e}")

    def reset_symbol(self, symbol: str) -> None:
        """Clean up tracking state for a symbol after full exit."""
        with self._lock:
            self._breakeven_applied.discard(symbol)
            self._partial_exited.discard(symbol)
            self._trailing_stops.pop(symbol, None)
            self._dynamic_target_r.pop(symbol, None)


# ── Module-level singleton ────────────────────────────────────────
position_manager = PositionManager()
