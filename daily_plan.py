"""
daily_plan.py
─────────────
Generates a structured daily trading plan with time-stamped checklist.
Covers 4 phases of the trading day:
  - Pre-market  (8:30 AM – 9:15 AM)
  - Opening     (9:15 AM – 10:30 AM)
  - Midday      (10:30 AM – 2:00 PM)
  - Closing     (2:00 PM – 3:30 PM)

Uses Claude to personalise the plan based on:
  - Today's macro environment
  - Nightly playbook (if available)
  - Current open positions
  - Yesterday's trade outcomes

Run manually:   python daily_plan.py
Auto-runs:      main.py calls this at 8:45 AM each morning
API endpoint:   GET /plan/today
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("daily_plan")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
PLANS_DIR         = "db/daily_plans"
os.makedirs(PLANS_DIR, exist_ok=True)


@dataclass
class ChecklistItem:
    time:        str         # e.g. "08:45"
    task:        str         # what to do
    detail:      str         # how to do it / what to look for
    phase:       str         # PRE_MARKET | OPENING | MIDDAY | CLOSING
    priority:    str         # HIGH | MEDIUM | LOW
    automated:   bool        # True = bot does it, False = you do it
    done:        bool  = False
    id:          str   = ""


@dataclass
class DailyPlan:
    date:           str
    generated_at:   str
    market_theme:   str          # overall theme for today
    risk_level:     str          # LOW | NORMAL | HIGH | EXTREME
    focus_stocks:   list         # top stocks to watch today
    checklist:      list[ChecklistItem]
    macro_context:  str
    key_levels:     dict         # important Nifty levels for today
    analyst_briefing: str        # Claude's morning briefing
    rules_for_today:  list[str]  # specific rules based on current conditions


class DailyPlanGenerator:

    def generate(self, force_refresh: bool = False) -> DailyPlan:
        """
        Generate today's trading plan.
        Uses cached plan if already generated today.
        """
        today     = datetime.now(tz=IST).date().strftime("%Y-%m-%d")
        plan_path = os.path.join(PLANS_DIR, f"plan_{today}.json")

        # Return cached plan if exists and not forced
        if not force_refresh and os.path.exists(plan_path):
            try:
                with open(plan_path) as f:
                    data = json.load(f)
                logger.info(f"[DailyPlan] Loaded cached plan for {today}")
                return self._from_dict(data)
            except Exception:
                pass

        logger.info(f"[DailyPlan] Generating plan for {today}...")

        # ── Gather context ────────────────────────────────────────
        macro        = self._get_macro()
        playbook     = self._load_playbook(today)
        positions    = self._get_positions()
        nifty_levels = self._compute_nifty_levels()

        # ── Build base checklist ──────────────────────────────────
        checklist = self._build_base_checklist(macro, playbook, positions)

        # ── Determine focus stocks ────────────────────────────────
        focus_stocks = self._get_focus_stocks(playbook, positions)

        # ── Risk level ────────────────────────────────────────────
        risk_level = self._assess_risk_level(macro)

        # ── Claude briefing ───────────────────────────────────────
        briefing, rules = self._generate_briefing(
            macro, playbook, positions, focus_stocks, risk_level
        )

        plan = DailyPlan(
            date             = today,
            generated_at     = datetime.now(tz=IST).isoformat(),
            market_theme     = self._detect_theme(macro, playbook),
            risk_level       = risk_level,
            focus_stocks     = focus_stocks,
            checklist        = checklist,
            macro_context    = macro.get("summary", "Macro data unavailable"),
            key_levels       = nifty_levels,
            analyst_briefing = briefing,
            rules_for_today  = rules,
        )

        self._save_plan(plan, plan_path)
        logger.info(f"[DailyPlan] Plan generated: {len(checklist)} tasks, "
                    f"risk={risk_level}, {len(focus_stocks)} focus stocks")
        return plan

    # ─────────────────────────────────────────────────────────────
    # CHECKLIST BUILDER
    # ─────────────────────────────────────────────────────────────

    def _build_base_checklist(self, macro: dict, playbook: dict, positions: list) -> list[ChecklistItem]:
        """Build the full day checklist — 20+ specific tasks."""
        items = []
        vix   = macro.get("nifty_vix", 15)
        risk  = self._assess_risk_level(macro)

        # ── PRE-MARKET (8:30 – 9:14) ─────────────────────────────
        items += [
            ChecklistItem(
                id="PM01", time="08:30", phase="PRE_MARKET", priority="HIGH",
                automated=False,
                task="Check overnight global markets",
                detail=(
                    "Check: SGX Nifty futures, S&P 500 futures, Dow futures, "
                    "Nasdaq futures, Crude oil, Gold, USD/INR. "
                    "If SGX Nifty gap > 1% — reduce position sizes today."
                ),
            ),
            ChecklistItem(
                id="PM02", time="08:35", phase="PRE_MARKET", priority="HIGH",
                automated=True,
                task="Bot: Refresh Fyers access token",
                detail="python generate_token.py — must complete before 9:00 AM",
            ),
            ChecklistItem(
                id="PM03", time="08:40", phase="PRE_MARKET", priority="HIGH",
                automated=False,
                task="Read nightly playbook",
                detail=(
                    "Open db/playbooks/playbook_TODAY.json. "
                    "Review: risk level, active themes, top 5 stock plays, macro context. "
                    "This is your trading brief for the day."
                ),
            ),
            ChecklistItem(
                id="PM04", time="08:45", phase="PRE_MARKET", priority="HIGH",
                automated=True,
                task="Bot: Start main.py",
                detail=(
                    "python main.py — confirm WebSocket connects to Fyers. "
                    "Watch for: '25 NSE symbols subscribed' in logs."
                ),
            ),
            ChecklistItem(
                id="PM05", time="08:50", phase="PRE_MARKET", priority="MEDIUM",
                automated=False,
                task="Check NSE announcements and corporate actions",
                detail=(
                    "Visit nseindia.com → Corporate Actions. "
                    "Look for: ex-dividend dates, bonus issues, board meetings today. "
                    "Avoid buying stocks with ex-dividend today."
                ),
            ),
            ChecklistItem(
                id="PM06", time="08:55", phase="PRE_MARKET", priority="HIGH",
                automated=False,
                task=f"Review Nifty VIX — currently {vix:.1f}",
                detail=(
                    f"VIX {vix:.1f} = {'ELEVATED — reduce sizes 30%' if vix > 20 else 'NORMAL — proceed as planned' if vix < 16 else 'MODERATE — be selective'}. "
                    "VIX > 25: do NOT open new positions, manage existing only."
                ),
            ),
            ChecklistItem(
                id="PM07", time="09:00", phase="PRE_MARKET", priority="MEDIUM",
                automated=False,
                task="Set key Nifty levels for today",
                detail=(
                    "Mark these on your chart: Previous day high/low, "
                    "Weekly high/low, Key EMAs (21, 50, 200 daily). "
                    "Nifty above all EMAs = bullish day expected."
                ),
            ),
            ChecklistItem(
                id="PM08", time="09:05", phase="PRE_MARKET", priority="HIGH",
                automated=False,
                task="Review open positions — any overnight risk?",
                detail=(
                    f"You have {len(positions)} open position(s). "
                    "Check: Is stop loss still valid? "
                    "Did any news affect your holdings overnight? "
                    "Decide: Hold / Tighten stop / Exit before open."
                ) if positions else (
                    "No open positions. Clean slate — wait for confirmed setups."
                ),
            ),
            ChecklistItem(
                id="PM09", time="09:10", phase="PRE_MARKET", priority="MEDIUM",
                automated=False,
                task="Open dashboard — verify bot is live",
                detail=(
                    "http://localhost:3000 — confirm: "
                    "WebSocket connected (green dot), "
                    "Kill switch OFF, "
                    "Mode = MANUAL (until you're confident)."
                ),
            ),
            ChecklistItem(
                id="PM10", time="09:14", phase="PRE_MARKET", priority="HIGH",
                automated=False,
                task="Final go/no-go decision",
                detail=(
                    "Do NOT trade today if: VIX > 25 OR SGX gap > -1.5% OR "
                    "you did not sleep well OR you have important personal commitments. "
                    "Missing a day costs nothing. A bad day can cost capital."
                ),
            ),
        ]

        # ── OPENING (9:15 – 10:30) ────────────────────────────────
        items += [
            ChecklistItem(
                id="OP01", time="09:15", phase="OPENING", priority="HIGH",
                automated=False,
                task="Watch the open — DO NOT trade first 5 minutes",
                detail=(
                    "9:15–9:20: Observe only. Let the market reveal direction. "
                    "Gap up + holding = bullish. Gap up + fading = trap. "
                    "First 5 candles tell you the day's character."
                ),
            ),
            ChecklistItem(
                id="OP02", time="09:20", phase="OPENING", priority="HIGH",
                automated=True,
                task="Bot: First evaluation cycle fires",
                detail=(
                    "Bot begins scanning all 25 symbols. "
                    "Signals with confidence > 65% queue in dashboard. "
                    "Review each signal: does it match the day's theme?"
                ),
            ),
            ChecklistItem(
                id="OP03", time="09:25", phase="OPENING", priority="HIGH",
                automated=False,
                task="Assess opening character",
                detail=(
                    "Is Nifty above or below yesterday's close? "
                    "Above = favour long setups. Below = be cautious with longs. "
                    "High volume open = conviction. Low volume = wait."
                ),
            ),
            ChecklistItem(
                id="OP04", time="09:30", phase="OPENING", priority="MEDIUM",
                automated=False,
                task="Check focus stocks — are they setting up?",
                detail=(
                    "Review each focus stock from playbook. "
                    "Is price near the entry zone? "
                    "Is volume above average? "
                    "Does the sector theme still hold today?"
                ),
            ),
            ChecklistItem(
                id="OP05", time="09:45", phase="OPENING", priority="HIGH",
                automated=False,
                task="Review any pending signals in dashboard",
                detail=(
                    "For each pending signal: "
                    "1. Does the thesis still make sense? "
                    "2. Is the entry price still valid? "
                    "3. Is market direction supporting this trade? "
                    "Confirm or reject. Never confirm blindly."
                ),
            ),
            ChecklistItem(
                id="OP06", time="10:00", phase="OPENING", priority="MEDIUM",
                automated=False,
                task=f"Max {'1-2' if risk == 'HIGH' else '2-3'} new positions before 10:30",
                detail=(
                    f"Opening hour rule: max {'1' if risk == 'HIGH' else '2'} new trade(s). "
                    "Save capacity for midday setups. "
                    "Do not chase stocks that have already moved 2%+ from open."
                ),
            ),
            ChecklistItem(
                id="OP07", time="10:15", phase="OPENING", priority="MEDIUM",
                automated=True,
                task="Bot: Intelligence layer running on all signals",
                detail=(
                    "Each signal goes through: news check, macro check, "
                    "fundamental guard, analyst conviction score. "
                    "Only signals with conviction > 6/10 reach your dashboard."
                ),
            ),
            ChecklistItem(
                id="OP08", time="10:30", phase="OPENING", priority="HIGH",
                automated=False,
                task="Opening hour review — how is the day going?",
                detail=(
                    "After 1 hour: Is your thesis correct? "
                    "Are positions moving in the right direction? "
                    "If 2 consecutive losses: STOP. Re-evaluate. "
                    "Bad days start with small losses that compound."
                ),
            ),
        ]

        # ── MIDDAY (10:30 – 14:00) ────────────────────────────────
        items += [
            ChecklistItem(
                id="MD01", time="10:30", phase="MIDDAY", priority="MEDIUM",
                automated=False,
                task="Enter midday observation mode",
                detail=(
                    "10:30–12:00 is often the quietest part of the day. "
                    "Do not force trades. Let positions breathe. "
                    "Monitor stops — adjust if price moves significantly in your favour."
                ),
            ),
            ChecklistItem(
                id="MD02", time="11:00", phase="MIDDAY", priority="MEDIUM",
                automated=True,
                task="Bot: Hourly evaluation cycles running",
                detail=(
                    "Bot evaluates all symbols every 60 seconds. "
                    "New signals may appear on mean-reversion setups "
                    "as morning volatility settles."
                ),
            ),
            ChecklistItem(
                id="MD03", time="11:30", phase="MIDDAY", priority="MEDIUM",
                automated=False,
                task="Trailing stop review",
                detail=(
                    "For each profitable position: "
                    "Has price moved more than 1R in your favour? "
                    "If yes: move stop to breakeven. "
                    "Never let a winner turn into a loser."
                ),
            ),
            ChecklistItem(
                id="MD04", time="12:00", phase="MIDDAY", priority="HIGH",
                automated=False,
                task="Midday portfolio review",
                detail=(
                    "Check dashboard: total unrealised P&L. "
                    "If down > 1.5% for the day: STOP trading new positions. "
                    "Manage existing. Protect capital. There is always tomorrow."
                ),
            ),
            ChecklistItem(
                id="MD05", time="12:30", phase="MIDDAY", priority="LOW",
                automated=False,
                task="Check macro updates — any RBI or global news?",
                detail=(
                    "RBI often makes announcements midday. "
                    "Check: moneycontrol.com/markets, ET Markets. "
                    "Any surprise = re-evaluate all positions immediately."
                ),
            ),
            ChecklistItem(
                id="MD06", time="13:00", phase="MIDDAY", priority="MEDIUM",
                automated=True,
                task="Bot: FII/DII flow data available (NSE publishes ~1 PM)",
                detail=(
                    "NSE publishes institutional flows around 1 PM. "
                    "Bot macro module refreshes automatically. "
                    "FII net buyer > ₹500Cr = bullish confirmation."
                ),
            ),
            ChecklistItem(
                id="MD07", time="13:30", phase="MIDDAY", priority="MEDIUM",
                automated=False,
                task="Look for afternoon breakout setups",
                detail=(
                    "1:30–2:00 PM often sees fresh momentum as global markets open. "
                    "Watch for: stocks consolidating near highs, "
                    "volume picking up, sector rotation. "
                    "These are the cleanest breakout entries."
                ),
            ),
        ]

        # ── CLOSING (14:00 – 15:30) ───────────────────────────────
        items += [
            ChecklistItem(
                id="CL01", time="14:00", phase="CLOSING", priority="HIGH",
                automated=False,
                task="Enter closing phase — start wrapping up",
                detail=(
                    "After 2 PM: Do not open new swing positions. "
                    "Exception: strong breakout with clear catalyst. "
                    "Focus on managing existing positions toward target or stop."
                ),
            ),
            ChecklistItem(
                id="CL02", time="14:30", phase="CLOSING", priority="HIGH",
                automated=False,
                task="Intraday position decision — hold overnight?",
                detail=(
                    "For each open position, decide: "
                    "Hold overnight (conviction + clear stop)? "
                    "Or exit today (reduce overnight risk)? "
                    "Positions without a clear overnight thesis should be closed."
                ),
            ),
            ChecklistItem(
                id="CL03", time="15:00", phase="CLOSING", priority="HIGH",
                automated=False,
                task="Final 30-minute watch — closing momentum",
                detail=(
                    "Last 30 minutes often sees institutional rebalancing. "
                    "Strong close (near day's high) = bullish. "
                    "Weak close (near day's low) = bearish for tomorrow. "
                    "Adjust tomorrow's bias accordingly."
                ),
            ),
            ChecklistItem(
                id="CL04", time="15:15", phase="CLOSING", priority="MEDIUM",
                automated=True,
                task="Bot: Stop all new signal evaluations",
                detail=(
                    "Bot automatically stops generating new signals at 3:15 PM. "
                    "Any pending signals in MANUAL queue should be rejected — "
                    "too late in the day to open new positions."
                ),
            ),
            ChecklistItem(
                id="CL05", time="15:25", phase="CLOSING", priority="HIGH",
                automated=False,
                task="Verify all stop losses are set for overnight positions",
                detail=(
                    "Every position you hold overnight MUST have a stop loss placed. "
                    "Check on Fyers: are the SL orders showing in your order book? "
                    "If not: place them now before market closes."
                ),
            ),
            ChecklistItem(
                id="CL06", time="15:31", phase="CLOSING", priority="HIGH",
                automated=False,
                task="Post-market: Record today's trades in journal",
                detail=(
                    "Write 3 things: "
                    "1. What worked and why. "
                    "2. What didn't work and why. "
                    "3. One thing to do differently tomorrow. "
                    "This feeds the journal_analyser for weekly review."
                ),
            ),
            ChecklistItem(
                id="CL07", time="15:45", phase="CLOSING", priority="MEDIUM",
                automated=True,
                task="Bot: Nightly agent runs (auto-scheduled)",
                detail=(
                    "nightly_agent.py starts at 8:30 PM automatically. "
                    "It reads today's news, detects tomorrow's themes, "
                    "scans NSE universe, runs backtests, saves playbook. "
                    "Tomorrow's plan will be ready by 9:00 PM."
                ),
            ),
        ]

        return items

    # ─────────────────────────────────────────────────────────────
    # CLAUDE BRIEFING
    # ─────────────────────────────────────────────────────────────

    def _generate_briefing(
        self, macro: dict, playbook: dict, positions: list,
        focus_stocks: list, risk_level: str
    ) -> tuple[str, list[str]]:
        """Generate morning analyst briefing using Claude or rule-based."""
        if ANTHROPIC_API_KEY:
            return self._claude_briefing(macro, playbook, positions, focus_stocks, risk_level)
        return self._rule_briefing(macro, playbook, positions, focus_stocks, risk_level)

    def _claude_briefing(
        self, macro, playbook, positions, focus_stocks, risk_level
    ) -> tuple[str, list[str]]:
        themes     = [t.get("name", "") for t in playbook.get("themes", [])]
        plays      = playbook.get("stock_plays", [])[:3]
        plays_text = "\n".join([
            f"  {p.get('symbol','').replace('NSE:','').replace('-EQ','')} — "
            f"{p.get('thesis','')[:80]}"
            for p in plays
        ]) or "  No specific plays from nightly playbook"

        pos_text = (
            "\n".join([f"  {p.get('symbol','')} — P&L ₹{p.get('unrealised_pnl',0):+,.0f}"
                       for p in positions])
            if positions else "  No open positions"
        )

        prompt = f"""You are a senior trading desk analyst giving the morning briefing.
Be concise, specific, and actionable. Write like a Bloomberg terminal alert.

TODAY'S CONTEXT:
  Date:       {datetime.now(tz=IST).date().strftime('%A, %d %b %Y')}
  Risk level: {risk_level}
  VIX:        {macro.get('nifty_vix', 0):.1f} ({macro.get('vix_signal', 'unknown')})
  FII flow:   ₹{macro.get('fii_net_flow', 0):+,.0f} Cr ({macro.get('fii_signal', 'unknown')})
  SPX:        {macro.get('sp500_change_pct', 0):+.1f}%
  Crude:      ${macro.get('crude_oil_usd', 0):.0f} ({macro.get('crude_signal', 'stable')})

ACTIVE THEMES: {', '.join(themes) or 'None detected'}

TONIGHT'S STOCK PLAYS:
{plays_text}

OPEN POSITIONS:
{pos_text}

Write a morning briefing (max 120 words) covering:
1. Opening tone (1 sentence — bullish/neutral/bearish and why)
2. Key focus for today (what to watch)
3. Top trade opportunity

Then write exactly 3 rules specific to TODAY's conditions:
BRIEFING: <your briefing>
RULES: ["rule 1", "rule 2", "rule 3"]"""

        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 400,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=20,
            )
            content = resp.json()["content"][0]["text"]
            briefing = content
            rules    = []
            if "RULES:" in content:
                parts    = content.split("RULES:")
                briefing = parts[0].replace("BRIEFING:", "").strip()
                try:
                    rules = json.loads(parts[1].strip())
                except Exception:
                    rules = [parts[1].strip()]
            return briefing, rules

        except Exception as e:
            logger.debug(f"Claude briefing failed: {e}")
            return self._rule_briefing(macro, playbook, positions, focus_stocks, risk_level)

    def _rule_briefing(
        self, macro, playbook, positions, focus_stocks, risk_level
    ) -> tuple[str, list[str]]:
        vix         = macro.get("nifty_vix", 15)
        fii         = macro.get("fii_net_flow", 0)
        spx         = macro.get("sp500_change_pct", 0)
        fii_signal  = macro.get("fii_signal", "neutral")
        themes      = [t.get("name", "") for t in playbook.get("themes", [])]

        tone = "BULLISH" if (spx > 0.5 and fii > 500) else \
               "BEARISH" if (spx < -0.5 or fii < -500) else "NEUTRAL"

        briefing_parts = [
            f"[SIMULATION MODE — Add ANTHROPIC_API_KEY for AI briefing]",
            f"",
            f"Opening tone: {tone}. "
            f"SGX/SPX overnight: {spx:+.1f}%. "
            f"VIX at {vix:.1f} ({macro.get('vix_signal','normal')}). "
            f"FII {fii_signal} at ₹{fii:+,.0f}Cr.",
        ]

        if themes:
            briefing_parts.append(f"Active themes: {', '.join(themes[:3])}.")

        if focus_stocks:
            briefing_parts.append(
                f"Watch: {', '.join(s.replace('NSE:','').replace('-EQ','') for s in focus_stocks[:3])}."
            )

        rules = []
        if vix > 20:
            rules.append(f"VIX elevated at {vix:.0f} — reduce all position sizes by 30%")
        else:
            rules.append(f"VIX normal at {vix:.0f} — proceed with standard position sizing")

        if fii < -1000:
            rules.append("FII selling heavily — avoid banking and large-cap longs today")
        elif fii > 1000:
            rules.append("FII buying — banking and large-cap longs have institutional support")
        else:
            rules.append("FII flows neutral — focus on stock-specific themes")

        if spx < -1:
            rules.append("US markets down overnight — wait for Indian market to stabilise before entering")
        elif positions:
            rules.append(f"You have {len(positions)} open position(s) — protect P&L before adding new trades")
        else:
            rules.append("No open positions — wait for high-conviction setups, do not force trades")

        return "\n".join(briefing_parts), rules

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _get_macro(self) -> dict:
        try:
            from intelligence.macro_data import macro_collector
            snap = macro_collector.get_snapshot()
            return {
                "nifty_vix":       snap.nifty_vix,
                "vix_signal":      snap.vix_signal,
                "fii_net_flow":    snap.fii_net_flow,
                "fii_signal":      snap.fii_signal,
                "sp500_change_pct": snap.sp500_change_pct,
                "global_signal":   snap.global_signal,
                "crude_oil_usd":   snap.crude_oil_usd,
                "crude_signal":    snap.crude_signal,
                "usdinr":          snap.usdinr,
                "macro_score":     snap.macro_score,
                "summary":         snap.summary,
            }
        except Exception as e:
            logger.debug(f"Macro fetch failed: {e}")
            return {"nifty_vix": 15, "vix_signal": "normal", "fii_net_flow": 0,
                    "fii_signal": "neutral", "sp500_change_pct": 0,
                    "crude_oil_usd": 80, "macro_score": 0, "summary": "Macro unavailable"}

    def _load_playbook(self, today: str) -> dict:
        today_clean = today.replace("-", "")
        path = os.path.join("db", "playbooks", f"playbook_{today_clean}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"themes": [], "stock_plays": [], "watchlist": []}

    def _get_positions(self) -> list:
        try:
            from risk.portfolio_tracker import portfolio_tracker
            return portfolio_tracker.get_open_positions()
        except Exception:
            return []

    def _get_focus_stocks(self, playbook: dict, positions: list) -> list:
        focus = []
        # Add stocks from playbook
        for play in playbook.get("stock_plays", [])[:5]:
            sym = play.get("symbol", "")
            if sym and sym not in focus:
                focus.append(sym)
        # Add open position symbols
        for pos in positions:
            sym = pos.get("symbol", "")
            if sym and sym not in focus:
                focus.append(sym)
        return focus[:8]

    def _assess_risk_level(self, macro: dict) -> str:
        vix   = macro.get("nifty_vix", 15)
        score = macro.get("macro_score", 0)
        if vix > 25 or score < -6:  return "EXTREME"
        if vix > 20 or score < -3:  return "HIGH"
        if vix < 14 and score > 3:  return "LOW"
        return "NORMAL"

    def _detect_theme(self, macro: dict, playbook: dict) -> str:
        themes = playbook.get("themes", [])
        if themes:
            return themes[0].get("description", "Mixed market")
        score = macro.get("macro_score", 0)
        if score > 4:   return "Bullish macro environment — FII buying, low VIX"
        if score < -4:  return "Risk-off environment — defensive positioning advised"
        return "Neutral market — stock-specific themes dominate"

    def _compute_nifty_levels(self) -> dict:
        """Fetch key Nifty levels from Yahoo Finance."""
        try:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
                "?interval=1d&range=30d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8
            )
            data   = resp.json()
            result = data["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            highs  = result["indicators"]["quote"][0]["high"]
            lows   = result["indicators"]["quote"][0]["low"]

            closes = [c for c in closes if c]
            highs  = [h for h in highs  if h]
            lows   = [l for l in lows   if l]

            current  = closes[-1] if closes else 0
            prev_close = closes[-2] if len(closes) >= 2 else current
            week_high = max(highs[-5:]) if len(highs) >= 5 else current
            week_low  = min(lows[-5:])  if len(lows)  >= 5 else current
            month_high = max(highs) if highs else current
            month_low  = min(lows)  if lows  else current

            # Simple EMA approximations from close data
            ema20 = sum(closes[-20:]) / min(20, len(closes))

            return {
                "current":    round(current, 0),
                "prev_close": round(prev_close, 0),
                "week_high":  round(week_high, 0),
                "week_low":   round(week_low, 0),
                "month_high": round(month_high, 0),
                "month_low":  round(month_low, 0),
                "ema20":      round(ema20, 0),
                "gap_pct":    round((current - prev_close) / prev_close * 100, 2),
            }
        except Exception as e:
            logger.debug(f"Nifty levels fetch failed: {e}")
            return {}

    def _save_plan(self, plan: DailyPlan, path: str) -> None:
        data = {
            "date":             plan.date,
            "generated_at":     plan.generated_at,
            "market_theme":     plan.market_theme,
            "risk_level":       plan.risk_level,
            "focus_stocks":     plan.focus_stocks,
            "macro_context":    plan.macro_context,
            "key_levels":       plan.key_levels,
            "analyst_briefing": plan.analyst_briefing,
            "rules_for_today":  plan.rules_for_today,
            "checklist": [
                {
                    "id":        item.id,
                    "time":      item.time,
                    "phase":     item.phase,
                    "priority":  item.priority,
                    "automated": item.automated,
                    "task":      item.task,
                    "detail":    item.detail,
                    "done":      item.done,
                }
                for item in plan.checklist
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"[DailyPlan] Saved: {path}")

    def _from_dict(self, data: dict) -> DailyPlan:
        checklist = [
            ChecklistItem(
                id        = item.get("id", ""),
                time      = item.get("time", ""),
                phase     = item.get("phase", ""),
                priority  = item.get("priority", "MEDIUM"),
                automated = item.get("automated", False),
                task      = item.get("task", ""),
                detail    = item.get("detail", ""),
                done      = item.get("done", False),
            )
            for item in data.get("checklist", [])
        ]
        return DailyPlan(
            date             = data.get("date", ""),
            generated_at     = data.get("generated_at", ""),
            market_theme     = data.get("market_theme", ""),
            risk_level       = data.get("risk_level", "NORMAL"),
            focus_stocks     = data.get("focus_stocks", []),
            checklist        = checklist,
            macro_context    = data.get("macro_context", ""),
            key_levels       = data.get("key_levels", {}),
            analyst_briefing = data.get("analyst_briefing", ""),
            rules_for_today  = data.get("rules_for_today", []),
        )

    def mark_done(self, item_id: str, today: str = None) -> bool:
        """Mark a checklist item as done and save."""
        today     = today or datetime.now(tz=IST).date().strftime("%Y-%m-%d")
        plan_path = os.path.join(PLANS_DIR, f"plan_{today}.json")
        if not os.path.exists(plan_path):
            return False
        try:
            with open(plan_path) as f:
                data = json.load(f)
            for item in data.get("checklist", []):
                if item.get("id") == item_id:
                    item["done"] = True
            with open(plan_path, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False


# ── Module-level singleton ────────────────────────────────────────
daily_plan_generator = DailyPlanGenerator()


# ── Standalone execution ──────────────────────────────────────────
if __name__ == "__main__":
    plan = daily_plan_generator.generate(force_refresh=True)
    print(f"\n{'=' * 65}")
    print(f"DAILY TRADING PLAN — {plan.date}")
    print(f"{'=' * 65}")
    print(f"Theme:      {plan.market_theme}")
    print(f"Risk level: {plan.risk_level}")
    print(f"Macro:      {plan.macro_context}")
    if plan.key_levels:
        print(f"\nKey Nifty levels:")
        for k, v in plan.key_levels.items():
            print(f"  {k:<12} {v}")
    if plan.focus_stocks:
        print(f"\nFocus stocks: {', '.join(s.replace('NSE:','').replace('-EQ','') for s in plan.focus_stocks)}")
    print(f"\nMorning briefing:\n  {plan.analyst_briefing}")
    if plan.rules_for_today:
        print(f"\nRules for today:")
        for i, rule in enumerate(plan.rules_for_today, 1):
            print(f"  {i}. {rule}")
    print(f"\n{'─' * 65}")
    current_phase = None
    for item in plan.checklist:
        if item.phase != current_phase:
            phase_labels = {
                "PRE_MARKET": "PRE-MARKET  (8:30 – 9:14)",
                "OPENING":    "OPENING     (9:15 – 10:30)",
                "MIDDAY":     "MIDDAY      (10:30 – 14:00)",
                "CLOSING":    "CLOSING     (14:00 – 15:30)",
            }
            print(f"\n── {phase_labels.get(item.phase, item.phase)} ──")
            current_phase = item.phase
        bot_tag  = " [BOT]" if item.automated else ""
        prio_tag = " ⚠" if item.priority == "HIGH" else ""
        print(f"  {item.time}  {item.task}{bot_tag}{prio_tag}")
    print(f"\n{'=' * 65}\n")