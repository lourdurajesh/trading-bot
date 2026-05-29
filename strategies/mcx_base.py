"""
mcx_base.py
───────────
Abstract base class for MCX commodity option strategies.

Design patterns:
  Template Method — MCXStrategy.evaluate() defines the algorithm skeleton;
                    concrete strategies only implement generate_signal().
  Strategy        — CommodityOptions holds a list[MCXStrategy] sorted by
                    priority and calls evaluate() until one returns a signal.

Contract for every concrete strategy:
  1. Inherit from MCXStrategy
  2. Declare @property name, priority, default_config
  3. Implement generate_signal(df, spot, now) → Optional[MCXSignalResult]

Session guards (holiday, opening blackout, entry cutoff) are enforced by
MCXSessionCalendar in run_cycle() BEFORE per-instrument evaluation starts.
Strategies must NOT duplicate those checks — generate_signal() is only
called when the session is confirmed open.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Optional

import pandas as pd
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# UNIFORM CONFIG CONTRACT
# ─────────────────────────────────────────────────────────────────────

@dataclass
class MCXStrategyConfig:
    """
    Uniform configuration for every MCX strategy.

    Read-only fields (identity/display) are set once in default_config and
    never written to the persistence file. Mutable fields (risk params) are
    persisted to config/commodity_strategy_config.json and can be updated
    at runtime via the /commodity/config API.

    All exit params are fractions of net_debit so SL/target distances
    scale with what was actually paid (cheap spread = tight SL).
    """

    # ── Read-only identity ──────────────────────────────────────────
    priority:             int    # evaluation order; 1 = first
    risk:                 str    # "LOW" | "MEDIUM" | "HIGH"
    risk_label:           str    # one-line description shown in dashboard
    risk_color:           str    # hex colour for dashboard badge

    # ── Cooldown (mutable) ──────────────────────────────────────────
    cooldown_enabled:     bool   = True
    cooldown_hours:       float  = 2.0

    # ── Exit parameters — fraction of net_debit (mutable) ───────────
    sl_debit_pct:         float  = 0.40   # stop loss
    trail_debit_pct:      float  = 0.30   # trailing SL distance below/above peak
    trail_trigger_pct:    float  = 0.50   # trailing activates once gain >= this
    target_pct:           float  = 0.65   # initial exit target
    target_upgraded_pct:  float  = 0.80   # raised target after strong move
    target_upgrade_mult:  float  = 2.0    # trigger: move >= mult x SL distance

    # ── Signal condition parameters (mutable) ─────────────────────
    # Each strategy only reads the fields relevant to its logic;
    # unused fields in the other strategies have no effect.
    #
    # TrendSpread
    rsi_long_min:         float  = 55.0   # LONG: RSI must be above this
    rsi_long_max:         float  = 68.0   # LONG: RSI must be below this (avoid overbought)
    rsi_short_min:        float  = 32.0   # SHORT: RSI must be above this (avoid oversold)
    rsi_short_max:        float  = 45.0   # SHORT: RSI must be below this
    ema_gap_min_pct:      float  = 0.3    # min EMA5-EMA20 separation % (noise filter)
    min_rvol_trend:       float  = 1.3    # RVOL floor — crossover on dead volume is noise
    min_adx_trend:        float  = 20.0   # ADX floor — filters EMA crossovers in ranging markets
    #
    # RSIReversalSpread
    rsi_oversold:         float  = 32.0   # LONG trigger: RSI below this = oversold
    rsi_overbought:       float  = 68.0   # SHORT trigger: RSI above this = overbought
    ema_proximity_pct:    float  = 0.5    # max EMA5-EMA20 divergence % (trend-intact check)
    #
    # BreakoutSpread
    breakout_lookback:    int    = 12     # bars used to define the consolidation range
    atr_buffer_mult:      float  = 0.30   # ATR multiplier for breakout confirmation buffer
    rsi_breakout_long:    float  = 55.0   # LONG breakout: RSI must exceed this (raised from 52 — real momentum)
    rsi_breakout_short:   float  = 45.0   # SHORT breakout: RSI must be below this (lowered from 48)
    min_rvol_breakout:    float  = 1.5    # RVOL floor — breakout without volume is a false breakout
    min_adx_breakout:     float  = 20.0   # ADX floor — filters ranging markets where range highs are noise

    # Fields that must never be written to the config file or updated via API
    _READONLY_FIELDS: ClassVar[frozenset] = frozenset(
        {"priority", "risk", "risk_label", "risk_color"}
    )

    def to_dict(self) -> dict:
        """Full dict for API responses (excludes private/ClassVar attributes)."""
        return {
            k: v for k, v in self.__dict__.items()
            if not k.startswith("_")
        }

    def mutable_dict(self) -> dict:
        """Only mutable fields — written to the config persistence file."""
        return {
            k: v for k, v in self.__dict__.items()
            if not k.startswith("_") and k not in self._READONLY_FIELDS
        }

    def apply_overrides(self, overrides: dict) -> None:
        """Apply a dict of mutable-field overrides (e.g. loaded from config file)."""
        for k, v in overrides.items():
            if k not in self._READONLY_FIELDS and hasattr(self, k) and not k.startswith("_"):
                setattr(self, k, v)


# ─────────────────────────────────────────────────────────────────────
# SIGNAL RESULT
# ─────────────────────────────────────────────────────────────────────

@dataclass
class MCXSignalResult:
    """
    Output from MCXStrategy.generate_signal().
    Returned to CommodityOptions._evaluate() for position sizing and trade construction.
    """
    direction:      str              # "LONG" | "SHORT"
    strategy_name:  str
    signal_reason:  str              # human-readable — logged and stored in DB
    rsi_val:        float
    ema5_val:       float
    ema20_val:      float
    breakout_level: Optional[float] = None   # set only by BreakoutSpreadStrategy


# ─────────────────────────────────────────────────────────────────────
# ABSTRACT BASE
# ─────────────────────────────────────────────────────────────────────

class MCXStrategy(ABC):
    """
    Abstract base for all MCX commodity option strategies.

    Template Method Pattern
    ───────────────────────
    evaluate()         Public entry point. Wraps generate_signal() in
                       error handling and logging. Do NOT override.

    generate_signal()  Hook. Implement the actual strategy logic here.
                       Only called when the session is confirmed open
                       (MCXSessionCalendar.get_status().allows_entry is True).
                       Must NOT check holidays, opening blackout, or cutoff.

    Strategy Pattern
    ────────────────
    CommodityOptions holds a list[MCXStrategy] sorted by priority and calls
    evaluate() in order — first non-None result wins.
    """

    def __init__(self) -> None:
        self._config: Optional[MCXStrategyConfig] = None

    # ── Abstract interface ───────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy name — must match commodity_strategy_config.json keys."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Evaluation priority. Lower number = evaluated first."""
        ...

    @property
    @abstractmethod
    def default_config(self) -> MCXStrategyConfig:
        """Factory-default config. Called once to seed self._config."""
        ...

    @abstractmethod
    def generate_signal(
        self,
        df:  pd.DataFrame,
        spot: float,
        now:  datetime,
    ) -> Optional[MCXSignalResult]:
        """
        Core strategy logic. Return MCXSignalResult if a setup is found, else None.

        Preconditions (guaranteed by the caller):
          - Session is confirmed open (MCXSessionCalendar.allows_entry == True)
          - df has at least 20 rows of 1H OHLCV data
          - spot is within instrument min/max_price range

        Do NOT check market hours, holidays, or opening blackout here.
        """
        ...

    # ── Template method ──────────────────────────────────────────────

    def evaluate(
        self,
        df:   pd.DataFrame,
        spot: float,
        now:  datetime,
    ) -> Optional[MCXSignalResult]:
        """
        Template method — calls generate_signal() and handles errors.
        Do NOT override in subclasses.
        """
        try:
            return self.generate_signal(df, spot, now)
        except Exception as exc:
            logger.debug(f"[{self.name}] evaluate() error: {exc}", exc_info=True)
            return None

    # ── Config management ────────────────────────────────────────────

    @property
    def config(self) -> MCXStrategyConfig:
        """Live config — default_config overlaid with any persisted overrides."""
        if self._config is None:
            self._config = self.default_config
        return self._config

    def apply_config_overrides(self, overrides: dict) -> None:
        """Push persisted overrides onto this strategy's config."""
        self.config.apply_overrides(overrides)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} priority={self.priority}>"
