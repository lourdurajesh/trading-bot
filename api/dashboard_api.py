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
  GET  /signals/health     — WHY no trades are firing (drought, blockers, action points)
  GET  /signals/drought    — compact drought badge
  GET  /learning/trades    — NSE learning paper trades (SimpleRSI / SimpleMomentum)
  GET  /learning/stats     — NSE learning aggregate stats
  GET  /learning/review    — NSE learning trades grouped by outcome bucket
  GET  /commodity/trades   — MCX commodity options paper trades
  GET  /commodity/stats    — MCX commodity options aggregate stats
  GET  /commodity/chain/{symbol} — last fetched MCX options chain snapshot
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

IST = ZoneInfo("Asia/Kolkata")
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

def _get_learning_payload(prev_hash: str) -> tuple[dict | None, str]:
    """
    Return (data, new_hash).
    data is None when trades haven't changed since prev_hash.
    Pass prev_hash="" on first call to force the initial push.
    Hash is per-WS-connection so every new client gets the full dataset on connect.
    """
    import hashlib
    try:
        from learning_engine import learning_engine
        trades = learning_engine.get_trades(limit=200)
        stats  = learning_engine.get_stats()
        h = hashlib.md5(
            b",".join(f"{t['id']}:{t['status']}".encode() for t in trades)
        ).hexdigest()[:12]
        if h == prev_hash:
            return None, h
        return {"trades": trades, "stats": stats}, h
    except Exception:
        return None, prev_hash


# ─────────────────────────────────────────────────────────────────
# REST ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.get("/audit/recent")
def get_audit_log(limit: int = 100, event_type: str = None):
    """Return recent audit log entries."""
    try:
        from audit_log import audit_log
        return {"entries": audit_log.get_recent(limit=limit, event_type=event_type or None)}
    except Exception as e:
        return {"entries": [], "error": str(e)}


@app.post("/audit/export")
def export_audit():
    """Export full audit log to CSV."""
    try:
        from audit_log import audit_log
        path = audit_log.export_csv()
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/decisions/daily")
def daily_decisions(date: str = None):
    """
    Daily decision journal — what the bot evaluated, decided, and why.
    date: YYYY-MM-DD in IST (defaults to today).
    Returns per-symbol decision trails grouped by outcome category.
    """
    import sqlite3, json as _json
    from audit_log import AUDIT_DB_PATH

    ist_now = datetime.now(IST)
    target_date = date or ist_now.strftime("%Y-%m-%d")

    # Fetch all rows for the target date
    try:
        with sqlite3.connect(AUDIT_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE timestamp LIKE ? ORDER BY id ASC",
                (f"{target_date}%",)
            ).fetchall()
    except Exception as e:
        return {"error": str(e), "date": target_date, "symbols": [], "session": {}, "trades": []}

    # Parse rows
    events = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = _json.loads(d.get("details") or "{}")
        except Exception:
            d["details"] = {}
        # Extract HH:MM:SS from ISO timestamp
        ts = d.get("timestamp", "")
        d["time"] = ts[11:19] if len(ts) >= 19 else ts
        events.append(d)

    # Session-level events
    session_events = [e for e in events if e["event_type"] in ("BOT_START", "BOT_STOP", "MODE_CHANGE", "KILL_SWITCH", "TOKEN_REFRESH")]
    bot_start = next((e["time"] for e in events if e["event_type"] == "BOT_START"), None)
    mode = next(
        (e["details"].get("new") or e["reason"].split("→")[-1].strip()
         for e in reversed(events) if e["event_type"] == "MODE_CHANGE"),
        None
    )

    # Group symbol-level events
    SYMBOL_EVENTS = {"SIGNAL_GENERATED", "INTELLIGENCE_VETO", "SIGNAL_REJECTED",
                     "ORDER_PLACED", "ORDER_FILLED", "ORDER_FAILED",
                     "POSITION_OPENED", "POSITION_CLOSED", "STOP_HIT", "TARGET_HIT",
                     "TRAILING_STOP", "PAPER_TRADE"}
    sym_map: dict = {}
    for e in events:
        sym = e.get("symbol", "")
        if not sym or e["event_type"] not in SYMBOL_EVENTS:
            continue
        if sym not in sym_map:
            sym_map[sym] = []
        sym_map[sym].append({
            "time":      e["time"],
            "type":      e["event_type"],
            "direction": e.get("direction", ""),
            "strategy":  e.get("strategy", ""),
            "reason":    e.get("reason", ""),
            "price":     e.get("price", 0),
            "qty":       e.get("qty", 0),
            "details":   e["details"],
        })

    # Determine outcome per symbol
    OUTCOME_ORDER = {"TRADED": 0, "PAPER_TRADED": 1, "VETOED": 2, "RISK_REJECTED": 3, "SIGNAL_GENERATED": 4}
    symbols_out = []
    for sym, evts in sym_map.items():
        types = {ev["type"] for ev in evts}
        if "ORDER_PLACED" in types or "ORDER_FILLED" in types or "POSITION_OPENED" in types:
            outcome = "TRADED"
        elif "PAPER_TRADE" in types:
            outcome = "PAPER_TRADED"
        elif "INTELLIGENCE_VETO" in types:
            outcome = "VETOED"
        elif "SIGNAL_REJECTED" in types:
            outcome = "RISK_REJECTED"
        else:
            outcome = "SIGNAL_GENERATED"

        final_reason = ""
        for et in ("INTELLIGENCE_VETO", "SIGNAL_REJECTED", "ORDER_PLACED", "PAPER_TRADE"):
            match = next((ev["reason"] for ev in reversed(evts) if ev["type"] == et), None)
            if match:
                final_reason = match
                break

        display = sym.replace("NSE:", "").replace("-EQ", "").replace("MCX:", "").replace("BSE:", "")
        symbols_out.append({
            "symbol":       sym,
            "display":      display,
            "outcome":      outcome,
            "final_reason": final_reason,
            "events":       evts,
        })

    symbols_out.sort(key=lambda x: OUTCOME_ORDER.get(x["outcome"], 99))

    # Trades placed today
    trades = [
        {
            "time":      e["time"],
            "symbol":    e.get("symbol", ""),
            "display":   e.get("symbol","").replace("NSE:","").replace("-EQ","").replace("MCX:",""),
            "direction": e.get("direction", ""),
            "qty":       e.get("qty", 0),
            "price":     e.get("price", 0),
            "strategy":  e.get("strategy", ""),
            "paper":     bool(e.get("paper", 0)),
        }
        for e in events
        if e["event_type"] in ("ORDER_PLACED", "PAPER_TRADE", "POSITION_OPENED")
    ]

    # Rejection breakdown
    rejection_breakdown = {}
    for e in events:
        if e["event_type"] in ("SIGNAL_REJECTED", "INTELLIGENCE_VETO"):
            reason = e.get("reason", "")
            layer  = e.get("details", {}).get("layer", "unknown")
            if e["event_type"] == "INTELLIGENCE_VETO":
                if "earnings" in reason.lower():
                    cat = "earnings_blackout"
                elif "macro" in reason.lower():
                    cat = "macro_veto"
                else:
                    cat = "intelligence_veto"
            elif "risk_reward" in reason.lower() or "r:r" in reason.lower():
                cat = "poor_risk_reward"
            elif "heat" in reason.lower():
                cat = "portfolio_heat"
            elif "loss limit" in reason.lower():
                cat = "daily_loss_limit"
            elif "cooldown" in reason.lower():
                cat = "cooldown"
            elif "position" in reason.lower() and "max" in reason.lower():
                cat = "max_positions"
            else:
                cat = layer if layer != "unknown" else "other"
            rejection_breakdown[cat] = rejection_breakdown.get(cat, 0) + 1

    # ── Conviction score for the target date ─────────────────────────
    conviction = None
    try:
        from intelligence.conviction_scorer import conviction_scorer
        conviction = conviction_scorer.get_for_date(target_date)
        # Fall back to in-memory last score for today
        if conviction is None and target_date == ist_now.strftime("%Y-%m-%d"):
            last = conviction_scorer.get_last_score()
            if last:
                conviction = {
                    "date":        target_date,
                    "timestamp":   last.timestamp,
                    "score":       last.score,
                    "direction":   last.direction,
                    "tradeable":   last.tradeable,
                    "capital_pct": last.capital_pct,
                    "reasons":     last.reasons,
                    "fii_score":   last.fii_score,
                    "oi_score":    last.oi_score,
                    "iep_score":   last.iep_score,
                    "vix_score":   last.vix_score,
                    "gift_score":  last.gift_score,
                    "rs_score":    last.rs_score,
                }
    except Exception:
        pass

    return {
        "date":   target_date,
        "session": {
            "bot_started":           bot_start,
            "mode":                  mode,
            "total_symbols_active":  len(sym_map),
            "total_signals_generated": sum(
                1 for e in events if e["event_type"] == "SIGNAL_GENERATED"
            ),
            "total_vetoed":    sum(1 for e in events if e["event_type"] == "INTELLIGENCE_VETO"),
            "total_rejected":  sum(1 for e in events if e["event_type"] == "SIGNAL_REJECTED"),
            "total_traded":    len([s for s in symbols_out if s["outcome"] == "TRADED"]),
            "session_events":  [{"time": e["time"], "type": e["event_type"], "reason": e.get("reason","")} for e in session_events],
        },
        "symbols":             symbols_out,
        "trades":              trades,
        "rejection_breakdown": rejection_breakdown,
        "conviction":          conviction,
    }


@app.get("/options/chain/{symbol:path}")
def get_options_chain(symbol: str):
    """Return live options chain for a symbol."""
    try:
        from execution.options_executor import options_executor
        chain = options_executor._get_chain(symbol)
        if not chain:
            return {"available": False, "message": "Chain unavailable (Fyers not connected or symbol not supported)"}
        expiries = chain.get("expiryData", [])
        return {
            "available":        True,
            "underlying_value": chain.get("underlyingValue"),
            "expiry_count":     len(expiries),
            "nearest_expiry":   expiries[0].get("expiry") if expiries else None,
            "strikes_count":    len(expiries[0].get("optionsChain", [])) if expiries else 0,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


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


# ─────────────────────────────────────────────────────────────────
# LEARNING PAPER TRADES
# ─────────────────────────────────────────────────────────────────

@app.get("/learning/trades")
def learning_trades(status: str = None, limit: int = 200):
    """
    All learning paper trades.
    ?status=OPEN|CLOSED   filter by status
    ?limit=N              max rows (default 200)
    Each trade includes full entry metadata (RSI, EMA, ATR etc.)
    for post-trade review and strategy refinement.
    """
    try:
        from learning_engine import learning_engine
        return {"trades": learning_engine.get_trades(status=status, limit=limit)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/learning/stats")
def learning_stats():
    """
    Aggregate performance of learning strategies.
    Returns win rate, average R, breakdown by strategy/direction/exit-reason.
    """
    try:
        from learning_engine import learning_engine
        return learning_engine.get_stats()
    except Exception as e:
        return {"error": str(e)}


@app.get("/learning/review")
def learning_review(strategy: str = None):
    """
    Closed learning trades grouped by outcome bucket
    (strong_win / small_win / scratch / small_loss / large_loss).
    Pass ?strategy=SimpleRSI or ?strategy=SimpleMomentum to filter.
    """
    try:
        from learning_engine import learning_engine
        trades = learning_engine.get_review(strategy=strategy)
        from collections import defaultdict
        grouped: dict = defaultdict(list)
        for t in trades:
            grouped[t["outcome_bucket"]].append(t)
        return {
            "by_outcome": dict(grouped),
            "total":      len(trades),
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────
# COMMODITY OPTIONS LEARNING (MCX paper trades — standalone engine)
# ─────────────────────────────────────────────────────────────────

@app.get("/commodity/trades")
def commodity_trades(status: str = None, symbol: str = None, limit: int = 200):
    """
    MCX commodity options paper trades.
    ?status=OPEN|CLOSED   filter by status
    ?symbol=CRUDEOIL      filter by commodity (partial match)
    ?limit=N              max rows (default 200)
    Each trade includes spread legs, greeks, P&L, and entry metadata.
    """
    try:
        from commodity_options_learning import commodity_options
        trades = commodity_options.get_trades(status=status, limit=limit)
        if symbol:
            sym_upper = symbol.upper()
            trades = [t for t in trades if sym_upper in t.get("instrument", "").upper()]
        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/commodity/stats")
def commodity_stats():
    """
    Aggregate performance of MCX commodity options paper trades.
    Returns win rate, avg R, best/worst trade, breakdown by commodity and direction.
    """
    try:
        from commodity_options_learning import commodity_options
        return commodity_options.get_stats()
    except Exception as e:
        return {"error": str(e)}


@app.get("/commodity/chain/{symbol:path}")
def commodity_chain(symbol: str):
    """
    Last fetched options chain snapshot for an MCX symbol.
    symbol — commodity name, e.g. CRUDEOIL or full Fyers code MCX:CRUDEOIL25JUNFUT
    Returns spot price, ATM strike, sampled bid/ask from the cached chain.
    """
    try:
        from commodity_options_learning import commodity_options
        snapshot = commodity_options.get_chain_snapshot(symbol.upper())
        if snapshot is None:
            return {
                "available": False,
                "message":   "No chain data yet — engine runs during MCX hours (09:00–23:30 IST)",
            }
        return {"available": True, **snapshot}
    except Exception as e:
        return {"available": False, "error": str(e)}


@app.get("/commodity/chain-full/{symbol:path}")
def commodity_chain_full(symbol: str, expiry_idx: int = 0):
    """
    Full MCX options chain for a symbol — all strikes with CE/PE LTP, IV, OI, Greeks.
    symbol     — short name (CRUDEOIL) or full Fyers code (MCX:CRUDEOIL25JUNFUT)
    expiry_idx — which expiry to display (0 = nearest)
    """
    try:
        from commodity_options_learning import commodity_options
        return commodity_options.get_full_chain(symbol.upper(), expiry_idx=expiry_idx)
    except Exception as e:
        return {"error": str(e), "source": "error", "strikes": [], "expiries": []}


@app.get("/commodity/ltp-debug")
def commodity_ltp_debug():
    """
    Debug endpoint — shows what LTP the data store has for each MCX futures symbol.
    Also shows subscribed vs. stored symbols for open positions.
    """
    try:
        from commodity_options_learning import commodity_options, _fyers_sym, MCX_CONTRACTS, ALL_MCX_SHORTS
        open_comm = commodity_options.get_trades(status="OPEN")
        result = {}
        for short in ALL_MCX_SHORTS:
            active_sym = _fyers_sym(short)
            active_ltp = store.get_ltp(active_sym)
            meta       = MCX_CONTRACTS.get(short, {})
            result[short] = {
                "active_symbol": active_sym,
                "active_ltp":    active_ltp,
                "min_price":     meta.get("min_price"),
                "max_price":     meta.get("max_price"),
                "in_range":      meta.get("min_price", 0) <= (active_ltp or 0) <= meta.get("max_price", 0),
            }
        for t in open_comm:
            inst = t.get("instrument", "?")
            sym  = t.get("symbol")
            if inst in result:
                result[inst]["stored_symbol"] = sym
                result[inst]["stored_ltp"]    = store.get_ltp(sym) if sym else None
                result[inst]["symbol_match"]  = sym == result[inst]["active_symbol"]
        return {"mcx_ltps": result, "store_symbols": [s for s in store.get_active_symbols() if "MCX" in s]}
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
                dt = datetime.fromtimestamp(mtime, tz=IST)
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
            start  = datetime.fromtimestamp(mtime, tz=IST)
            now    = datetime.now(tz=IST)
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


@app.get("/review/daily")
def get_daily_review(date_str: str = None, capital: float = None):
    """
    Today's performance review — open positions, closed trades, audit trail.
    Query params:
      date    — YYYY-MM-DD (default: today IST)
      capital — portfolio size for % P&L (default: TOTAL_CAPITAL from .env)
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from daily_review import (
            fetch_open_positions, fetch_closed_today,
            fetch_paper_closed_today, fetch_audit_today,
        )
        from config.settings import TOTAL_CAPITAL
        from datetime import date as _date

        day = (
            _date.fromisoformat(date_str)
            if date_str else
            datetime.now(tz=IST).date()
        )
        cap = capital or TOTAL_CAPITAL

        open_pos   = fetch_open_positions(day)
        closed     = fetch_closed_today(day)
        paper      = fetch_paper_closed_today(day)
        audit      = fetch_audit_today(day)

        pnls       = [t["pnl"] for t in closed]
        total_pnl  = sum(pnls)
        winners    = [p for p in pnls if p > 0]
        losers     = [p for p in pnls if p <= 0]
        win_rate   = round(len(winners) / len(pnls) * 100, 1) if pnls else 0.0

        type_counts: dict = {}
        for e in audit:
            et = e["event_type"]
            type_counts[et] = type_counts.get(et, 0) + 1

        rejected = [e for e in audit if e["event_type"] in ("SIGNAL_REJECTED", "INTELLIGENCE_VETO")]
        reason_counts: dict = {}
        for e in rejected:
            r = (e.get("reason") or "unknown")[:80]
            reason_counts[r] = reason_counts.get(r, 0) + 1

        return {
            "date":    day.isoformat(),
            "capital": cap,
            "live": {
                "closed_trades":   len(closed),
                "open_positions":  len(open_pos),
                "realised_pnl":    round(total_pnl, 2),
                "pnl_pct":         round(total_pnl / cap * 100, 3) if cap else 0,
                "win_rate_pct":    win_rate,
                "winners":         len(winners),
                "losers":          len(losers),
                "open":            open_pos,
                "trades":          closed,
            },
            "paper": {
                "closed_trades": len(paper),
                "realised_pnl":  round(sum(t["pnl"] for t in paper), 2),
                "trades":        paper,
            },
            "audit": {
                "total_events":      len(audit),
                "event_counts":      type_counts,
                "rejected_signals":  len(rejected),
                "rejection_reasons": reason_counts,
                "kill_switch_fired": type_counts.get("KILL_SWITCH", 0) > 0,
                "timeline":          audit,
            },
        }

    except Exception as e:
        return {"error": str(e)}


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(tz=IST).isoformat()}


@app.get("/system/alerts")
def get_system_alerts():
    """Active system health alerts — token failures, API errors, etc."""
    try:
        from system_health import system_health
        return {
            "alerts":       system_health.get_alerts(),
            "has_critical": system_health.has_critical(),
            "count":        len(system_health.get_alerts()),
        }
    except Exception as e:
        return {"alerts": [], "error": str(e)}


@app.post("/system/token/refresh")
def trigger_token_refresh():
    """Manually trigger a Fyers token refresh (bypasses cooldown)."""
    try:
        from token_manager import token_manager
        token_manager._last_attempt = None   # bypass cooldown for manual trigger
        ok = token_manager.check_and_refresh_if_needed()
        return {"ok": ok, "status": token_manager.get_status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────
# SIGNAL HEALTH — WHY ARE WE NOT TRADING?
# ─────────────────────────────────────────────────────────────────

@app.get("/signals/health")
def signals_health():
    """
    Returns a structured report explaining WHY no trades are firing.
    Includes: drought length, top blocking conditions (categorised),
    per-symbol last skip reason, and action-point hints.
    Refreshes every cycle (~60 s) — poll this to stay current.
    """
    try:
        from analysis.signal_health import health_monitor
        return health_monitor.snapshot()
    except Exception as e:
        return {"error": str(e)}


@app.get("/signals/drought")
def signals_drought():
    """
    Compact drought summary: days without a trade + status badge.
    Suitable for a dashboard status indicator.
    """
    try:
        from analysis.signal_health import health_monitor
        snap = health_monitor.snapshot()
        return {
            "drought_days":   snap["drought_days"],
            "drought_status": snap["drought_status"],   # OK / CAUTION / WARNING / CRITICAL
            "last_trade":     snap["last_trade"],
            "signals_today":  snap["signals_today"],
            "top_3_reasons": [
                {"category": b["category"], "hint": b["hint"]}
                for b in snap["top_blockers_today"][:3]
            ],
        }
    except Exception as e:
        return {"error": str(e)}


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


@app.post("/positions/force-close")
def force_close_position(body: dict):
    """
    Manually dismiss a stale open position.
    Use when the broker already closed the position but the bot missed the fill
    (crash during exit, manual broker close, option expiry, EOD auto-squareoff).
    Body: {"symbol": "NSE:NIFTYBANK-INDEX", "reason": "BROKER_CLOSED"}
    """
    symbol = (body.get("symbol") or "").strip()
    reason = (body.get("reason") or "MANUAL_CLOSE").strip()
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        ok = portfolio_tracker.force_close(symbol, reason=reason)
        if ok:
            try:
                from audit_log import audit_log
                audit_log.bot_event("MANUAL_FORCE_CLOSE", f"Dashboard force-close: {symbol} ({reason})")
            except Exception:
                pass
        return {"success": ok, "symbol": symbol,
                "error": None if ok else f"No open position found for {symbol}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/commodity/trades/{trade_id}/close")
def commodity_manual_close(trade_id: str):
    """
    Manually close an open MCX commodity option trade at the current spot price.
    Records MANUAL_CLOSE as exit reason and removes it from the engine's open positions.
    """
    try:
        from commodity_options_learning import commodity_options, _fyers_sym, MCX_CONTRACTS, MCX_STRATEGY_CONFIG
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        _IST = _ZI("Asia/Kolkata")

        # Find the trade in DB
        open_trades = commodity_options.get_trades(status="OPEN")
        trade = next((t for t in open_trades if t["id"] == trade_id), None)
        if not trade:
            return {"success": False, "error": f"No open trade found with id {trade_id}"}

        instrument = trade["instrument"]
        direction  = trade["direction"]
        net_debit  = trade["net_debit"]
        entry_spot = trade["spot_at_entry"]
        lot_size   = trade.get("lot_size", 1)

        # Get current spot for P&L estimate
        sym = trade.get("symbol")
        spot = None
        try:
            active_sym = _fyers_sym(instrument)
            spot = store.get_ltp(active_sym)
        except Exception:
            pass
        if not spot and sym:
            spot = store.get_ltp(sym)
        if not spot:
            spot = entry_spot  # last resort: no P&L movement

        spot_move = spot - entry_spot if direction == "LONG" else entry_spot - spot
        est_pnl   = round(spot_move * 0.35, 2)
        pnl_r     = round(est_pnl / net_debit, 2) if net_debit > 0 else 0
        pnl_inr   = round(est_pnl * lot_size, 2)

        commodity_options._db_close(trade_id, spot, "MANUAL_CLOSE", pnl_inr, pnl_r)

        # Remove from in-memory positions so exit monitoring doesn't re-process it
        key_to_remove = next(
            (k for k, v in commodity_options._open_positions.items() if v.get("id") == trade_id),
            None,
        )
        if key_to_remove:
            del commodity_options._open_positions[key_to_remove]

        # Apply entry cooldown — MANUAL_CLOSE always triggers cooldown regardless of
        # cooldown_enabled flag, because the user closed it due to a perceived problem.
        strategy  = trade.get("strategy", "")
        strat_cfg = MCX_STRATEGY_CONFIG.get(strategy, {})
        hours = strat_cfg.get("cooldown_hours", 3)   # default 3h if strategy unknown
        resume_at = _dt.now(tz=_IST) + _td(hours=hours)
        commodity_options._entry_cooldown[instrument] = resume_at
        cooldown_applied = True
        logger.info(
            f"[CommOpts] {instrument} MANUAL_CLOSE — entry cooldown {hours}h "
            f"until {resume_at.strftime('%H:%M')} IST"
        )

        try:
            from audit_log import audit_log
            audit_log.bot_event(
                "COMMODITY_MANUAL_CLOSE",
                f"Dashboard manual close: {trade_id} {instrument} {direction} "
                f"spot={spot:.2f} pnl=₹{pnl_inr:+.0f}"
                + (f" | cooldown {strat_cfg.get('cooldown_hours',3)}h" if cooldown_applied else ""),
            )
        except Exception:
            pass

        result: dict = {
            "success":    True,
            "trade_id":   trade_id,
            "instrument": instrument,
            "spot":       spot,
            "pnl_inr":    pnl_inr,
            "pnl_r":      pnl_r,
        }
        if cooldown_applied:
            result["cooldown_hours"]  = strat_cfg.get("cooldown_hours", 3)
            result["cooldown_resume"] = commodity_options._entry_cooldown[instrument].strftime("%H:%M")
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


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


@app.get("/conviction/today")
def get_conviction_today():
    """Return today's pre-market conviction score with full component breakdown."""
    try:
        from intelligence.conviction_scorer import conviction_scorer
        today = datetime.now(tz=IST).strftime("%Y-%m-%d")
        saved = conviction_scorer.get_for_date(today)
        if saved:
            return saved
        last = conviction_scorer.get_last_score()
        if last:
            return {
                "date":        today,
                "timestamp":   last.timestamp,
                "score":       last.score,
                "direction":   last.direction,
                "tradeable":   last.tradeable,
                "capital_pct": last.capital_pct,
                "reasons":     last.reasons,
                "fii_score":   last.fii_score,
                "oi_score":    last.oi_score,
                "iep_score":   last.iep_score,
                "vix_score":   last.vix_score,
                "gift_score":  last.gift_score,
                "rs_score":    last.rs_score,
            }
        return {"error": "No conviction score computed today (bot not started or pre-market not run yet)"}
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


@app.post("/agents/nightly")
def run_nightly_agent():
    """Trigger nightly agent in background thread."""
    import threading
    def run():
        try:
            from nightly_agent import run_nightly_agent
            run_nightly_agent()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Nightly agent failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": "Nightly agent started in background — check Live Logs tab"}


@app.post("/agents/weekly")
def run_weekly_agent():
    """Trigger weekly agent in background thread."""
    import threading
    def run():
        try:
            from weekly_agent import run_weekly_agent
            run_weekly_agent()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Weekly agent failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": "Weekly agent started — this takes 10-15 minutes. Check Live Logs tab"}


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
# STRATEGY CONFIGURATION
# ─────────────────────────────────────────────────────────────────

@app.get("/config/strategies")
def get_strategy_config():
    """
    Return full schema for all strategies including current values,
    types, min/max bounds, and descriptions. Used by the config UI.
    """
    try:
        from config.strategy_config import get_schema
        return {"ok": True, "config": get_schema()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/config/strategies/{strategy}/{param}")
def update_strategy_param(strategy: str, param: str, body: dict):
    """
    Update a single strategy parameter at runtime.
    Body: {"value": <new_value>}
    Change takes effect on the next evaluation cycle (no restart needed).
    """
    value = body.get("value")
    if value is None:
        return {"ok": False, "error": "body must contain 'value'"}
    try:
        from config.strategy_config import update
        ok, err = update(strategy, param, value)
        if not ok:
            return {"ok": False, "error": err}
        return {"ok": True, "strategy": strategy, "param": param, "value": value}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────
# MCX COMMODITY STRATEGY CONFIGURATION
# ─────────────────────────────────────────────────────────────────

@app.get("/commodity/config")
def get_commodity_strategy_config():
    """Return MCX strategy risk profiles, cooldown settings, and active cooldowns."""
    try:
        from commodity_options_learning import commodity_options
        return {
            "ok":         True,
            "strategies": commodity_options.get_strategy_config(),
            "cooldowns":  commodity_options.get_cooldown_status(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/commodity/config/{strategy}/{param}")
def update_commodity_strategy_config(strategy: str, param: str, body: dict):
    """Update a single MCX strategy param (cooldown_enabled, cooldown_hours) at runtime."""
    value = body.get("value")
    if value is None:
        return {"ok": False, "error": "body must contain 'value'"}
    try:
        from commodity_options_learning import commodity_options
        ok, err = commodity_options.update_strategy_config(strategy, param, value)
        if not ok:
            return {"ok": False, "error": err}
        return {"ok": True, "strategy": strategy, "param": param, "value": value}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/commodity/cooldown/{instrument}")
def clear_commodity_cooldown(instrument: str):
    """Manually clear an active entry cooldown for a commodity instrument."""
    try:
        from commodity_options_learning import commodity_options
        was_active = instrument in commodity_options._entry_cooldown
        commodity_options._entry_cooldown.pop(instrument, None)
        commodity_options._cooldown_cleared.add(instrument)
        return {"ok": True, "cleared": was_active, "instrument": instrument}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────
# WEBSOCKET — live push every 2 seconds
# ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    logger.info(f"Dashboard WebSocket connected. Clients: {len(_ws_clients)}")
    # Per-connection hash: starts empty so the first tick always pushes learning data
    conn_learning_hash = ""
    try:
        while True:
            payload, conn_learning_hash = _build_live_payload(conn_learning_hash)
            await ws.send_text(json.dumps(payload))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket client disconnected: {e}")
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


def _build_live_payload(conn_learning_hash: str = "") -> tuple[dict, str]:
    """Build the real-time payload sent to dashboard every 2 seconds.
    Returns (payload, new_conn_learning_hash) so the WS loop can track
    per-connection state."""
    stats     = portfolio_tracker.get_stats()
    risk      = risk_manager.status()
    positions = portfolio_tracker.get_open_positions()
    pending   = order_manager.get_pending_signals()

    # Live LTPs for production open positions
    ltps = {}
    for pos in positions:
        nfo_sym = pos.get("options_meta", {}).get("nfo_symbol") if pos.get("signal_type") == "OPTIONS" else None
        ltps[pos["symbol"]] = store.get_ltp(nfo_sym or pos["symbol"])

    # Live LTPs for open learning trades (separate dict, sent every tick)
    learning_ltps: dict = {}
    try:
        from learning_engine import learning_engine
        open_learning = learning_engine.get_trades(status="OPEN")
        for t in open_learning:
            sym  = t["symbol"]
            meta = t.get("metadata") or {}
            is_opts = meta.get("instrument_type") == "nse_options"
            nfo_sym = meta.get("nfo_symbol") if is_opts else None

            if is_opts:
                # Options: only emit if we have a real contract symbol — never emit index LTP
                if nfo_sym:
                    ltp = store.get_ltp(nfo_sym)
                    if ltp:
                        learning_ltps[nfo_sym] = ltp  # frontend keys by nfo_symbol
            else:
                ltp = store.get_ltp(sym)
                if ltp:
                    learning_ltps[sym] = ltp
    except Exception:
        pass

    # Paper wallet balance (only when paper trading is active)
    paper_wallet = None
    try:
        from paper_trading import paper_trading_engine
        if paper_trading_engine.is_active():
            paper_wallet = {
                "balance":      paper_trading_engine.get_balance(),
                "is_exhausted": paper_trading_engine.is_capital_exhausted(),
                "starting":     500_000.0,
            }
    except Exception:
        pass

    # System health alerts — any component failures needing attention
    system_alerts: list[dict] = []
    try:
        from system_health import system_health
        system_alerts = system_health.get_alerts()
    except Exception:
        pass

    # Token manager status
    token_status = None
    try:
        from token_manager import token_manager
        token_status = token_manager.get_status()
    except Exception:
        pass

    # Live spot LTPs for open commodity option positions (keyed by instrument name)
    # Each entry: {"ltp": float, "age_s": float|None} where age_s is seconds since last tick
    commodity_ltps: dict = {}
    try:
        from commodity_options_learning import commodity_options, _fyers_sym, MCX_CONTRACTS
        _now_ist = datetime.now(tz=IST)
        open_comm = commodity_options.get_trades(status="OPEN")
        seen_instruments: set = set()
        for t in open_comm:
            instrument = t.get("instrument")  # e.g. "CRUDEOIL"
            sym        = t.get("symbol")
            if not instrument or instrument in seen_instruments:
                continue
            seen_instruments.add(instrument)
            # Always try the current active contract first — same approach as the engine
            ltp      = None
            age_s    = None
            tick_sym = None
            try:
                active_sym = _fyers_sym(instrument)
                ltp = store.get_ltp(active_sym)
                if ltp:
                    tick_sym = active_sym
            except Exception:
                pass
            # Fallback: stored symbol (handles edge case where active_sym lookup fails)
            if not ltp and sym:
                ltp = store.get_ltp(sym)
                if ltp:
                    tick_sym = sym
            # Validate range — discard if value looks like an options premium or is stale
            if ltp and instrument:
                meta  = MCX_CONTRACTS.get(instrument, {})
                min_p = meta.get("min_price", 0)
                max_p = meta.get("max_price", float("inf"))
                if ltp < min_p or ltp > max_p:
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        f"[Dashboard] MCX LTP {ltp:.1f} out of range [{min_p}-{max_p}] "
                        f"for {instrument} — discarding stale value"
                    )
                    ltp = None
                    tick_sym = None
            # Compute tick age so the frontend can warn on stale data
            if ltp and tick_sym:
                try:
                    last_tick = store.get_latest_tick(tick_sym)
                    if last_tick and last_tick.get("timestamp"):
                        age_s = (_now_ist - last_tick["timestamp"]).total_seconds()
                except Exception:
                    pass
            # Key by instrument name so the frontend lookup is rollover-safe
            if ltp:
                commodity_ltps[instrument] = {"ltp": ltp, "age_s": age_s}
    except Exception:
        pass

    # Learning trades+stats — only when changed (per-connection hash)
    learning, new_hash = _get_learning_payload(conn_learning_hash)

    return {
        "timestamp":       datetime.now(tz=IST).isoformat(),
        "mode":            order_manager.mode,
        "stats":           stats,
        "risk":            risk,
        "positions":       positions,
        "pending_signals": pending,
        "ltps":            ltps,
        "learning_ltps":   learning_ltps,
        "commodity_ltps":  commodity_ltps,
        "paper_wallet":    paper_wallet,
        "system_alerts":   system_alerts,
        "token_status":    token_status,
        "learning":        learning,
    }, new_hash