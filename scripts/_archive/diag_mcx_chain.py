"""
diag_mcx_chain.py — verify the commodity engine now parses the flat MCX chain:
_leg_data / _chain_lookup must return real ltp/bid/ask/oi for ATM strikes
(so CHAIN_MARK can fire and B2.3 bid/ask/OI realism works).

Run on server:  PYTHONPATH=. ./venv/bin/python scripts/diag_mcx_chain.py
"""
from execution.fyers_broker import fyers_broker
from commodity_options_learning import commodity_options, _fyers_sym, MCX_CONTRACTS, _atm_strike


def check(inst: str):
    sym = _fyers_sym(inst)
    print("=" * 60)
    print(f"{inst}  ({sym})")
    chain = commodity_options._get_chain(sym)
    if not chain:
        print("  chain fetch FAILED")
        return
    # underlying spot from the embedded row (strike -1)
    spot = 0.0
    for r in chain.get("optionsChain", []):
        if str(r.get("option_type", "")) == "" and r.get("ltp"):
            spot = float(r["ltp"]); break
    step = MCX_CONTRACTS[inst]["strike_step"]
    atm  = _atm_strike(spot, step) if spot else 0
    otm  = atm + 2 * step
    print(f"  spot={spot} atm={atm} otm={otm}")
    for label, k in (("ATM call", atm), ("OTM call", otm)):
        q = commodity_options._leg_data(chain, k, "call")
        if q:
            print(f"  {label} {k}: ltp={q['ltp']} bid={q['bid']} ask={q['ask']} oi={q['oi']} sym={q['symbol']}")
        else:
            print(f"  {label} {k}: None")
    ltp, iv, s = commodity_options._chain_lookup(chain, atm, "call", 0)
    print(f"  _chain_lookup(atm) -> ltp={ltp} sym={s}  ({'REAL' if ltp else 'BS fallback'})")


if __name__ == "__main__":
    fyers_broker.initialise()
    if not fyers_broker._initialised:
        print("Fyers not initialised."); raise SystemExit(1)
    for inst in ("CRUDEOIL", "COPPER", "NATURALGAS"):
        if inst in MCX_CONTRACTS:
            check(inst)
