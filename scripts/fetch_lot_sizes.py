"""
fetch_lot_sizes.py
──────────────────
Refresh config/nse_instruments.json lot sizes from the authoritative Fyers
public symbol master (no auth required). NSE revises F&O lot sizes ~quarterly
(Mar/Jun/Sep/Dec); run this after each revision so lot sizes are never stale.

  python scripts/fetch_lot_sizes.py            # update indices + existing equities
  python scripts/fetch_lot_sizes.py --all-eq   # also add every F&O equity found

Why this exists: lot size feeds BOTH position sizing AND P&L. A wrong lot size
silently corrupts every number downstream. This keeps the JSON a refreshable
cache of an authoritative source instead of hand-maintained static values.

Source: https://public.fyers.in/sym_details/NSE_FO.csv
Columns used (0-indexed): 1=name, 3=lot_size, 8=expiry_epoch, 13=underlying
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request

SYM_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "config", "nse_instruments.json")

# Index underlyings we care about (BSE SENSEX is a separate master — left as-is).
INDEX_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]


def fetch_nearest_expiry_lots() -> dict:
    """Return {underlying: lot_size} using each underlying's NEAREST active future."""
    with urllib.request.urlopen(SYM_URL, timeout=30) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    now = time.time()
    best: dict[str, tuple[float, int]] = {}   # underlying -> (expiry_epoch, lot)
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 14 or "FUT" not in r[1]:
            continue
        try:
            lot = int(r[3]); exp = float(r[8])
        except (ValueError, IndexError):
            continue
        und = r[13]
        if exp < now:                      # skip expired contracts
            continue
        if und not in best or exp < best[und][0]:
            best[und] = (exp, lot)
    return {u: v[1] for u, v in best.items()}


def main(all_equities: bool = False) -> None:
    lots = fetch_nearest_expiry_lots()
    if not lots:
        print("ERROR: no lot sizes parsed — symbol master format may have changed.")
        sys.exit(1)

    with open(JSON_PATH) as f:
        data = json.load(f)

    changes = []

    # Indices
    for u in INDEX_UNDERLYINGS:
        if u in lots and data["index_lot_sizes"].get(u) != lots[u]:
            changes.append(f"  index {u}: {data['index_lot_sizes'].get(u)} -> {lots[u]}")
            data["index_lot_sizes"][u] = lots[u]

    # Equities — refresh those already configured; optionally add all F&O equities.
    eq_targets = set(data["equity_lot_sizes"].keys())
    if all_equities:
        eq_targets |= {u for u in lots if u not in INDEX_UNDERLYINGS}
    for u in sorted(eq_targets):
        if u in lots and data["equity_lot_sizes"].get(u) != lots[u]:
            changes.append(f"  equity {u}: {data['equity_lot_sizes'].get(u)} -> {lots[u]}")
            data["equity_lot_sizes"][u] = lots[u]

    data["_comment"] = ("NSE F&O lot sizes (from Fyers symbol master) + strike steps. "
                        "Refresh lots with: python scripts/fetch_lot_sizes.py")
    data["_lots_updated"] = time.strftime("%Y-%m-%d")

    with open(JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    if changes:
        print(f"Updated {len(changes)} lot size(s) in {JSON_PATH}:")
        print("\n".join(changes))
    else:
        print("All lot sizes already current.")


if __name__ == "__main__":
    main(all_equities="--all-eq" in sys.argv)
