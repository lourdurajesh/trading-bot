"""
base_strategy.py
────────────────
Abstract base class that all strategy modules inherit from.
Defines the Signal dataclass and the evaluate() interface.

Every strategy must:
  1. Inherit from BaseStrategy
  2. Implement evaluate(symbol) → Optional[Signal]
  3. Set self.name and self.timeframe

Signal objects flow:
  Strategy.evaluate() → RiskManager.validate() → OrderManager.route()
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Optional

import pandas as pd

from data.data_store import store

logger = logging.getLogger(__name__)


class Direction(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


class SignalType(str, Enum):
    EQUITY  = "EQUITY"    # straight buy/sell of stock
    OPTIONS = "OPTIONS"   # options order — see options_meta


@dataclass
class Signal:
    """
    A trade signal produced by a strategy module.
    Passed to RiskManager for validation, then OrderManager for execution.
    """
    # Identity
    symbol:       str
    strategy:     str               # name of the strategy that generated this
    direction:    Direction
    signal_type:  SignalType = SignalType.EQUITY

    # Price levels (set by strategy)
    entry:        float = 0.0
    stop_loss:    float = 0.0
    target_1:     float = 0.0
    target_2:     float = 0.0       # optional second target
    target_3:     float = 0.0       # optional third target

    # Confidence & metadata
    confidence:   float = 0.0       # 0.0 – 1.0
    risk_reward:  float = 0.0       # calculated by strategy
    timeframe:    str   = "1H"
    regime:       str   = ""        # regime at signal time
    reason:       str   = ""        # human-readable explanation

    # Options-specific (populated by options strategies)
    options_meta: dict  = field(default_factory=dict)

    # Filled by RiskManager
    position_size:   int   = 0      # shares / lots
    capital_at_risk: float = 0.0    # INR / USD

    # Lifecycle
    created_at:   datetime = field(default_factory=lambda: datetime.now(tz=IST))
    expires_at:   Optional[datetime] = None   # signal expires if not acted on

    def is_valid(self) -> bool:
        """Basic sanity check before passing to risk manager."""
        if self.entry <= 0 or self.stop_loss <= 0:
            return False
        if self.signal_type == SignalType.OPTIONS:
            strategy_type = (self.options_meta or {}).get("strategy", "")
            if strategy_type in ("short_strangle", "iron_condor"):
                # Short premium: entry = credit received; stop = 2× credit (value rises = loss)
                if self.stop_loss <= self.entry:
                    return False
            else:
                # Debit spread: entry = premium paid; stop < entry (exit when premium decays 50%)
                if self.stop_loss >= self.entry:
                    return False
        else:
            if self.direction == Direction.LONG and self.stop_loss >= self.entry:
                return False
            if self.direction == Direction.SHORT and self.stop_loss <= self.entry:
                return False
        if self.target_1 <= 0:
            return False
        if self.confidence < 0.0 or self.confidence > 1.0:
            return False
        return True

    def calculate_rr(self) -> float:
        """Calculate and store Risk:Reward ratio."""
        risk   = abs(self.entry - self.stop_loss)
        reward = abs(self.target_1 - self.entry)
        if risk == 0:
            return 0.0
        self.risk_reward = round(reward / risk, 2)
        return self.risk_reward

    def to_dict(self) -> dict:
        """Serialise to dict for dashboard API / logging."""
        return {
            "symbol":        self.symbol,
            "strategy":      self.strategy,
            "direction":     self.direction.value,
            "signal_type":   self.signal_type.value,
            "entry":         self.entry,
            "stop_loss":     self.stop_loss,
            "target_1":      self.target_1,
            "target_2":      self.target_2,
            "confidence":    self.confidence,
            "risk_reward":   self.risk_reward,
            "timeframe":     self.timeframe,
            "regime":        self.regime,
            "reason":        self.reason,
            "position_size": self.position_size,
            "created_at":    self.created_at.isoformat(),
        }


class BaseStrategy(ABC):
    """
    Abstract base for all strategy modules.

    Subclasses implement evaluate(symbol) and return a Signal or None.
    """

    def __init__(self):
        self.name:          str  = "BaseStrategy"
        self.timeframe:     str  = "1H"        # primary signal timeframe
        self.confirm_tf:    str  = "1D"       # confirmation timeframe
        self.enabled:       bool = True
        self.backtest_mode: bool = False       # set True by BacktestEngine to skip live-only guards

    # ─────────────────────────────────────────────────────────────
    # ABSTRACT — subclasses must implement
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    def evaluate(self, symbol: str) -> Optional[Signal]:
        """
        Analyse the symbol and return a Signal if conditions are met.
        Return None if no trade setup found.
        """
        ...

    # ─────────────────────────────────────────────────────────────
    # SHARED HELPERS — available to all strategies
    # ─────────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, timeframe: str = None, n: int = 200) -> Optional[pd.DataFrame]:
        """Fetch OHLCV from DataStore. Returns None if insufficient data."""
        tf = timeframe or self.timeframe
        df = store.get_ohlcv(symbol, tf, n=n)
        if df is None or len(df) < 50:
            logger.debug(f"{self.name}: insufficient data for {symbol} [{tf}]")
            return None
        return df

    def get_multi_tf(self, symbol: str) -> dict[str, Optional[pd.DataFrame]]:
        """Fetch signal TF + confirmation TF in one call."""
        return {
            self.timeframe:  self.get_ohlcv(symbol, self.timeframe),
            self.confirm_tf: self.get_ohlcv(symbol, self.confirm_tf),
        }

    def get_ltp(self, symbol: str) -> Optional[float]:
        return store.get_ltp(symbol)

    def log_signal(self, signal: Signal) -> None:
        logger.info(
            f"[{self.name}] SIGNAL {signal.direction.value} {signal.symbol} | "
            f"Entry: {signal.entry:.2f} | SL: {signal.stop_loss:.2f} | "
            f"T1: {signal.target_1:.2f} | RR: {signal.risk_reward:.1f} | "
            f"Conf: {signal.confidence:.0%} | {signal.reason}"
        )

    def log_skip(self, symbol: str, reason: str) -> None:
        logger.debug(f"[{self.name}] SKIP {symbol}: {reason}")
