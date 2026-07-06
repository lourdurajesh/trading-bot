"""
test_learning_pipeline.py
──────────────────────────
Local integration test for slice 6c — the learning book routed through the
SAME shared pipeline production uses (RiskManager -> OrderManager ->
PortfolioTracker -> PositionManager), instead of its own private
_open_trade/_check_exits loop.

This does NOT re-test strategy logic (test_pipeline.py + each strategy's own
tests cover that) — it verifies the WIRING: a hand-built Signal submitted via
learning_engine._submit() lands in the learning book's own tracker, sizes via
the learning book's own RiskManager, exits via the learning book's own
PositionManager, and none of that ever touches the LIVE book's globals.

Run: python -m tests.test_learning_pipeline
"""

import os
import sys
import logging

os.environ.setdefault("PAPER_TRADING",       "true")
os.environ.setdefault("BOT_MODE",            "AUTO")
os.environ.setdefault("TOTAL_CAPITAL",       "500000")
os.environ.setdefault("RISK_PER_TRADE_PCT",  "1.5")
os.environ.setdefault("MIN_SIGNAL_CONFIDENCE","0.55")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_learning_pipeline")

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from data.data_store import store
from strategies.base_strategy import Signal, Direction, SignalType

IST = ZoneInfo("Asia/Kolkata")
# order_manager._execute() blocks NSE entries outside 09:15-15:15 IST (a real,
# pre-existing production safeguard -- learning now correctly inherits it,
# since it bypassed order_manager entirely before slice 6c). Fix the clock so
# this test is not at the mercy of what time it happens to run.
_FAKE_MARKET_TIME = datetime(2026, 7, 6, 11, 0, 0, tzinfo=IST)


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FAKE_MARKET_TIME


def make_ohlcv(n: int = 60, base: float = 1300.0) -> pd.DataFrame:
    np.random.seed(7)
    closes = base + np.cumsum(np.random.normal(0, 2, n))
    opens  = closes - np.random.uniform(1, 3, n)
    highs  = np.maximum(opens, closes) + np.random.uniform(1, 4, n)
    lows   = np.minimum(opens, closes) - np.random.uniform(1, 4, n)
    vols   = np.random.randint(10000, 50000, n).astype(float)
    idx = pd.date_range(end=datetime.now(tz=timezone.utc), periods=n, freq="15min")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)


SYMBOL = "NSE:RELIANCE-EQ"


def inject_price(price: float) -> None:
    store._ltp[SYMBOL] = price


def test_book_isolation():
    """The learning book's RiskManager/tracker must be distinct instances from
    the LIVE globals, and LEARNING must skip the duplicate-symbol / max-open-
    positions rules the LIVE book enforces."""
    logger.info("\n── Test: book isolation ──")
    from learning_engine import learning_engine
    from risk.risk_manager import risk_manager as live_risk_manager
    from risk.portfolio_tracker import portfolio_tracker as live_tracker

    assert learning_engine._nse_risk_manager is not live_risk_manager, \
        "learning RiskManager must not be the LIVE singleton"
    assert learning_engine._nse_tracker is not live_tracker, \
        "learning tracker must not be the LIVE singleton"
    assert learning_engine._nse_risk_manager._book == "LEARNING"
    assert learning_engine._nse_tracker._book == "LEARNING"
    assert learning_engine._nse_tracker._segment == "live"   # schema name, not book identity
    assert learning_engine._nse_tracker._db_path.endswith("learning.db")
    assert learning_engine._nse_position_manager._book == "LEARNING"
    logger.info("  PASS — learning book uses its own RiskManager/tracker/PositionManager")


def test_entry_and_exit_via_shared_pipeline():
    """Submit a hand-built equity Signal through learning_engine._submit(), confirm
    it lands in the learning tracker sized by the learning RiskManager, then move
    price through the stop and confirm PositionManager closes it."""
    logger.info("\n── Test: entry + STOP exit through the shared pipeline ──")
    from learning_engine import learning_engine

    entry, stop, target = 1300.0, 1280.0, 1360.0
    inject_price(entry)
    store.load_historical(SYMBOL, "1H", make_ohlcv(60, entry).reset_index()
                         .rename(columns={"index": "timestamp"}))

    sig = Signal(
        symbol=SYMBOL, strategy="TrendFollow", direction=Direction.LONG,
        signal_type=SignalType.EQUITY, entry=entry, stop_loss=stop,
        target_1=target, confidence=0.75, timeframe="1H",
        regime="TRENDING", reason="smoke test",
    )
    sig.calculate_rr()
    assert sig.is_valid(), "hand-built signal should be template-valid"

    before = len(learning_engine._nse_tracker.get_open_positions())
    learning_engine._submit(sig, "TrendFollow_LRN")
    open_positions = learning_engine._nse_tracker.get_open_positions()
    assert len(open_positions) == before + 1, \
        f"expected a new open position, had {before}, now {len(open_positions)}"
    pos = next(p for p in open_positions if p["symbol"] == SYMBOL)
    assert pos["strategy"] == "TrendFollow_LRN"
    assert pos["position_size"] > 0, "RiskManager must have sized this trade"
    logger.info(f"  OPEN {pos['id']} | qty={pos['position_size']} entry={pos['entry_price']}")

    # Move price through the stop and let the shared PositionManager close it.
    inject_price(stop - 1.0)
    learning_engine._nse_position_manager.check_all()

    still_open = learning_engine._nse_tracker.get_open_positions()
    assert not any(p["id"] == pos["id"] for p in still_open), \
        "position should have closed on STOP"
    closed = learning_engine.get_trades(status="CLOSED", limit=10)
    match = next((t for t in closed if t["id"] == pos["id"]), None)
    assert match is not None, "closed trade should be readable via get_trades()"
    assert match["exit_reason"] == "STOP", f"expected STOP, got {match['exit_reason']}"
    # The schema-bridge check: new-pipeline rows store realised_pnl, not pnl_pts/
    # pnl_r/fees directly — get_trades() must derive them, not show zeros.
    assert match["pnl_pts"] != 0, "pnl_pts must be derived from entry/exit, not left at schema default 0"
    assert match["pnl_r"] < 0, "a STOP-out should show a negative R"
    assert match["qty"] == pos["position_size"], "qty must come from the real position_size"
    logger.info(f"  CLOSED {match['id']} | reason={match['exit_reason']} pnl_pts={match['pnl_pts']} pnl_r={match['pnl_r']} pnl_inr={match['pnl_inr']}")

    stats = learning_engine.get_stats()
    assert stats["total_closed"] >= 1
    assert "TrendFollow_LRN" in stats["by_strategy"]
    logger.info(f"  PASS — get_stats() win_rate={stats['win_rate_pct']}% avg_r={stats['avg_r']}")


def test_daily_pnl_does_not_leak_to_live():
    """Closing a learning position must update the LEARNING RiskManager's daily
    P&L, never the LIVE singleton's (the cross-book leak fixed in slice 6c-5)."""
    logger.info("\n── Test: daily P&L isolation on close ──")
    from learning_engine import learning_engine
    from risk.risk_manager import risk_manager as live_risk_manager

    live_pnl_before = live_risk_manager.daily_pnl
    learning_pnl_before = learning_engine._nse_risk_manager.daily_pnl

    entry, stop, target = 1300.0, 1280.0, 1360.0
    inject_price(entry)
    sig = Signal(
        symbol=SYMBOL, strategy="MeanReversion", direction=Direction.LONG,
        signal_type=SignalType.EQUITY, entry=entry, stop_loss=stop,
        target_1=target, confidence=0.75, timeframe="1H",
        regime="RANGING", reason="smoke test 2",
    )
    sig.calculate_rr()
    learning_engine._submit(sig, "MeanReversion_LRN")
    inject_price(stop - 1.0)
    learning_engine._nse_position_manager.check_all()

    assert live_risk_manager.daily_pnl == live_pnl_before, \
        "LIVE risk_manager's daily P&L must be untouched by a learning trade closing"
    assert learning_engine._nse_risk_manager.daily_pnl != learning_pnl_before, \
        "LEARNING risk_manager's daily P&L should have moved"
    logger.info(
        f"  PASS — live daily_pnl unchanged ({live_pnl_before}), "
        f"learning daily_pnl moved ({learning_pnl_before} -> {learning_engine._nse_risk_manager.daily_pnl})"
    )


def test_multi_strategy_same_symbol():
    """LEARNING must allow two strategies to hold the same symbol at once (the
    bake-off) — the duplicate-symbol reject only applies to LIVE/PAPER."""
    logger.info("\n── Test: multi-strategy same-symbol (bake-off) ──")
    from learning_engine import learning_engine

    inject_price(1300.0)
    sig_a = Signal(symbol=SYMBOL, strategy="SimpleMomentum", direction=Direction.LONG,
                   signal_type=SignalType.EQUITY, entry=1300.0, stop_loss=1280.0,
                   target_1=1360.0, confidence=0.75, timeframe="1H",
                   regime="TRENDING", reason="A")
    sig_b = Signal(symbol=SYMBOL, strategy="SimpleRSI", direction=Direction.LONG,
                   signal_type=SignalType.EQUITY, entry=1300.0, stop_loss=1285.0,
                   target_1=1330.0, confidence=0.75, timeframe="1H",
                   regime="TRENDING", reason="B")
    sig_a.calculate_rr(); sig_b.calculate_rr()

    before = len([p for p in learning_engine._nse_tracker.get_open_positions() if p["symbol"] == SYMBOL])
    learning_engine._submit(sig_a, "SimpleMomentum_LRN")
    learning_engine._submit(sig_b, "SimpleRSI_LRN")
    after = [p for p in learning_engine._nse_tracker.get_open_positions() if p["symbol"] == SYMBOL]
    assert len(after) == before + 2, \
        f"both strategies should hold {SYMBOL} at once, got {len(after)-before} new positions"
    logger.info(f"  PASS — {len(after)} concurrent positions on {SYMBOL}")


if __name__ == "__main__":
    tests = [
        test_book_isolation,
        test_entry_and_exit_via_shared_pipeline,
        test_daily_pnl_does_not_leak_to_live,
        test_multi_strategy_same_symbol,
    ]
    results = []
    with patch("execution.order_manager.datetime", _FakeDatetime), \
         patch("execution.position_manager.datetime", _FakeDatetime):
        for t in tests:
            try:
                t()
                results.append((t.__name__, "PASS", ""))
            except AssertionError as e:
                results.append((t.__name__, "FAIL", str(e)))
            except Exception as e:
                results.append((t.__name__, "ERROR", str(e)))

    logger.info("\n" + "=" * 60)
    logger.info("  TEST RESULTS")
    logger.info("=" * 60)
    ok = True
    for name, status, msg in results:
        mark = "✓" if status == "PASS" else "✗"
        logger.info(f"  {mark}  {name:38s} {status}{': ' + msg if msg else ''}")
        if status != "PASS":
            ok = False
    logger.info("=" * 60)
    sys.exit(0 if ok else 1)
