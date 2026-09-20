# TEESwap Architecture

**Version:** 0.2 (draft)
**Date:** 2026-09-18

## Design principles

1. **Unified typed interface.** A single set of typed schemas (Litestar + Pydantic or equivalent) defines all tool inputs and outputs. MCP tool definitions, HTTP endpoints, x402 paywalls, and OpenAPI specs are derived from these types — no hand-maintained duplicates.
2. **Minimal dependencies.** No large SDK libraries. Chain interaction is via small CLI tools called as subprocesses. HTTP APIs where available. Python core, Rust for crypto.
3. **Per-swap isolation.** Each swap execution runs in a gVisor container with only the key material for that specific swap. A compromised container can't access other swaps' keys.
4. **Crypto in Rust, glue in Python.** Attestation (vaportpm), signing (cast), HPKE, and Ed25519 are Rust binaries called as subprocesses. Python handles orchestration, HTTP, state machines, and tool composition.
5. **Verifiable by default.** Every tool result carries a Verifiable MCP (SEP-2133) `tee-nitro-v1` attestation. The attestation is not optional — it's part of the response construction pipeline.

## System layers

```
┌─────────────────────────────────────────────────────────┐
│  Agent / User                                           │
├─────────────────────────────────────────────────────────┤
│  Attestation Proxy (optional, Rust)                     │
│  vaportpm-verify, HPKE encryption, PCR enforcement      │
│  Exposes: plain MCP (stdio) or local HTTP               │
├───────────────────────────┬─────────────────────────────┤
│  E2EE (HPKE blind exec)  │  or direct HTTPS            │
├───────────────────────────┴─────────────────────────────┤
│  API surface (Litestar, Python)                         │
│  MCP (stateless, 2026-07-28) + HTTP JSON + x402         │
│  Derived from shared typed schemas                      │
├─────────────────────────────────────────────────────────┤
│  Verifiable MCP layer (Python + Rust subprocess)        │
│  Commitment computation, _meta construction,            │
│  vaportpm-attest calls, HPKE decryption                 │
├─────────────────────────────────────────────────────────┤
│  Orchestrator (Python)                                  │
│  Route planning, order state machine,                   │
│  tool composition, failure handling                     │
├──────────┬──────────┬──────────┬────────────────────────┤
│  Chain   │  Protocol│  Wallet  │  Data                  │
│  tools   │  tools   │  tools   │  tools                 │
├──────────┴──────────┴──────────┴────────────────────────┤
│  gVisor execution containers                            │
│  Per-swap isolation, scoped key material                │
├─────────────────────────────────────────────────────────┤
│  lockboot (stage0 → stage1 → stage2)                    │
│  Verified boot, dm-verity rootfs, dm-crypt /data,       │
│  TPM-bound keys (PCR 14 = code, PCR 15 = config)        │
└─────────────────────────────────────────────────────────┘
```

## API surface

### Typed schema substrate

All tool interfaces are defined once as typed Python models (Pydantic or msgspec):

```python
class QuoteRequest(Struct):
    input_token: str
    input_chain: str
    input_amount: str   # human-readable decimal
    output_token: str
    output_chain: str
    recipient: str
    risk_preference: Literal["low", "medium", "high"] = "medium"
    order: Literal["RECOMMENDED", "FASTEST", "CHEAPEST", "SAFEST"] = "RECOMMENDED"
    slippage_bps: int = 50  # 1-500
    exclude_protocols: list[str] = []
    max_price_cap: str | None = None
```

From this one definition, we derive:
- **MCP tool schema** — JSON Schema for `teeswap_quote` inputSchema
- **HTTP endpoint** — `POST /quote` with request body validation
- **x402 paywall** — middleware that returns 402 before the handler runs
- **OpenAPI spec** — auto-generated documentation

Litestar handles this natively — route handlers are typed, schemas drop out.

### API styles served

| Style | Transport | How it works |
|---|---|---|
| **MCP (stateless)** | Streamable HTTP | `Mcp-Method` / `Mcp-Name` headers, per-request `_meta`, no sessions |
| **HTTP JSON** | POST | Standard REST-like endpoints, JSON request/response |
| **x402 Bazaar** | HTTP 402 | Auto-discovered by x402-mcp gateways |

All three are views of the same handler logic. The MCP transport wraps the handler in JSON-RPC. The HTTP transport exposes it directly. x402 is middleware.

### Stateless MCP (2026-07-28)

- No `initialize` handshake, no `Mcp-Session-Id`
- Each request carries capabilities in `_meta`
- `Mcp-Method: tools/call`, `Mcp-Name: teeswap_quote` headers for gateway routing
- `teeswap_routes` response includes `ttlMs` for client-side caching
- Application state via explicit handles: `quoteId`, `orderId`
- Quote/execute/status can route to different backend pools via header-based routing

## Verifiable MCP integration (SEP-2133)

### Server-side (Python + Rust)

Every tool result goes through the attestation pipeline before being returned:

```
Handler produces result
    │
    ├── Python: compute inputCommitment = sha256(JCS(arguments))
    ├── Python: compute outputCommitment = sha256(JCS(content))
    ├── Python: construct binding payload (circuitHash + commitments + nonce)
    │
    ├── Rust subprocess: vaportpm-attest
    │   └── produces COSE_Sign1/CBOR attestation document
    │       (PCRs, certificate chain, enclave public key, nonce)
    │
    ├── Rust subprocess: ed25519-sign (or vaportpm subcommand)
    │   └── signs binding payload with enclave key
    │
    └── Python: assemble _meta["io.../verifiable-tools"] block
        (proof, proofFormat, commitments, nonce, teeAttestation)
```

### HPKE keypair lifecycle

On application startup (inside the verified lockboot environment):

1. Python generates an X25519 keypair in memory (`cryptography` library)
2. Extends a TPM PCR with the hash of the public key (via vaportpm)
3. `vaportpm-attest` produces an attestation document covering this PCR
4. The public key is advertised in `server/discover` under `blindPublicKeys["hpke-v1"]`

Verification by client/proxy:
- Certificate chain → cloud provider root (Nitro/GCP)
- Code PCRs (14) → pinned to published release values
- Key PCR → consistent with the advertised HPKE public key (read from attestation, not pinned in advance)
- Proof of possession → TEE signs a challenge with the private key

The key PCR value changes each boot (ephemeral key). The client doesn't need to know it in advance — it reads it from the attestation and checks consistency. The code PCRs are what gets pinned to the release.

### Blind execution (HPKE)

For `verifiable-tools/call` requests with encrypted arguments:

```
Encrypted request arrives
    │
    ├── Python: HPKE decrypt (in-process, cryptography library)
    │   └── RFC 9180, X25519/HKDF-SHA256/AES-128-GCM
    │   └── AAD = JCS({tool, inputCommitment, encryptionScheme})
    │   └── private key is in application memory (never on disk)
    │
    ├── Python: verify inputCommitment = sha256(salt || JCS(arguments))
    ├── Python: execute tool handler with plaintext arguments
    ├── Python: attestation pipeline (as above)
    │
    └── Response (optionally encrypted reply if replyPublicKey provided)
```

No Rust subprocess needed for HPKE — the key lives in Python process memory and the crypto is standard (`x25519` + `AESGCM` from the `cryptography` package).

### Python dependencies for SEP-2133

| Need | Solution |
|---|---|
| JSON Canonicalization (RFC 8785) | `jcs` or `canonicaljson` package |
| SHA-256 | `hashlib` (stdlib) |
| CBOR decode (for attestation inspection) | `cbor2` |
| HPKE (X25519 + AES-128-GCM) | `cryptography` package (in-process, key in memory) |
| Ed25519 sign (binding fields) | `cryptography` package or vaportpm subcommand |
| Attestation generation | `vaportpm-attest` (Rust subprocess) |
| PCR extension (HPKE key binding) | vaportpm (Rust subprocess) |

### Rust CLI tools for crypto

| Tool | Commands | Notes |
|---|---|---|
| **`vaportpm-attest`** | Generate vTPM attestation document | Already exists |
| **`vaportpm-verify`** | Verify attestation (proxy-side) | Already exists |
| **`vaportpm`** | PCR extend (bind HPKE public key) | Already exists or trivial addition |

## Internal tool categories

### Chain tools — on-chain interaction primitives

Each chain family uses an existing maintained CLI tool. Python calls them as subprocesses, passing JSON on stdin, reading JSON on stdout. Key material via file descriptor, never CLI args.

**`cast`** (Foundry) — EVM encoding, signing, submission:
```
encode-call, encode-transfer, encode-approve, encode-permit2,
encode-eip3009, sign, submit (via Python httpx), call, balance, nonce, gas
```

**`solana-tools-lite`** — Solana encoding and signing:
```
encode-transfer, encode-swap, sign, balance
```

Submission for both is via Python `httpx` (raw JSON-RPC). `cast` encodes and signs; Python submits.

### Protocol tools — bridge/DEX wrappers

Python modules (not separate binaries). Each knows which contract/function/args to use. They compose `cast` calls for encoding and `httpx` for API/RPC.

**`proto_cow`** — CoW Protocol
- `submit_order()` — EIP-712 sign via cast, POST to CoW orderbook API
- `order_status()` — GET from CoW API
- `cancel_order()` — on-chain cancel via cast

**`proto_across`** — Across V3
- `deposit()` — encode depositV3 via cast, submit via httpx
- `fill_status()` — query Across API
- `refund_status()` — check refund eligibility

**`proto_cctp`** — Circle CCTP V2
- `burn()` — encode depositForBurn via cast
- `attest_status()` — poll Circle Iris API
- `mint()` — encode receiveMessage on destination via cast

Additional protocol modules follow the same pattern. Each is a thin Python file (~100-200 lines) that knows the contract addresses, function signatures, and API endpoints.

### Wallet tools — key operations

Interface to lockboot key management. The orchestrator calls these to derive swap-specific keys and prepare key material for gVisor containers.

```python
wallet_derive(swap_id: str, chain_id: int) -> KeyInfo
wallet_sign(key_path: Path, data: bytes, sig_type: str) -> bytes
```

Implementation is operator-provided (lockboot infrastructure).

### Data tools — pricing, risk, monitoring

All Python:

- **Pricing:** `httpx` calls to DEX aggregator APIs (CoW, Jupiter, 1inch)
- **Risk scoring:** parse audit data from `products/*.md` route flags
- **Monitoring:** poll RPCs via `httpx` (`eth_getTransactionReceipt`, `getSignatureStatuses`)

## Orchestrator

The orchestrator is the Python core (Litestar application):

1. Receives swap requests via API surface (MCP/HTTP/x402)
2. Runs the Verifiable MCP attestation pipeline on every response
3. Plans routes (calls pricing APIs, risk scorer)
4. Spawns a gVisor container for the swap (scoped key material)
5. Composes a sequence of tool calls for the route
6. Executes the sequence inside the container
7. Monitors progress (polls RPCs)
8. Handles failures (retry, alternative route, refund)

### Order state machine

```
QUOTED → PAID → EXECUTING → LEG_1_SUBMITTED → LEG_1_CONFIRMED →
  LEG_2_SUBMITTED → LEG_2_CONFIRMED → DELIVERED → COMPLETE

Failure branches:
  PAID → REFUNDING → REFUNDED
  EXECUTING → FAILED → REFUNDING → REFUNDED
  LEG_N_SUBMITTED → LEG_N_FAILED → (retry or refund)
```

State transitions are logged. Each transition records: timestamp, tool call, tool output. Each status query response is attested (Verifiable MCP).

## gVisor container model

Each swap execution gets its own gVisor container:

**Provided:**
- Scoped key file (readable only by this container)
- Route plan (JSON: sequence of operations)
- Tool binaries (cast, solana-tools-lite — mounted read-only)
- Network access to allowlisted RPC endpoints only

**NOT provided:**
- Other swaps' key material
- The master key store or vaportpm credentials
- Access to the orchestrator's state
- Network access beyond allowlisted RPCs

**Lifecycle:**
1. Orchestrator spawns container with scoped key + route plan
2. Container executes the route
3. Container reports results back to orchestrator (stdout/file)
4. Container is destroyed, key file is wiped

Crash recovery: orchestrator spawns new container with same scoped key, resumes from last confirmed state, or initiates refund.

## Attestation proxy (Rust, distributable binary)

Separate deliverable. Small Rust binary embedding `vaportpm-verify`:

```
Agent ──stdio──► Proxy ──E2EE (HPKE)──► TEE server
                   │
                   ├── On connect: verify vTPM attestation
                   │   ├── Certificate chain → cloud provider root
                   │   ├── PCR 14 → pinned release values (from GitHub release)
                   │   ├── PCR 15 → expected configuration hash
                   │   └── HPKE public key → bound in attestation user_data
                   │
                   ├── Per-request: verify tee-nitro-v1 proof on every tool result
                   │   ├── inputCommitment matches what we sent
                   │   ├── outputCommitment matches returned content
                   │   ├── nonce matches (anti-replay)
                   │   └── enclave signature valid
                   │
                   └── Exposes: plain MCP (stdio) to the agent
```

The proxy implements Verifiable MCP client-side per SEP-2133. It is not required — agents can connect directly if they trust the operator.

## External tool dependencies

| Tool | What it does | Language | Install |
|---|---|---|---|
| **`cast`** | EVM ABI encoding, tx signing, calldata | Rust | `foundryup` |
| **`solana-tools-lite`** | Solana tx signing, message verification | Rust | `cargo install` |
| **`vaportpm-attest`** | Generate vTPM attestation document | Rust | lockboot |

Everything else is Python (`httpx` for RPC/API, Litestar for serving, `cryptography` for HPKE/Ed25519, `jcs`/`cbor2` for SEP-2133 data formats). HPKE is in-process Python — the key lives in application memory, no subprocess needed.

## Chain support

| Chain family | Signing/encoding | RPC/submission | Coverage |
|---|---|---|---|
| **EVM** (all) | `cast` | Python `httpx` | Ethereum, Arbitrum, Base, Optimism, Polygon, BSC, Avalanche, Linea, Scroll, zkSync, Gnosis, and every EVM L2/L3 |
| **Solana** | `solana-tools-lite` | Python `httpx` | Solana mainnet |
| **Bitcoin** | deferred | — | Route via bridges |
| **Cosmos/IBC** | deferred | — | Route via IBC Eureka (EVM-side) |

## What's NOT in the codebase

- No `web3.py`, `ethers.js`, `viem`, or `@solana/web3.js`
- No large chain SDK imports
- No `node_modules`
- No custom attestation protocols — SEP-2133 + vaportpm
- No RA-TLS
- Contract ABIs stored as JSON files, loaded by protocol wrappers
- RPC interaction is raw JSON-RPC over `httpx`

## Example: USDC (Base) → ETH (Arbitrum)

### Flow

```
Agent calls teeswap_quote(USDC, base, 1000, ETH, arbitrum, 0xUser)
    │
    ├── Proxy: encrypts args via HPKE, sends to TEE
    │
    ├── TEE: decrypts, evaluates routes:
    │   ├── Leg 1: swap USDC→WETH on Base via CoW (Class A)
    │   ├── Leg 2: bridge WETH Base→Arbitrum via Across (Class C)
    │   └── Risk score: low (A+C, no admin pause, no incidents)
    │
    ├── TEE: computes commitments, generates attestation
    ├── TEE: returns quote + tee-nitro-v1 proof in _meta
    │
    ├── Proxy: verifies attestation, forwards to agent
    │
Agent calls teeswap_execute(quoteId)
    │
    ├── x402 payment settles (USDC → TEE EOA)
    │
    ├── TEE spawns gVisor container with scoped key:
    │   ├── cast: approve USDC to CoW VaultRelayer
    │   ├── cast: sign EIP-712 CoW order
    │   ├── httpx: POST order to CoW API
    │   ├── httpx: poll CoW API until filled
    │   ├── cast: encode Across depositV3(WETH, arbitrum, 0xUser)
    │   ├── cast: sign deposit tx
    │   ├── httpx: submit to Base RPC
    │   └── httpx: poll Across API for fill on Arbitrum
    │
    ├── Across relayer fills → WETH arrives at 0xUser on Arbitrum
    │
    └── TEE marks order COMPLETE, attested status response
```
