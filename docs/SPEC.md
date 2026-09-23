# TEESwap Specification

**Version:** 0.2 (draft)
**Date:** 2026-09-18

## 1. Overview

TEESwap is an x402-compatible cross-chain swap aggregator running inside a verified TEE. It receives user payment via x402, routes through existing bridge and DEX infrastructure, and delivers output tokens to the user's destination address. Every tool result carries a cryptographic attestation proving the TEE executed the correct code on the claimed inputs.

**Core properties:**
- Zero starting capital — operates on user tokens, not protocol inventory
- Verified execution — the full chain from source code to running enclave is auditable via lockboot (GitHub CI → UKI → PCR-pinned attestation)
- Verifiable MCP — implements SEP-2133 with `tee-vaportpm-v1` attestation on every tool result, and HPKE blind execution for E2EE
- x402 native — discoverable via Bazaar, payable via standard x402 flow, with its own facilitator
- Risk-aware routing — uses the revocability audit data to score and select routes
- No shared approval target — eliminates the aggregator gateway exploit pattern (Socket $3.3M, LI.FI $11.6M)

## 2. Actors

| Actor | Role |
|---|---|
| **Agent / User** | Requests swap via MCP or HTTP, signs x402 payment, receives output tokens |
| **TEE Engine** | Runs inside a lockboot-verified cloud VM. Generates quotes, manages keypairs, executes routes, runs facilitator, produces attestations |
| **Attestation Proxy** | Optional distributable Rust binary on the user's machine. Embeds `vaportpm-verify`, enforces PCR policy, proxies MCP calls with E2EE |
| **Underlying protocols** | Bridges, DEXes, and settlement layers the TEE routes through |
| **x402 Bazaar** | Discovery layer — indexes the service for agents |

## 3. Trust Model

### 3.1 The verification chain (lockboot)

TEESwap's trust does not rest on "the operator says it's a TEE." The lockboot stack (github.com/lockboot) provides a staged verified boot chain where each stage verifies the next before executing it:

**stage0** — UEFI Secure NetBoot. The cloud VM boots a minimal UEFI payload over HTTP. stage0 fetches the stage1 UKI from the URL in cloud provider metadata, verifies it (pinned SHA-256 hash or ed25519 signature), measures it into TPM **PCR 14**, and chain-loads it. No TFTP, no DHCP options, no operator-controlled boot media.

**stage1** — Unified Kernel Image (UKI), runs as PID 1. Reads the stage2 manifest from cloud metadata (IMDSv2), downloads and verifies the stage2 payload, extends PCR 14, and executes it from a sealed in-memory image (memfd — nothing touches disk). vaportpm is built into stage1, providing pre-execution attestation.

**stage2** — Container loader. Payload-agnostic: any container image (in this case, the TEESwap server) is appended as a self-extracting zip. stage2 mounts the container's rootfs via **dm-verity** (immutable, hash-verified) with an ephemeral tmpfs overlay. Encrypted `/data` partition via **dm-crypt** AES-256-XTS, with keys derived from TPM PCR-bound sessions — a PCR mismatch means garbage decryption, fail-closed. Configuration (stdin JSON) is hashed into **PCR 15**.

**Result:** PCR 14 covers the entire code chain (stage0 → stage1 → stage2 → container rootfs). PCR 15 covers configuration. An operator cannot substitute different code without changing PCR 14, which would fail verification against published release values AND destroy access to the encrypted data partition.

**Attestation:** `vaportpm-attest` generates a vTPM attestation document containing the PCR measurements. `vaportpm-verify` checks: certificate chain → cloud provider root, PCR values → published release values, nonce → freshness.

Within this verified environment, TEESwap runs per-swap gVisor containers for isolation between concurrent swaps (scoped key material, allowlisted RPC endpoints only).

### 3.2 Per-result attestation (Verifiable MCP)

Beyond verifying the running code on connection, every MCP tool result carries a `tee-vaportpm-v1` attestation per the Verifiable MCP extension (SEP-2133):

- `inputCommitment` = `sha256(salt || JCS(arguments))` — binds the proof to exactly the inputs the client sent
- `outputCommitment` = `sha256(JCS(content))` — binds the proof to exactly the result returned
- `nonce` — prevents replay of old proofs
- `teeAttestation` — the vTPM attestation document (COSE_Sign1 / CBOR)
- `proof` — Ed25519 signature by the enclave key over the binding fields

This means: even if the TLS connection is intercepted, a man-in-the-middle cannot fabricate tool results. The proof is tied to the enclave measurement, the specific inputs, and the specific outputs.

### 3.3 Blind execution (HPKE)

On startup, the TEE generates an X25519 keypair in memory, extends a TPM PCR with the public key hash, and produces an attestation document covering it. Clients verify: code PCRs match the pinned release, key PCR is consistent with the advertised public key, and a live signature proves possession.

Clients encrypt tool arguments using HPKE (RFC 9180, X25519/HKDF-SHA256/AES-128-GCM) to this attested public key. The MCP hosting layer and network infrastructure never see the plaintext — only the TEE enclave decrypts (the private key lives in application memory, never on disk). This protects swap parameters, amounts, and recipient addresses from infrastructure-level observers.

### 3.4 What attestation does NOT cover

- Correctness of external data (RPC responses, price feeds) — mitigated by input provenance attestations (oracle-sig-v1, zktls-tlsn-v1) where available
- Availability of the TEE (it can go offline)
- Behavior of underlying protocols the TEE routes through
- Network-level attacks (censorship, reorgs)

## 4. API Surface

TEESwap exposes a single unified typed interface (Python, Litestar) from which multiple API styles are derived:

| Style | Transport | Discovery | Payment |
|---|---|---|---|
| **MCP** | Stdio (via proxy) or Streamable HTTP | MCP registries, tool listing | x402 on tool calls |
| **Stateless MCP** (2026-07-28) | HTTP, no sessions | `server/discover`, header routing | x402 on tool calls |
| **HTTP JSON** | POST endpoints | OpenAPI/JSON Schema | x402 or separate payment |
| **x402 Bazaar** | HTTP 402 flow | CDP Bazaar discovery | x402 native |

All styles share the same typed schemas (Pydantic or equivalent), validation, error messages, and response shapes. The MCP tool definitions are generated from the same models that power the HTTP endpoints.

### 4.1 Stateless MCP (2026-07-28)

TEESwap targets MCP `2026-07-28` (stateless):
- No `initialize` handshake, no `Mcp-Session-Id`
- Each request carries protocol version + capabilities in `_meta`
- Per-request Verifiable MCP attestation (no session state to verify)
- `Mcp-Method` / `Mcp-Name` headers enable gateway routing (quotes to one pool, execution to another)
- Cacheable `teeswap_routes` via `ttlMs`
- Application-level state via explicit handles (`quoteId`, `orderId`) — the canonical pattern for stateless MCP

### 4.2 Bazaar auto-discovery

Once TEESwap lists on CDP Bazaar, any agent running an x402-mcp gateway (which auto-syncs ~117 tools from Bazaar) gets TEESwap tools automatically without configuration.

## 5. MCP Tools

TEESwap's MCP server exposes five tools following the resolve → quote → execute → status pattern established by the best existing servers (Relay, VaultPilot, Jupiter).

### `teeswap_routes` — free, read-only, cacheable

```
inputSchema:
  inputChain:  string (optional, filter by source chain — human-readable: "base", "arbitrum", "ethereum")
  outputChain: string (optional, filter by destination chain)
  inputToken:  string (optional, filter by source token — symbol "USDC" or address)
  outputToken: string (optional, filter by destination token)
```

Returns supported pairs, chains, estimated fee ranges, and route availability. Free because it's static data cached via `ttlMs`. Annotations: `readOnly + openWorld`.

### `teeswap_quote` — x402-paywalled (micro-fee)

```
inputSchema:
  inputToken:   string  — token symbol ("USDC") or contract address
  inputChain:   string  — human-readable chain name
  inputAmount:  string  — human-readable decimal amount, NOT raw wei ("100.5" for 100.5 USDC)
  outputToken:  string
  outputChain:  string
  recipient:    string  — destination address (format must match destination chain)
  riskPreference: enum ["low", "medium", "high"] (optional, default "medium")
  order:        enum ["RECOMMENDED", "FASTEST", "CHEAPEST", "SAFEST"] (optional, default "RECOMMENDED")
  slippageBps:  integer (optional, 1-500, default 50)
  excludeProtocols: string[] (optional — protocol names to avoid)
  maxPriceCap:  string (optional — refuse if total cost exceeds this, in input token units)
```

The micro-fee covers route computation. Returns:

```
output:
  quoteId:       string
  outputAmount:  string (human-readable decimal)
  minOutputAmount: string
  route:
    legs: [{ protocol, action, chain, class, estimatedTime }]
    estimatedTotalTime: string
    riskProfile: { score, maxAdminRisk, worstLegIncidents12mo, hasJudgmentOracleDependency }
  inputMethod:   enum ["x402-passthrough", "x402-to-tee", "direct-deposit"]
  depositAddress: string (only for direct-deposit)
  deadline:      integer (unix timestamp)
  execution:     x402 payment requirements for the swap itself
  attestation:   Verifiable MCP tee-vaportpm-v1 proof metadata
```

The quote response is itself attested — the risk profile and route selection are provably computed by the TEE.

Annotations: `readOnly + openWorld` (it computes but doesn't mutate).

### `teeswap_execute` — x402-paywalled (swap amount)

```
inputSchema:
  quoteId: string
```

The x402 payment for the full swap amount is handled by the MCP client (or proxy). The TEE verifies payment, begins execution. Returns:

```
output:
  orderId:  string
  status:   "executing"
  legs:     [{ step, protocol, status }]
```

Annotations: `destructive + openWorld`, NOT idempotent.

### `teeswap_status` — free

```
inputSchema:
  orderId: string
```

Returns current state with per-leg detail:

```
output:
  status: enum ["executing", "leg_1_submitted", "leg_1_confirmed", "leg_2_submitted",
                "leg_2_confirmed", "delivered", "complete", "failed", "refunding", "refunded"]
  legs: [{ step, protocol, chain, txHash, status, timestamp }]
  outputAmount: string (actual, once delivered)
  attestation:  Verifiable MCP proof metadata (each status update is attested)
```

Annotations: `readOnly + openWorld`.

### `teeswap_refund` — free

```
inputSchema:
  orderId: string
```

For failed/timed-out orders. The TEE initiates the refund — tokens return to the user's source address. Returns refund status and txHash. If the TEE holds tokens in its EOA, it sends them back directly. If tokens are in a bridge escrow, returns the bridge's refund timeline.

Annotations: `destructive + idempotent + openWorld`.

### 5.1 Safety patterns (adopted from VaultPilot/Circle)

- **Human-readable amounts** — `inputAmount` is "100.5" not "100500000". The TEE resolves decimals on-chain.
- **Slippage cap** — hard maximum 500 bps (5%). Higher values require explicit `acknowledgeHighSlippage`.
- **Max price cap** — agent can set a ceiling on total cost. Tool refuses if the quote exceeds it.
- **Route exclusion** — agent can blocklist specific protocols.
- **Route ranking** — RECOMMENDED / FASTEST / CHEAPEST / SAFEST preference.
- **Recipient validation** — cross-chain `recipient` format must match destination chain. No cross-chain sends to mismatched address formats.

## 6. Lifecycle of a Swap

### 6.1 Discovery

The TEE Engine registers on x402 Bazaar and MCP registries. Agents discover TEESwap via:
- Bazaar search for "swap", "bridge", "cross-chain"
- MCP server directories
- x402-mcp auto-sync gateways
- Direct URL configuration

### 6.2 Quote (x402 micro-fee)

Agent calls `teeswap_quote`. Payment follows the x402 MCP transport (see `X402.md`): the first call returns an `isError` result with `PaymentRequired`, the client repeats the call with `params._meta["x402/payment"]`, and the paid result carries `_meta["x402/payment-response"]`:

```
Agent ──teeswap_quote──► Proxy ──(E2EE)──► TEE
                                            ├── evaluate routes
                                            ├── score risk (audit data)
                                            ├── compute output amount
                                            ├── generate attestation
                                            └── return quote + x402 execution payment
```

### 6.3 Execution (x402 swap amount)

Agent calls `teeswap_execute` with the `quoteId`. MCP client pays the swap amount via x402. TEE receives tokens and executes:

**Same-chain swap:** TEE submits to target DEX (CoW orderbook API, UniswapX, etc.)
**Cross-chain bridge:** TEE deposits into bridge contract with user's destination address as recipient
**Multi-hop:** TEE chains steps (swap → bridge → optional destination swap)

### 6.4 Delivery and completion

Output tokens arrive at the user's destination address. TEE monitors for confirmation. Each status update is attested.

## 7. Token Custody

The TEE manages EOA keypairs on each supported chain. Key management is operator-provided via the lockboot/vaportpm infrastructure. The custody model is **hybrid per-route**.

### 7.1 x402 all-inclusive (primary)

The agent pays once via x402. The TEE handles everything — token receipt, routing, delivery. The agent doesn't know or care about intermediate addresses. This is the revenue center.

| Input method | When used | TEE holds tokens? |
|---|---|---|
| **x402 atomic pass-through** | EIP-3009 tokens (USDC), simple routes | No — tokens go user → protocol directly |
| **x402 to TEE EOA** | Permit2 tokens, multi-hop routes | Briefly (~1 block) |

### 7.2 Direct deposit (secondary)

For tokens without Permit2/EIP-3009, native assets (ETH), or chains without x402 support:
- Quote response includes a TEE-controlled deposit address
- User sends tokens to that address
- TEE detects receipt and executes the route

### 7.3 Custody design rules

1. **Prefer atomic pass-through** — tokens never touch the TEE's address
2. **For cross-chain, set user as bridge recipient** — bridge delivers directly to user even if TEE dies
3. **Multi-hop exception** — TEE receives on destination chain to perform second swap; execute immediately to minimize custody window
4. **TEE availability and key recovery** are operational concerns handled by lockboot infrastructure

## 8. Facilitator

TEESwap runs its own x402 facilitator inside the TEE:

1. Receives the user's signed x402 authorization
2. Verifies signature and payment parameters
3. Settles on-chain
4. Triggers the execution engine

Self-hosted — no Coinbase CDP dependency. No third-party facilitator downtime. The facilitator and execution engine share the same TEE.

### 8.1 Compliance posture

Operator-decided. The TEE code can include or exclude AML/sanctions screening. The attestation makes the compliance posture auditable — anyone can verify whether the running code includes screening by checking the release's source and matching it to the attested PCR measurements.

## 9. Risk-Aware Routing

The TEE uses the revocability audit data as a routing input:

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

Risk is exposed in the quote. Agents can constrain via `riskPreference` and `excludeProtocols`.

## 10. Integrated Protocols

### Tier 1 — Cleanest

| Protocol | Class | Integration |
|---|---|---|
| CoW Protocol | A | HTTP orderbook API, EIP-712 signing |
| UniswapX | A | Order API or on-chain fill |
| x402 (same-chain) | A | Native |
| The Compact | B | On-chain deposit, allocator model |
| OIF Escrow | B | On-chain escrow open |
| Skip Go Fast | C* | On-chain submitOrder, permissionless timeout |

### Tier 2 — Acceptable with caveats

| Protocol | Class | Caveat |
|---|---|---|
| Across V3 | C | Dataworker dependency, but $34B volume |
| CCTP V2 | C | Circle Iris attestation, USDC-only |
| Mayan Swift | C* | Wormhole Guardian dependency |
| IBC/Eureka | B | Permissioned relayers (Eureka) |

### Tier 3 / Excluded

See INDEX.md for full audit. Tier 3 protocols (1inch Fusion+, deBridge DLN, Wormhole NTT, Axelar) may be added with caution flags. Excluded protocols (Aori, THORChain, Maya) are never routed through.

## 11. Attestation Proxy

A distributable Rust binary that provides the trust layer for any MCP client:

### 11.1 What it does

1. Connects to the TEE's MCP endpoint
2. Verifies the vTPM attestation via `vaportpm-verify`:
   - Certificate chain → cloud provider root (AWS Nitro / GCP)
   - PCR measurements → published release values from the GitHub release
   - HPKE public key binding → key is provably enclave-resident
3. Establishes HPKE-encrypted channel (Verifiable MCP blind execution)
4. Exposes plain MCP locally (stdio for agent integration)
5. Verifies every tool result's `tee-vaportpm-v1` attestation proof
6. Rejects results with mismatched measurements, stale attestations, or invalid signatures

### 11.2 What it is NOT

- Not a wallet — does not hold keys (unless integrated with the user's local key management)
- Not a new protocol — implements Verifiable MCP (SEP-2133) client-side
- Not required — agents can connect directly over HTTPS if they trust the operator

### 11.3 PCR verification

The proxy pins PCR values to a specific TEESwap release. When the TEESwap server updates:
- The new release publishes new PCR values on GitHub
- The proxy fetches the new values (or the user updates the proxy)
- Until the proxy accepts the new measurements, it rejects the updated server

This is the strongest possible PCR verification: measurements are tied to reproducible builds via lockboot's verified boot chain. The proxy doesn't just check "is there an attestation?" — it checks "is this exactly the code I expect?"

## 12. Failure Modes and Recovery

| Failure | Tokens are... | Recovery |
|---|---|---|
| Atomic pass-through reverts | In user's wallet | Automatic |
| TEE offline before routing (tokens in EOA) | In TEE EOA | TEE restarts via lockboot infra |
| TEE crashes mid multi-hop | Split: TEE EOA + bridge escrow | TEE restart; bridge follows its own timeout |
| Bridge delivers to user directly | At destination | Complete |
| Bridge fails | In bridge escrow | Bridge's refund mechanism |
| Bridge delivers to TEE's dst EOA | In TEE dst EOA | TEE forwards to user on restart |
| Stablecoin freeze on TEE EOA | Frozen | No on-chain recovery (issuer-dependent) |

## 13. Gas and Fee Model

- **Quote fee:** x402 micro-fee ($0.001–$0.01) per quote. Covers route computation.
- **Swap fee:** embedded in spread. TEE computes optimal route and takes a basis-point fee.
- **Gas:** TEE pays gas from operator-funded wallets, replenished from swap fees.
- **No facilitator fee:** self-operated.

## 14. Future Extensions

- **Streaming swaps** — split large orders into chunks (DCA-style)
- **Limit orders** — TEE monitors price, executes when conditions met
- **Multi-party batching** — batch user orders for better pricing (CoW-style)
- **Cross-chain composability** — chain swap output into DeFi actions on destination
- **Input provenance** — oracle-sig-v1 / zktls-tlsn-v1 to prove price feeds are real
- **Deferred proofs** — for high-frequency agents, prove a sample of results on-demand
