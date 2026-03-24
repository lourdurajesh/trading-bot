"""
news_scraper.py
───────────────
Scrapes real-time news and social sentiment for a given stock.
Sources:
  - NSE corporate announcements (official, most reliable)
  - Economic Times RSS feed
  - Moneycontrol RSS feed
  - StockTwits (free API)
  - Reddit (r/IndiaInvestments, r/Nifty50) via free API

All results are returned as a list of NewsItem dataclasses.
No API keys required for any of these sources.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Request timeout and cache duration
REQUEST_TIMEOUT  = 8    # seconds
CACHE_MINUTES    = 15   # re-fetch after this many minutes


@dataclass
class NewsItem:
    source:     str
    headline:   str
    summary:    str
    url:        str
    published:  datetime
    sentiment:  str = "unknown"   # filled by analyst_agent
    score:      float = 0.0       # -10 to +10, filled by analyst_agent


class NewsCache:
    """Simple in-memory cache to avoid hammering sources."""
    def __init__(self):
        self._cache: dict[str, tuple[list[NewsItem], datetime]] = {}

    def get(self, key: str) -> Optional[list[NewsItem]]:
        if key in self._cache:
            items, cached_at = self._cache[key]
            if datetime.now(tz=IST) - cached_at < timedelta(minutes=CACHE_MINUTES):
                return items
        return None

    def set(self, key: str, items: list[NewsItem]) -> None:
        self._cache[key] = (items, datetime.now(tz=IST))


_cache = NewsCache()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_news_for_symbol(symbol: str, max_items: int = 20) -> list[NewsItem]:
    """
    Fetch recent news and social sentiment for a given symbol.
    Returns combined list from all sources, sorted by recency.

    symbol: Fyers format e.g. "NSE:RELIANCE-EQ"
    """
    # Extract clean company name for search queries
    company = _symbol_to_company(symbol)
    ticker  = _symbol_to_ticker(symbol)

    cache_key = f"{ticker}_{datetime.now(tz=IST).strftime('%Y%m%d%H%M')[:11]}"
    cached = _cache.get(cache_key)
    if cached:
        logger.debug(f"[NewsScr] Cache hit for {ticker}")
        return cached

    all_news: list[NewsItem] = []

    # Run all scrapers
    scrapers = [
        lambda: _scrape_nse_announcements(ticker),
        lambda: _scrape_et_rss(company),
        lambda: _scrape_moneycontrol_rss(company),
        lambda: _scrape_stocktwits(ticker),
        lambda: _scrape_reddit(company),
    ]

    for scraper in scrapers:
        try:
            items = scraper()
            all_news.extend(items)
        except Exception as e:
            logger.debug(f"[NewsScr] Scraper failed (non-fatal): {e}")

    # Sort by recency, deduplicate similar headlines
    all_news.sort(key=lambda x: x.published, reverse=True)
    all_news = _deduplicate(all_news)[:max_items]

    _cache.set(cache_key, all_news)
    logger.info(f"[NewsScr] {ticker}: {len(all_news)} items from {len(scrapers)} sources")
    return all_news


# ─────────────────────────────────────────────────────────────────
# SOURCE SCRAPERS
# ─────────────────────────────────────────────────────────────────

def _scrape_nse_announcements(ticker: str) -> list[NewsItem]:
    """NSE corporate announcements — most reliable, official source."""
    url = f"https://www.nseindia.com/api/corp-info?symbol={ticker}&section=announcements"
    items = []
    try:
        # NSE requires session cookies
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        announcements = data.get("data", [])[:5]
        for ann in announcements:
            dt_str = ann.get("an_dt", "")
            try:
                pub = datetime.strptime(dt_str, "%d-%b-%Y").replace(tzinfo=IST)
            except Exception:
                pub = datetime.now(tz=IST)
            items.append(NewsItem(
                source    = "NSE",
                headline  = ann.get("subject", "")[:200],
                summary   = ann.get("desc", "")[:500],
                url       = f"https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                published = pub,
            ))
    except Exception as e:
        logger.debug(f"[NewsScr] NSE announcements failed: {e}")
    return items


def _scrape_et_rss(company: str) -> list[NewsItem]:
    """Economic Times RSS — broad financial news coverage."""
    query = company.replace(" ", "+")
    url   = f"https://economictimes.indiatimes.com/rssfeedstopstories.cms"
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.content, "xml")
        for item in soup.find_all("item")[:15]:
            title = item.find("title")
            desc  = item.find("description")
            link  = item.find("link")
            pub   = item.find("pubDate")
            if not title:
                continue
            headline = title.text.strip()
            # Only include if relevant to this company
            if not _is_relevant(headline, company):
                continue
            try:
                pub_dt = datetime.strptime(
                    pub.text.strip(), "%a, %d %b %Y %H:%M:%S %z"
                ) if pub else datetime.now(tz=IST)
            except Exception:
                pub_dt = datetime.now(tz=IST)
            items.append(NewsItem(
                source    = "EconomicTimes",
                headline  = headline,
                summary   = BeautifulSoup(desc.text, "html.parser").get_text()[:300] if desc else "",
                url       = link.text.strip() if link else url,
                published = pub_dt,
            ))
    except Exception as e:
        logger.debug(f"[NewsScr] ET RSS failed: {e}")
    return items


def _scrape_moneycontrol_rss(company: str) -> list[NewsItem]:
    """Moneycontrol RSS — strong Indian market coverage."""
    url   = "https://www.moneycontrol.com/rss/marketreports.xml"
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.content, "xml")
        for item in soup.find_all("item")[:15]:
            title = item.find("title")
            desc  = item.find("description")
            link  = item.find("link")
            pub   = item.find("pubDate")
            if not title:
                continue
            headline = title.text.strip()
            if not _is_relevant(headline, company):
                continue
            try:
                pub_dt = datetime.strptime(
                    pub.text.strip(), "%a, %d %b %Y %H:%M:%S %z"
                ) if pub else datetime.now(tz=IST)
            except Exception:
                pub_dt = datetime.now(tz=IST)
            items.append(NewsItem(
                source    = "Moneycontrol",
                headline  = headline,
                summary   = BeautifulSoup(desc.text, "html.parser").get_text()[:300] if desc else "",
                url       = link.text.strip() if link else url,
                published = pub_dt,
            ))
    except Exception as e:
        logger.debug(f"[NewsScr] Moneycontrol RSS failed: {e}")
    return items


def _scrape_stocktwits(ticker: str) -> list[NewsItem]:
    """StockTwits — free API, retail trader sentiment."""
    url   = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        messages = data.get("messages", [])[:10]
        for msg in messages:
            body      = msg.get("body", "")[:280]
            created   = msg.get("created_at", "")
            sentiment = msg.get("entities", {}).get("sentiment", {})
            bull_bear = sentiment.get("basic", "neutral") if sentiment else "neutral"
            try:
                pub_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=IST)
            except Exception:
                pub_dt = datetime.now(tz=IST)
            items.append(NewsItem(
                source    = "StockTwits",
                headline  = body[:100],
                summary   = f"[{bull_bear.upper()}] {body}",
                url       = f"https://stocktwits.com/symbol/{ticker}",
                published = pub_dt,
                sentiment = bull_bear,
            ))
    except Exception as e:
        logger.debug(f"[NewsScr] StockTwits failed: {e}")
    return items


def _scrape_reddit(company: str) -> list[NewsItem]:
    """Reddit — r/IndiaInvestments and r/Nifty50 via free JSON API."""
    subreddits = ["IndiaInvestments", "Nifty50", "IndianStockMarket"]
    items = []
    for sub in subreddits:
        try:
            url  = f"https://www.reddit.com/r/{sub}/search.json?q={company}&sort=new&limit=5&restrict_sr=1"
            resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=REQUEST_TIMEOUT)
            data = resp.json()
            posts = data.get("data", {}).get("children", [])
            for post in posts:
                p = post.get("data", {})
                title  = p.get("title", "")
                body   = p.get("selftext", "")[:300]
                link   = f"https://reddit.com{p.get('permalink', '')}"
                score  = p.get("score", 0)
                created = p.get("created_utc", 0)
                pub_dt = datetime.fromtimestamp(created, tz=IST) if created else datetime.now(tz=IST)
                items.append(NewsItem(
                    source    = f"Reddit/{sub}",
                    headline  = title[:200],
                    summary   = f"[{score} upvotes] {body}",
                    url       = link,
                    published = pub_dt,
                ))
        except Exception as e:
            logger.debug(f"[NewsScr] Reddit/{sub} failed: {e}")
        time.sleep(0.5)   # Reddit rate limit
    return items


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

# Map Fyers symbol → company search name
SYMBOL_MAP = {
    "RELIANCE":    "Reliance Industries",
    "TCS":         "TCS Tata Consultancy",
    "HDFCBANK":    "HDFC Bank",
    "INFY":        "Infosys",
    "ICICIBANK":   "ICICI Bank",
    "HINDUNILVR":  "Hindustan Unilever HUL",
    "ITC":         "ITC Limited",
    "SBIN":        "State Bank SBI",
    "BHARTIARTL":  "Bharti Airtel",
    "KOTAKBANK":   "Kotak Bank",
    "LT":          "Larsen Toubro L&T",
    "AXISBANK":    "Axis Bank",
    "WIPRO":       "Wipro",
    "HCLTECH":     "HCL Technologies",
    "ASIANPAINT":  "Asian Paints",
    "MARUTI":      "Maruti Suzuki",
    "BAJFINANCE":  "Bajaj Finance",
    "TITAN":       "Titan Company",
    "ULTRACEMCO":  "UltraTech Cement",
    "NESTLEIND":   "Nestle India",
    "PERSISTENT":  "Persistent Systems",
    "COFORGE":     "Coforge",
    "POLICYBZR":   "PolicyBazaar PB Fintech",
    "NIFTY50":     "Nifty 50 index market",
    "NIFTYBANK":   "Bank Nifty banking sector",
}


def _symbol_to_ticker(symbol: str) -> str:
    """NSE:RELIANCE-EQ → RELIANCE"""
    return symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "")


def _symbol_to_company(symbol: str) -> str:
    """NSE:RELIANCE-EQ → Reliance Industries"""
    ticker = _symbol_to_ticker(symbol)
    return SYMBOL_MAP.get(ticker, ticker)


def _is_relevant(text: str, company: str) -> bool:
    """Check if a news headline mentions the company."""
    company_words = company.lower().split()
    text_lower    = text.lower()
    return any(word in text_lower for word in company_words if len(word) > 3)


def _deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """Remove near-duplicate headlines."""
    seen = set()
    unique = []
    for item in items:
        key = item.headline[:60].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
