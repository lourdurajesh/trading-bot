"""
dashboard_api.py
────────────────
FastAPI server — REST + WebSocket for the React dashboard.

Endpoints:
  GET  /stats              — portfolio stats
  GET  /positions          — open positions with live P&L
  GET  /signals/pending    — signals awaiting manual confirm
  POST /signals/{id}/confirm
  POST /signals/{id}/reject
  GET  /risk               — risk manager status
  POST /mode/{mode}        — switch AUTO / MANUAL
  POST /kill-switch/reset  — reset kill switch
  WS   /ws/live            — real-time push (ticks every 2s)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from data.data_store import store
from execution.order_manager import order_manager
from risk.portfolio_tracker import portfolio_tracker
from risk.risk_manager import risk_manager

logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaLens Trading Bot", version="1.0")

# Allow React dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Track connected WebSocket clients
_ws_clients: list[WebSocket] = []


# ─────────────────────────────────────────────────────────────────
# REST ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(tz=timezone.utc).isoformat()}


@app.get("/stats")
def get_stats():
    return portfolio_tracker.get_stats()


@app.get("/positions")
def get_positions():
    return portfolio_tracker.get_open_positions()


@app.get("/signals/pending")
def get_pending():
    return order_manager.get_pending_signals()


@app.post("/signals/{signal_id}/confirm")
def confirm_signal(signal_id: str):
    ok = order_manager.confirm(signal_id)
    return {"confirmed": ok, "signal_id": signal_id}


@app.post("/signals/{signal_id}/reject")
def reject_signal(signal_id: str):
    ok = order_manager.reject(signal_id)
    return {"rejected": ok, "signal_id": signal_id}


@app.get("/risk")
def get_risk():
    return risk_manager.status()


@app.post("/mode/{mode}")
def set_mode(mode: str):
    if mode.upper() not in ("AUTO", "MANUAL"):
        return {"error": "Mode must be AUTO or MANUAL"}
    order_manager.set_mode(mode.upper())
    return {"mode": mode.upper()}


@app.post("/kill-switch/reset")
def reset_kill_switch():
    risk_manager.reset_kill_switch()
    return {"kill_switch_active": False}


@app.get("/ltp/{symbol:path}")
def get_ltp(symbol: str):
    ltp = store.get_ltp(symbol)
    return {"symbol": symbol, "ltp": ltp}


@app.get("/symbols")
def get_symbols():
    return {"symbols": store.get_active_symbols()}


@app.get("/plan/today")
def get_daily_plan():
    try:
        from daily_plan import daily_plan_generator
        plan = daily_plan_generator.generate()
        return {
            "date":             plan.date,
            "market_theme":     plan.market_theme,
            "risk_level":       plan.risk_level,
            "focus_stocks":     plan.focus_stocks,
            "macro_context":    plan.macro_context,
            "key_levels":       plan.key_levels,
            "analyst_briefing": plan.analyst_briefing,
            "rules_for_today":  plan.rules_for_today,
            "checklist": [
                {"id": i.id, "time": i.time, "phase": i.phase,
                 "priority": i.priority, "automated": i.automated,
                 "task": i.task, "detail": i.detail, "done": i.done}
                for i in plan.checklist
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/plan/done/{item_id}")
def mark_done(item_id: str):
    try:
        from daily_plan import daily_plan_generator
        ok = daily_plan_generator.mark_done(item_id)
        return {"ok": ok, "item_id": item_id}
    except Exception as e:
        return {"error": str(e)}


@app.get("/journal/analysis")
def get_journal_analysis():
    try:
        from journal_analyser import journal_analyser
        analysis = journal_analyser.analyse(min_trades=1)
        if not analysis:
            return {"error": "No trades found"}
        return {
            "generated_at":      analysis.generated_at,
            "total_trades":      analysis.total_trades,
            "date_range":        analysis.date_range,
            "win_rate":          analysis.win_rate,
            "profit_factor":     analysis.profit_factor,
            "total_pnl":         analysis.total_pnl,
            "avg_rr_achieved":   analysis.avg_rr_achieved,
            "avg_holding_days":  analysis.avg_holding_days,
            "best_day":          analysis.best_day,
            "worst_day":         analysis.worst_day,
            "best_hour":         analysis.best_hour,
            "best_strategy":     analysis.best_strategy,
            "exit_too_early":    analysis.exit_too_early,
            "hold_losers_long":  analysis.hold_losers_long,
            "biases": [
                {"name": b.name, "detected": b.detected,
                 "severity": b.severity, "evidence": b.evidence, "fix": b.fix}
                for b in analysis.biases
            ],
            "personalised_rules":  analysis.personalised_rules,
            "narrative":           analysis.narrative,
            "strengths":           analysis.strengths,
            "improvement_areas":   analysis.improvement_areas,
            "missed_opportunities": analysis.missed_opportunities,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/plan/refresh")
def refresh_plan():
    try:
        from daily_plan import daily_plan_generator
        plan = daily_plan_generator.generate(force_refresh=True)
        return {"ok": True, "date": plan.date, "tasks": len(plan.checklist)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/portfolio/analysis")
def get_portfolio_analysis():
    try:
        from portfolio_analyser import portfolio_analyser
        positions = portfolio_tracker.get_open_positions()
        analysis  = portfolio_analyser.analyse(positions)
        return {
            "generated_at":      analysis.generated_at.isoformat(),
            "total_value":       analysis.total_value,
            "total_pnl":         analysis.total_pnl,
            "portfolio_beta":    analysis.portfolio_beta,
            "risk_rating":       analysis.risk_rating,
            "concentration":     analysis.concentration_score,
            "sector_exposure":   analysis.sector_exposure,
            "correlations":      [
                {"a": c.symbol_a, "b": c.symbol_b,
                 "corr": c.correlation, "risk": c.risk_level, "note": c.note}
                for c in analysis.correlations[:10]
            ],
            "stress_tests":      [
                {"scenario": s.scenario, "nifty_drop": s.nifty_drop_pct,
                 "loss_inr": s.estimated_portfolio_loss,
                 "loss_pct": s.estimated_portfolio_loss_pct,
                 "worst": s.worst_position}
                for s in analysis.stress_tests
            ],
            "hedge_suggestions": [
                {"instrument": h.instrument, "strategy": h.strategy,
                 "purpose": h.purpose, "cost": h.cost_estimate,
                 "protection": h.protection}
                for h in analysis.hedge_suggestions
            ],
            "analyst_narrative": analysis.analyst_narrative,
            "action_items":      analysis.action_items,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────
# WEBSOCKET — live push every 2 seconds
# ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    logger.info(f"Dashboard WebSocket connected. Clients: {len(_ws_clients)}")
    try:
        while True:
            payload = _build_live_payload()
            await ws.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket client disconnected: {e}")
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


def _build_live_payload() -> dict:
    """Build the real-time payload sent to dashboard every 2 seconds."""
    stats     = portfolio_tracker.get_stats()
    risk      = risk_manager.status()
    positions = portfolio_tracker.get_open_positions()
    pending   = order_manager.get_pending_signals()

    # Live LTPs for open positions
    ltps = {
        pos["symbol"]: store.get_ltp(pos["symbol"])
        for pos in positions
    }

    return {
        "timestamp":       datetime.now(tz=timezone.utc).isoformat(),
        "mode":            order_manager.mode,
        "stats":           stats,
        "risk":            risk,
        "positions":       positions,
        "pending_signals": pending,
        "ltps":            ltps,
    }
