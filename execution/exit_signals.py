"""
exit_signals.py
───────────────
SINGLE SOURCE for STRUCTURAL exits — the market-structure reversal/weakening checks
that close a trade which went into profit but is turning over, BEFORE it round-trips
to the stop. This is the "exit the moment the trend reverses" logic a manual trader
applies; SL/TP/EOD stay as the hard backstops.

Evaluated on the UNDERLYING OHLCV (the index for options, the symbol for equity) so
the decision reflects real price structure, not option-premium noise. Used by every
engine (learning_engine, position_manager, commodity) via one call — no duplication.

Families (each independently toggleable in config/exit_signals.json):
  ATR_TRAIL      — chandelier: exit when price falls peak − atr_mult×ATR (volatility-aware)
  SWING_BREAK    — exit when price closes below the recent swing low (structure break)
  TREND_BREAK    — exit when price closes back below the fast EMA after riding above it
  MOMENTUM_FADE  — exit when momentum rolls over (RSI peaked high and turning down)
  STAGNATION     — exit when N bars pass with no new high (move has stalled; theta drag)

All families ARM only once the trade is in profit by `arm_profit_pct` of the entry
reference — so a fresh entry is never choked, and we only protect an open gain.
SHORT positions mirror every rule.
"""
import json
import os
from typing import Optional

import pandas as pd

from analysis.indicators import ema as _ema, rsi as _rsi, atr as _atr

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "config", "exit_signals.json")


def _load_cfg() -> dict:
    try:
        with open(_CFG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False}


_CFG = _load_cfg()


def config() -> dict:
    return _CFG


def structural_exit(df: Optional[pd.DataFrame], direction: str, entry_ref: float,
                    *, cfg: Optional[dict] = None) -> Optional[str]:
    """Return a structural-exit reason (or None) for a trade on the given underlying.

      df        : underlying OHLCV, CLOSED bars (columns: open/high/low/close).
      direction : "LONG" or "SHORT".
      entry_ref : entry price of the underlying (index spot / equity entry) — used to
                  gate on profit so structural exits only arm once we're in the money.
    Reason ∈ {ATR_TRAIL, SWING_BREAK, TREND_BREAK, MOMENTUM_FADE, STAGNATION}.
    """
    c = cfg or _CFG
    if not c.get("enabled", False):
        return None
    if df is None or len(df) < int(c.get("min_bars", 20)) or entry_ref <= 0:
        return None
    try:
        o, h, l, cl = df["open"], df["high"], df["low"], df["close"]
        price = float(cl.iloc[-1])
        long = (direction or "LONG").upper() == "LONG"

        # ── arm only once in profit ──
        profit_pct = (price - entry_ref) / entry_ref if long else (entry_ref - price) / entry_ref
        if profit_pct < float(c.get("arm_profit_pct", 0.0)):
            return None

        # ── ATR trail (chandelier) ──
        a = c.get("atr_trail", {})
        if a.get("enabled"):
            atr_v = float(_atr(df, int(a.get("period", 14))).iloc[-1])
            pw = int(a.get("peak_window", 40))
            mult = float(a.get("mult", 2.5))
            if atr_v > 0:
                if long:
                    peak = float(h.iloc[-pw:].max())
                    if price <= peak - mult * atr_v:
                        return "ATR_TRAIL"
                else:
                    trough = float(l.iloc[-pw:].min())
                    if price >= trough + mult * atr_v:
                        return "ATR_TRAIL"

        # ── swing-structure break ──
        s = c.get("swing_break", {})
        if s.get("enabled"):
            lb = int(s.get("lookback", 5))
            if len(df) > lb:
                if long:
                    swing_low = float(l.iloc[-(lb + 1):-1].min())
                    if price < swing_low:
                        return "SWING_BREAK"
                else:
                    swing_high = float(h.iloc[-(lb + 1):-1].max())
                    if price > swing_high:
                        return "SWING_BREAK"

        # ── trend break (close back through the fast EMA) ──
        t = c.get("trend_break", {})
        if t.get("enabled"):
            e = _ema(cl, int(t.get("ema", 9)))
            ep, en = float(e.iloc[-2]), float(e.iloc[-1])
            cp = float(cl.iloc[-2])
            if long and cp >= ep and price < en:
                return "TREND_BREAK"
            if (not long) and cp <= ep and price > en:
                return "TREND_BREAK"

        # ── momentum fade (RSI peaked then rolling over) ──
        m = c.get("momentum_fade", {})
        if m.get("enabled"):
            r = _rsi(cl)
            roll = int(m.get("roll_bars", 2))
            hi = float(m.get("rsi_high", 60.0))
            if len(r) > roll + 1:
                window = r.iloc[-(roll + 2):]
                if long:
                    peaked = float(window.max()) >= hi
                    rolling = all(float(r.iloc[-i]) < float(r.iloc[-i - 1]) for i in range(1, roll + 1))
                    if peaked and rolling:
                        return "MOMENTUM_FADE"
                else:
                    troughed = float(window.min()) <= (100.0 - hi)
                    rolling = all(float(r.iloc[-i]) > float(r.iloc[-i - 1]) for i in range(1, roll + 1))
                    if troughed and rolling:
                        return "MOMENTUM_FADE"

        # ── stagnation (no new high/low in N bars — move stalled) ──
        g = c.get("stagnation", {})
        if g.get("enabled"):
            n = int(g.get("bars", 8))
            if len(df) >= 2 * n:
                if long:
                    if float(h.iloc[-n:].max()) <= float(h.iloc[-2 * n:-n].max()):
                        return "STAGNATION"
                else:
                    if float(l.iloc[-n:].min()) >= float(l.iloc[-2 * n:-n].min()):
                        return "STAGNATION"
    except Exception:
        return None
    return None


__all__ = ["structural_exit", "config"]
