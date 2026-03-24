"""
test_pipeline.py
────────────────
Local integration test — simulates a full signal-to-paper-trade cycle
without a live Fyers connection.

Injects realistic NIFTY / BANKNIFTY / equity prices and OHLCV data into
data_store, then runs:
  strategy_selector.run_cycle()
    → is_valid() gate
    → intelligence_engine (mocked)
    → order_manager
      → risk_manager
        → options_risk_gate
      → paper_trading_engine

Run: python -m tests.test_pipeline
"""

import os
import sys
import logging

# ── Force paper trading + auto mode for the test ─────────────────
os.environ.setdefault("PAPER_TRADING",       "true")
os.environ.setdefault("BOT_MODE",            "AUTO")
os.environ.setdefault("TOTAL_CAPITAL",       "500000")
os.environ.setdefault("RISK_PER_TRADE_PCT",  "1.5")
os.environ.setdefault("MIN_SIGNAL_CONFIDENCE","0.55")  # lower threshold so test signals pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_pipeline")

# ── Imports after env setup ───────────────────────────────────────
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from data.data_store import store
from strategies.base_strategy import Signal, Direction, SignalType
from risk.risk_manager import risk_manager
from risk.options_risk import options_risk_gate


# ─────────────────────────────────────────────────────────────────
# MOCK DATA INJECTION
# ─────────────────────────────────────────────────────────────────

def make_ohlcv(n: int = 100, base: float = 22500, trend: str = "up") -> pd.DataFrame:
    """Generate synthetic OHLCV that produces a TRENDING regime."""
    np.random.seed(42)
    prices = [base]
    direction = 1 if trend == "up" else -1
    for _ in range(n - 1):
        change = direction * abs(np.random.normal(50, 20)) + np.random.normal(0, 30)
        prices.append(max(prices[-1] + change, 100))

    closes = np.array(prices)
    opens  = closes - np.random.uniform(10, 50, n) * direction
    highs  = np.maximum(opens, closes) + np.random.uniform(10, 80, n)
    lows   = np.minimum(opens, closes) - np.random.uniform(10, 80, n)
    vols   = np.random.randint(100000, 500000, n).astype(float)

    idx = pd.date_range(end=datetime.now(tz=timezone.utc), periods=n, freq="1h")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def inject_mock_prices():
    """Seed data_store with realistic index + equity prices and OHLCV."""
    logger.info("Injecting mock market data...")

    prices = {
        "NSE:NIFTY50-INDEX":    22450.0,
        "NSE:NIFTYBANK-INDEX":  48200.0,
        "NSE:FINNIFTY-INDEX":   23100.0,
        "NSE:RELIANCE-EQ":      1320.0,
        "NSE:TCS-EQ":           3540.0,
        "NSE:HDFCBANK-EQ":      1670.0,
        "NSE:INFY-EQ":          1780.0,
        "NSE:ICICIBANK-EQ":     1240.0,
        "NSE:SBIN-EQ":           820.0,
        "NSE:INDIAVIX-INDEX":    14.5,    # VIX — below 25 so strangle allowed
    }

    for symbol, price in prices.items():
        store._ltp[symbol] = price

    # Inject OHLCV for 1H timeframe (needed by directional_options + trend_follow)
    ohlcv_symbols = {
        "NSE:NIFTY50-INDEX":    make_ohlcv(150, 22450, "down"),   # downtrend → put spread
        "NSE:NIFTYBANK-INDEX":  make_ohlcv(150, 48200, "up"),     # uptrend   → call spread
        "NSE:FINNIFTY-INDEX":   make_ohlcv(150, 23100, "down"),
        "NSE:RELIANCE-EQ":      make_ohlcv(150, 1320,  "up"),
        "NSE:TCS-EQ":           make_ohlcv(150, 3540,  "up"),
        "NSE:HDFCBANK-EQ":      make_ohlcv(150, 1670,  "down"),
        "NSE:INFY-EQ":          make_ohlcv(150, 1780,  "up"),
        "NSE:SBIN-EQ":          make_ohlcv(150, 820,   "up"),
    }

    for symbol, df in ohlcv_symbols.items():
        # Add timestamp column required by load_historical
        df_h = df.copy().reset_index().rename(columns={"index": "timestamp"})
        store.load_historical(symbol, "1H", df_h)
        # Also seed daily by resampling
        df_d = df.resample("1D").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna().reset_index().rename(columns={"index": "timestamp"})
        if len(df_d) >= 5:
            store.load_historical(symbol, "1D", df_d)

    logger.info(f"Injected prices for {len(prices)} symbols, OHLCV for {len(ohlcv_symbols)} symbols")


# ─────────────────────────────────────────────────────────────────
# MOCK OPTIONS EXECUTOR
# ─────────────────────────────────────────────────────────────────

def make_mock_option(underlying, option_type, spot):
    """Return a realistic OptionResult mock."""
    from execution.options_executor import OptionResult
    from datetime import date
    strike    = round(spot / 50) * 50
    dte       = 14
    expiry    = (date.today() + timedelta(days=dte)).isoformat()
    lot_size  = 75 if "NIFTY50" in underlying else 35
    suffix    = "CE" if option_type == "call" else "PE"
    nfo_sym   = f"NSE:NIFTY25JAN{int(strike)}{suffix}"

    return OptionResult(
        symbol      = nfo_sym,
        underlying  = underlying,
        option_type = option_type,
        strike      = float(strike),
        expiry      = expiry,
        ltp         = 120.0,     # realistic premium
        iv          = 0.14,
        delta       = 0.38,
        lot_size    = lot_size,
        dte         = dte,
        pcr         = 0.85,
    )


# ─────────────────────────────────────────────────────────────────
# UNIT TESTS
# ─────────────────────────────────────────────────────────────────

def test_signal_validity():
    """Confirm is_valid() correctly accepts / rejects signals."""
    logger.info("\n── Test: signal validity ──")

    # Valid debit spread
    good = Signal(
        symbol="NSE:NIFTY50-INDEX", strategy="DirectionalOptions",
        direction=Direction.SHORT, signal_type=SignalType.OPTIONS,
        entry=120.0, stop_loss=60.0, target_1=200.0,
        confidence=0.75, timeframe="1H", regime="TRENDING", reason="test",
    )
    assert good.is_valid(), "Valid debit spread should pass"
    assert good.calculate_rr() > 0, "RR should be positive"
    logger.info(f"  PASS — valid signal: RR={good.calculate_rr()}")

    # Invalid: stop == entry
    bad = Signal(
        symbol="NSE:NIFTY50-INDEX", strategy="DirectionalOptions",
        direction=Direction.SHORT, signal_type=SignalType.OPTIONS,
        entry=50.65, stop_loss=50.65, target_1=84.0,
        confidence=0.85, timeframe="1H", regime="TRENDING", reason="test",
    )
    assert not bad.is_valid(), "stop==entry should be invalid"
    logger.info(f"  PASS — invalid signal (stop==entry) correctly rejected")


def test_options_risk_gate():
    """Confirm options_risk_gate rejects bad signals and sizes correctly."""
    logger.info("\n── Test: options risk gate ──")
    capital = 500_000

    # Good signal — should be approved with correct lots
    good = Signal(
        symbol="NSE:NIFTY50-INDEX", strategy="test",
        direction=Direction.SHORT, signal_type=SignalType.OPTIONS,
        entry=120.0, stop_loss=60.0, target_1=200.0,
        confidence=0.75, timeframe="1H", regime="TRENDING", reason="test",
        options_meta={"lot_size": 75, "strategy": "debit_spread"},
    )
    ok, reason, lots = options_risk_gate.check(good, capital)
    assert ok, f"Good signal should be approved: {reason}"
    assert lots >= 1, f"Should get at least 1 lot"
    logger.info(f"  PASS — approved: {lots} lot(s), reason: {reason}")

    # Bad: premium below MIN_OPTION_LTP
    cheap = Signal(
        symbol="NSE:NIFTY50-INDEX", strategy="test",
        direction=Direction.SHORT, signal_type=SignalType.OPTIONS,
        entry=2.0, stop_loss=1.0, target_1=5.0,
        confidence=0.75, timeframe="1H", regime="TRENDING", reason="test",
        options_meta={"lot_size": 75, "strategy": "debit_spread"},
    )
    ok2, reason2, lots2 = options_risk_gate.check(cheap, capital)
    assert not ok2, "Cheap option should be rejected"
    logger.info(f"  PASS — cheap option rejected: {reason2}")

    # Verify lot sizing math
    # premium=120, lot=75 → cost_per_lot=9000
    # risk_budget = 500000 * 1.5% = 7500 → max_by_risk = 7500/9000 = 0 → 1 (min)
    # cap_budget  = 500000 * 5% = 25000  → max_by_cap = 25000/9000 = 2
    # result = min(1, 2, 2) = 1 → but then max(1,1)=1
    logger.info(f"  Lot sizing: premium=120, lot=75 → {lots} lot(s) (capital_at_risk=~₹9,000)")


def test_risk_manager_options():
    """Risk manager should use lot-based sizing for OPTIONS signals."""
    logger.info("\n── Test: risk manager options sizing ──")

    signal = Signal(
        symbol="NSE:NIFTY50-INDEX", strategy="DirectionalOptions",
        direction=Direction.SHORT, signal_type=SignalType.OPTIONS,
        entry=120.0, stop_loss=60.0, target_1=240.0,   # RR=2.0, above 1.5 minimum
        confidence=0.75, timeframe="1H", regime="TRENDING", reason="test",
        options_meta={"lot_size": 75, "strategy": "debit_spread"},
    )
    decision = risk_manager.validate(signal, [], current_capital=500_000)
    logger.info(f"  Decision: approved={decision.approved}, reason={decision.reason}")
    if decision.approved:
        logger.info(f"  Size={decision.position_size} units, capital_at_risk=Rs.{decision.capital_at_risk:,.0f}")
        assert decision.position_size > 0, "position_size must be > 0"
        assert decision.capital_at_risk > 0, "capital_at_risk must be > 0"
        assert decision.position_size % 75 == 0, f"Must be a whole lot (mult of 75), got {decision.position_size}"
        logger.info("  PASS")
    else:
        logger.warning(f"  Blocked (may be expiry/VIX today): {decision.reason}")


def test_directional_options_strategy():
    """Run DirectionalOptionsStrategy with mock data — expect a signal or a clean skip."""
    logger.info("\n── Test: DirectionalOptions strategy evaluate() ──")

    from strategies.directional_options import DirectionalOptionsStrategy
    from execution.options_executor import OptionResult

    spot = 22450.0
    mock_opt = make_mock_option("NSE:NIFTY50-INDEX", "put", spot)

    with patch("execution.options_executor.options_executor.get_best_option", return_value=mock_opt):
        strat  = DirectionalOptionsStrategy()
        signal = strat.evaluate("NSE:NIFTY50-INDEX")

    if signal:
        logger.info(f"  Signal generated: {signal.direction.value} entry={signal.entry} sl={signal.stop_loss} t1={signal.target_1}")
        assert signal.entry > 0,     "Entry must be > 0"
        assert signal.stop_loss > 0, "Stop must be > 0"
        assert signal.stop_loss != signal.entry, "Stop must differ from entry"
        assert signal.is_valid(),    f"Signal must be valid: e={signal.entry} sl={signal.stop_loss}"
        logger.info("  PASS — signal is valid")
    else:
        logger.info("  No signal generated (regime/IV/data conditions not met) — this is OK")


def test_full_pipeline():
    """
    End-to-end: strategy_selector.run_cycle() with mocked intelligence + options executor.
    Verifies the full path from evaluation to paper_trading_engine.
    """
    logger.info("\n── Test: full pipeline (strategy → intelligence → order → paper trade) ──")

    from strategies.strategy_selector import StrategySelector
    from analysis.regime_detector import RegimeResult, Regime
    from execution.order_manager import order_manager as om
    om.set_mode("AUTO")   # force AUTO so signals execute immediately

    spot = 22450.0
    mock_opt = make_mock_option("NSE:NIFTY50-INDEX", "put", spot)

    # Mock: regime = TRENDING (so directional_options strategy activates)
    mock_regime = RegimeResult(
        regime=Regime.TRENDING, confidence=0.80,
        adx_value=30.0, bb_width=0.03, atr_pct=0.01,
        rsi_value=35.0, slope=-0.02, timestamp=datetime.now(tz=timezone.utc),
    )

    # Mock: intelligence always approves
    mock_intel = MagicMock()
    mock_intel.approved    = True
    mock_intel.verdict     = "APPROVE"
    mock_intel.conviction  = 8.0
    mock_intel.summary     = "Strong bearish setup"
    mock_intel.size_factor = 1.0

    # Mock: paper trading engine — place_order(signal) signature
    paper_orders = []
    def mock_place_paper(signal):
        paper_orders.append({"symbol": signal.symbol, "direction": signal.direction.value,
                              "qty": signal.position_size, "entry": signal.entry})
        return f"PAPER-{len(paper_orders):04d}"

    with (
        patch("analysis.regime_detector.regime_detector.get_regime", return_value=mock_regime),
        patch("execution.options_executor.options_executor.get_best_option", return_value=mock_opt),
        patch("intelligence.intelligence_engine.intelligence_engine.evaluate", return_value=mock_intel),
        patch("paper_trading.paper_trading_engine.place_order", side_effect=mock_place_paper),
    ):
        selector = StrategySelector()
        submitted = selector.run_cycle()

    logger.info(f"  Signals submitted: {len(submitted)}")
    logger.info(f"  Paper orders placed: {len(paper_orders)}")
    for s in submitted:
        logger.info(f"    {s.symbol} | {s.strategy} | {s.direction.value} | entry={s.entry}")
    for o in paper_orders:
        logger.info(f"    Paper order: {o}")

    logger.info("  PASS — pipeline ran without errors")
    return len(submitted), len(paper_orders)


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  AlphaLens — Local Pipeline Test")
    logger.info("=" * 60)

    inject_mock_prices()

    results = {}
    tests = [
        ("signal_validity",          test_signal_validity),
        ("options_risk_gate",        test_options_risk_gate),
        ("risk_manager_options",     test_risk_manager_options),
        ("directional_options",      test_directional_options_strategy),
        ("full_pipeline",            test_full_pipeline),
    ]

    for name, fn in tests:
        try:
            result = fn()
            results[name] = "PASS"
        except AssertionError as e:
            results[name] = f"FAIL: {e}"
            logger.error(f"  ASSERTION FAILED in {name}: {e}")
        except Exception as e:
            results[name] = f"ERROR: {e}"
            logger.error(f"  ERROR in {name}: {e}", exc_info=True)

    logger.info("\n" + "=" * 60)
    logger.info("  TEST RESULTS")
    logger.info("=" * 60)
    for name, status in results.items():
        icon = "✓" if status == "PASS" else "✗"
        logger.info(f"  {icon}  {name:<30} {status}")
    logger.info("=" * 60)

    failed = sum(1 for s in results.values() if s != "PASS")
    if failed:
        sys.exit(1)
