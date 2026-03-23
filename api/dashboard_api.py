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

@app.get("/paper/stats")
def get_paper_stats():
    try:
        from paper_trading import paper_trading_engine
        return paper_trading_engine.get_paper_stats()
    except Exception as e:
        return {"error": str(e)}

@app.get("/paper/positions")
def get_paper_positions():
    try:
        from paper_trading import paper_trading_engine
        return paper_trading_engine.get_paper_positions()
    except Exception as e:
        return {"error": str(e)}

@app.get("/logs")
def get_logs(lines: int = 50):
    """Return last N lines from bot log."""
    try:
        import os
        log_path = "logs/bot.log"
        if not os.path.exists(log_path):
            return {"lines": [], "error": "Log file not found"}
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        last_lines = all_lines[-lines:]
        return {
            "lines": [l.rstrip() for l in last_lines],
            "total": len(all_lines),
            "file":  log_path,
        }
    except Exception as e:
        return {"lines": [], "error": str(e)}


@app.get("/services/status")
def get_services_status():
    """Return status of all bot services and cron jobs."""
    import subprocess, os
    status = {}

    # Check processes running
    def is_running(name):
        try:
            result = subprocess.run(
                ["pgrep", "-f", name],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
            return False

    status["bot"]       = {"running": is_running("main.py"),      "label": "Trading Bot"}
    status["watchdog"]  = {"running": is_running("watchdog.py"),  "label": "Watchdog"}
    status["dashboard"] = {"running": is_running("http.server"),  "label": "Dashboard Server"}

    # Check last run times from log files
    def last_run(log_file):
        try:
            if os.path.exists(log_file):
                mtime = os.path.getmtime(log_file)
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                return dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass
        return "Never"

    status["nightly_agent"] = {
        "label":    "Nightly Agent",
        "last_run": last_run("logs/nightly.log"),
        "running":  False,
    }
    status["weekly_agent"] = {
        "label":    "Weekly Agent",
        "last_run": last_run("logs/weekly.log"),
        "running":  False,
    }
    status["token_refresh"] = {
        "label":    "Token Refresh",
        "last_run": last_run("logs/token.log"),
        "running":  False,
    }

    # Check cron is active
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "cron"],
            capture_output=True, text=True
        )
        status["cron"] = {
            "label":   "Cron Scheduler",
            "running": result.stdout.strip() == "active",
        }
    except Exception:
        status["cron"] = {"label": "Cron Scheduler", "running": False}

    # Bot uptime
    try:
        result = subprocess.run(
            ["stat", "-c", "%Y", "logs/bot.log"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            from datetime import datetime, timezone
            mtime  = int(result.stdout.strip())
            start  = datetime.fromtimestamp(mtime, tz=timezone.utc)
            now    = datetime.now(tz=timezone.utc)
            uptime = str(now - start).split(".")[0]
            status["uptime"] = uptime
    except Exception:
        status["uptime"] = "Unknown"

    return status


@app.get("/backtest/results")
def get_backtest_results():
    """Return latest backtest results from weekly report."""
    import os, json, glob
    try:
        reports = sorted(
            glob.glob("db/weekly_reports/*.json"),
            reverse=True
        )
        if not reports:
            return {"available": False, "message": "No backtest results yet. Run weekly_agent.py first."}

        with open(reports[0]) as f:
            data = json.load(f)

        grades = data.get("strategy_grades", {})
        result = {
            "available":   True,
            "week_ending": data.get("week_ending", ""),
            "strategies":  {},
        }

        for strat, info in grades.items():
            results   = info.get("results", {})
            top_10    = info.get("top_10", [])
            avoid     = info.get("avoid", [])
            avg_pf    = info.get("avg_pf", 0)

            # Build leaderboard
            leaderboard = sorted(
                [
                    {
                        "symbol":       sym,
                        "win_rate":     r.get("win_rate", 0),
                        "profit_factor": r.get("profit_factor", 0),
                        "sharpe":       r.get("sharpe", 0),
                        "total_return": r.get("total_return", 0),
                        "total_trades": r.get("total_trades", 0),
                        "max_drawdown": r.get("max_drawdown", 0),
                    }
                    for sym, r in results.items()
                ],
                key=lambda x: x["profit_factor"],
                reverse=True,
            )

            result["strategies"][strat] = {
                "avg_profit_factor": avg_pf,
                "top_10":           top_10,
                "avoid":            avoid,
                "leaderboard":      leaderboard[:15],
            }

        return result

    except Exception as e:
        return {"available": False, "error": str(e)}


@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    """Stream live log lines via WebSocket."""
    import asyncio, os
    await ws.accept()
    log_path = "logs/bot.log"
    last_pos  = 0

    # Send last 30 lines immediately on connect
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                for line in lines[-30:]:
                    await ws.send_text(line.rstrip())
                last_pos = f.tell()
    except Exception:
        pass

    # Then stream new lines as they appear
    try:
        while True:
            try:
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        new_lines = f.readlines()
                        last_pos  = f.tell()
                    for line in new_lines:
                        line = line.rstrip()
                        if line:
                            await ws.send_text(line)
            except Exception:
                pass
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass


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