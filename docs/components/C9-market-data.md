# C9 — Market Data

[← back to TECH_SPEC](../TECH_SPEC.md) · Layer: Ingest (Bot process) · Status: ✅ healthy — **the model pattern**

## Purpose
The single source of live market data for the whole Bot. One in-memory store holds OHLCV + last
price for every subscribed instrument; per-broker **stream adapters** feed it. Everything downstream
(analytics, strategies, both Evaluators) reads from this one store — never from a broker directly.

This is the reference for the whole architecture: **one shared store + per-source adapters.** The
unification target is to make the divergent engines look like this layer. See [ADR-008](../ADR.md).

## Children
| Child | Role |
|---|---|
| `data_store` | The in-memory OHLCV + LTP store; snapshot persistence; readiness checks |
| `fyers_stream` | WebSocket adapter for **NSE + MCX** → writes into `data_store` |
| `alpaca_stream` | WebSocket adapter for **US** → writes into `data_store` |

Adding a new data source = a new stream adapter feeding the same store. No second store, ever.

## Interfaces
```
# Read (consumers)
store.get_ohlcv(symbol, timeframe, n)   → DataFrame | None
store.get_ltp(symbol) / store.get_last_price(symbol) → float | None
store.is_ready(symbol, timeframe, min_candles) → bool
store.last_tick_time(symbol)            → datetime   # (for health / feed-disconnect, §13/§12)

# Write (adapters only)
FyersStream.start()/stop() · AlpacaStream.start()/stop()   → push ticks into store
FyersStream.refresh_mcx_subscriptions()                    → dynamic MCX symbol set
```

## Owning modules
| File | LoC | Role | Verdict |
|---|---|---|---|
| `data/data_store.py` | 377 | the one store (33 importers, no parallel store) | ✅ CORE |
| `data/fyers_stream.py` | 671 | Fyers WS adapter (NSE+MCX) | ✅ CORE (adapter) |
| `data/alpaca_stream.py` | 244 | Alpaca WS adapter (US) | ✅ CORE (adapter) |

No target change to structure — it is already correct.

## Dependencies
- **Uses:** C8 Broker Adapters (`fyers_broker`, `alpaca_broker`) for the connections; C17 infra
  (token for auth). Streams instantiated once in `main.py`, started together.
- **Used by:** C11 Analytics, C1 Strategies, C4 Evaluators (LTP for exits), C12/C14 — ~33 modules.
- **Boundary:** part of the **Bot process** (Diagram A). No UI dependency.

## Current state → target
- **Today:** fully unified — one `data_store`, two adapters feeding it, no parallel store. ✅
- **Target:** unchanged structurally. Two *enhancements* land here as part of production-hardening:
  1. **Feed-disconnect detection** (§12 reliability): expose stale-tick detection so the Orchestrator
     can **pause the EntryEvaluator while keeping the ExitEvaluator alive** on last-known price.
  2. **`last_tick_time` per feed** surfaced for the health/heartbeat endpoint (§13 observability).

## Known feed characteristics (belong in the adapter/gate, not the loop)
- **Fyers gives no real-time MCX volume** (`vol_traded_today=0` on every tick). Consumers that need
  volume (RVOL gate) must detect "no live volume" and skip the gate — handled in `mcx_base.mcx_live_volume`,
  not in the data layer. (The RVOL bug came from checking whole-df volume incl. seeded history.)

## Related tasks
No unification task (already done). Touched by cross-cutting:
[reliability-recovery](../cross-cutting/reliability-recovery.md) (feed-disconnect policy),
[observability](../cross-cutting/observability.md) (last-tick health).

## Open items
- Confirm `last_tick_time` is exposed per-symbol/per-feed for the health check (may need a small add).
