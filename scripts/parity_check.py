"""
parity_check.py
───────────────
The verification gate for the pipeline-unification slices. A refactor slice must not change
WHAT the bot trades — only WHERE the logic lives. This captures the canonical dashboard feed
(`/book/trades` + `/book/positions`) into a normalized snapshot and diffs two snapshots, so we
can prove a slice is behaviour-preserving: same trades, same structural fields, same P&L.

Read-only: it only GETs the loopback dashboard API. It never writes the DB.

Usage (run on the server, where the API is on 127.0.0.1:8000):
    python3 scripts/parity_check.py capture /tmp/parity_before.json
    # ... apply the slice, restart the API ...
    python3 scripts/parity_check.py capture /tmp/parity_after.json
    python3 scripts/parity_check.py diff /tmp/parity_before.json /tmp/parity_after.json
    # exit 0 = identical (parity holds); exit 1 = structural difference (slice changed behaviour)

Stdlib only (urllib/json) so it runs without the venv.
"""
import json
import sys
import urllib.request

BASE_URL = "http://127.0.0.1:8000"

# Fields that must be IDENTICAL across a behaviour-preserving slice (structural).
# Live mark-to-market fields (ltp, unrealized P&L, and the OPEN-row P&L which is unrealized)
# are excluded — they move tick to tick and are not what a storage/logic refactor changes.
TRADE_STRUCTURAL = ("book", "segment", "strategy", "symbol", "instrument", "direction",
                    "status", "entry_time", "exit_time", "entry", "exit", "qty",
                    "sl", "target", "exit_reason")
TRADE_PNL_WHEN_CLOSED = ("pnl_inr", "pnl_r")   # static once CLOSED → compared only for closed rows
POSITION_STRUCTURAL = ("book", "segment", "strategy", "symbol", "instrument", "direction",
                       "qty", "entry")


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE_URL + path, timeout=15) as r:
        return json.loads(r.read().decode())


def capture(outfile: str) -> None:
    trades = _get("/book/trades?limit=500").get("trades", [])
    positions = _get("/book/positions").get("positions", [])
    snap = {
        "trades":    {t["id"]: t for t in trades if t.get("id")},
        "positions": {p["id"]: p for p in positions if p.get("id")},
    }
    with open(outfile, "w") as f:
        json.dump(snap, f, indent=2, sort_keys=True)
    print(f"captured {len(snap['trades'])} trades, {len(snap['positions'])} positions → {outfile}")


def _cmp(a: dict, b: dict, fields, label: str) -> list:
    diffs = []
    ids = set(a) | set(b)
    for tid in sorted(ids):
        if tid not in a:
            diffs.append(f"  + {label} {tid} ADDED")
            continue
        if tid not in b:
            diffs.append(f"  - {label} {tid} REMOVED")
            continue
        ra, rb = a[tid], b[tid]
        cmp_fields = list(fields)
        if label == "trade" and ra.get("status") == "CLOSED" and rb.get("status") == "CLOSED":
            cmp_fields += list(TRADE_PNL_WHEN_CLOSED)
        for f in cmp_fields:
            if ra.get(f) != rb.get(f):
                diffs.append(f"  ~ {label} {tid}.{f}: {ra.get(f)!r} → {rb.get(f)!r}")
    return diffs


def diff(file_a: str, file_b: str) -> int:
    a = json.load(open(file_a))
    b = json.load(open(file_b))
    diffs = _cmp(a["trades"], b["trades"], TRADE_STRUCTURAL, "trade")
    diffs += _cmp(a["positions"], b["positions"], POSITION_STRUCTURAL, "position")
    if not diffs:
        print(f"PARITY OK — {len(a['trades'])} trades, {len(a['positions'])} positions identical")
        return 0
    print(f"PARITY FAILED — {len(diffs)} structural difference(s):")
    print("\n".join(diffs))
    return 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "capture":
        capture(sys.argv[2]); return 0
    if len(sys.argv) >= 4 and sys.argv[1] == "diff":
        return diff(sys.argv[2], sys.argv[3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
