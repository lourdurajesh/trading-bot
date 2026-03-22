"""
weekly_agent.py
───────────────
Runs every Sunday for deep analysis.
More thorough than nightly — full 3-year backtest on all candidates,
strategy optimisation, portfolio review, and weekly outlook.

Run manually:   python weekly_agent.py
Scheduled:      Windows Task Scheduler every Sunday at 9:00 AM
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("weekly_agent")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
REPORTS_DIR       = "db/weekly_reports"
os.makedirs(REPORTS_DIR, exist_ok=True)


def run_weekly_agent():
    """Full weekly deep analysis."""
    logger.info("=" * 60)
    logger.info("  Weekly Agent — Deep Analysis Starting")
    logger.info(f"  {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}")
    logger.info("=" * 60)

    report = {
        "week_ending":      datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "generated_at":     datetime.now(tz=timezone.utc).isoformat(),
        "strategy_grades":  {},
        "top_candidates":   [],
        "weekly_outlook":   "",
        "portfolio_review": {},
        "risk_parameters":  {},
    }

    # ── 1. Full backtest all strategies on all watchlist symbols ──
    logger.info("Phase 1: Full backtest — all strategies × all symbols")
    strategy_grades = _full_backtest()
    report["strategy_grades"] = strategy_grades

    # ── 2. Deep universe scan ─────────────────────────────────────
    logger.info("Phase 2: Deep universe scan")
    top_candidates = _deep_universe_scan()
    report["top_candidates"] = top_candidates

    # ── 3. Weekly outlook from Claude ────────────────────────────
    logger.info("Phase 3: Generating weekly outlook")
    outlook = _generate_weekly_outlook(strategy_grades, top_candidates)
    report["weekly_outlook"] = outlook

    # ── 4. Portfolio review ───────────────────────────────────────
    logger.info("Phase 4: Portfolio review")
    report["portfolio_review"] = _review_portfolio()

    # ── 5. Risk parameter calibration ────────────────────────────
    logger.info("Phase 5: Risk parameter review")
    report["risk_parameters"] = _review_risk_params(strategy_grades)

    # ── 6. Save report ────────────────────────────────────────────
    week_str     = datetime.now(tz=timezone.utc).strftime("%Y_W%W")
    report_path  = os.path.join(REPORTS_DIR, f"weekly_{week_str}.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Also save readable text report
    text_report  = _format_text_report(report)
    text_path    = report_path.replace(".json", ".txt")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text_report)

    logger.info(f"Weekly report saved: {report_path}")
    logger.info(f"Text report saved:   {text_path}")

    _send_weekly_alert(report)

    logger.info("=" * 60)
    logger.info("  Weekly Agent — Complete")
    logger.info("=" * 60)

    return report


def _full_backtest() -> dict:
    """Run full 3-year backtest on all watchlist symbols × all strategies."""
    from backtesting.data_fetcher import fetch_all
    from backtesting.backtest_engine import BacktestEngine
    from backtesting.performance import compute_metrics, format_report
    from strategies.trend_follow import TrendFollowStrategy
    from strategies.mean_reversion import MeanReversionStrategy
    from config.watchlist import ALL_NSE_SYMBOLS

    engine     = BacktestEngine()
    strategies = {
        "TrendFollow":    TrendFollowStrategy(),
        "MeanReversion":  MeanReversionStrategy(),
    }

    # Fetch 3 years of daily data for all symbols
    logger.info(f"  Fetching 3y daily data for {len(ALL_NSE_SYMBOLS)} symbols...")
    all_data = fetch_all(ALL_NSE_SYMBOLS, "1D", years_back=3)

    grades = {}
    for strat_name, strategy in strategies.items():
        logger.info(f"  Running {strat_name} on {len(all_data)} symbols...")
        strat_results = {}
        for symbol, df in all_data.items():
            try:
                result = engine.run(symbol, df, strategy, "1D")
                result = compute_metrics(result)
                strat_results[symbol] = {
                    "win_rate":       round(result.win_rate * 100, 1),
                    "profit_factor":  result.profit_factor,
                    "sharpe":         result.sharpe_ratio,
                    "max_drawdown":   result.max_drawdown_pct,
                    "total_return":   result.total_return_pct,
                    "total_trades":   result.total_trades,
                    "expectancy":     result.expectancy,
                }
                logger.info(f"    {symbol}: {result.summary()}")
            except Exception as e:
                logger.debug(f"    {symbol} failed: {e}")

        # Rank symbols by profit factor for this strategy
        ranked = sorted(
            strat_results.items(),
            key=lambda x: x[1].get("profit_factor", 0),
            reverse=True
        )
        grades[strat_name] = {
            "results":    strat_results,
            "top_10":     [s for s, _ in ranked[:10]],
            "avoid":      [s for s, r in ranked if r.get("profit_factor", 0) < 1.0],
            "avg_pf":     round(
                sum(r.get("profit_factor", 0) for _, r in strat_results.items())
                / max(len(strat_results), 1), 2
            ),
        }
        logger.info(f"  {strat_name}: avg PF={grades[strat_name]['avg_pf']:.2f}, "
                    f"top: {grades[strat_name]['top_10'][:3]}")

    return grades


def _deep_universe_scan() -> list[dict]:
    """Deep scan of NSE universe — more candidates than nightly."""
    from intelligence.theme_detector import theme_detector
    from intelligence.universe_scanner import universe_scanner
    from intelligence.news_scraper import get_news_for_symbol

    # Use currently active themes
    themes = theme_detector.get_active_themes()
    if not themes:
        # Fetch fresh headlines to detect themes
        from nightly_agent import _fetch_market_headlines
        headlines = _fetch_market_headlines()
        themes    = theme_detector.detect(headlines)

    candidates = universe_scanner.scan(themes, max_stocks=50)
    return [
        {
            "symbol":           c.symbol,
            "company":          c.company_name,
            "sector":           c.sector,
            "themes":           c.theme_match,
            "conviction":       c.theme_conviction,
            "overall_score":    c.overall_score,
        }
        for c in candidates[:20]
    ]


def _generate_weekly_outlook(strategy_grades: dict, top_candidates: list) -> str:
    """Generate weekly market outlook using Claude or rule-based."""
    from intelligence.macro_data import macro_collector
    macro = macro_collector.get_snapshot(force_refresh=True)

    if not ANTHROPIC_API_KEY:
        best_strat   = max(strategy_grades.items(),
                          key=lambda x: x[1].get("avg_pf", 0))[0] if strategy_grades else "TrendFollow"
        top_symbols  = [c["symbol"] for c in top_candidates[:5]]
        return (
            f"[SIMULATION] Weekly outlook for week ahead:\n"
            f"Macro score: {macro.macro_score:+.1f}/10 — {macro.summary}\n"
            f"Best performing strategy: {best_strat}\n"
            f"Top candidates: {', '.join(top_symbols)}\n"
            f"Add ANTHROPIC_API_KEY for full AI-generated outlook."
        )

    import requests
    candidates_text = "\n".join([
        f"  {c['symbol']} ({c['sector']}) — themes: {', '.join(c['themes'])}"
        for c in top_candidates[:10]
    ])

    strategy_text = "\n".join([
        f"  {name}: avg PF={data.get('avg_pf', 0):.2f}, top stocks: {data.get('top_10', [])[:3]}"
        for name, data in strategy_grades.items()
    ])

    prompt = f"""You are a senior Indian equity fund manager preparing a weekly briefing.

Macro environment:
  {macro.summary}
  Macro score: {macro.macro_score:+.1f}/10

Strategy backtest results this week:
{strategy_text}

Top theme-driven stock candidates:
{candidates_text}

Write a concise weekly outlook (max 200 words) covering:
1. Overall market environment for the week ahead
2. Which strategies to favour and why
3. Top 3 specific stock opportunities with brief thesis
4. Key risks to watch

Write in a professional analyst style, specific to Indian markets."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        return resp.json()["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude weekly outlook failed: {e}")
        return "Outlook generation failed — check API key"


def _review_portfolio() -> dict:
    """Review current portfolio performance."""
    try:
        from risk.portfolio_tracker import portfolio_tracker
        stats = portfolio_tracker.get_stats()
        return {
            "total_pnl":        stats.get("total_pnl", 0),
            "total_pnl_pct":    stats.get("total_pnl_pct", 0),
            "win_rate":         stats.get("win_rate", 0),
            "total_trades":     stats.get("total_trades", 0),
            "drawdown":         stats.get("drawdown_pct", 0),
            "avg_rr":           stats.get("avg_rr", 0),
        }
    except Exception as e:
        logger.debug(f"Portfolio review failed: {e}")
        return {}


def _review_risk_params(strategy_grades: dict) -> dict:
    """Suggest risk parameter adjustments based on backtest results."""
    from config.settings import RISK_PER_TRADE_PCT, DAILY_LOSS_LIMIT_PCT
    suggestions = {}

    # If strategies are performing poorly, suggest reducing risk
    avg_pfs = [d.get("avg_pf", 1.0) for d in strategy_grades.values()]
    overall_pf = sum(avg_pfs) / len(avg_pfs) if avg_pfs else 1.0

    if overall_pf < 1.0:
        suggestions["risk_per_trade_pct"] = max(0.5, RISK_PER_TRADE_PCT * 0.75)
        suggestions["reason"] = "Strategies underperforming — reduce risk per trade"
    elif overall_pf > 2.0:
        suggestions["risk_per_trade_pct"] = min(2.5, RISK_PER_TRADE_PCT * 1.1)
        suggestions["reason"] = "Strategies performing well — can modestly increase risk"
    else:
        suggestions["risk_per_trade_pct"] = RISK_PER_TRADE_PCT
        suggestions["reason"] = "Performance in acceptable range — maintain current risk"

    return suggestions


def _format_text_report(report: dict) -> str:
    """Format weekly report as readable text."""
    lines = [
        "=" * 70,
        f"WEEKLY ANALYSIS REPORT — {report['week_ending']}",
        "=" * 70,
        "",
        "WEEKLY OUTLOOK:",
        report.get("weekly_outlook", "Not available"),
        "",
        "STRATEGY PERFORMANCE:",
    ]
    for strat, data in report.get("strategy_grades", {}).items():
        lines.append(f"  {strat}: avg PF={data.get('avg_pf', 0):.2f}")
        lines.append(f"    Top symbols: {data.get('top_10', [])[:5]}")
        lines.append(f"    Avoid:       {data.get('avoid', [])[:5]}")

    lines += [
        "",
        "TOP CANDIDATES FOR NEXT WEEK:",
    ]
    for c in report.get("top_candidates", [])[:10]:
        lines.append(
            f"  {c['symbol'].replace('NSE:','').replace('-EQ','')} "
            f"({c['sector']}) — {', '.join(c['themes'])}"
        )

    portfolio = report.get("portfolio_review", {})
    if portfolio:
        lines += [
            "",
            "PORTFOLIO REVIEW:",
            f"  Total P&L:   ₹{portfolio.get('total_pnl', 0):+,.0f} ({portfolio.get('total_pnl_pct', 0):+.1f}%)",
            f"  Win rate:    {portfolio.get('win_rate', 0):.0f}%",
            f"  Total trades:{portfolio.get('total_trades', 0)}",
            f"  Max DD:      {portfolio.get('drawdown', 0):.1f}%",
        ]

    risk = report.get("risk_parameters", {})
    if risk:
        lines += [
            "",
            "RISK PARAMETER REVIEW:",
            f"  Suggested risk/trade: {risk.get('risk_per_trade_pct', 0):.1f}%",
            f"  Reason: {risk.get('reason', '')}",
        ]

    lines += ["", "=" * 70]
    return "\n".join(lines)


def _send_weekly_alert(report: dict) -> None:
    """Send weekly report summary via Telegram."""
    try:
        from notifications.alert_service import alert_service
        outlook_preview = report.get("weekly_outlook", "")[:200]
        top_stocks = ", ".join([
            c["symbol"].replace("NSE:", "").replace("-EQ", "")
            for c in report.get("top_candidates", [])[:5]
        ])
        msg = (
            f"📊 *WEEKLY ANALYSIS READY*\n"
            f"Week: `{report['week_ending']}`\n\n"
            f"Top candidates: `{top_stocks}`\n\n"
            f"Outlook: {outlook_preview}..."
        )
        alert_service._send(msg)
    except Exception as e:
        logger.debug(f"Weekly alert failed: {e}")


if __name__ == "__main__":
    run_weekly_agent()
