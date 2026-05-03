"""
nse_participant_collector.py
────────────────────────────
Fetches NSE F&O participant-wise OI data daily at 5:30 PM IST.
Tracks FII net long/short in index futures (NIFTY, BANKNIFTY).
Computes day-over-day change — this is the signal, not the absolute level.

Schedule: run daily at 17:30 IST via main.py cron hook.
Output:   db/fii_fo_history.json

FII net change signal rules:
  +10,000 contracts/day = BULLISH (+3 score in conviction_scorer)
  -10,000 contracts/day = BEARISH (-3 score)
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "fii_fo_history.json")

# NSE CSV archive — more reliable than the JSON API which needs a live session cookie
_NSE_CSV_URL = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date}.csv"

# Participant codes in the NSE CSV (column header values)
_FII_CODE = "FII"
_DII_CODE = "DII"
_CLIENT_CODE = "Client"
_PRO_CODE = "Pro"


@dataclass
class ParticipantOIRow:
    date: str                   # YYYY-MM-DD
    symbol: str                 # NIFTY or BANKNIFTY
    fii_long: int
    fii_short: int
    fii_net: int
    fii_net_change: int         # vs previous day — THE SIGNAL
    dii_long: int
    dii_short: int
    dii_net: int
    client_long: int
    client_short: int
    client_net: int


class NSEParticipantCollector:
    """
    Downloads the daily NSE F&O participant-wise OI CSV and parses FII positions.

    The day-over-day FII net change is the key signal:
      > +10,000 contracts = institutional accumulation = BULLISH
      < -10,000 contracts = institutional selling     = BEARISH

    Usage:
        collector = NSEParticipantCollector()
        rows = collector.collect()   # call at 17:30 IST daily
    """

    def __init__(self):
        self._history: list[dict] = []
        self._load_history()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def collect(self, target_date: Optional[date] = None) -> list[ParticipantOIRow]:
        """
        Fetch today's NSE participant OI CSV, parse FII positions, persist.
        Returns list of ParticipantOIRow (one per index symbol).
        Call at 17:30 IST — data is published around 17:15 IST.
        """
        if target_date is None:
            target_date = datetime.now(tz=IST).date()

        date_str = target_date.strftime("%d%m%Y")   # NSE uses DDMMYYYY in filename
        url = _NSE_CSV_URL.format(date=date_str)

        logger.info(f"[NSECollector] Fetching participant OI for {target_date}: {url}")

        raw_csv = self._fetch_csv(url)
        if not raw_csv:
            logger.warning(f"[NSECollector] No data fetched for {target_date}")
            return []

        rows = self._parse_csv(raw_csv, target_date.isoformat())
        if not rows:
            logger.warning(f"[NSECollector] CSV parsed but no index rows found for {target_date}")
            return []

        self._append_history(rows)
        logger.info(
            f"[NSECollector] Collected {len(rows)} rows for {target_date}. "
            f"FII net changes: {[(r.symbol, r.fii_net_change) for r in rows]}"
        )
        return rows

    def get_latest(self, symbol: str = "INDEX") -> Optional[ParticipantOIRow]:
        """
        Return the most recent row for a symbol.
        Used by conviction_scorer at 9:00 AM to get previous day's signal.
        """
        symbol_rows = [r for r in self._history if r.get("symbol") == symbol]
        if not symbol_rows:
            return None
        latest = symbol_rows[-1]
        return ParticipantOIRow(**latest)

    def get_fii_signal(self, symbol: str = "INDEX") -> tuple[int, str]:
        """
        Returns (score, reason) for conviction_scorer.
          +3 BULLISH if fii_net_change > +10,000
          -3 BEARISH if fii_net_change < -10,000
          0  NEUTRAL otherwise
        """
        row = self.get_latest(symbol)
        if row is None:
            return 0, f"No FII data for {symbol}"

        change = row.fii_net_change
        if change > 10_000:
            return 3, f"FII bought {change:+,} {symbol} futures (strongly bullish)"
        elif change > 5_000:
            return 2, f"FII bought {change:+,} {symbol} futures (moderately bullish)"
        elif change < -10_000:
            return -3, f"FII sold {change:+,} {symbol} futures (strongly bearish)"
        elif change < -5_000:
            return -2, f"FII sold {change:+,} {symbol} futures (moderately bearish)"
        else:
            return 0, f"FII net change {change:+,} {symbol} futures (neutral)"

    def get_history_df(self, symbol: str = "INDEX", days: int = 30) -> list[dict]:
        """Return last N days of history for a symbol (for analysis/charts)."""
        rows = [r for r in self._history if r.get("symbol") == symbol]
        return rows[-days:]

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — fetch & parse
    # ─────────────────────────────────────────────────────────────

    def _fetch_csv(self, url: str, retries: int = 3) -> Optional[str]:
        """Download CSV from NSE archives with retry and browser-like headers."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.nseindia.com/",
        }
        session = requests.Session()
        # Establish session cookie via NSE homepage first
        try:
            session.get("https://www.nseindia.com/", headers=headers, timeout=10)
            time.sleep(1)
        except Exception:
            pass  # proceed without cookie — archives often work anyway

        for attempt in range(retries):
            try:
                resp = session.get(url, headers=headers, timeout=30)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code == 404:
                    logger.warning(f"[NSECollector] 404 — data not yet published or holiday: {url}")
                    return None
                else:
                    logger.warning(f"[NSECollector] HTTP {resp.status_code} on attempt {attempt+1}")
            except requests.RequestException as e:
                logger.warning(f"[NSECollector] Fetch error attempt {attempt+1}: {e}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))

        return None

    def _parse_csv(self, csv_text: str, date_str: str) -> list[ParticipantOIRow]:
        """
        Parse NSE participant-wise OI CSV.

        NSE CSV format (columns):
          Client Type | Future Index Long | Future Index Short | Future Stock Long |
          Future Stock Short | Option Index Call Long | Option Index Call Short |
          Option Index Put Long | Option Index Put Short | ...

        We only care about Future Index Long/Short for NIFTY and BANKNIFTY.
        NSE aggregates all indices in one row per participant type — we store the
        total index futures position (NIFTY + BANKNIFTY combined) as "INDEX".
        """
        lines = [l.strip() for l in csv_text.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return []

        # Find the header line (contains "Client Type" or "FII")
        header_idx = 0
        for i, line in enumerate(lines):
            if "Client Type" in line or "Future Index Long" in line:
                header_idx = i
                break

        try:
            headers = [h.strip().strip('"') for h in lines[header_idx].split(",")]
        except Exception:
            return []

        # Map column names to indices
        def col(name: str) -> int:
            for i, h in enumerate(headers):
                if name.lower() in h.lower():
                    return i
            return -1

        client_type_col   = col("Client Type")
        fut_idx_long_col  = col("Future Index Long")
        fut_idx_short_col = col("Future Index Short")

        if any(c < 0 for c in [client_type_col, fut_idx_long_col, fut_idx_short_col]):
            logger.warning(f"[NSECollector] Could not find expected columns in CSV. Headers: {headers[:10]}")
            return []

        participant_data: dict[str, tuple[int, int]] = {}

        for line in lines[header_idx + 1:]:
            if not line or line.startswith("#"):
                continue
            cols = [c.strip().strip('"') for c in line.split(",")]
            if len(cols) <= max(client_type_col, fut_idx_long_col, fut_idx_short_col):
                continue

            client_type = cols[client_type_col].strip()
            if client_type not in (_FII_CODE, _DII_CODE, _CLIENT_CODE, _PRO_CODE):
                continue

            try:
                long_pos  = int(cols[fut_idx_long_col].replace(",", "") or 0)
                short_pos = int(cols[fut_idx_short_col].replace(",", "") or 0)
                participant_data[client_type] = (long_pos, short_pos)
            except (ValueError, IndexError):
                continue

        if _FII_CODE not in participant_data:
            logger.warning(f"[NSECollector] FII row not found in CSV for {date_str}")
            return []

        fii_long, fii_short   = participant_data.get(_FII_CODE, (0, 0))
        dii_long, dii_short   = participant_data.get(_DII_CODE, (0, 0))
        client_long, client_short = participant_data.get(_CLIENT_CODE, (0, 0))

        fii_net    = fii_long - fii_short
        dii_net    = dii_long - dii_short
        client_net = client_long - client_short

        # Calculate day-over-day FII net change
        prev_fii_net = self._get_previous_fii_net("INDEX")
        fii_net_change = fii_net - prev_fii_net if prev_fii_net is not None else 0

        row = ParticipantOIRow(
            date           = date_str,
            symbol         = "INDEX",     # aggregated NIFTY+BANKNIFTY index futures
            fii_long       = fii_long,
            fii_short      = fii_short,
            fii_net        = fii_net,
            fii_net_change = fii_net_change,
            dii_long       = dii_long,
            dii_short      = dii_short,
            dii_net        = dii_net,
            client_long    = client_long,
            client_short   = client_short,
            client_net     = client_net,
        )
        return [row]

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — persistence
    # ─────────────────────────────────────────────────────────────

    def _get_previous_fii_net(self, symbol: str) -> Optional[int]:
        """Return the most recent stored FII net for a symbol."""
        rows = [r for r in self._history if r.get("symbol") == symbol]
        if not rows:
            return None
        return rows[-1].get("fii_net")

    def _append_history(self, rows: list[ParticipantOIRow]) -> None:
        """Append new rows to history and persist."""
        today_str = rows[0].date if rows else ""

        # Remove any existing entry for the same date (idempotent re-runs)
        self._history = [r for r in self._history if r.get("date") != today_str]

        for row in rows:
            self._history.append(asdict(row))

        # Keep last 365 days
        self._history = self._history[-365:]
        self._save_history()

    def _load_history(self) -> None:
        try:
            if os.path.exists(_DB_PATH):
                with open(_DB_PATH) as f:
                    self._history = json.load(f)
                logger.info(f"[NSECollector] Loaded {len(self._history)} FII history rows")
        except Exception as e:
            logger.warning(f"[NSECollector] Could not load history: {e}")
            self._history = []

    def _save_history(self) -> None:
        try:
            os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
            with open(_DB_PATH, "w") as f:
                json.dump(self._history, f, indent=2)
        except Exception as e:
            logger.warning(f"[NSECollector] Could not save history: {e}")


# ── Module-level singleton ────────────────────────────────────────
nse_participant_collector = NSEParticipantCollector()
