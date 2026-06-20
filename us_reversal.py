"""
us_reversal.py
──────────────
US index-ETF Reversal options PAPER engine (SPY = S&P 500, QQQ = Nasdaq-100).

The US equivalent of the Indian index Reversal strategy: the red→green reclaim
pattern on 5m bars, traded as ATM weekly CALL-buying with an underlying % trailing
stop + EOD square-off. Premiums are Black-Scholes modeled (validated by
scripts/backtest_reversal_us_index.py — pooled PF ~2.5). Self-contained with its own
DB table so it never collides with the NSE learning engine's exit loop.

PAPER ONLY. No real Alpaca option orders — this measures the modeled edge on live
SPY/QQQ data before any thought of going live (which has India/FEMA + Alpaca-options
hurdles; a US/Canada-resident account would be needed).
"""
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET  = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
DB_PATH = "db/trades.db"

R_FREE      = 0.045
SPREAD      = 0.005     # 0.5%/side on premium
COMMISSION  = 0.65      # per contract, each way
MULT        = 100       # shares per US option contract
STRIKE_STEP = 1.0
RSI_LOW, RSI_HIGH, MIN_RVOL, MIN_BARS = 30.0, 70.0, 1.2, 30

# symbol -> (assumed IV, initial SL %, trail %)  — best config from the backtest
CFG = {"SPY": (0.14, 0.5, 0.9), "QQQ": (0.18, 0.5, 0.9)}


def _dte_days(d) -> int:
    days = (4 - d.weekday()) % 7      # Friday weekly expiry
    return days if days != 0 else 7


def _bs_call(spot, strike, dte_y, iv) -> float:
    if dte_y <= 0:
        return max(0.0, spot - strike)
    from analysis.options_engine import options_engine
    return options_engine.black_scholes(spot, strike, dte_y, R_FREE, iv, "call").price


class USReversalEngine:
    """SPY/QQQ Reversal call-buying paper engine. Call run_cycle() each loop."""

    def __init__(self):
        self._open: dict[str, dict] = {}     # symbol -> open position
        self._init_db()
        self._load_open()

    # ── DB ───────────────────────────────────────────────────────
    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS us_reversal_trades(
                    id TEXT PRIMARY KEY, symbol TEXT, strategy TEXT, status TEXT,
                    entry_time TEXT, entry_spot REAL, strike REAL, dte INTEGER, iv REAL,
                    entry_premium REAL, sl_pct REAL, trail_pct REAL,
                    exit_time TEXT, exit_spot REAL, exit_premium REAL, pnl REAL,
                    exit_reason TEXT, peak_spot REAL, stop_spot REAL)
            """)

    def _load_open(self) -> None:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute("SELECT * FROM us_reversal_trades WHERE status='OPEN'"):
                self._open[r["symbol"]] = dict(r)
        if self._open:
            logger.info(f"[USReversal] Loaded {len(self._open)} open paper positions")

    def _persist_open(self, p: dict) -> None:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("""INSERT OR REPLACE INTO us_reversal_trades
                (id,symbol,strategy,status,entry_time,entry_spot,strike,dte,iv,
                 entry_premium,sl_pct,trail_pct,peak_spot,stop_spot)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["id"], p["symbol"], p["strategy"], "OPEN", p["entry_time"],
                 p["entry_spot"], p["strike"], p["dte"], p["iv"], p["entry_premium"],
                 p["sl_pct"], p["trail_pct"], p["peak_spot"], p["stop_spot"]))

    def _persist_close(self, p: dict, spot, exit_prem, pnl, reason) -> None:
        with sqlite3.connect(DB_PATH) as c:
            c.execute("""UPDATE us_reversal_trades SET status='CLOSED', exit_time=?,
                exit_spot=?, exit_premium=?, pnl=?, exit_reason=?, peak_spot=?, stop_spot=?
                WHERE id=?""",
                (datetime.now(tz=ET).isoformat(), round(spot, 2), round(exit_prem, 2),
                 round(pnl, 2), reason, p["peak_spot"], p["stop_spot"], p["id"]))

    # ── Cycle ────────────────────────────────────────────────────
    def run_cycle(self) -> None:
        now = datetime.now(tz=ET)
        t = now.time()
        open_session  = (t >= datetime.strptime("09:30", "%H:%M").time() and
                         t <  datetime.strptime("16:00", "%H:%M").time())
        eod           = t >= datetime.strptime("15:55", "%H:%M").time()
        allow_entry   = (t >= datetime.strptime("09:35", "%H:%M").time() and
                         t <  datetime.strptime("15:45", "%H:%M").time())
        if not open_session and not self._open:
            return
        try:
            from data.data_store import store
        except Exception:
            return

        for sym, (iv, sl_pct, trail_pct) in CFG.items():
            spot = store.get_ltp(sym)
            # ── Manage an open position ──────────────────────────
            if sym in self._open:
                p = self._open[sym]
                if spot and spot > 0:
                    p["peak_spot"] = max(p["peak_spot"], float(spot))
                    trail = p["peak_spot"] * (1 - p["trail_pct"] / 100)
                    p["stop_spot"] = max(p["stop_spot"], trail)
                exit_reason = None
                if spot and spot > 0 and spot <= p["stop_spot"]:
                    exit_reason = "TRAIL" if p["stop_spot"] > p["entry_spot"] * (1 - p["sl_pct"]/100) else "STOP"
                elif eod:
                    exit_reason = "EOD"
                if exit_reason:
                    self._close(sym, float(spot or p["entry_spot"]), exit_reason)
                continue

            # ── Look for a new entry ─────────────────────────────
            if not allow_entry:
                continue
            self._maybe_enter(sym, iv, sl_pct, trail_pct, store)

    def _maybe_enter(self, sym, iv, sl_pct, trail_pct, store) -> None:
        df = store.get_ohlcv(sym, "5m", n=50)
        if df is None or len(df) < MIN_BARS:
            return
        try:
            from analysis.indicators import rsi as calc_rsi, relative_volume
            o, c = df["open"], df["close"]
            red_then_green = (c.iloc[-2] < o.iloc[-2] and c.iloc[-1] > o.iloc[-1]
                              and c.iloc[-1] > o.iloc[-2])
            if not red_then_green:
                return
            rsis = calc_rsi(c)
            rsi_now, rsi_prev = float(rsis.iloc[-1]), float(rsis.iloc[-2])
            if not (RSI_LOW < rsi_now < RSI_HIGH and rsi_now > rsi_prev):
                return
            vol_present = df["volume"].sum() > 0
            rvol = float(relative_volume(df).iloc[-1])
            if vol_present and rvol < MIN_RVOL:
                return
            spot = float(store.get_ltp(sym) or c.iloc[-1])
            strike = round(spot / STRIKE_STEP) * STRIKE_STEP
            dte = _dte_days(datetime.now(tz=ET).date())
            prem = _bs_call(spot, strike, dte / 365.0, iv)
            if prem <= 0.05:
                return
            now_iso = datetime.now(tz=ET).isoformat()
            p = {
                "id": f"USR-{uuid.uuid4().hex[:8].upper()}", "symbol": sym,
                "strategy": f"ReversalUS_{sym}", "entry_time": now_iso,
                "entry_spot": round(spot, 2), "strike": strike, "dte": dte, "iv": iv,
                "entry_premium": round(prem, 2), "sl_pct": sl_pct, "trail_pct": trail_pct,
                "peak_spot": round(spot, 2), "stop_spot": round(spot * (1 - sl_pct/100), 2),
            }
            self._open[sym] = p
            self._persist_open(p)
            logger.info(
                f"[USReversal] OPEN {sym} {strike}C ${prem:.2f} spot=${spot:.2f} "
                f"DTE={dte} RSI {rsi_prev:.0f}->{rsi_now:.0f} SL/trail {sl_pct}/{trail_pct}%"
            )
        except Exception as exc:
            logger.debug(f"[USReversal] entry error {sym}: {exc}")

    def _close(self, sym, spot, reason) -> None:
        p = self._open.pop(sym)
        held_min = max(0, (datetime.now(tz=ET) - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60)
        dte_y = max(0.0, p["dte"] / 365.0 - held_min / (60 * 24 * 365))
        exit_mid = _bs_call(spot, p["strike"], dte_y, p["iv"])
        entry_fill = p["entry_premium"] * (1 + SPREAD)
        exit_fill  = exit_mid * (1 - SPREAD)
        pnl = (exit_fill - entry_fill) * MULT - 2 * COMMISSION
        self._persist_close(p, spot, exit_mid, pnl, reason)
        logger.info(f"[USReversal] CLOSE {sym} {reason} spot=${spot:.2f} prem ${exit_mid:.2f} | P&L ${pnl:+.0f}")

    # ── Stats (for the dashboard) ────────────────────────────────
    def get_stats(self) -> dict:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            closed = [dict(r) for r in c.execute("SELECT * FROM us_reversal_trades WHERE status='CLOSED'")]
            n_open = c.execute("SELECT COUNT(*) FROM us_reversal_trades WHERE status='OPEN'").fetchone()[0]
        if not closed:
            return {"total_closed": 0, "total_open": n_open, "message": "No closed US trades yet."}
        wins = [t for t in closed if (t["pnl"] or 0) > 0]
        gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in closed if (t["pnl"] or 0) <= 0)
        by = {}
        for t in closed:
            by.setdefault(t["symbol"], []).append(t["pnl"] or 0)
        return {
            "total_closed": len(closed), "total_open": n_open,
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
            "pf": round(gw / gl, 2) if gl > 0 else None,
            "total_pnl_usd": round(sum(t["pnl"] or 0 for t in closed), 2),
            "by_symbol": {s: {"n": len(v), "pnl": round(sum(v), 2)} for s, v in by.items()},
        }


us_reversal = USReversalEngine()
