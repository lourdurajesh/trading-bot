"""
commodity_options_learning.py
─────────────────────────────
Standalone paper-trading engine for MCX commodity options.

Completely separate from:
  - Production strategy system
  - NSE learning_engine.py
  - NSE options_executor.py

What it does:
  Every cycle during MCX hours (09:00–23:30 IST):
    1. Checks the 1H commodity futures trend (EMA20 + RSI)
    2. If bullish: tries to buy an ATM call debit spread
       If bearish: tries to buy an ATM put debit spread
    3. Fetches live MCX options chain via Fyers (falls back to
       Black-Scholes simulation when chain is unavailable)
    4. Logs everything to commodity_learning_trades table

Review via API:
  GET /commodity/trades   — all paper trades
  GET /commodity/stats    — win rate, avg R, per-instrument breakdown
  GET /commodity/chain/{symbol}  — live chain snapshot for a symbol

MCX trading hours:
  Metals  (GOLD, SILVER, COPPER): 09:00–23:30 IST
  Energy  (CRUDEOIL, NATGAS):     09:00–23:30 IST
  Agri    (not included here):    09:00–17:00 IST
"""

import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, date, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

IST     = ZoneInfo("Asia/Kolkata")
DB_PATH = "db/trades.db"

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# MCX COMMODITY METADATA
# ─────────────────────────────────────────────────────────────────

MCX_MARKET_OPEN  = dtime(9, 0)
MCX_MARKET_CLOSE = dtime(23, 30)
# Stop taking new entries 30 min before close (spread widens)
MCX_ENTRY_CUTOFF = dtime(23, 0)

# symbol → {lot_size, strike_step, typical_iv, price_unit}
MCX_CONTRACTS = {
    "MCX:CRUDEOIL25JUNFUT": {
        "short":       "CRUDEOIL",
        "lot_size":    100,        # barrels per lot
        "strike_step": 100,        # INR per barrel
        "typical_iv":  0.40,       # ~40% historical
        "price_unit":  "INR/bbl",
        "min_price":   3000,
        "max_price":   12000,
    },
    "MCX:GOLD25JUNFUT": {
        "short":       "GOLD",
        "lot_size":    100,        # units (1 unit = 10g)
        "strike_step": 500,        # INR per 10g
        "typical_iv":  0.18,
        "price_unit":  "INR/10g",
        "min_price":   50000,
        "max_price":   120000,
    },
    "MCX:SILVER25JUNFUT": {
        "short":       "SILVER",
        "lot_size":    30,         # kg
        "strike_step": 1000,       # INR per kg
        "typical_iv":  0.28,
        "price_unit":  "INR/kg",
        "min_price":   60000,
        "max_price":   120000,
    },
    "MCX:COPPER25JUNFUT": {
        "short":       "COPPER",
        "lot_size":    2500,       # kg (1 lot = 2.5 MT)
        "strike_step": 10,         # INR per kg
        "typical_iv":  0.25,
        "price_unit":  "INR/kg",
        "min_price":   400,
        "max_price":   1200,
    },
    "MCX:NATURALGAS25JUNFUT": {
        "short":       "NATURALGAS",
        "lot_size":    1250,       # mmBtu
        "strike_step": 10,         # INR per mmBtu
        "typical_iv":  0.55,       # very volatile
        "price_unit":  "INR/mmBtu",
        "min_price":   100,
        "max_price":   600,
    },
}

ALL_MCX_SYMBOLS = list(MCX_CONTRACTS.keys())


# ─────────────────────────────────────────────────────────────────
# BLACK-SCHOLES HELPERS (simulation fallback)
# ─────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_price(spot, strike, iv, dte_days, opt_type="call", r=0.065) -> float:
    """Black-Scholes option price. r = India risk-free rate ~6.5%."""
    if dte_days <= 0 or iv <= 0 or spot <= 0:
        return 0.0
    T = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        if opt_type == "call":
            return spot * _norm_cdf(d1) - strike * math.exp(-r * T) * _norm_cdf(d2)
        else:
            return strike * math.exp(-r * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    except Exception:
        return 0.0


def _atm_strike(spot: float, step: int) -> int:
    """Round spot to nearest strike_step."""
    return int(round(spot / step) * step)


# ─────────────────────────────────────────────────────────────────
# COMMODITY OPTIONS LEARNING ENGINE
# ─────────────────────────────────────────────────────────────────

class CommodityOptionsLearning:
    """
    Standalone paper-trade engine for MCX commodity options.
    Run learning_cycle() every 60 seconds during MCX hours.
    """

    def __init__(self):
        self._open_positions: dict[str, dict] = {}  # key → trade
        self._chain_cache:    dict[str, tuple] = {}  # symbol → (data, fetched_at)
        self._init_db()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """Call once per minute during MCX hours."""
        now = datetime.now(tz=IST)
        if not (MCX_MARKET_OPEN <= now.time() <= MCX_MARKET_CLOSE):
            return

        from data.data_store import store

        # 1. Exit monitoring
        self._check_exits(store, now)

        # 2. New entry scan (stop 30 min before close)
        if now.time() <= MCX_ENTRY_CUTOFF:
            for symbol in ALL_MCX_SYMBOLS:
                self._evaluate(symbol, store, now)

    # ─────────────────────────────────────────────────────────────
    # STRATEGY LOGIC
    # ─────────────────────────────────────────────────────────────

    def _evaluate(self, symbol: str, store, now: datetime) -> None:
        meta = MCX_CONTRACTS[symbol]

        # Already have an open position for this symbol?
        if any(symbol in k for k in self._open_positions):
            return

        # Get futures price
        spot = store.get_ltp(symbol)
        if not spot or spot < meta["min_price"] or spot > meta["max_price"]:
            logger.debug(f"[CommOpts] {meta['short']} spot {spot} — invalid/unavailable")
            return

        # 1H trend check
        df = store.get_ohlcv(symbol, "1H", n=50)
        if df is None or len(df) < 20:
            logger.debug(f"[CommOpts] {meta['short']} insufficient 1H data")
            return

        direction, rsi_val, ema20_val = self._get_direction(df, spot)
        if not direction:
            return

        # Fetch options chain (or simulate)
        chain = self._get_chain(symbol)
        opt_type = "call" if direction == "LONG" else "put"

        trade = self._build_trade(
            symbol=symbol, meta=meta, spot=spot, direction=direction,
            opt_type=opt_type, chain=chain, rsi_val=rsi_val,
            ema20_val=ema20_val, now=now,
        )
        if trade:
            self._open_trade(trade)

    def _get_direction(self, df, spot: float):
        """Returns (direction, rsi_val, ema20_val) or (None, _, _)."""
        try:
            from analysis.indicators import rsi as calc_rsi, ema as calc_ema
            close    = df["close"]
            rsi_val  = calc_rsi(close).iloc[-1]
            ema20    = calc_ema(close, 20).iloc[-1]
            ema5     = calc_ema(close, 5).iloc[-1]

            # Bullish: price above EMA20, short EMA above long EMA, RSI 50-75
            if ema5 > ema20 and spot > ema20 and 50 < rsi_val < 75:
                return "LONG", round(rsi_val, 1), round(ema20, 2)
            # Bearish: price below EMA20, short EMA below long EMA, RSI 25-50
            if ema5 < ema20 and spot < ema20 and 25 < rsi_val < 50:
                return "SHORT", round(rsi_val, 1), round(ema20, 2)
        except Exception as exc:
            logger.debug(f"[CommOpts] Indicator error: {exc}")
        return None, 0, 0

    def _build_trade(
        self, symbol, meta, spot, direction, opt_type, chain,
        rsi_val, ema20_val, now,
    ) -> Optional[dict]:
        """Build a debit spread trade (buy ATM + sell OTM)."""
        step    = meta["strike_step"]
        iv      = meta["typical_iv"]
        lot     = meta["lot_size"]
        dte     = 21   # target ~3 weeks to expiry

        atm  = _atm_strike(spot, step)

        # OTM leg is 2 strikes away (defines spread width)
        otm  = (atm + 2 * step) if opt_type == "call" else (atm - 2 * step)

        # Try to read real prices from chain; fall back to BS
        atm_premium = otm_premium = None

        if chain:
            atm_premium, real_iv, nfo_sym = self._chain_lookup(
                chain, atm, opt_type, dte
            )
            if real_iv and real_iv > 0:
                iv = real_iv
            otm_premium, _, _ = self._chain_lookup(chain, otm, opt_type, dte)

        if atm_premium is None or atm_premium <= 0:
            atm_premium = _bs_price(spot, atm, iv, dte, opt_type)
        if otm_premium is None or otm_premium <= 0:
            otm_premium = _bs_price(spot, otm, iv, dte, opt_type)

        # Net debit = buy ATM − sell OTM credit
        net_debit  = round(atm_premium - otm_premium, 2)
        spread_width = abs(atm - otm)
        max_profit   = round(spread_width - net_debit, 2)

        if net_debit <= 0 or max_profit <= 0:
            logger.debug(f"[CommOpts] {meta['short']} invalid spread pricing: debit={net_debit}")
            return None

        rr = round(max_profit / net_debit, 2)
        if rr < 1.0:
            logger.debug(f"[CommOpts] {meta['short']} R:R {rr:.2f} too low")
            return None

        # Notional risk per lot
        risk_per_lot = round(net_debit * lot, 2)

        trade_id = f"COM-{uuid.uuid4().hex[:8].upper()}"
        return {
            "id":           trade_id,
            "symbol":       symbol,
            "instrument":   meta["short"],
            "direction":    direction,
            "opt_type":     opt_type,
            "strategy":     "CommodityDebitSpread",
            "entry_time":   now.isoformat(),
            "status":       "OPEN",
            "spot_at_entry": round(spot, 2),
            "atm_strike":   atm,
            "otm_strike":   otm,
            "net_debit":    net_debit,
            "max_profit":   max_profit,
            "spread_width": spread_width,
            "rr":           rr,
            "iv_used":      round(iv, 4),
            "lot_size":     lot,
            "risk_per_lot": risk_per_lot,
            "dte":          dte,
            "data_source":  "live_chain" if chain else "bs_estimate",
            "metadata": {
                "rsi":       rsi_val,
                "ema20":     ema20_val,
                "spot":      round(spot, 2),
                "iv_pct":    round(iv * 100, 1),
                "atm_prem":  round(atm_premium, 2),
                "otm_prem":  round(otm_premium, 2),
                "price_unit": meta["price_unit"],
            },
        }

    # ─────────────────────────────────────────────────────────────
    # EXIT MONITORING
    # ─────────────────────────────────────────────────────────────

    def _check_exits(self, store, now: datetime) -> None:
        closed = []
        for key, trade in list(self._open_positions.items()):
            symbol = trade["symbol"]
            spot   = store.get_ltp(symbol)
            if not spot:
                continue

            direction  = trade["direction"]
            entry_spot = trade["spot_at_entry"]
            net_debit  = trade["net_debit"]
            max_profit = trade["max_profit"]
            exit_reason = None
            pnl_approx  = None

            # Approximate option P&L from underlying move
            # Debit spread delta ≈ 0.3–0.4; use 0.35 as estimate
            spot_move = spot - entry_spot if direction == "LONG" else entry_spot - spot
            est_delta = 0.35
            est_pnl   = round(spot_move * est_delta, 2)  # per unit, not per lot

            # Exit at 50% profit or 100% loss (max loss = debit paid)
            if est_pnl >= max_profit * 0.50:
                exit_reason = "TARGET_50PCT"
                pnl_approx  = round(net_debit * 0.50, 2)
            elif est_pnl <= -net_debit:
                exit_reason = "MAX_LOSS"
                pnl_approx  = round(-net_debit, 2)
            elif now.time() >= dtime(23, 15):
                exit_reason = "EOD_MCX"
                pnl_approx  = round(est_pnl, 2)

            if exit_reason:
                pnl_r = round(pnl_approx / net_debit, 2) if net_debit > 0 else 0
                self._db_close(trade["id"], spot, exit_reason, pnl_approx, pnl_r)
                closed.append(key)
                logger.info(
                    f"[CommOpts] CLOSE {trade['id']} | {trade['instrument']} "
                    f"{exit_reason} spot={spot:.2f} est_pnl={pnl_approx:+.2f} "
                    f"({pnl_r:+.1f}R)"
                )

        for k in closed:
            del self._open_positions[k]

    def _open_trade(self, trade: dict) -> None:
        key = f"{trade['symbol']}:{trade['direction']}"
        self._open_positions[key] = trade
        self._db_insert(trade)
        logger.info(
            f"[CommOpts] OPEN {trade['id']} | {trade['instrument']} "
            f"{trade['direction']} debit spread | "
            f"spot={trade['spot_at_entry']:.2f} "
            f"ATM={trade['atm_strike']} OTM={trade['otm_strike']} "
            f"debit={trade['net_debit']:.2f} maxP={trade['max_profit']:.2f} "
            f"R:R={trade['rr']:.2f} | src={trade['data_source']}"
        )

    # ─────────────────────────────────────────────────────────────
    # FYERS CHAIN FETCH
    # ─────────────────────────────────────────────────────────────

    def _get_chain(self, symbol: str) -> Optional[dict]:
        """Try to fetch MCX options chain from Fyers. Returns None on failure."""
        now = datetime.now(tz=IST)
        if symbol in self._chain_cache:
            cached, fetched_at = self._chain_cache[symbol]
            if (now - fetched_at).total_seconds() < 120:
                return cached

        try:
            from execution.fyers_broker import fyers_broker
            if not fyers_broker._initialised or not fyers_broker._client:
                return None

            resp = fyers_broker._client.optionchain(data={
                "symbol":      symbol,
                "strikecount": 10,
                "timestamp":   "",
            })

            if resp.get("s") != "ok":
                logger.debug(f"[CommOpts] Chain fetch failed for {symbol}: {resp.get('message')}")
                return None

            data = resp.get("data", {})
            self._chain_cache[symbol] = (data, now)
            logger.info(f"[CommOpts] Live chain fetched for {symbol}")
            return data

        except Exception as e:
            logger.debug(f"[CommOpts] Chain exception for {symbol}: {e}")
            return None

    def _chain_lookup(
        self, chain: dict, strike: int, opt_type: str, target_dte: int
    ) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Find a specific strike/type in chain data.
        Returns (ltp, iv, nfo_symbol) or (None, None, None).
        """
        try:
            expiries = chain.get("expiryData", [])
            if not expiries:
                return None, None, None

            # Pick expiry closest to target_dte
            from datetime import datetime as dt
            today = dt.now(tz=IST).date()
            best_expiry = None
            best_diff   = 9999

            for exp in expiries:
                expiry_str = exp.get("expiry", "")
                try:
                    exp_date = dt.strptime(expiry_str, "%Y-%m-%d").date()
                    diff = abs((exp_date - today).days - target_dte)
                    if diff < best_diff:
                        best_diff, best_expiry = diff, exp
                except Exception:
                    continue

            if not best_expiry:
                return None, None, None

            field = "CE" if opt_type == "call" else "PE"
            for row in best_expiry.get("optionsChain", []):
                if abs(row.get("strikePrice", 0) - strike) < 1:
                    leg = row.get(field, {})
                    ltp = leg.get("ltp") or leg.get("close_price")
                    iv  = leg.get("iv", 0)
                    sym = leg.get("symbol", "")
                    if ltp and ltp > 0:
                        return float(ltp), float(iv) if iv else None, sym
        except Exception as exc:
            logger.debug(f"[CommOpts] Chain lookup error: {exc}")
        return None, None, None

    # ─────────────────────────────────────────────────────────────
    # DATABASE
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commodity_learning_trades (
                    id              TEXT PRIMARY KEY,
                    symbol          TEXT,
                    instrument      TEXT,
                    direction       TEXT,
                    opt_type        TEXT,
                    strategy        TEXT,
                    spot_at_entry   REAL,
                    spot_at_exit    REAL DEFAULT 0,
                    atm_strike      INTEGER,
                    otm_strike      INTEGER,
                    net_debit       REAL,
                    max_profit      REAL,
                    spread_width    REAL,
                    rr              REAL,
                    iv_used         REAL,
                    lot_size        INTEGER,
                    risk_per_lot    REAL,
                    dte             INTEGER,
                    pnl_approx      REAL DEFAULT 0,
                    pnl_r           REAL DEFAULT 0,
                    status          TEXT DEFAULT 'OPEN',
                    exit_reason     TEXT DEFAULT '',
                    data_source     TEXT DEFAULT 'bs_estimate',
                    entry_time      TEXT,
                    exit_time       TEXT DEFAULT '',
                    metadata        TEXT DEFAULT '{}'
                )
            """)
        logger.info("[CommOpts] DB table ready")

    def _db_insert(self, t: dict) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO commodity_learning_trades
                (id, symbol, instrument, direction, opt_type, strategy,
                 spot_at_entry, atm_strike, otm_strike, net_debit, max_profit,
                 spread_width, rr, iv_used, lot_size, risk_per_lot, dte,
                 status, data_source, entry_time, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t["id"], t["symbol"], t["instrument"], t["direction"],
                t["opt_type"], t["strategy"], t["spot_at_entry"],
                t["atm_strike"], t["otm_strike"], t["net_debit"],
                t["max_profit"], t["spread_width"], t["rr"], t["iv_used"],
                t["lot_size"], t["risk_per_lot"], t["dte"],
                t["status"], t["data_source"], t["entry_time"],
                json.dumps(t.get("metadata", {})),
            ))

    def _db_close(
        self, trade_id: str, exit_spot: float,
        reason: str, pnl: float, pnl_r: float,
    ) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                UPDATE commodity_learning_trades
                SET spot_at_exit=?, exit_reason=?, pnl_approx=?, pnl_r=?,
                    status='CLOSED', exit_time=?
                WHERE id=?
            """, (
                exit_spot, reason,
                round(pnl, 2), round(pnl_r, 2),
                datetime.now(tz=IST).isoformat(),
                trade_id,
            ))

    # ─────────────────────────────────────────────────────────────
    # READ API
    # ─────────────────────────────────────────────────────────────

    def get_trades(self, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM commodity_learning_trades "
                    "WHERE status=? ORDER BY entry_time DESC LIMIT ?",
                    (status.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM commodity_learning_trades "
                    "ORDER BY entry_time DESC LIMIT ?",
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
        trades = self.get_trades(status="CLOSED", limit=1000)
        open_t = self.get_trades(status="OPEN")
        if not trades:
            return {
                "total_closed": 0,
                "total_open":   len(open_t),
                "message": "No closed commodity option trades yet.",
            }

        all_r  = [t["pnl_r"] for t in trades]
        wins   = [r for r in all_r if r > 0]

        by_instrument: dict = {}
        for t in trades:
            ins = t["instrument"]
            if ins not in by_instrument:
                by_instrument[ins] = {"total": 0, "wins": 0, "total_r": 0.0}
            by_instrument[ins]["total"]   += 1
            by_instrument[ins]["total_r"] += t["pnl_r"]
            if t["pnl_r"] > 0:
                by_instrument[ins]["wins"] += 1

        for ins, d in by_instrument.items():
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1)
            d["avg_r"]    = round(d["total_r"] / d["total"], 2)

        return {
            "total_closed":   len(trades),
            "total_open":     len(open_t),
            "win_rate_pct":   round(len(wins) / len(trades) * 100, 1),
            "avg_r":          round(sum(all_r) / len(all_r), 2),
            "total_r":        round(sum(all_r), 2),
            "best_trade_r":   round(max(all_r), 2),
            "worst_trade_r":  round(min(all_r), 2),
            "by_instrument":  by_instrument,
            "data_sources":   _count_field(trades, "data_source"),
            "exit_reasons":   _count_field(trades, "exit_reason"),
            "open_positions": [
                {k: v for k, v in t.items() if k != "metadata"}
                for t in open_t
            ],
        }

    def get_chain_snapshot(self, symbol: str) -> dict:
        """Return a human-readable chain snapshot for the dashboard."""
        chain = self._get_chain(symbol)
        spot  = None
        try:
            from data.data_store import store
            spot = store.get_ltp(symbol)
        except Exception:
            pass

        if not chain:
            meta = MCX_CONTRACTS.get(symbol, {})
            iv   = meta.get("typical_iv", 0.35)
            step = meta.get("strike_step", 100)
            atm  = _atm_strike(spot or 0, step) if spot else 0
            return {
                "source":   "bs_estimate",
                "symbol":   symbol,
                "spot":     spot,
                "atm":      atm,
                "note":     "Live chain unavailable — showing Black-Scholes estimates",
                "strikes": [
                    {
                        "strike": atm + (i - 3) * step,
                        "call":   round(_bs_price(spot or atm, atm + (i - 3) * step, iv, 21, "call"), 2),
                        "put":    round(_bs_price(spot or atm, atm + (i - 3) * step, iv, 21, "put"),  2),
                    }
                    for i in range(7)
                ] if spot else [],
            }

        return {
            "source":            "live_chain",
            "symbol":            symbol,
            "spot":              chain.get("underlyingValue"),
            "expiry_count":      len(chain.get("expiryData", [])),
            "nearest_expiry":    chain.get("expiryData", [{}])[0].get("expiry") if chain.get("expiryData") else None,
            "strikes_available": len(chain.get("expiryData", [{}])[0].get("optionsChain", [])) if chain.get("expiryData") else 0,
        }


def _count_field(trades: list[dict], field: str) -> dict:
    c: dict = {}
    for t in trades:
        v = t.get(field, "unknown")
        c[v] = c.get(v, 0) + 1
    return c


# Module-level singleton
commodity_options = CommodityOptionsLearning()
