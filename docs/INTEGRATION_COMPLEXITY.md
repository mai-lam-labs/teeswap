# Integration Complexity Assessment — Tier 1 and 2 Protocols

**Date:** 2026-09-16

For each protocol: what does the TEE actually need to do, how much of it is on-chain vs HTTP API, and how hard is the integration?

---

## Tier 1

### UniswapX
**What TEESwap does:** fill orders as a filler (or route through existing fillers)

**Option A — Use as filler (on-chain):**
- Encode and submit `execute(SignedOrder)` on the Reactor contract
- Need: Reactor ABI (4 functions), Permit2 integration, order decoding
- `cast` encodes the calldata, Python submits via RPC
- Complexity: **medium** — you need to understand the order format, resolve dutch auctions, handle exclusivity windows

**Option B — Route through UniswapX API (HTTP):**
- UniswapX has an off-chain order API where you submit orders and fillers compete
- TEESwap could submit the user's swap as a UniswapX order and let existing fillers handle it
- But: this means the user's tokens need to be in a wallet with Permit2 approval to the Reactor — which means the TEE's address, not pass-through
- Complexity: **low** if using the API, but adds the TEE-holds-tokens step

**Verdict:** Option B is simpler but adds custody. Option A is more work but enables pass-through. **Same-chain only** — doesn't help with cross-chain.

### CoW Protocol
**What TEESwap does:** submit orders to CoW's solver auction

**Integration:**
- Submit a signed order to CoW's HTTP orderbook API (`POST /api/v1/orders`)
- The order is an EIP-712 signed message — TEE signs with `cast wallet sign`
- CoW solvers compete to fill. Settlement pulls from the TEE's address via approval to GPv2VaultRelayer.
- TEE needs: ERC-20 approval to VaultRelayer (one-time per token), then just HTTP API calls
- Monitor fill status via HTTP API (`GET /api/v1/orders/{uid}`)
- Complexity: **low** — mostly HTTP API. One `cast send` for the initial token approval, then everything is off-chain signing + HTTP.

**Verdict:** Very easy integration. HTTP API does the heavy lifting. **Same-chain only.**

### The Compact
**What TEESwap does:** deposit user tokens into a resource lock

**Integration:**
- Call `depositERC20(token, lockTag, amount, recipient)` on The Compact contract
- Or use Permit2 variant: `depositERC20ViaPermit2(...)`
- The `lockTag` encodes allocatorId + scope + resetPeriod
- TEE needs to be registered as an allocator (or use an existing one)
- For claiming: allocator co-signs the claim (TEE signs as allocator)
- Complexity: **medium** — the lockTag encoding and allocator registration add setup complexity, but the actual deposit is one contract call via `cast`

**Verdict:** Medium setup, simple per-swap. Good for cross-chain when paired with a bridge.

### OIF Escrow
**What TEESwap does:** deposit into InputSettlerEscrow, or fill as a solver on the output side

**Option A — Deposit as user (escrow source tokens):**
- Call `open(StandardOrder)` on InputSettlerEscrow
- The StandardOrder struct has: inputs, outputs, fillDeadline, expires, user, originChainId, etc.
- One `cast` call to encode and submit
- Complexity: **medium** — struct encoding is involved but well-defined

**Option B — Fill as solver (deliver on destination):**
- Call `fill()` on OutputSettlerSimple on the destination chain
- Then `finalise()` on source chain after oracle proof
- Need to interact with the oracle (Hyperlane) for proof submission
- Complexity: **high** — requires understanding the full solver flow, oracle integration, and two-chain coordination

**Verdict:** Option A is medium. Option B (acting as solver) is high. For TEESwap, you'd likely use OIF as the escrow layer and let existing OIF solvers handle the fill — or act as solver yourself if you want to control execution.

### Skip Go Fast
**What TEESwap does:** submit orders to FastTransferGateway

**Integration:**
- Call `submitOrder(...)` on FastTransferGateway on source chain
- The order specifies: recipient, input amount, output amount, destination chain, timeout
- Gateway escrows the tokens
- Existing Skip solvers fill on destination chain
- Monitor via RPC (check order status on destination)
- Refund: `initiateTimeout()` callable by anyone after timeout — simple `cast` call
- Complexity: **low-medium** — one contract call to submit, one to refund if needed. The solver network handles filling.

**Verdict:** Relatively easy. You submit the order on-chain and the existing solver network does the cross-chain work.

---

## Tier 2

### Across V3
**What TEESwap does:** deposit into SpokePool

**Integration:**
- Call `depositV3(...)` on the SpokePool contract
- Parameters: depositor, recipient, inputToken, outputToken, inputAmount, outputAmount, destinationChainId, exclusiveRelayer, quoteTimestamp, fillDeadline, exclusivityDeadline, message
- Across relayers fill on the destination chain automatically
- TEE deposits and waits — the Across relayer network handles the rest
- Monitor via Across API (check fill status)
- Complexity: **low-medium** — one `cast` call with many parameters, but it's a single function. Across has good API/SDK documentation for parameter construction.

**Verdict:** Straightforward. One deposit call, relayer network handles fill. This is probably the easiest cross-chain bridge integration.

### Mayan Swift
**What TEESwap does:** deposit into Swift source contract

**Integration:**
- Solana-based auction mechanism — drivers compete on Solana
- Source chain deposit is EVM (if starting from EVM)
- Need to interact with Mayan's API for quote and route parameters
- Source chain contract call via `cast`
- Complexity: **medium** — the Solana auction adds complexity, but if you're just depositing on EVM and letting Mayan's driver network fill, it's one contract call + API

**Verdict:** Medium. API-driven quote, one on-chain deposit. Drivers handle the rest.

### CCTP V2
**What TEESwap does:** burn USDC on source, receive attestation, mint on destination

**Integration:**
- Call `depositForBurn(amount, destinationDomain, mintRecipient, burnToken)` on TokenMessenger
- Poll Circle's Iris API for attestation (`GET /v1/attestations/{messageHash}`)
- Call `receiveMessage(message, attestation)` on destination MessageTransmitter
- Three steps: burn, wait for attestation, mint
- Complexity: **low-medium** — three well-documented calls. The Iris polling is just HTTP. **USDC only.**

**Verdict:** Easy integration, well-documented, but USDC-only limits its utility.

### IBC Eureka (via Solidity contracts on Ethereum)
**What TEESwap does:** send IBC transfer from Ethereum

**Integration:**
- Call `sendTransfer(...)` on ICS20Transfer contract
- Need: proper channel/port configuration, timeout timestamp
- The IBC relayer network handles packet delivery
- Timeout: relayer submits timeout proof if packet expires
- Complexity: **medium-high** — IBC Eureka on EVM is newer, less integration documentation, channel configuration is non-trivial. Permissioned relayers (RELAYER_ROLE) add a dependency.

**Verdict:** Higher effort. The IBC Eureka Solidity deployment is newer and the permissioned relayer model means you can't just rely on any relayer.

---

## Summary: effort vs. value

| Protocol | Complexity | Cross-chain? | On-chain calls | HTTP API? | Priority |
|---|---|---|---|---|---|
| **CoW Protocol** | Low | No (same-chain) | 1 (approval) | Yes (orderbook API) | High — easiest DEX integration |
| **Across V3** | Low-medium | Yes | 1 (deposit) | Yes (status API) | **High — best cross-chain effort/value** |
| **CCTP V2** | Low-medium | Yes | 2 (burn + mint) | Yes (Iris attestation API) | High — but USDC only |
| **Skip Go Fast** | Low-medium | Yes | 1 (submitOrder) | Partial | Medium — less liquidity than Across |
| **UniswapX** | Low (API) / Med (filler) | No | 1 | Yes (order API) | Medium — same-chain only |
| **OIF Escrow** | Medium | Yes (with solver) | 1-2 | No | Medium — more complex, but clean escrow |
| **The Compact** | Medium | Partial (needs bridge) | 1-2 | No | Medium — good base layer, needs pairing |
| **Mayan Swift** | Medium | Yes | 1 | Yes (quote API) | Lower — less volume, Wormhole dependency |
| **IBC Eureka** | Medium-high | Yes | 1+ | No | Lower — newer, less documented |

## Recommended integration order

1. **CoW Protocol** — same-chain swaps. HTTP API, minimal on-chain. Gets you live fast.
2. **Across V3** — cross-chain. One `cast` call per swap. Largest cross-chain volume. Best effort-to-coverage ratio.
3. **CCTP V2** — cross-chain USDC. Three calls but well-documented. Covers the highest-volume stablecoin route.
4. **Skip Go Fast** or **OIF Escrow** — additional cross-chain coverage with better revocability properties than Across.
5. **UniswapX** — same-chain, adds competitive pricing to CoW.
6. **The rest** as demand dictates.

With just CoW + Across + CCTP, you cover same-chain EVM swaps, cross-chain EVM transfers, and USDC-specific fast transfers. That's a huge amount of the addressable market with three integrations.
