"""
nightly_agent.py
────────────────
Runs every night at 8:30 PM after market close.
Prepares tomorrow's trading playbook.

What it does:
  1. Fetches all market news from the day
  2. Detects active themes
  3. Scans NSE universe for theme-matched stocks
  4. Runs quick backtest on candidates
  5. Uses Claude to write tomorrow's strategy for each stock
  6. Saves playbook to db/playbook_YYYYMMDD.json
  7. Updates config/watchlist dynamically

Run manually:   python nightly_agent.py
Scheduled:      Windows Task Scheduler at 8:30 PM daily
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import requests

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

from config.logging_ist import setup_logging
setup_logging(level=logging.INFO, fmt="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nightly_agent")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
PLAYBOOK_DIR      = "db/playbooks"
os.makedirs(PLAYBOOK_DIR, exist_ok=True)


def run_nightly_agent():
    """Main nightly agent routine."""
    logger.info("=" * 60)
    logger.info("  Nightly Agent — Starting")
    logger.info(f"  {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M IST')}")
    logger.info("=" * 60)

    playbook = {
        "date":          datetime.now(tz=IST).strftime("%Y-%m-%d"),
        "generated_at":  datetime.now(tz=IST).isoformat(),
        "themes":        [],
        "watchlist":     [],
        "stock_plays":   [],
        "macro_context": "",
        "risk_level":    "NORMAL",   # LOW | NORMAL | HIGH | EXTREME
    }

    # ── Step 1: Collect today's news ─────────────────────────────
    logger.info("Step 1: Collecting market news...")
    headlines = _fetch_market_headlines()
    logger.info(f"  {len(headlines)} headlines collected")

    # ── Step 2: Detect themes ─────────────────────────────────────
    logger.info("Step 2: Detecting market themes...")
    from intelligence.theme_detector import theme_detector
    themes = theme_detector.detect(headlines)
    playbook["themes"] = [
        {
            "name":       t.name,
            "description": t.description,
            "catalyst":   t.catalyst,
            "direction":  t.direction,
            "duration":   t.duration,
            "sectors":    t.sectors,
            "conviction": t.conviction,
        }
        for t in themes
    ]
    logger.info(f"  {len(themes)} themes: {[t.name for t in themes]}")

    # ── Step 3: Macro snapshot ────────────────────────────────────
    logger.info("Step 3: Fetching macro data...")
    from intelligence.macro_data import macro_collector
    macro = macro_collector.get_snapshot(force_refresh=True)
    playbook["macro_context"] = macro.summary

    # Set risk level based on macro
    if macro.nifty_vix > 25 or macro.macro_score < -6:
        playbook["risk_level"] = "EXTREME"
    elif macro.nifty_vix > 20 or macro.macro_score < -3:
        playbook["risk_level"] = "HIGH"
    elif macro.nifty_vix < 14 and macro.macro_score > 3:
        playbook["risk_level"] = "LOW"
    logger.info(f"  Macro score: {macro.macro_score:+.1f} | Risk: {playbook['risk_level']}")

    # ── Step 4: Scan universe for candidates ──────────────────────
    logger.info("Step 4: Scanning NSE universe...")
    from intelligence.universe_scanner import universe_scanner
    from config.watchlist import ALL_NSE_SYMBOLS
    candidates = universe_scanner.scan(themes, max_stocks=40)

    # Build dynamic watchlist from already-scanned candidates (avoids double scan)
    additions = []
    for c in candidates:
        if c.symbol not in ALL_NSE_SYMBOLS and len(additions) < 20:
            additions.append(c.symbol)
    dynamic_watchlist = list(ALL_NSE_SYMBOLS) + additions
    logger.info(
        f"[UniverseScanner] Watchlist: {len(ALL_NSE_SYMBOLS)} base + "
        f"{len(additions)} theme additions = {len(dynamic_watchlist)} total"
    )
    playbook["watchlist"] = dynamic_watchlist
    logger.info(f"  {len(candidates)} candidates | {len(dynamic_watchlist)} total watchlist")

    # ── Step 5: Quick backtest top candidates ─────────────────────
    logger.info("Step 5: Running quick backtests...")
    backtest_results = _run_quick_backtests(candidates[:15])

    # ── Step 6: Generate stock plays with Claude ──────────────────
    logger.info("Step 6: Generating stock plays...")
    stock_plays = _generate_stock_plays(
        candidates[:10], backtest_results, macro, themes
    )
    playbook["stock_plays"] = stock_plays
    logger.info(f"  {len(stock_plays)} stock plays generated")

    # ── Step 7: Save playbook ─────────────────────────────────────
    date_str      = datetime.now(tz=IST).strftime("%Y%m%d")
    playbook_path = os.path.join(PLAYBOOK_DIR, f"playbook_{date_str}.json")
    with open(playbook_path, "w") as f:
        json.dump(playbook, f, indent=2, default=str)

    logger.info(f"Playbook saved: {playbook_path}")

    # ── Step 8: Update live watchlist for tomorrow ────────────────
    _update_live_watchlist(dynamic_watchlist)

    # ── Step 9: Send summary alert ────────────────────────────────
    _send_nightly_alert(playbook)

    logger.info("=" * 60)
    logger.info("  Nightly Agent — Complete")
    logger.info("=" * 60)

    return playbook


def _fetch_market_headlines() -> list[str]:
    """Fetch today's market headlines from multiple sources."""
    headlines = []
    sources = [
        "https://economictimes.indiatimes.com/rssfeedstopstories.cms",
        "https://www.moneycontrol.com/rss/marketreports.xml",
    ]
    for url in sources:
        try:
            from bs4 import BeautifulSoup
            import requests
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            soup = BeautifulSoup(resp.content, "xml")
            for item in soup.find_all("item")[:20]:
                title = item.find("title")
                if title:
                    headlines.append(title.text.strip())
        except Exception as e:
            logger.debug(f"Headline fetch failed for {url}: {e}")

    # NSE bulk deals and major announcements
    try:
        import requests
        session = requests.Session()
        session.get("https://www.nseindia.com",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp = session.get(
            "https://www.nseindia.com/api/corporates-announcements?index=equities",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10
        )
        data = resp.json().get("data", [])[:20]
        for item in data:
            subject = item.get("subject", "")
            if subject:
                headlines.append(f"NSE: {subject}")
    except Exception as e:
        logger.debug(f"NSE announcements failed: {e}")

    return list(set(headlines))   # deduplicate


def _run_quick_backtests(candidates) -> dict:
    """Run quick backtest on top candidates."""
    from backtesting.data_fetcher import fetch_historical
    from backtesting.backtest_engine import BacktestEngine
    from backtesting.performance import compute_metrics
    from strategies.trend_follow import TrendFollowStrategy
    from strategies.mean_reversion import MeanReversionStrategy

    engine     = BacktestEngine()
    strategies = [TrendFollowStrategy(), MeanReversionStrategy()]
    results    = {}

    for candidate in candidates:
        try:
            df = fetch_historical(candidate.symbol, "1D", years_back=3)
            if df is None or len(df) < 100:
                continue

            best_result = None
            for strategy in strategies:
                result = engine.run(candidate.symbol, df, strategy, "1D")
                result = compute_metrics(result)
                if best_result is None or result.profit_factor > best_result.profit_factor:
                    best_result = result

            if best_result:
                results[candidate.symbol] = {
                    "strategy":       best_result.strategy,
                    "win_rate":       round(best_result.win_rate * 100, 1),
                    "profit_factor":  best_result.profit_factor,
                    "sharpe":         best_result.sharpe_ratio,
                    "max_drawdown":   best_result.max_drawdown_pct,
                    "total_trades":   best_result.total_trades,
                    "total_return":   best_result.total_return_pct,
                }
                logger.info(f"  {candidate.symbol}: {best_result.summary()}")

        except Exception as e:
            logger.debug(f"  Backtest failed for {candidate.symbol}: {e}")

    return results


def _generate_stock_plays(candidates, backtest_results, macro, themes) -> list[dict]:
    """
    Generate specific trade setups for each candidate.
    Uses Claude if available, else generates rule-based plays.
    """
    plays = []

    for candidate in candidates:
        bt = backtest_results.get(candidate.symbol, {})

        # Skip if backtest shows poor historical performance
        if bt and bt.get("profit_factor", 0) < 1.0:
            logger.info(f"  Skipping {candidate.symbol}: poor backtest (PF={bt.get('profit_factor', 0):.2f})")
            continue

        if ANTHROPIC_API_KEY:
            play = _generate_play_with_claude(candidate, bt, macro, themes)
        else:
            play = _generate_play_with_rules(candidate, bt, macro)

        if play:
            plays.append(play)

    return plays


def _generate_play_with_claude(candidate, bt: dict, macro, themes) -> Optional[dict]:
    """Ask Claude to generate a specific trade setup for a stock."""
    theme_text = "\n".join([
        f"  - {t.name}: {t.description} ({t.direction}, {t.duration})"
        for t in themes if t.name in candidate.theme_match
    ])

    bt_text = (
        f"  Win rate: {bt.get('win_rate', 0):.0f}% | "
        f"Profit factor: {bt.get('profit_factor', 0):.2f} | "
        f"Sharpe: {bt.get('sharpe', 0):.2f} | "
        f"Total return: {bt.get('total_return', 0):+.0f}%"
        if bt else "  No backtest data available"
    )

    prompt = f"""You are a senior NSE equity analyst preparing tomorrow's trading playbook.

Stock: {candidate.symbol} ({candidate.company_name})
Sector: {candidate.sector}
Current price: ₹{candidate.price:.2f}
Avg daily turnover: ₹{candidate.avg_turnover_cr:.1f} Crores

Why this stock is on the radar:
{theme_text}

3-year backtest results ({bt.get('strategy', 'trend')} strategy):
{bt_text}

Macro environment:
  {macro.summary}
  Macro score: {macro.macro_score:+.1f}/10

Generate a specific trade setup for tomorrow. Respond ONLY with JSON:
{{
  "symbol": "{candidate.symbol}",
  "company": "{candidate.company_name}",
  "thesis": "one paragraph trade thesis",
  "strategy": "TREND_FOLLOW or MEAN_REVERSION or BREAKOUT_WATCH",
  "entry_zone": {{"low": 0.0, "high": 0.0}},
  "stop_loss": 0.0,
  "target_1": 0.0,
  "target_2": 0.0,
  "expected_duration": "X days",
  "conviction": 0.0-10.0,
  "catalysts": ["catalyst1", "catalyst2"],
  "risks": ["risk1", "risk2"],
  "themes": {candidate.theme_match}
}}"""

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        content = resp.json()["content"][0]["text"].strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content)

    except Exception as e:
        logger.debug(f"Claude play generation failed for {candidate.symbol}: {e}")
        return _generate_play_with_rules(candidate, bt, macro)


def _generate_play_with_rules(candidate, bt: dict, macro) -> dict:
    """Rule-based play generation when Claude API not available."""
    price = candidate.price
    if price <= 0:
        return {}

    atr_estimate = price * 0.02   # rough 2% ATR estimate
    return {
        "symbol":            candidate.symbol,
        "company":           candidate.company_name,
        "thesis":            f"Theme-driven opportunity: {', '.join(candidate.theme_match)}. "
                             f"Sector: {candidate.sector}.",
        "strategy":          "BREAKOUT_WATCH",
        "entry_zone":        {"low": round(price * 0.99, 2), "high": round(price * 1.01, 2)},
        "stop_loss":         round(price - 2 * atr_estimate, 2),
        "target_1":          round(price + 3 * atr_estimate, 2),
        "target_2":          round(price + 5 * atr_estimate, 2),
        "expected_duration": "5-15 days",
        "conviction":        round(candidate.theme_conviction * 8, 1),
        "catalysts":         candidate.theme_match,
        "risks":             ["Theme fade", "Macro reversal"],
        "themes":            candidate.theme_match,
        "simulated":         True,
    }


def _update_live_watchlist(symbols: list[str]) -> None:
    """Write tomorrow's watchlist to a JSON file for the bot to read at startup."""
    watchlist_path = "db/dynamic_watchlist.json"
    os.makedirs("db", exist_ok=True)
    with open(watchlist_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(tz=IST).isoformat(),
            "symbols":      symbols,
        }, f, indent=2)
    logger.info(f"Dynamic watchlist saved: {len(symbols)} symbols → {watchlist_path}")


def _send_nightly_alert(playbook: dict) -> None:
    """Send Telegram summary of tonight's playbook."""
    try:
        from notifications.alert_service import alert_service
        themes_text = ", ".join(t["name"] for t in playbook["themes"][:3])
        plays_text  = "\n".join(
            f"  {p['symbol'].replace('NSE:','').replace('-EQ','')} "
            f"({p.get('conviction', 0):.0f}/10) — {p.get('strategy', '')}"
            for p in playbook["stock_plays"][:5]
        )
        msg = (
            f"📋 *NIGHTLY PLAYBOOK READY*\n"
            f"Risk level: `{playbook['risk_level']}`\n"
            f"Themes: `{themes_text}`\n"
            f"Macro: `{playbook['macro_context'][:80]}`\n\n"
            f"Top plays:\n{plays_text}\n\n"
            f"Watchlist: `{len(playbook['watchlist'])} stocks`"
        )
        alert_service._send(msg)
    except Exception as e:
        logger.debug(f"Nightly alert failed: {e}")


if __name__ == "__main__":
    run_nightly_agent()
