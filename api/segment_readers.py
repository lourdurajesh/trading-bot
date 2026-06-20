"""
segment_readers.py
──────────────────
SINGLE SOURCE for "given a market SEGMENT, read its trades / stats / review for the
dashboard." The dashboard's trades, stats and review are ONE data-concern each; the only
per-category difference is WHICH engine (and, until U5-slice-2, which table) the rows come
from. That difference lives here as an adapter keyed by `segment`, so there is one
`/trades`, one `/stats` and one `/review` endpoint (U7) instead of `/learning`, `/commodity`
and `/us` clones.

When U5-slice-2 lands (one ledger with a `segment` column), every reader below collapses
to a single `WHERE segment=?` query and this dispatch disappears — the endpoints and the
screen that call it do not change.

Segments:
  nse  → learning_engine        (NSE index-options learning paper trades)
  mcx  → commodity_options      (MCX commodity-options paper trades)
  us   → us_reversal            (US SPY/QQQ Reversal paper trades)
"""
from typing import Optional

# Accept friendly aliases so old URLs (/learning, /commodity) map cleanly onto segments.
_ALIASES = {
    "nse": "nse", "learning": "nse", "index": "nse",
    "mcx": "mcx", "commodity": "mcx", "comm": "mcx",
    "us": "us", "usa": "us",
}

SEGMENTS = ("nse", "mcx", "us")


def normalize(segment: Optional[str]) -> str:
    seg = (segment or "").strip().lower()
    if seg not in _ALIASES:
        raise ValueError(
            f"unknown segment '{segment}' — expected one of {', '.join(SEGMENTS)}"
        )
    return _ALIASES[seg]


def trades(segment: str, status: Optional[str] = None, limit: int = 200,
           symbol: Optional[str] = None) -> list:
    """Trade rows for a segment. `symbol` filters MCX by instrument (partial match);
    it is ignored by segments that have no per-instrument split."""
    seg = normalize(segment)
    if seg == "nse":
        from learning_engine import learning_engine
        return learning_engine.get_trades(status=status, limit=limit)
    if seg == "mcx":
        from commodity_options_learning import commodity_options
        rows = commodity_options.get_trades(status=status, limit=limit)
        if symbol:
            su = symbol.upper()
            rows = [t for t in rows if su in t.get("instrument", "").upper()]
        return rows
    # us
    from us_reversal import us_reversal
    return us_reversal.get_trades(status=status, limit=limit)


def stats(segment: str) -> dict:
    """Aggregate stats for a segment."""
    seg = normalize(segment)
    if seg == "nse":
        from learning_engine import learning_engine
        return learning_engine.get_stats()
    if seg == "mcx":
        from commodity_options_learning import commodity_options
        return commodity_options.get_stats()
    from us_reversal import us_reversal
    return us_reversal.get_stats()


def review(segment: str, strategy: Optional[str] = None) -> dict:
    """Closed trades grouped by outcome bucket. Only the NSE learning engine exposes
    review buckets today; other segments return an empty grouping with the same shape."""
    seg = normalize(segment)
    if seg == "nse":
        from learning_engine import learning_engine
        rows = learning_engine.get_review(strategy=strategy)
        from collections import defaultdict
        grouped: dict = defaultdict(list)
        for t in rows:
            grouped[t["outcome_bucket"]].append(t)
        return {"by_outcome": dict(grouped), "total": len(rows)}
    return {"by_outcome": {}, "total": 0, "message": f"review not available for segment '{seg}'"}
