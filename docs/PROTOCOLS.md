# Protocols

What comes after same-chain transfers: swaps and bridges, as operations Mai can
plan (see `EXECUTION.md`). Nothing here is built yet. This is the intent and the
rules routes will follow.

The protocols come from the revocability audit (`INDEX.md` in the IntentSpace
repo, alongside this one). The effort to integrate each is assessed in
`research/INTEGRATION_COMPLEXITY.md`, and what already exists as MCP servers in
`research/MCP_RESEARCH.md`.

## Custody on a route

Each protocol is another place the user's funds can be, and the rules keep that
exposure small:

1. **Prefer atomic pass-through.** Where a protocol can take the funds straight
   from the job's account and deliver them to the recipient in one step, do
   that: the funds are never somewhere Mai has to watch.
2. **Cross-chain, the user is the bridge's recipient.** The bridge delivers to
   them directly, even if TEESwap is gone by then.
3. **Multi-hop is the exception.** When a second swap is needed on the
   destination chain, a job account receives there, and Mai acts on it at once,
   to keep the custody window short.

Every hop is an operation with side effects that are checked, not assumed, and
what's held along the way is in the job's holdings. If Mai can't finish, the
tools go down as usual: the owner gets the keys to wherever the funds are held.

## Risk-aware routing

Routes are chosen for risk, not only price and speed, using the audit's data:

```
score(leg) = price_score × speed_score × risk_score

risk_score factors:
  - protocol.class: A > B > C > D
  - protocol.user_can_self_refund: yes > no
  - protocol.has_immediate_admin_pause: penalize
  - protocol.contracts_upgradeable: penalize
  - protocol.known_incident_count_12mo: penalize if > 0
  - protocol.judgment_oracle_dependencies: penalize
  - protocol.stablecoin_freeze_exposed: match against input token
```

The route's risk is part of the quote, which the user accepts or rejects, and the
user can constrain it (a risk preference, protocols to exclude).

## Tiers

### Tier 1: cleanest

| Protocol | Class | Integration |
|---|---|---|
| CoW Protocol | A | HTTP orderbook API, EIP-712 signing |
| UniswapX | A | Order API or on-chain fill |
| x402 (same-chain) | A | Native |
| The Compact | B | On-chain deposit, allocator model |
| OIF Escrow | B | On-chain escrow open |
| Skip Go Fast | C* | On-chain submitOrder, permissionless timeout |

### Tier 2: acceptable with caveats

| Protocol | Class | Caveat |
|---|---|---|
| Across V3 | C | Dataworker dependency, but $34B volume |
| CCTP V2 | C | Circle Iris attestation, USDC-only |
| Mayan Swift | C* | Wormhole Guardian dependency |
| IBC/Eureka | B | Permissioned relayers (Eureka) |

### Tier 3 and excluded

Tier 3 protocols (1inch Fusion+, deBridge DLN, Wormhole NTT, Axelar) may be added
with caution flags. Excluded protocols (Aori, THORChain, Maya) are never routed
through. The audit has the reasons.

## Where funds can be when things go wrong

| Failure | Funds are | Recovery |
|---|---|---|
| Atomic pass-through reverts | In the job's account | Mai re-plans, or the tools go down |
| TEESwap stops mid-route | Job accounts, or a protocol's escrow | On restart Mai resumes from the record (needs durable state) |
| Bridge delivers to the user directly | At the destination | Complete |
| Bridge fails | In the bridge's escrow | The bridge's own refund, to the job's account |
| Bridge delivers to a job account | In the job's account on the destination | Mai continues from there |
| Stablecoin frozen in a job account | Frozen | Only the issuer can undo it |
